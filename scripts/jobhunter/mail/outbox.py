"""The mailer run (design 1.3.7, 2.3.1, 2.3.3, 2.3.4): `jh.py mail run`, every 5 minutes, no model.

run_once(conn, max_sends=1, fetch=True):
  1. lock `mailer` (one run at a time), expire stale tokens to unknown (gate.expire);
  2. stop when paused or `global` is open (E_PAUSED / E_BREAKER_OPEN, exit 5); skip all Gmail traffic while a
     `gmail` breaker is open (the breaker already alerted the person);
  3. IMAP fetch when due (gmail.fetch_every_minutes): replies, bounces, confirmations, security emails;
  4. IMAP reconciliation of unknown email actions (Message-ID search, 15 minutes and 24 hours apart);
  5. the Gmail stop rules (breakers.email_health: the rolling bounce-rate pause and stop, the one-complaint cut
     of the cold cap) whenever a new hard bounce or complaint was recorded since the mailer last ran them, so they
     act before the next send instead of at nightly housekeeping;
  6. at most max_sends approved email drafts whose send_after passed, follow-ups first, then application
     emails, then cold emails: cheap gate checks, precheck over IMAP, gate.reserve, build the message from the
     approved text and check its canonical sha256, SMTP connect/EHLO/AUTH/MAIL FROM/RCPT TO, commit `armed`
     with the Message-ID, DATA, then confirm (250), fail (refused) or unknown (no answer).
A draft that can never go out (duplicate or company rule, exclusion, QC or text problem, already sent, recipient
refused) is expired with a notification, so the mailer does not retry it every run.
Transactions are short; no network call ever runs inside one.

On the web_ui route (the default) the mailer sends nothing, fetches nothing and needs no app password: the result
carries `handled_by: browser_lane` and `blocked.reason = web_ui_route`. Only when the person also connected an app
password (optional) does it log in to IMAP, read-only, for the one check the reconcile table gives code on that
route: the `imap_sent_search` of unknown web sends (design 2.3.4). An IMAP failure there never trips a breaker
(the browser lane keeps working); it is reported and the person is told once a day.
"""
from __future__ import annotations

import hashlib
import json
import os
import types

from .. import breakers, canon, ceilings, config as _config, db, gate, jobstate, locks, pacing, reconcile
from ..errors import Denied, exit_code
from ..events import enqueue_notification, log_event, open_human_task
from . import (MAILER_AGENT, MailError, browser_lane, credentials, imap_client, is_connected, owner_address, owner_name,
               smtp_sender)
from . import fetch as fetch_mod
from . import mime, precheck
from .smtp import reply_line

LOCK_NAME = "mailer"
LOCK_TTL_S = 240
MAX_CANDIDATES = 5
KIND_ORDER = ("followup_email", "application_email", "cold_email")
SYSTEM = types.SimpleNamespace(cls="system", agent_id=None)
EXPIRE_EXITS = (3, 6, 7, 10)             # the draft can never go out as it is
KEEP_CODES = ("E_CHANNEL_DISABLED",)     # exit 7 but temporary (the person turned a channel off)
HEALTH_META = "mail_health_seen"          # the bounce and complaint evidence the last health check saw


# ---------------------------------------------------------------- helpers
class _LazyImap:
    """Opens the IMAP session on first use; close() is safe when it never opened."""

    def __init__(self, creds):
        self.creds = creds
        self.client = None

    def get(self):
        if self.client is None:
            c = imap_client(self.creds)
            c.open()
            self.client = c
        return self.client

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None


def candidates(conn, limit: int = MAX_CANDIDATES) -> list:
    """Approved mailer drafts due now, without a live action, in priority order."""
    ts = canon.now()
    return conn.execute(
        "SELECT * FROM drafts d WHERE d.status = 'approved' AND d.send_route = 'mailer' "
        "AND d.kind IN ('followup_email','application_email','cold_email') "
        "AND (d.send_after IS NULL OR d.send_after <= ?) AND (d.expires_at IS NULL OR d.expires_at > ?) "
        "AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.draft_id = d.id AND a.status IN "
        "('reserved','armed','sent','failed_after_click','unknown','imported')) "
        "ORDER BY CASE d.kind WHEN 'followup_email' THEN 0 WHEN 'application_email' THEN 1 ELSE 2 END, "
        "COALESCE(d.send_after, d.approved_at, d.created_at), d.id LIMIT ?", (ts, ts, int(limit))).fetchall()


def expire_draft(conn, draft_row, reason: str, why: str) -> None:
    """Take an approved draft out of the send queue (its own transaction) and tell the person."""
    with db.tx(conn):
        cur = conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_row["id"],)).fetchone()
        if cur is None or cur[0] != "approved":
            return
        jobstate.set_draft_status(conn, draft_row["id"], "expired", ("mailer:" + reason)[:200], "mailer")
        enqueue_notification(conn, "mail_dropped:%s" % draft_row["draft_uid"], "normal", "info",
                             "Email draft %s (%s) was not sent: %s" % (draft_row["draft_uid"],
                                                                         draft_row["kind"].replace("_", " "),
                                                                         why[:300]))
        log_event(conn, "mail_draft_dropped", draft_uid=draft_row["draft_uid"], reason=reason)


def _trip_for_reply(conn, code, text, stage: str) -> str | None:
    """Trip the breaker named by smtp.json for an SMTP or IMAP reply; returns the scope or None."""
    from ..detect import smtp_signature
    sig = smtp_signature(reply_line(code, text))
    if not sig or not sig.get("trip"):
        return None
    rc = sig.get("reason_code") or "gmail_unexpected_state"
    scope = (breakers.POLICIES.get(rc) or {}).get("scope") or "gmail"
    breakers.trip(conn, scope, rc, "%s at %s: %s" % (sig.get("id"), stage, reply_line(code, text)[:300]),
                  by="mailer")
    return scope


def _gmail_blocked(conn) -> dict | None:
    """An open `gmail` breaker (or a Gmail pause) stops all Gmail traffic of the mailer, reads included."""
    ts = canon.now()
    for r in conn.execute("SELECT scope, reason_code, requires_human, auto_close_at FROM breakers WHERE state = 'open' "
                          "AND scope IN ('gmail','pause:gmail') ORDER BY scope").fetchall():
        if not r["requires_human"] and r["auto_close_at"] and r["auto_close_at"] <= ts:
            continue
        return {"scope": r["scope"], "reason_code": r["reason_code"]}
    return None


def _pre_gate(conn, cfg: dict, draft) -> None:
    """The cheap reserve checks before any IMAP work (reserve repeats all of them)."""
    kind = draft["kind"]
    breakers.check_breakers(conn, breakers.scopes_for("gmail", kind))
    contact = draft["contact_id"]
    if kind == "followup_email" and draft["thread_key"]:
        th = conn.execute("SELECT contact_id FROM threads WHERE thread_key = ?", (draft["thread_key"],)).fetchone()
        contact = th[0] if th else contact
    gate.check_hours(conn, cfg, kind, "gmail", contact)
    pacing.pace_check(conn, "gmail", kind, cfg)
    ceilings.check(conn, "gmail", kind, cfg=cfg)


def _attachment(conn, draft, action) -> dict | None:
    if draft["kind"] != "application_email":
        return None
    v = conn.execute("SELECT * FROM resume_variants WHERE id = ?", (draft["attachment_variant_id"],)).fetchone() \
        if draft["attachment_variant_id"] else None
    if v is None:
        raise Denied("E_QC_HASH_MISMATCH", "the approved resume variant is missing")
    try:
        p = json.loads(draft["payload_json"] or "{}").get("attachment") or {}
    except ValueError:
        p = {}
    filename = p.get("filename")
    if not filename:
        raise Denied("E_QC_HASH_MISMATCH", "the approved draft has no attachment name")
    try:
        with open(v["pdf_path"], "rb") as fh:
            data = fh.read()
    except OSError:
        raise Denied("E_QC_HASH_MISMATCH", "the approved resume PDF cannot be read")
    sha = hashlib.sha256(data).hexdigest()
    want = {v["pdf_sha256"], p.get("sha256")}
    if action["attachment_sha256"]:
        want.add(action["attachment_sha256"])
    if want != {sha}:
        raise Denied("E_QC_HASH_MISMATCH", "the resume PDF changed after approval")
    return {"filename": filename, "data": data, "sha256": sha}


# ---------------------------------------------------------------- one send
def send_one(conn, draft, precheck_id: int, owner: dict, sender, cycle_id: str | None = None) -> dict:
    """Reserve, build, SMTP, resolve. Returns {status, token?, code?, message?}."""
    from .. import drafts as drafts_mod
    try:
        with db.tx(conn):
            r = gate.reserve(conn, kind=draft["kind"], draft_id=draft["id"], precheck_id=precheck_id,
                             platform="gmail", agent_id=MAILER_AGENT, route="mailer", cycle_id=cycle_id, lane="mailer")
    except Denied as d:
        return {"status": "refused", "code": d.code, "message": d.message, "retry_after_s": d.retry_after}
    token = r["token"]
    action = gate.action_by_token(conn, token)
    # build the approved bytes
    try:
        send_text = drafts_mod.send_text(conn, draft["id"])
        if canon.sha256_text(send_text) != action["approved_sha256"]:
            raise Denied("E_QC_HASH_MISMATCH", "the stored text differs from the approved text")
        att = _attachment(conn, draft, action)
        in_reply_to = None
        if draft["kind"] == "followup_email" and action["thread_key"]:
            th = conn.execute("SELECT first_message_id FROM threads WHERE thread_key = ?",
                              (action["thread_key"],)).fetchone()
            in_reply_to = th[0] if th else None
        msg = mime.build_message(draft, action, owner, att, send_text=send_text, in_reply_to=in_reply_to)
        if canon.sha256_text(mime.canonical_from_message(msg, draft["kind"])) != action["approved_sha256"]:
            raise Denied("E_QC_HASH_MISMATCH", "the built message differs from the approved text")
    except Exception as exc:
        d = exc if isinstance(exc, Denied) else Denied("E_INTERNAL", "%s: %s" % (type(exc).__name__, exc))
        with db.tx(conn):
            gate.fail(conn, token, "not_attempted", "mailer did not send: %s" % d.message, SYSTEM)
        return {"status": "refused", "code": d.code, "message": d.message, "token": token, "built": False}
    mid = mime.message_id_for(token)
    armed = {"ok": False}

    def before_data():
        with db.tx(conn):
            gate.mark_armed(conn, token, message_id=mid)  # the gate is the one writer of actions (design 2.5)
        armed["ok"] = True

    res = sender.send(msg, before_data=before_data)
    line = reply_line(res.get("reply_code"), res.get("reply_text"))
    outcome = res.get("outcome")
    out = {"token": token, "smtp": {k: res.get(k) for k in ("outcome", "stage_reached", "reply_code", "reply_text")}}
    if outcome == "sent":
        try:
            with db.tx(conn):
                gate.confirm(conn, token, "SMTP %s" % line, message_id=mid)
            out["status"] = "sent"
        except Exception as exc:  # it went out: leave it armed; expiry makes it unknown and IMAP reconciles it
            with db.tx(conn):
                enqueue_notification(conn, "mail_confirm_failed:%s" % token, "high", "alert",
                                     "An email went out (%s) but recording it failed: %s. It will be matched by its "
                                     "Message-ID." % (token, str(exc)[:200]))
            out.update(status="sent_unrecorded", error=str(exc)[:300])
        return out
    if outcome in ("rejected_before_data", "aborted_before_data") and not armed["ok"]:
        reason = "smtp_rejected_before_data" if outcome == "rejected_before_data" else "not_attempted"
        with db.tx(conn):
            gate.fail(conn, token, reason, "SMTP %s at %s: %s" % (res.get("error") or "", res.get("stage_reached"),
                                                                line), SYSTEM)
            scope = _trip_for_reply(conn, res.get("reply_code"), res.get("reply_text"), res.get("stage_reached")) \
                if res.get("reply_code") or res.get("auth_failed") else None
            if res.get("auth_failed") and scope is None:
                breakers.trip(conn, "gmail", "gmail_auth_failed", "SMTP login refused: %s" % line[:300], by="mailer")
                scope = "gmail"
        out.update(status="rejected", stage=res.get("stage_reached"), reply=line, tripped=scope)
        code = res.get("reply_code") or 0
        if res.get("stage_reached") == "rcpt" and 500 <= code < 600 and scope is None:
            expire_draft(conn, draft, "recipient_refused", "Gmail refused the recipient address (%s)" % line)
        if res.get("stage_reached") in ("connect", "ehlo") and not code:
            out["transport_error"] = res.get("error")
        return out
    if outcome == "rejected_at_data":
        with db.tx(conn):
            gate.fail(conn, token, "smtp_rejected", "SMTP refused the message: %s" % line, SYSTEM)
            scope = _trip_for_reply(conn, res.get("reply_code"), res.get("reply_text"), "data")
        out.update(status="rejected", stage="data", reply=line, tripped=scope)
        if 500 <= (res.get("reply_code") or 0) < 600 and scope is None:
            expire_draft(conn, draft, "smtp_rejected", "Gmail refused the message (%s)" % line)
        return out
    # unknown: no answer after DATA started (or the arm/abort state is unclear)
    with db.tx(conn):
        a = gate.action_by_token(conn, token)
        if a["status"] in ("reserved", "armed"):
            gate.mark_unknown(conn, token, note="SMTP %s at %s: %s" % (res.get("error") or "no answer",
                                                                      res.get("stage_reached"), line))
    out.update(status="unknown", reply=line)
    return out


# ---------------------------------------------------------------- reconciliation over IMAP
IMAP_METHODS = ("imap_message_id", "imap_sent_search")


def reconcile_unknown(conn, imap, methods: tuple = IMAP_METHODS) -> list[dict]:
    """IMAP checks the reconcile table names for the mailer route (Message-ID search) and the web route
    (Sent search when the mail connection exists). Only checks whose time has come are run."""
    done = []
    ts = canon.now()
    for item in reconcile.work_list(conn, route="mailer"):
        if item["method"] not in methods:
            continue
        if item.get("not_before") and item["not_before"] > ts:
            continue
        a = gate.action_by_token(conn, item["token"])
        if item["method"] == "imap_message_id":
            if not a["message_id"]:
                continue
            q = "rfc822msgid:%s" % mime.bare_id(a["message_id"])
        else:
            if not a["recipient"]:
                continue
            q = "in:sent to:%s after:%s" % (a["recipient"], fetch_mod.gmail_date(a["armed_at"] or a["reserved_at"]))
        n = imap.count(q)
        result = "found" if n > 0 else "not_found"
        try:
            with db.tx(conn):
                r = reconcile.record_check(conn, item["token"], item["method"], result, "IMAP %s: %d" % (q, n),
                                           by=MAILER_AGENT)
            done.append({"token": item["token"], "method": item["method"], "result": result, "status": r["status"]})
        except Denied as d:
            done.append({"token": item["token"], "method": item["method"], "result": result, "refused": d.code})
    return done


# ---------------------------------------------------------------- the run
def _fetch_due(conn, cfg: dict) -> bool:
    every = max(15, int((cfg.get("gmail") or {}).get("fetch_every_minutes") or 30))
    last = db.meta_get(conn, "mail_fetch_last_at")
    if not last:
        return True
    try:
        return canon.seconds_between(last, canon.now()) >= every * 60
    except ValueError:
        return True


def _health_evidence(conn) -> str:
    """What can make the Gmail stop rules act between nightly housekeeping runs: hard bounces on email threads
    (fetch records them, U6 applies them) and complaint or opt-out exclusions (U6 records them from a classified
    reply). A change that does not move a rule only costs one extra check."""
    b = conn.execute("SELECT count(*), max(COALESCE(reply_at, updated_at)) FROM threads WHERE channel = 'email' "
                     "AND reply_class = 'bounce'").fetchone()
    c = conn.execute("SELECT count(*), max(created_at) FROM exclusions WHERE source IN ('complaint','reply_optout')"
                     ).fetchone()
    return json.dumps([b[0], b[1], c[0], c[1]])


def health_check(conn, cfg: dict) -> dict | None:
    """Run breakers.email_health (design 4.5 stop rules) when new bounce or complaint evidence arrived since the
    mailer last ran it; returns its result, or None when nothing changed. Only new evidence triggers it, so a
    breaker the person reset is not re-opened from the same data before anything new happens (housekeeping still
    runs the full check every night)."""
    with db.tx(conn):
        seen = _health_evidence(conn)
        if db.meta_get(conn, HEALTH_META) == seen:
            return None
        res = breakers.email_health(conn, cfg)
        db.meta_set(conn, HEALTH_META, seen, "system")
    return res


def _handle_imap_error(conn, e: MailError, out: dict) -> None:
    if e.auth_failed:
        with db.tx(conn):
            breakers.trip(conn, "gmail", "gmail_auth_failed", "IMAP login refused: %s" % e.reply[:300], by="mailer")
        out["tripped"] = "gmail"
    out["errors"].append({"code": e.code, "stage": e.stage, "message": e.message, "reply": e.reply})


def _web_imap_failed(conn, e: MailError, out: dict) -> None:
    """Web route: the optional IMAP check failed. Report it (at most one notification a day), trip nothing."""
    out["errors"].append({"code": e.code, "stage": e.stage, "message": e.message, "reply": e.reply})
    with db.tx(conn):
        enqueue_notification(conn, "mail_web_imap_failed:%s" % canon.now()[:10], "normal", "info",
                             "The optional Gmail app password did not work for the read-only check of unknown "
                             "sends (%s). Sending in the browser is not affected. Connect a new app password with "
                             "./jobhunter mail connect, or ignore this." % (e.stage or "login"))


def _web_route_run(conn, cfg: dict, out: dict) -> dict:
    """web_ui route with the optional app password: the read-only IMAP Sent search of unknown web sends."""
    if not any(i["method"] == "imap_sent_search" for i in reconcile.work_list(conn, route="mailer")):
        return out
    holder = "mailer:%d" % os.getpid()
    with db.tx(conn):
        got = locks.acquire(conn, LOCK_NAME, holder, LOCK_TTL_S)
    if not got:
        out["imap_skipped"] = {"reason": "another_mailer_run"}
        return out
    lazy = None
    try:
        if breakers.is_paused():
            out["imap_skipped"] = {"reason": "paused"}
            return out
        try:
            breakers.check_breakers(conn, ["global"])
        except Denied as d:
            out["imap_skipped"] = {"reason": d.code, "message": d.message}
            return out
        blocked = _gmail_blocked(conn)
        if blocked:
            out["imap_skipped"] = blocked
            return out
        try:
            owner_addr = owner_address(cfg)
        except Denied as d:
            out["imap_skipped"] = {"reason": "no_owner_address", "message": d.message}
            return out
        try:
            creds = credentials()
        except Denied as d:
            out["imap_skipped"] = {"reason": "not_connected", "message": d.message}
            return out
        if creds.account.lower() != owner_addr:
            out["imap_skipped"] = {"reason": "account_mismatch",
                                   "message": "owner.gmail_address differs from the connected account"}
            return out
        lazy = _LazyImap(creds)
        try:
            out["reconciled"] = reconcile_unknown(conn, lazy.get(), methods=("imap_sent_search",))
        except MailError as e:
            _web_imap_failed(conn, e, out)
        return out
    finally:
        if lazy is not None:
            lazy.close()
        try:
            with db.tx(conn):
                locks.release(conn, LOCK_NAME, holder)
        except Denied:
            pass


def run_once(conn, max_sends: int = 1, fetch: bool = True, cycle_id: str | None = None) -> dict:
    """One mailer run (module doc). Manages its own transactions."""
    out = {"sent": [], "fetched": 0, "replies_classified": 0, "packets_written": 0, "confirmations_seen": 0,
           "reconciled": [], "attempts": [], "skipped": [], "expired_tokens": 0, "errors": [], "blocked": None}
    cfg = _config.load(conn)
    if gate.email_route(cfg) != "mailer":
        lane = browser_lane("send")
        out.update(route=lane["route"], handled_by=lane["handled_by"])
        out["blocked"] = {"reason": "web_ui_route", "handled_by": lane["handled_by"], "message": lane["message"]}
        if is_connected(conn):
            _web_route_run(conn, cfg, out)
        return out
    if not is_connected(conn):
        with db.tx(conn):
            open_human_task(conn, "connect_mail", "Connect Gmail for sending: ./jobhunter mail connect "
                            "(docs/EMAIL-SETUP.md).")
        out["blocked"] = {"reason": "not_connected"}
        return out
    holder = "mailer:%d" % os.getpid()
    with db.tx(conn):
        got = locks.acquire(conn, LOCK_NAME, holder, LOCK_TTL_S)
    if not got:
        out["blocked"] = {"reason": "another_mailer_run"}
        return out
    lazy = None
    try:
        with db.tx(conn):
            out["expired_tokens"] = gate.expire(conn)
        if breakers.is_paused():
            raise Denied("E_PAUSED", "the job hunter is paused", data=breakers.paused_info() or {})
        breakers.check_breakers(conn, ["global"])
        blocked = _gmail_blocked(conn)
        if blocked:
            out["blocked"] = blocked
            return out
        try:
            owner_addr = owner_address(cfg)
        except Denied as d:
            out["blocked"] = {"reason": "no_owner_address", "message": d.message}
            return out
        creds = credentials()
        if creds.account.lower() != owner_addr:
            out["blocked"] = {"reason": "account_mismatch",
                              "message": "owner.gmail_address differs from the connected account; run mail connect"}
            return out
        lazy = _LazyImap(creds)
        # 1. fetch
        if fetch and _fetch_due(conn, cfg):
            try:
                st = fetch_mod.fetch(conn, lazy.get(), owner_addr=owner_addr)
                for k in ("fetched", "replies_classified", "packets_written", "confirmations_seen"):
                    out[k] += st.get(k, 0)
                if st.get("tripped"):
                    out["tripped"] = st["tripped"]
                out["errors"] += st.get("errors", [])
            except MailError as e:
                _handle_imap_error(conn, e, out)
                return out
        # 2. reconcile unknown email actions over IMAP
        if reconcile.work_list(conn, route="mailer"):
            try:
                out["reconciled"] = reconcile_unknown(conn, lazy.get())
            except MailError as e:
                _handle_imap_error(conn, e, out)
                return out
        if _gmail_blocked(conn):
            out["blocked"] = _gmail_blocked(conn)
            return out
        # 3. stop rules on new bounces and complaints, before any send (the cold cap clamp and gmail.cold)
        health = health_check(conn, cfg)
        if health is not None:
            out["health"] = health
            if health["tripped"] and not out.get("tripped"):
                out["tripped"] = "gmail.cold"
        # 4. sends
        max_sends = max(0, min(int(max_sends), 3))
        if max_sends == 0:
            return out
        owner = {"address": owner_addr, "name": owner_name(cfg)}
        sender = None
        sends = 0
        for draft in candidates(conn):
            if sends >= max_sends:
                break
            item = {"draft_uid": draft["draft_uid"], "kind": draft["kind"]}
            try:
                _pre_gate(conn, cfg, draft)
            except Denied as d:
                item.update(code=d.code, message=d.message, retry_after_s=d.retry_after)
                out["skipped"].append(item)
                if d.code in ("E_PACING", "E_PAUSED") or (d.code == "E_BREAKER_OPEN" and
                                                          (d.data or {}).get("scope") in ("global", "gmail")):
                    break
                continue
            try:
                pid = precheck.run_precheck(conn, lazy.get(), draft, cycle_id=cycle_id, owner=owner_addr)
            except MailError as e:
                _handle_imap_error(conn, e, out)
                break
            except Denied as d:
                item.update(code=d.code, message=d.message)
                out["skipped"].append(item)
                if exit_code(d.code) in EXPIRE_EXITS and d.code not in KEEP_CODES:
                    expire_draft(conn, draft, d.code.lower(), d.message)
                continue
            result = precheck.result_of(conn, pid)
            if result == "already_done":
                item.update(code="E_ALREADY_DONE", message="the Sent folder shows it went out before")
                out["skipped"].append(item)
                expire_draft(conn, draft, "already_done", "a message to this person or company was already sent "
                             "(found in Gmail Sent) and is now recorded")
                continue
            if result != "clear":
                item.update(code="E_PRECONDITION", message="the precheck was %s" % result)
                out["skipped"].append(item)
                continue
            if sender is None:
                sender = smtp_sender(creds)
            r = send_one(conn, draft, pid, owner, sender, cycle_id=cycle_id)
            r.update(item)
            if r["status"] == "refused":
                out["skipped"].append(r)
                code = r.get("code") or "E_INTERNAL"
                if exit_code(code) in EXPIRE_EXITS and code not in KEEP_CODES:
                    expire_draft(conn, draft, code.lower(), r.get("message") or code)
                if exit_code(code) in (4, 5, 8) or code in ("E_TOKEN_OPEN", "E_ROUTE_UNAVAILABLE", "E_GUARD_MISSING"):
                    if code != "E_OUTSIDE_HOURS":
                        break
                continue
            out["attempts"].append(r)
            sends += 1
            if r["status"] in ("sent", "sent_unrecorded"):
                out["sent"].append(r["token"])
            if r.get("transport_error"):
                out["errors"].append({"code": "E_MAIL_TRANSPORT", "stage": r.get("stage"),
                                      "message": r["transport_error"]})
                break
            if r.get("tripped"):
                out["tripped"] = r["tripped"]
                break
        return out
    finally:
        if lazy is not None:
            lazy.close()
        try:
            with db.tx(conn):
                locks.release(conn, LOCK_NAME, holder)
        except Denied:
            pass
