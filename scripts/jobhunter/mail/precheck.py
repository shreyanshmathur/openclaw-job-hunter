"""Precheck by code over IMAP for the app_password route (design 2.3.1 step 1, 12.7).

The checks and Gmail queries come from gate.precheck_plan (U1), the same plan the web route runs in the
browser; the values are counted over IMAP and recorded with gate.record_precheck(source='code_imap'):

- cold_email, application_email: sent_to_address, sent_to_other_addresses, sent_company_query, outbox_query,
  scheduled_query are message counts (any > 0 means already_done; gate records an imported action).
- followup_email: thread_has_reply (any message in the Gmail conversation of our first message that is not
  from us and not an automatic reply, or any such message from the recipient since the first send) and
  company_inbound_since_first (messages from the company domain since the first send, automatic replies
  not counted).
A query the plan leaves empty (no other address, no company alias) counts as 0. IMAP errors raise MailError,
so nothing is recorded and nothing is sent in that run (fail closed).

On the web_ui route (the default) this module is not used: the agent runs the same plan in Gmail in the browser
and records it with `gate precheck --file` (skill jobhunter-gmail-web). run_precheck() refuses there with
E_ROUTE_UNAVAILABLE and the browser_lane() data, before any IMAP call.
"""
from __future__ import annotations

from .. import canon, db, gate
from ..errors import Denied
from . import browser_lane, is_web_route
from .fetch import gmail_date, is_auto_ack, is_bounce, is_out_of_office
from .mime import bare_id

PLATFORM = "gmail"


def _not_ours(h: dict, owner: str) -> bool:
    return (h.get("from_addr") or "").lower() != owner


def _real_messages(imap, uids: list[str], owner: str) -> int:
    """Messages among uids that are not from us and not automatic replies (bounces count)."""
    if not uids:
        return 0
    n = 0
    for h in imap.fetch_headers(uids):
        if not _not_ours(h, owner):
            continue
        if is_bounce(h):
            n += 1
            continue
        if is_out_of_office(h, "") or is_auto_ack(h, ""):
            continue
        n += 1
    return n


def followup_values(conn, imap, plan: dict, owner: str) -> dict:
    thread_key = plan["target"].get("thread_key")
    th = conn.execute("SELECT * FROM threads WHERE thread_key = ?", (thread_key,)).fetchone()
    if th is None:
        raise Denied("E_FOLLOWUP_BINDING", "no thread %s" % thread_key)
    first = conn.execute("SELECT recipient, sent_at, reserved_at, message_id FROM actions WHERE id = ?",
                         (th["first_action_id"],)).fetchone()
    first_sent = (first["sent_at"] or first["reserved_at"]) if first else th["created_at"]
    mid = th["first_message_id"] or (first["message_id"] if first else None)
    reply_uids: set = set()
    if mid:
        for uid in imap.search("rfc822msgid:%s" % bare_id(mid)):
            reply_uids |= set(imap.thread_uids(uid))
    recipient = (first["recipient"] if first else None) or plan["target"].get("recipient")
    if recipient:
        reply_uids |= set(imap.search("from:%s after:%s" % (recipient, gmail_date(first_sent))))
    has_reply = _real_messages(imap, sorted(reply_uids, key=int), owner) > 0
    company_n = 0
    for c in plan["checks"]:
        if c["name"] == "company_inbound_since_first" and c.get("query"):
            company_n = _real_messages(imap, imap.search(c["query"]), owner)
    return {"thread_has_reply": has_reply, "company_inbound_since_first": company_n}


def run_precheck(conn, imap, draft_row, cycle_id: str | None = None, owner: str | None = None) -> int:
    """Run the plan's checks over IMAP and record them; returns the precheck id. Manages its own
    transaction (the IMAP searches run before it). Denied from the plan's cheap dedup check propagates."""
    kind = draft_row["kind"]
    if kind not in gate.EMAIL_KINDS:
        raise Denied("E_VALIDATION", "%s is not an email kind" % kind)
    from .. import config as _config
    if is_web_route(_config.load(conn)):
        lane = browser_lane("precheck")
        raise Denied("E_ROUTE_UNAVAILABLE", lane["message"], data=lane)
    owner = (owner or getattr(imap, "account", "") or "").lower()
    plan = gate.precheck_plan(conn, kind, route="mailer", job_id=draft_row["job_id"],
                              contact_id=draft_row["contact_id"], thread_key=draft_row["thread_key"])
    if kind == "followup_email":
        vals = followup_values(conn, imap, plan, owner)
    else:
        vals = {c["name"]: imap.count(c.get("query")) if c.get("query") else 0 for c in plan["checks"]}
    evidence = {"kind": kind, "platform": PLATFORM, "observed_at": canon.now(),
                "checks": [{"name": c["name"], "value": vals[c["name"]]} for c in plan["checks"]]}
    with db.tx(conn):
        res = gate.record_precheck(conn, kind, PLATFORM, evidence, "code_imap", job_id=draft_row["job_id"],
                                   contact_id=draft_row["contact_id"], thread_key=draft_row["thread_key"],
                                   cycle_id=cycle_id)
    return res["precheck_id"]


def result_of(conn, precheck_id: int) -> str | None:
    row = conn.execute("SELECT result FROM prechecks WHERE id = ?", (precheck_id,)).fetchone()
    return row[0] if row else None
