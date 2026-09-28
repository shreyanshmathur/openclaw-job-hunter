"""Bounce and opt-out hooks (U10, ENRICH-SPEC 9.5, 10 and 11.4). Hook rules: caller's transaction, no
commit, no network.

on_bounce: the provider call behind the bounced address gets bounced_at; 2 hard bounces on one provider's
addresses within 30 days open `enrich:<provider>` (bounce_strikes), 3 across providers open `enrich`. A breaker
already open there for another reason is escalated to bounce_strikes, and purged calls still count.
Unsent addresses from a stopped provider become unsendable (gatecheck and t_enrich_action_address).
on_optout: the person's provider data is purged at once (same fields as the retention purge).
"""
from __future__ import annotations

from ..canon import now, ts_add
from ..events import log_event
from . import budget, cache, settings


def _get(row, key):
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


# One strike per bounced address. A purge (forget, opt-out, complaint, retention) clears enrich_calls.email but
# keeps bounced_at, so a purged call counts by its request instead: strikes survive the purge.
STRIKE_KEY = "COALESCE(email, 'req:' || COALESCE(request_id, 'call' || id))"


def _strike_stop(conn, scope: str, detail: str) -> bool:
    """Open `scope` with reason bounce_strikes (waits for the owner's reset). A breaker already open there for
    another reason (a 429 back-off, a refused key, an error streak) is escalated, not skipped: the other stop
    would end by itself or with `enrich connect`, and the bounce stop must hold until reset. False when the
    scope is already stopped for bounces."""
    row = conn.execute("SELECT reason_code FROM breakers WHERE scope = ? AND state = 'open'", (scope,)).fetchone()
    if row is not None and row["reason_code"] == "bounce_strikes":
        return False
    budget.trip(conn, scope, "bounce_strikes", detail)
    return True


def on_bounce(conn, action_row, thread_row=None, s: dict | None = None) -> list:
    """Mark the provider result behind a bounced address and trip the strike breakers. Returns the scopes
    tripped."""
    contact_id = _get(action_row, "contact_id") or _get(thread_row, "contact_id")
    recipient = (_get(action_row, "recipient") or "").strip().lower()
    call_ids = set()
    if contact_id is not None:
        r = conn.execute("SELECT email_enrich_call_id, email FROM contacts WHERE id = ? AND email_source = 'provider'",
                         (contact_id,)).fetchone()
        if r is not None and r[0] is not None and (not recipient or (r[1] or "").lower() == recipient):
            call_ids.add(r[0])
    if recipient:
        for (cid,) in conn.execute("SELECT id FROM enrich_calls WHERE email = ? AND grade_hint IN ('B','C')",
                                   (recipient,)):
            call_ids.add(cid)
    if not call_ids:
        return []
    ts = now()
    q = ",".join("?" * len(call_ids))
    conn.execute("UPDATE enrich_calls SET bounced_at = COALESCE(bounced_at, ?), updated_at = ? WHERE id IN (%s)" % q,
                 [ts, ts] + sorted(call_ids))
    s = s if s is not None else settings.load(conn)
    per_p = int(s["bounce_strikes"]["per_provider_30d"])
    all_p = int(s["bounce_strikes"]["all_providers_30d"])
    since = ts_add(ts, days=-30)
    tripped = []
    provs = {r[0] for r in conn.execute("SELECT DISTINCT provider FROM enrich_calls WHERE id IN (%s)" % q,
                                        sorted(call_ids))}
    for p in sorted(provs):
        n = conn.execute("SELECT count(DISTINCT %s) FROM enrich_calls WHERE provider = ? AND bounced_at > ?" % STRIKE_KEY,
                         (p, since)).fetchone()[0]
        if n >= per_p and _strike_stop(conn, "enrich:" + p, "%d hard bounces on %s addresses in 30 days" % (n, p)):
            tripped.append("enrich:" + p)
    total = conn.execute("SELECT count(DISTINCT %s) FROM enrich_calls WHERE bounced_at > ?" % STRIKE_KEY,
                         (since,)).fetchone()[0]
    if total >= all_p and _strike_stop(conn, "enrich", "%d hard bounces on provider addresses in 30 days" % total):
        tripped.append("enrich")
    log_event(conn, "enrich_bounce", calls=len(call_ids), tripped=tripped)
    return tripped


def on_optout(conn, contact_id: int) -> int:
    """Purge the person's provider data at once (opt_out and complaint replies). Returns calls purged."""
    ids = cache._group(conn, contact_id)
    q = ",".join("?" * len(ids))
    call_ids = {r[0] for r in conn.execute(
        "SELECT c.id FROM enrich_calls c JOIN enrich_requests r ON r.id = c.request_id WHERE r.contact_id IN (%s)"
        % q, ids)}
    call_ids |= {r[0] for r in conn.execute("SELECT email_enrich_call_id FROM contacts WHERE id IN (%s) AND "
                                            "email_enrich_call_id IS NOT NULL" % q, ids)}
    n = cache.purge_calls(conn, sorted(call_ids))
    log_event(conn, "enrich_optout_purge", calls=n)
    return n
