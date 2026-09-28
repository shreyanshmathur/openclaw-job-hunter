"""Daily enrich housekeeping (U10, ENRICH-SPEC 8.2 and 9.4), run by the U1 housekeeping task `enrich` and
by `./jobhunter enrich housekeeping`. Each step runs in its own short transaction.

1. calls still `inflight` after 10 minutes (a crash): outcome unknown, charged at the maximum (fail closed);
2. `running` requests nobody holds for an hour: finished (not_found when a provider saw the person, else
   unavailable with the key hashes released);
3. retention: provider data never used by a live action and older than enrich.retention_days is purged
   from the contact and the call; used addresses are purged once every thread is closed, bounced, replied
   negatively or ghosted for retention_days (a positive or neutral reply keeps it);
4. rolling-31-day provider calls older than 400 days are deleted when nothing references them; lifetime
   provider calls are kept (their sum is the lifetime budget).
Outcome, credits and key hashes always stay (budget and "never twice").
"""
from __future__ import annotations

from .. import db, locks
from ..canon import now, ts_add
from ..events import log_event
from . import cache, providers, settings

INFLIGHT_STALE_MIN = 10
RUNNING_STALE_H = 1
PRUNE_DAYS = 400
LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")
KEEP_REPLY = ("positive", "neutral", "referral_offered")
KEEP_OUTCOME = ("screening_call", "interview", "offer", "referred")
DONE_REPLY = ("negative", "not_hiring", "opt_out", "complaint", "bounce")


def _q(n: int) -> str:
    return ",".join("?" * n)


def settle_stale_inflight(conn) -> int:
    ts = now()
    n = 0
    with db.tx(conn):
        for r in conn.execute("SELECT id, provider, op, credits_charged FROM enrich_calls WHERE outcome = 'inflight' "
                              "AND started_at < ?", (ts_add(ts, minutes=-INFLIGHT_STALE_MIN),)).fetchall():
            max_c = float(providers.get(r["provider"]).max_charge.get(r["op"], 1.0))
            conn.execute("UPDATE enrich_calls SET outcome = 'unknown', credits_charged = ?, finished_at = ?, "
                         "updated_at = ? WHERE id = ?", (max(max_c, float(r["credits_charged"])), ts, ts, r["id"]))
            n += 1
    return n


def finish_stale_running(conn) -> dict:
    ts = now()
    out = {"not_found": 0, "unavailable": 0}
    with db.tx(conn):
        for r in conn.execute("SELECT * FROM enrich_requests WHERE status = 'running' AND updated_at < ?",
                              (ts_add(ts, hours=-RUNNING_STALE_H),)).fetchall():
            if locks.is_held(conn, cache.lock_name(r["request_uid"])):
                continue
            seen = conn.execute("SELECT count(*) FROM enrich_calls WHERE request_id = ? AND outcome <> "
                                "'network_before_send'", (r["id"],)).fetchone()[0]
            if seen:
                conn.execute("UPDATE enrich_requests SET status = 'not_found', reason = 'stale', next_step = 3, "
                             "finished_at = ?, updated_at = ? WHERE id = ?", (ts, ts, r["id"]))
                out["not_found"] += 1
            else:
                cache.release_keys(conn, r["id"])
                conn.execute("UPDATE enrich_requests SET status = 'unavailable', reason = 'stale', next_step = 3, "
                             "finished_at = ?, updated_at = ? WHERE id = ?", (ts, ts, r["id"]))
                out["unavailable"] += 1
    return out


def _thread_done(t, cutoff: str) -> bool:
    if t["reply_class"] in KEEP_REPLY or t["outcome"] in KEEP_OUTCOME:
        return False
    closed = t["state"] in ("closed", "bounced") or t["outcome"] == "ghosted" or \
        (t["state"] == "replied" and t["reply_class"] in DONE_REPLY)
    return closed and (t["updated_at"] or "") < cutoff


def retention_purge(conn, s: dict | None = None) -> dict:
    s = s if s is not None else settings.load(conn)
    ts = now()
    cutoff = ts_add(ts, days=-int(s["retention_days"]))
    unused, used = [], []
    rows = conn.execute("SELECT id, email, finished_at, started_at FROM enrich_calls WHERE email IS NOT NULL "
                        "AND purged_at IS NULL").fetchall()
    for r in rows:
        acts = conn.execute("SELECT id FROM actions WHERE lower(recipient) = ? AND status IN (%s)" % _q(len(LIVE)),
                            [r["email"]] + list(LIVE)).fetchall()
        if not acts:
            if (r["finished_at"] or r["started_at"]) < cutoff:
                unused.append(r["id"])
            continue
        ids = [a[0] for a in acts]
        threads = conn.execute("SELECT state, reply_class, outcome, updated_at FROM threads WHERE first_action_id IN "
                               "(%s) OR followup_action_id IN (%s)" % (_q(len(ids)), _q(len(ids))), ids + ids).fetchall()
        if threads and len(threads) >= 1 and all(_thread_done(t, cutoff) for t in threads):
            used.append(r["id"])
    with db.tx(conn):
        n1 = cache.purge_calls(conn, unused)
        n2 = cache.purge_calls(conn, used)
    return {"unused": n1, "used": n2}


def prune_old(conn) -> int:
    ts = now()
    rolling = [p for p in settings.PROVIDERS if providers.get(p).budget_period != "lifetime"]
    with db.tx(conn):
        n = conn.execute(
            "DELETE FROM enrich_calls WHERE provider IN (%s) AND started_at < ? AND outcome <> 'inflight' "
            "AND id NOT IN (SELECT result_call_id FROM enrich_requests WHERE result_call_id IS NOT NULL) "
            "AND id NOT IN (SELECT email_enrich_call_id FROM contacts WHERE email_enrich_call_id IS NOT NULL)"
            % _q(len(rolling)), rolling + [ts_add(ts, days=-PRUNE_DAYS)]).rowcount
    return n


def run_housekeeping(conn) -> dict:
    """All steps; own transactions. Returns counts (no names, no addresses)."""
    out = {"inflight_settled": settle_stale_inflight(conn), "stale_requests": finish_stale_running(conn),
           "purged": retention_purge(conn), "pruned": prune_old(conn)}
    log_event(conn, "enrich_housekeeping", **{k: v for k, v in out.items()})
    return out
