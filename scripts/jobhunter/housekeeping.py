"""Nightly housekeeping (design 3.4 `housekeeping`, 2.3.3, 4.4, 4.5, 9). Every task runs in its own
transaction and a failing task never stops the others; the result lists each task's outcome.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time

from . import (audit, breakers, ceilings, config as _config, cycles, db, exclusions, gate, locks, paths, reconcile)
from .canon import now, seconds_between, ts_add
from .events import enqueue_notification, log_event, open_human_task

CYCLE_DIR_RE = re.compile(r"^C[0-9]{8}T[0-9]{6}Z[A-Z2-7]{4}$")
BACKUPS_KEEP = 14
WORK_DAYS = 7
LOG_DAYS = 30


def _backup(conn) -> dict:
    bdir = os.path.join(paths.state_dir(), "backups")
    os.makedirs(bdir, exist_ok=True)
    name = "jobhunter-%s.sqlite3" % now().replace(":", "").replace("-", "")
    dst_path = os.path.join(bdir, name)
    dst = sqlite3.connect(dst_path)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    os.chmod(dst_path, 0o600)
    olds = sorted(f for f in os.listdir(bdir) if f.startswith("jobhunter-") and f.endswith(".sqlite3"))
    removed = 0
    for f in olds[:-BACKUPS_KEEP]:
        os.unlink(os.path.join(bdir, f))
        removed += 1
    return {"path": dst_path, "removed": removed}


def _prune_work() -> dict:
    removed = 0
    try:
        ws = paths.ws_root()
    except Exception:
        return {"removed": 0, "note": "no ws_root"}
    cutoff = time.time() - WORK_DAYS * 86400
    for role in paths.ROLES:
        wdir = os.path.join(ws, role, "work")
        if not os.path.isdir(wdir):
            continue
        for name in os.listdir(wdir):
            p = os.path.join(wdir, name)
            if CYCLE_DIR_RE.match(name) and os.path.isdir(p) and not os.path.islink(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    return {"removed": removed}


def _prune_logs() -> dict:
    removed = 0
    cutoff = time.time() - LOG_DAYS * 86400
    ldir = paths.logs_dir()
    for sub in ("", "evidence"):
        d = os.path.join(ldir, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p) or name == "jh.log":
                continue
            if (sub == "evidence" or name.startswith(("guard-", "events-"))) and os.path.getmtime(p) < cutoff:
                os.unlink(p)
                removed += 1
    jl = os.path.join(ldir, "jh.log")
    if os.path.isfile(jl) and os.path.getsize(jl) > 5 * 1024 * 1024:
        os.replace(jl, jl + ".1")
    return {"removed": removed}


CODE_KEEP_DAYS = 30
STAGED_MAX_AGE_S = 3600


def _close_codes(conn) -> dict:
    """Close the codes of decided drafts, then delete closed codes issued more than 30 days ago so the code
    space (U3 approvals) never runs out; a code row is never deleted before 30 days (schema)."""
    n = conn.execute("UPDATE approval_codes SET closed_at = ? WHERE closed_at IS NULL AND draft_id IN "
                     "(SELECT id FROM drafts WHERE status <> 'awaiting_approval')", (now(),)).rowcount
    pruned = conn.execute("DELETE FROM approval_codes WHERE closed_at IS NOT NULL AND issued_at < ?",
                          (ts_add(now(), days=-CODE_KEEP_DAYS),)).rowcount
    return {"closed": n, "pruned": pruned}


def _unstage(conn) -> dict:
    """Staged resume copies left in the upload folder by a crashed session (cycle end removes its own)."""
    try:
        from . import resume
    except ImportError:
        return {"skipped": "resume module not installed"}
    return {"removed": resume.unstage_open(conn, older_than_s=STAGED_MAX_AGE_S)}


def _expire(conn) -> dict:
    out = {"tokens": gate.expire(conn)}
    try:
        from . import drafts
        out["drafts"] = drafts.expire(conn)
    except ImportError:
        out["drafts"] = "drafts module not installed"
    return out


def _ats_queue(conn, cfg: dict) -> dict:
    hours = int(cfg["gmail"]["confirmation_check_hours"])
    if not conn.execute("SELECT 1 FROM meta WHERE key = 'mail_connected_at'").fetchone():
        return {"skipped": "no mail connection"}
    from .keys import ATS_NAMES
    flagged = []
    for ats in ATS_NAMES:
        rows = conn.execute("SELECT a.company_id, a.sent_at FROM actions a WHERE a.kind = 'application' AND "
                            "a.platform = ? AND a.status = 'sent' AND a.sent_at < ? AND a.sent_at > ?",
                            (ats, ts_add(now(), hours=-hours), ts_add(now(), days=-30))).fetchall()
        missing = 0
        for r in rows:
            if not conn.execute("SELECT 1 FROM inbound_messages WHERE company_id = ? AND code_class = "
                                "'application_confirmation' AND received_at >= ?", (r[0], r[1])).fetchone():
                missing += 1
        key = "ats_human_queue:" + ats
        if missing >= 2 and not conn.execute("SELECT 1 FROM meta WHERE key = ? AND value = '1'", (key,)).fetchone():
            db.meta_set(conn, key, "1", "system")
            enqueue_notification(conn, key, "normal", "info",
                                 "No confirmation email for %d %s applications: new %s applications now wait for you."
                                 % (missing, ats, ats))
            flagged.append(ats)
    return {"flagged": flagged}


def _acceptance(conn, cfg: dict) -> dict:
    acc = ceilings.acceptance(conn, cfg)
    if acc["enough"] and acc["rate"] is not None and acc["rate"] < cfg["linkedin"]["adaptive"]["pause_below"]:
        row = conn.execute("SELECT state FROM breakers WHERE scope = 'linkedin.invites'").fetchone()
        if row is None or row[0] != "open":
            breakers.trip(conn, "linkedin.invites", "li_low_acceptance",
                          "invite acceptance %.0f%% on %d invites" % (acc["rate"] * 100, acc["invites_aged"]),
                          by="housekeeping")
            return {"tripped": True, "rate": acc["rate"]}
    return {"tripped": False, "rate": acc["rate"]}


def _suggest_auto(conn, cfg: dict) -> dict:
    s = cfg["approval"]["suggest_auto_after"]
    if cfg["approval"]["mode"] == "auto":
        return {"suggested": False, "why": "already auto"}
    if conn.execute("SELECT 1 FROM meta WHERE key = 'auto_suggested_at' AND value <> ''").fetchone():
        return {"suggested": False, "why": "already suggested"}
    first = conn.execute("SELECT min(approved_at) FROM drafts WHERE approved_by LIKE 'human:%'").fetchone()[0]
    if not first or seconds_between(first, now()) < int(s["min_days"]) * 86400:
        return {"suggested": False, "why": "too early"}
    rows = conn.execute(
        "SELECT d.status, d.approved_by, (SELECT q.code_verdict FROM qc_results q WHERE q.draft_id = d.id AND "
        "q.stage = 'review' ORDER BY q.id DESC LIMIT 1) AS verdict, (SELECT q.gates_failed FROM qc_results q WHERE "
        "q.draft_id = d.id AND q.stage = 'review' ORDER BY q.id DESC LIMIT 1) AS gates FROM drafts d WHERE "
        "d.approved_by LIKE 'human:%' OR d.status = 'skipped_by_human'").fetchall()
    n = len(rows)
    if n < int(s["min_human_reviewed"]):
        return {"suggested": False, "why": "only %d human decisions" % n}
    agree = sum(1 for r in rows if (r["approved_by"] and r["verdict"] == "pass") or
                (r["status"] == "skipped_by_human" and r["verdict"] == "fail"))
    truthful = sum(1 for r in rows if r["gates"] and "truthful" in r["gates"])
    if truthful or agree / float(n) < float(s["min_agreement"]):
        return {"suggested": False, "why": "agreement %.2f, truthfulness failures %d" % (agree / float(n), truthful)}
    open_human_task(conn, "suggest_auto", "QC matched your decisions on %d items." % n)
    enqueue_notification(conn, "suggest_auto", "normal", "info", "QC matched your decisions on %d items. To stop "
                         "reviewing, run ./jobhunter approval auto." % n)
    db.meta_set(conn, "auto_suggested_at", now(), "system")
    return {"suggested": True, "items": n}


def _summary(conn) -> dict:
    since = ts_add(now(), days=-1)
    q = lambda sql: conn.execute(sql, (since,)).fetchone()[0]  # noqa: E731
    return {"jobs_found": q("SELECT count(*) FROM jobs WHERE discovered_at > ?"),
            "sent": q("SELECT count(*) FROM actions WHERE status = 'sent' AND sent_at > ?"),
            "stops": q("SELECT count(*) FROM breaker_events WHERE event = 'trip' AND created_at > ?"),
            "cycles": q("SELECT count(*) FROM cycles WHERE started_at > ?")}


def _config_and_exclusions(conn) -> dict:
    out = {"config": _config.apply(conn)["meta"]}
    if exclusions.file_changed(conn):
        r = exclusions.import_csv(conn, None, False, None)
        out["exclusions"] = {k: r[k] for k in ("added", "reactivated", "unchanged", "missing_from_file")}
        out["exclusions"]["errors"] = r["errors"]     # import_csv also queues a notification for them
    from . import companies
    if companies.aliases_changed(conn):
        out["company_aliases"] = companies.import_aliases(conn)
    return out


def _enrich(conn) -> dict:
    """The optional email finder's daily steps (U10 enrich.housekeeping.run_housekeeping, own transactions):
    stale in-flight calls settled, stale lookups finished, retention purge. Skipped when not installed."""
    try:
        from .enrich import housekeeping as enrich_housekeeping
    except ImportError:
        return {"skipped": "the email finder is not installed"}
    return enrich_housekeeping.run_housekeeping(conn)


NONCE_DAYS = 2


def _prune_nonces(conn) -> dict:
    """Agent proof nonces (grants_used rows `ap:<nonce>` and `ep:<nonce>`, CLI route 5.3) older than two days:
    a proof is valid for 120 s, so a pruned nonce can never be replayed. Chat grant nonces are kept. Also
    removes the version 1 marker state/agent-proof-seen."""
    from . import auth
    cutoff = ts_add(now(), days=-NONCE_DAYS)
    cur = conn.execute("DELETE FROM grants_used WHERE (nonce LIKE 'ap:%' OR nonce LIKE 'ep:%') AND used_at < ?",
                       (cutoff,))
    return {"pruned": cur.rowcount, "v1_marker_removed": auth.remove_v1_marker()}


def _otp_accounts(conn) -> dict:
    """FEATURES-OTP-ACCOUNTS-CAPTCHA 2.10: expire waiting code requests, delete CAPTCHA screenshots past their
    retention, prune code_steps after 180 days and code_uses after 365 days (hash rows only), and mark account
    rows left in 'creating' for a day as failed."""
    from . import captcha, otp
    from .canon import ts_add
    out = {"requests_expired": otp.expire_waiting(conn), "screenshots_deleted": captcha.prune_screenshots(conn)}
    out["code_steps_pruned"] = conn.execute("DELETE FROM code_steps WHERE at < ?",
                                            (ts_add(now(), days=-180),)).rowcount
    out["code_uses_pruned"] = conn.execute("DELETE FROM code_uses WHERE used_at < ?",
                                           (ts_add(now(), days=-365),)).rowcount
    out["stale_creating"] = conn.execute("UPDATE ats_accounts SET status = 'failed', reason = 'stale_creating', "
                                         "updated_at = ? WHERE status = 'creating' AND created_at < ?",
                                         (now(), ts_add(now(), days=-1))).rowcount
    return out


TASKS = ("expire", "captcha", "otp_accounts", "codes", "staged", "config", "consent", "breakers", "clock", "reconcile", "locks", "nonces",
         "ats_queue", "acceptance", "email_health", "audit", "enrich",
         "suggest_auto", "summary", "optimize", "backup", "prune_work", "prune_logs", "sessions")


def run(conn, only: str | None = None) -> dict:
    """Run every task (or only one). Manages its own transactions."""
    if only is not None and only not in TASKS:
        from .errors import Denied
        raise Denied("E_USAGE", "unknown housekeeping task %r (%s)" % (only, ", ".join(TASKS)))
    cfg = _config.load(conn)
    results = {}
    from . import identity
    in_tx = {"expire": _expire, "codes": _close_codes, "staged": _unstage, "config": _config_and_exclusions,
             "consent": identity.sync_consent,
             "breakers": lambda c: {"closed": breakers.close_expired(c)},
             "clock": lambda c: {"problem": cycles.clock_check(c, cfg)},
             "reconcile": lambda c: {"tasks": reconcile.open_stale_tasks(c)},
             "locks": lambda c: {"pruned": locks.prune(c)}, "nonces": _prune_nonces,
             "ats_queue": lambda c: _ats_queue(c, cfg),
             "acceptance": lambda c: _acceptance(c, cfg), "email_health": lambda c: breakers.email_health(c, cfg),
             "suggest_auto": lambda c: _suggest_auto(c, cfg), "summary": _summary}
    for name in TASKS:
        if only and name != only:
            continue
        try:
            if name in in_tx:
                with db.tx(conn):
                    results[name] = in_tx[name](conn)
            elif name == "captcha":
                from . import captcha
                results[name] = captcha.expire_all(conn)
            elif name == "otp_accounts":
                with db.tx(conn):
                    results[name] = _otp_accounts(conn)
            elif name == "audit":
                results[name] = audit.run(conn, 2)     # IMAP outside the transaction, then a short write
            elif name == "enrich":
                results[name] = _enrich(conn)
            elif name == "optimize":
                conn.execute("PRAGMA optimize")
                results[name] = {"ok": True}
            elif name == "backup":
                results[name] = _backup(conn)
            elif name == "prune_work":
                results[name] = _prune_work()
            elif name == "prune_logs":
                results[name] = _prune_logs()
            elif name == "sessions":
                results[name] = {"skipped": "reviewer session pruning uses OpenClaw session retention [verify]"}
        except Exception as exc:
            results[name] = {"error": "%s: %s" % (type(exc).__name__, getattr(exc, "message", exc))}
    with db.tx(conn):
        db.meta_set(conn, "housekeeping_last_at", now(), "system")
        log_event(conn, "housekeeping", tasks=sorted(results), errors=sorted(k for k, v in results.items()
                                                                            if isinstance(v, dict) and "error" in v))
    return {"tasks": json.loads(json.dumps(results, default=str))}
