"""Functions U10 consumes from other units (U10, ENRICH-SPEC sections 11 and 16).

U1 gate.dedup_check, U6 emailcheck.mx_for_domain / guess_blocked / pattern_evidence / render_pattern and U6
contacts.set_address. The modules are imported when a function is first used (never at package import), so
the finder never forms an import cycle with the core. A missing function is a bug of the install: it fails
closed (no MX lookup means no lookup, no set_address means nothing is written and the result is not
sendable). Tests never replace these functions: they fake only the network under them (emailcheck.lookup_mx).
"""
from __future__ import annotations

import importlib

from ..errors import Denied

MODULE_OF = {"dedup_check": "gate", "set_address": "contacts", "mx_for_domain": "emailcheck",
             "guess_blocked": "emailcheck", "pattern_evidence": "emailcheck", "render_pattern": "emailcheck"}


def _real(name: str):
    try:
        mod = importlib.import_module("jobhunter." + MODULE_OF[name])
    except ImportError:
        return None
    return getattr(mod, name, None)


def available(name: str) -> bool:
    """Whether the other unit's function is installed (selftest, `enrich budget`)."""
    return _real(name) is not None


def dedup_check(conn, kind: str, **kw) -> dict:
    """U1 gate.dedup_check: {allowed, hits: [{rule, code, detail}]}, read only."""
    fn = _real("dedup_check")
    if fn is None:
        raise Denied("E_INTERNAL", "gate.dedup_check is not installed")
    return fn(conn, kind, **kw)


def mx_for_domain(domain: str) -> dict:
    """U6 emailcheck.mx_for_domain: {mx_ok, mx_hosts, provider_hint, cached} (network, never inside a
    transaction). A DNS failure is Denied(E_ENRICH_UNAVAILABLE), never "no MX"."""
    fn = _real("mx_for_domain")
    if fn is None:
        raise Denied("E_ENRICH_UNAVAILABLE", "no MX lookup is installed", data={"reason": "mx_unavailable"})
    return fn(domain)


def guess_blocked(conn, domain: str) -> bool:
    """U6 emailcheck.guess_blocked: the domain is on the no-guessing list after a bounce on a B or C address.
    Fails closed (blocked) when it is not installed."""
    fn = _real("guess_blocked")
    return True if fn is None else bool(fn(conn, domain))


def pattern_evidence(conn, domain: str) -> dict | None:
    """U6 emailcheck.pattern_evidence: {pattern, evidence_urls, ...} when at least 2 published addresses prove
    one pattern at the domain; None otherwise (also when it is not installed: no pattern step)."""
    fn = _real("pattern_evidence")
    return fn(conn, domain) if fn is not None else None


def render_pattern(pattern: str, first: str | None, last: str | None) -> str | None:
    """U6 emailcheck.render_pattern (pure)."""
    fn = _real("render_pattern")
    return fn(pattern, first, last) if fn is not None else None


def set_address(conn, contact_id: int, *, email: str, grade: str, source: str, evidence_url: str | None,
                enrich_call_id: int | None = None, mx_ok: bool | None = None) -> dict | None:
    """U6 contacts.set_address (section 11.1) in the caller's transaction: {contact_id, contact_uid, email,
    grade, source, do_not_contact, merged_with}. The contact it returns is the survivor when the address
    merged two rows; its email_mx_ok is then set from the MX pre-step (set_address clears it when the
    address changes). None when set_address is not installed: nothing is written."""
    fn = _real("set_address")
    if fn is None:
        return None
    out = fn(conn, contact_id, email=email, grade=grade, source=source, evidence_url=evidence_url,
             enrich_call_id=enrich_call_id)
    out = out if isinstance(out, dict) else {}
    if mx_ok is not None:
        cid = out.get("contact_id") or contact_id
        conn.execute("UPDATE contacts SET email_mx_ok = ? WHERE id = ? AND lower(email) = ?",
                     (1 if mx_ok else 0, cid, email.strip().lower()))
    return out
