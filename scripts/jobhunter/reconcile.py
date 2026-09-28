"""Reconciliation of unknown actions (design 2.3.4): the only ways a blocking action becomes free.

| Kind and route | Freed (failed) only when | Otherwise |
|---|---|---|
| email, mailer route | two imap_message_id not_found checks, the first >= 15 min after armed_at, the second >= 24 h after the first (not_found_twice) | found: sent |
| email, web route | two negative rounds of web_sent/outbox/scheduled searches (>= 15 min and >= 24 h later), plus an imap_sent_search when the mail connection exists | unknown; after 48 h a confirm_not_sent task |
| application | the human (confirm_not_sent, PIN) when no confirmation email arrived in the 24 h after armed_at | unknown forever |
| LinkedIn kinds | the human, as above | unknown; the Sent page can still move it to sent |

Agents report checks (found, not_found, unknowable); each call adds one reconcile_checks row. A
not_found report that comes too early is E_TOO_EARLY with retry_after_s and is not recorded.
"""
from __future__ import annotations

from . import gate
from .canon import now, seconds_between, ts_add
from .errors import Denied
from .events import log_event

FIRST_DELAY_S = 15 * 60
SECOND_DELAY_S = 24 * 3600
WEB_METHODS = ("web_sent_search", "web_outbox_search", "web_scheduled_search")
AGENT_METHODS = WEB_METHODS + ("li_sent_invites", "li_conversation", "ats_page", "inbox_confirmation")
METHODS = AGENT_METHODS + ("imap_message_id", "imap_sent_search", "human")
EMAIL = ("cold_email", "followup_email", "application_email")


def _base(a) -> str:
    return a["armed_at"] or a["reserved_at"]


def _checks(conn, action_id: int, method: str | None = None, result: str | None = None) -> list:
    q = "SELECT * FROM reconcile_checks WHERE action_id = ?"
    args = [action_id]
    if method:
        q += " AND method = ?"
        args.append(method)
    if result:
        q += " AND result = ?"
        args.append(result)
    return conn.execute(q + " ORDER BY checked_at, id", args).fetchall()


def _is_mailer_email(a) -> bool:
    return a["kind"] in EMAIL and a["route"] == "mailer"


def _is_web_email(a) -> bool:
    return a["kind"] in EMAIL and a["route"] == "browser"


def _mail_connected(conn) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE key = 'mail_connected_at' AND value <> ''").fetchone() is not None


def next_not_before(conn, a, method: str) -> str | None:
    """When the next not_found report for this method counts (None: no further check is useful)."""
    base = _base(a)
    first = ts_add(base, seconds=FIRST_DELAY_S)
    if _is_mailer_email(a) and method == "imap_message_id":
        prev = _checks(conn, a["id"], "imap_message_id", "not_found")
        if not prev:
            return first
        return ts_add(prev[0]["checked_at"], seconds=SECOND_DELAY_S)
    if _is_web_email(a) and method in WEB_METHODS:
        r1 = _round_done(conn, a, 1)
        if r1 is None:
            return first
        return ts_add(r1, seconds=SECOND_DELAY_S)
    return first


def _round_done(conn, a, n: int) -> str | None:
    """Latest check time of round n of the three web searches, or None when round n is incomplete."""
    first = ts_add(_base(a), seconds=FIRST_DELAY_S)
    per = {m: [r["checked_at"] for r in _checks(conn, a["id"], m, "not_found") if r["checked_at"] >= first]
           for m in WEB_METHODS}
    if n == 1:
        if all(per[m] for m in WEB_METHODS):
            return max(per[m][0] for m in WEB_METHODS)
        return None
    r1 = _round_done(conn, a, 1)
    if r1 is None:
        return None
    lim = ts_add(r1, seconds=SECOND_DELAY_S)
    later = {m: [t for t in per[m] if t >= lim] for m in WEB_METHODS}
    if all(later[m] for m in WEB_METHODS):
        return max(later[m][0] for m in WEB_METHODS)
    return None


def work_list(conn, route: str | None = None, agent_id: str | None = None) -> list[dict]:
    """Reconcile tasks for unknown actions: [{token, kind, platform, method, check, target, not_before}].
    route 'browser' lists what an agent checks in the browser; 'mailer' what code checks over IMAP."""
    out = []
    for a in conn.execute("SELECT * FROM actions WHERE status = 'unknown' ORDER BY id").fetchall():
        if agent_id and a["agent_id"] != agent_id:
            continue
        target = {"recipient": a["recipient"], "message_id": a["message_id"]}
        uids = gate._uids(conn, {"job_id": a["job_id"], "contact_id": a["contact_id"], "company_id": a["company_id"],
                                 "thread_key": a["thread_key"], "recipient": a["recipient"]})
        target.update({k: v for k, v in uids.items() if v})
        tasks = []
        if _is_mailer_email(a):
            tasks.append(("mailer", "imap_message_id", "search All Mail for the Message-ID %s" % (a["message_id"] or "")))
        elif _is_web_email(a):
            for m, what in zip(WEB_METHODS, ("Sent", "Outbox", "Scheduled")):
                tasks.append(("browser", m, "search %s for messages to %s" % (what, a["recipient"])))
            if _mail_connected(conn):
                tasks.append(("mailer", "imap_sent_search", "IMAP search in:sent to:%s" % a["recipient"]))
        elif a["kind"] == "application":
            tasks.append(("browser", "ats_page", "open the posting and look for an Applied state or confirmation"))
            tasks.append(("mailer", "inbox_confirmation", "look for a confirmation email from the company"))
        elif a["kind"] == "li_invite":
            tasks.append(("browser", "li_sent_invites", "find the person on the invitation manager Sent page"))
        elif a["platform"] == "linkedin":
            tasks.append(("browser", "li_conversation", "open the conversation and look for the message"))
        for r, m, what in tasks:
            if route and r != route:
                continue
            out.append({"token": a["token"], "kind": a["kind"], "platform": a["platform"], "method": m, "check": what,
                        "target": target, "not_before": next_not_before(conn, a, m), "route": r,
                        "checks_so_far": len(_checks(conn, a["id"]))})
    return out


def record_check(conn, token: str, method: str, result: str, detail: str, by: str, agent_id: str | None = None) -> dict:
    """Record one check and apply the table above. result: found | not_found | unknowable."""
    if method not in METHODS:
        raise Denied("E_VALIDATION", "unknown reconcile method %r" % method)
    if result not in ("found", "not_found", "unknowable"):
        raise Denied("E_VALIDATION", "result must be found, not_found or unknowable")
    a = gate.action_by_token(conn, token)
    if agent_id and a["agent_id"] != agent_id:
        raise Denied("E_NOT_FOUND", "the token belongs to another agent")
    if a["status"] != "unknown":
        return {"status": a["status"], "checks_so_far": len(_checks(conn, a["id"])), "next_check_after": None}
    ts = now()
    if result == "not_found":
        nb = next_not_before(conn, a, method)
        if nb and ts < nb:
            raise Denied("E_TOO_EARLY", "this check counts only after %s" % nb, retry_after=seconds_between(ts, nb),
                         data={"not_before": nb})
    stored = "error" if result == "unknowable" else result
    conn.execute("INSERT INTO reconcile_checks (action_id, method, result, detail, checked_at, by) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (a["id"], method, stored, (detail or "")[:4000], ts, by))
    log_event(conn, "reconcile_check", token=token, method=method, result=stored, by=by)
    status = "unknown"
    if result == "found":
        gate._mark_sent(conn, a, "reconcile %s: %s" % (method, (detail or "")[:3000]), by="reconcile")
        status = "sent"
    elif result == "not_found" and _freeable(conn, a):
        _free(conn, a, "not_found_twice", "reconcile: not found twice")
        status = "failed"
    nxt = None if status != "unknown" else next_not_before(conn, a, method)
    return {"status": status, "checks_so_far": len(_checks(conn, a["id"])), "next_check_after": nxt}


def _freeable(conn, a) -> bool:
    if _is_mailer_email(a):
        rows = _checks(conn, a["id"], "imap_message_id", "not_found")
        first_ok = ts_add(_base(a), seconds=FIRST_DELAY_S)
        valid = [r["checked_at"] for r in rows if r["checked_at"] >= first_ok]
        return len(valid) >= 2 and valid[-1] >= ts_add(valid[0], seconds=SECOND_DELAY_S)
    if _is_web_email(a):
        if _round_done(conn, a, 2) is None:
            return False
        if _mail_connected(conn):
            first_ok = ts_add(_base(a), seconds=FIRST_DELAY_S)
            return any(r["checked_at"] >= first_ok for r in _checks(conn, a["id"], "imap_sent_search", "not_found"))
        return True
    return False


def _free(conn, a, reason: str, evidence: str) -> None:
    ts = now()
    conn.execute("UPDATE actions SET status = 'failed', fail_reason = ?, resolved_at = ?, evidence = COALESCE(evidence, ?), "
                 "updated_at = ? WHERE id = ?", (reason, ts, evidence, ts, a["id"]))
    if a["kind"] in ("application", "application_email") and a["job_id"]:
        gate._job_after_fail(conn, a["job_id"], reason)
    log_event(conn, "reconcile_freed", token=a["token"], reason=reason)


def confirm_not_sent(conn, token: str, by: str) -> dict:
    """Human (PIN): an application, LinkedIn or web-route email action that stayed unknown is marked
    failed (human_confirmed_not_sent) when at least 24 h passed since armed_at and no confirmation email
    arrived from the company in that time."""
    a = gate.action_by_token(conn, token)
    if a["status"] != "unknown":
        raise Denied("E_PRECONDITION", "the token is %s, not unknown" % a["status"])
    if _is_mailer_email(a):
        raise Denied("E_PRECONDITION", "code-sent email is resolved by two IMAP checks, not by hand")
    base = _base(a)
    ts = now()
    wait = SECOND_DELAY_S if not _is_web_email(a) else 48 * 3600
    ready = ts_add(base, seconds=wait)
    if ts < ready:
        raise Denied("E_TOO_EARLY", "wait until %s" % ready, retry_after=seconds_between(ts, ready))
    if a["company_id"]:
        conf = conn.execute("SELECT 1 FROM inbound_messages WHERE company_id = ? AND code_class = "
                            "'application_confirmation' AND received_at >= ? AND received_at <= ?",
                            (a["company_id"], base, ts_add(base, seconds=SECOND_DELAY_S))).fetchone()
        if conf and a["kind"] in ("application", "application_email"):
            raise Denied("E_PRECONDITION", "a confirmation email arrived; the application went through")
    conn.execute("INSERT INTO reconcile_checks (action_id, method, result, detail, checked_at, by) "
                 "VALUES (?, 'human', 'not_found', 'confirmed not sent', ?, ?)", (a["id"], ts, by))
    _free(conn, a, "human_confirmed_not_sent", "confirmed not sent by " + by)
    return {"status": "failed", "token": token}


def open_stale_tasks(conn) -> int:
    """Housekeeping: a confirm_not_sent task for web-route email unknown for 48 h; an apply_manually task
    for unknown applications."""
    from .events import open_human_task
    n = 0
    for a in conn.execute("SELECT * FROM actions WHERE status = 'unknown'").fetchall():
        age = seconds_between(_base(a), now())
        if _is_web_email(a) and age >= 48 * 3600:
            open_human_task(conn, "confirm_not_sent", "Email %s to %s is still unknown after 48 hours. If it was not "
                            "sent run ./jobhunter reconcile not-sent %s" % (a["token"], a["recipient"], a["token"]),
                            action_id=a["id"], company_id=a["company_id"])
            n += 1
        elif a["kind"] == "application" and age >= SECOND_DELAY_S:
            open_human_task(conn, "apply_manually", "Check whether application %s went through; if not, apply by "
                            "hand or run ./jobhunter reconcile not-sent %s" % (a["token"], a["token"]),
                            job_id=a["job_id"], action_id=a["id"], company_id=a["company_id"])
            n += 1
    return n

