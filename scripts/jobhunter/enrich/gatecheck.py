"""Extra send-time rules for provider-sourced addresses, called by gate.reserve through
hooks.on_reserve_address (U10, ENRICH-SPEC 11.3). Hook rules: runs inside the caller's transaction, never
commits, no network, well under 100 ms.

Only for cold_email and application_email to a contact whose email_source is 'provider':
1. recipient == the contact's address == the linked call's address; the call has not bounced or been purged;
2. the result is younger than enrich.max_result_age_days;
3. no open `enrich` or `enrich:<provider>` breaker with reason bounce_strikes;
4. provider-sourced sends stay within enrich.max_share_of_cold_sends of the live email sends of 7 days.
"""
from __future__ import annotations

import math

from ..canon import now, parse_ts, ts_add
from ..errors import Denied
from . import budget, settings

KINDS = ("cold_email", "application_email")
LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")


def _norm(addr: str | None) -> str:
    return (addr or "").strip().lower()


def check(conn, *, kind: str, contact_id: int | None, recipient: str | None, reserved_at: str,
          s: dict | None = None) -> None:
    if kind not in KINDS or contact_id is None:
        return
    c = conn.execute("SELECT id, email, email_source, email_enrich_call_id FROM contacts WHERE id = ?",
                     (contact_id,)).fetchone()
    if c is None or c["email_source"] != "provider":
        return
    s = s if s is not None else settings.load(conn)
    call = conn.execute("SELECT * FROM enrich_calls WHERE id = ?", (c["email_enrich_call_id"],)).fetchone() \
        if c["email_enrich_call_id"] is not None else None
    if call is None or call["email"] is None or _norm(recipient) != _norm(c["email"]) or \
            _norm(c["email"]) != _norm(call["email"]):
        raise Denied("E_ADDRESS_GRADE", "the recipient is not the provider result stored for this contact")
    if call["bounced_at"] is not None or call["purged_at"] is not None:
        raise Denied("E_ADDRESS_GRADE", "the provider address bounced or was purged")
    finished = call["finished_at"] or call["started_at"]
    if finished < ts_add(reserved_at, days=-int(s["max_result_age_days"])):
        raise Denied("E_ADDRESS_GRADE", "the provider result is older than %d days (people change jobs)"
                     % int(s["max_result_age_days"]), data={"reason": "stale_result"})
    for scope in ("enrich", "enrich:" + call["provider"]):
        row = budget.breaker_row(conn, scope)
        if row is not None and row["reason_code"] == "bounce_strikes":
            raise Denied("E_ADDRESS_GRADE", "addresses from %s are stopped after bounces" % call["provider"],
                         data={"reason": "bounce_strikes", "scope": scope})
    since = ts_add(reserved_at, days=-7)
    q = ",".join("?" * len(LIVE))
    rows = conn.execute("SELECT a.reserved_at, c.email_source FROM actions a LEFT JOIN contacts c ON c.id = a.contact_id "
                        "WHERE a.kind IN ('cold_email','application_email') AND a.status IN (%s) AND a.reserved_at > ? "
                        "ORDER BY a.reserved_at" % q, list(LIVE) + [since]).fetchall()
    n = len(rows) + 1
    prov = [r["reserved_at"] for r in rows if r["email_source"] == "provider"]
    p = len(prov) + 1
    allowed = max(1, int(math.floor(float(s["max_share_of_cold_sends"]) * n + 1e-9)))
    if p > allowed:
        retry = None
        if prov:
            retry = max(1, int((parse_ts(ts_add(prov[0], days=7)) - parse_ts(reserved_at)).total_seconds()))
        raise Denied("E_CEILING", "provider-found addresses are at their share of cold email this week",
                     retry_after=retry, data={"reason": "provider_share", "provider_sends": p - 1, "sends": n - 1})


def on_reserve_address(conn, ctx: dict) -> None:
    """Entry point for hooks.on_reserve_address (INTEGRATION PATCH LIST U1-6): ctx carries kind, contact_id,
    recipient, company_id and reserved_at as gate.reserve knows them."""
    check(conn, kind=ctx.get("kind"), contact_id=ctx.get("contact_id"), recipient=ctx.get("recipient"),
          reserved_at=ctx.get("reserved_at") or now())
