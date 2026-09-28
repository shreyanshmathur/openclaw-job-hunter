"""Cross-unit hooks called inside U1's gate transactions (design 2.5, 11 rule 5).

The target modules are imported lazily so the core imports without them. A hook receives a connection
inside an open BEGIN IMMEDIATE transaction and must never commit, roll back, open a transaction, call
the network or take more than 100 ms. An exception in a hook rolls back the whole confirm or reserve;
a missing target module is E_INTERNAL for on_confirm and presend (a send is never confirmed without its
thread bookkeeping). The email finder hooks (on_reserve_address, on_bounce, on_optout, on_contact_merge,
on_forget) treat a missing finder as "nothing to do", except that on_reserve_address then refuses a
provider-found address (fail closed).
"""
from __future__ import annotations

import importlib

from .errors import Denied


def _target(module: str, func: str):
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        raise Denied("E_INTERNAL", "hook target %s is not installed: %s" % (module, exc))
    fn = getattr(mod, func, None)
    if fn is None:
        raise Denied("E_INTERNAL", "hook target %s.%s is missing" % (module, func))
    return fn


def on_confirm(conn, action_row) -> dict | None:
    """Called by gate.confirm after the action is marked sent; forwards to jobhunter.threads.on_confirm
    (U6), which creates or updates the thread and the follow-up due date, and returns its result:
    {thread_key, followup_due_at}, or None for kinds without a thread."""
    res = _target("jobhunter.threads", "on_confirm")(conn, action_row)
    return res if isinstance(res, dict) else None


def presend(conn, draft_id: int) -> dict:
    """Called by gate.reserve; forwards to jobhunter.qc.presend.presend (U3):
    {ok, sha256, send_text, blocks}."""
    return _target("jobhunter.qc.presend", "presend")(conn, draft_id)


# ---------------------------------------------------------------- email finder (U10) hooks
# The email finder is optional: these hooks import it lazily and, when it is missing, do nothing, with one
# exception. on_reserve_address fails closed for a provider-found address: without the finder's rules
# (result age, bounce strikes, share of cold email) such an address never goes out.
def _optional(module: str, func: str):
    """The hook target, or None when the module or the function is not installed."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    return getattr(mod, func, None)


def _email_source(conn, contact_id) -> str | None:
    if contact_id is None:
        return None
    try:
        row = conn.execute("SELECT email_source FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    except Exception:       # a database without the finder's columns holds no provider address
        return None
    return row[0] if row is not None else None


def on_reserve_address(conn, ctx: dict) -> None:
    """Called by gate.reserve after the address grade and MX check for cold_email and application_email with
    a person contact. ctx: {kind, contact_id, recipient, company_id, reserved_at}. Forwards to
    jobhunter.enrich.gatecheck.on_reserve_address (U10), which raises Denied when a provider-found address
    must not be sent. Without the finder: E_ADDRESS_GRADE for a provider-found address, else nothing."""
    fn = _optional("jobhunter.enrich.gatecheck", "on_reserve_address")
    if fn is None:
        if _email_source(conn, ctx.get("contact_id")) == "provider":
            raise Denied("E_ADDRESS_GRADE", "this address came from an email finder whose send-time rules are not "
                         "installed; it is not sent")
        return
    fn(conn, ctx)


def on_bounce(conn, action_row, thread_row=None) -> list:
    """Called by the bounce paths (U6 replies): marks the provider result behind a bounced address and trips
    the finder's bounce breakers. Returns the scopes tripped ([] without the finder)."""
    fn = _optional("jobhunter.enrich.feedback", "on_bounce")
    if fn is None:
        return []
    res = fn(conn, action_row, thread_row)
    return res if isinstance(res, list) else []


def on_optout(conn, contact_id: int) -> int:
    """Called for opt_out and complaint replies (U6): the person's provider data is purged at once."""
    fn = _optional("jobhunter.enrich.feedback", "on_optout")
    if fn is None or contact_id is None:
        return 0
    return int(fn(conn, contact_id) or 0)


def on_contact_merge(conn, from_id: int, to_id: int) -> None:
    """Called by people.merge: finder requests of the merged contact move to the survivor."""
    fn = _optional("jobhunter.enrich.cache", "on_merge")
    if fn is not None:
        fn(conn, from_id, to_id)


def on_forget(conn, contact_id: int) -> None:
    """Called by forget before the contact is cleared: finder requests become `forgotten` and the provider
    addresses and URLs are cleared (salted key hashes stay)."""
    fn = _optional("jobhunter.enrich.cache", "forget")
    if fn is not None:
        fn(conn, contact_id)
