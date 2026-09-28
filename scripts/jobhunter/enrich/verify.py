"""Address checks and grading on the existing A/B/C scale (U10, ENRICH-SPEC section 5).

A: published verbatim (set by U6 emailcheck.verify_address, never here). B: pattern proven by two
published addresses, or a mailbox-verified provider result (verified_by_finder, finder_agreement,
verified_by_verifier). C: everything weaker (blocked by default). X: invalid (internal, never stored on a
contact). All functions are pure except `suppression`, `known_invalid` and `lookup`, which only read the
database.
"""
from __future__ import annotations

import re

from .. import keys as keys_mod
from .providers import EMAIL_RE, ROLE_LOCALS, EnrichResult, normalize_status

NEEDS_VERIFY = ("accept_all", "unknown", "none")


def normalize(provider: str, raw_status: str | None) -> str:
    return normalize_status(provider, raw_status)


def _letters(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", keys_mod.fold(s or ""))


def precheck_address(email: str, *, company_domains: set, freemail: set, hosting=(), free_mail_flag: bool = False,
                     raw_status: str | None = None) -> str | None:
    """Reject reason for a provider address (5.1), or None when it may be graded:
    syntax, free_mail, domain_mismatch, role_address."""
    e = (email or "").strip().lower()
    if not EMAIL_RE.match(e):
        return "syntax"
    local, domain = e.rsplit("@", 1)
    reg = keys_mod.registrable_domain(domain) or domain
    hosting = tuple(hosting or ())
    if free_mail_flag or domain in freemail or reg in freemail or (raw_status or "").lower() in ("webmail", "disposable"):
        return "free_mail"
    if any(domain == h or domain.endswith("." + h) or reg == h for h in hosting):
        return "free_mail"
    if reg not in company_domains and domain not in company_domains:
        return "domain_mismatch"
    base = local.split("+", 1)[0]
    if base in ROLE_LOCALS or local in ROLE_LOCALS:
        return "role_address"
    return None


def name_plausible(local: str, first: str | None, last: str | None) -> bool:
    """After NFKD ASCII folding the local part contains the last name (>= 2 letters), the first name (>= 3
    letters), first initial + last name, or first name + last initial."""
    loc = _letters(local.split("+", 1)[0])
    f, l_ = _letters(first), _letters(last)
    if not loc:
        return False
    if len(l_) >= 2 and l_ in loc:
        return True
    if len(f) >= 3 and f in loc:
        return True
    if f and l_ and (f[0] + l_) in loc:
        return True
    if f and l_ and (f + l_[0]) in loc:
        return True
    return False


def grade(candidate: EnrichResult, *, verifier: EnrichResult | None, agreement: bool, pattern_match: bool,
          name_ok: bool, min_confidence: int) -> tuple:
    """(grade, reason) for one provider candidate (5.2, first matching row wins; an invalid verdict is
    checked first so nothing can outrank it)."""
    v = verifier.verification if verifier is not None else None
    if candidate.verification == "invalid" or v == "invalid" or candidate.outcome == "invalid":
        return "X", "invalid"
    if pattern_match:
        return "B", "pattern_evidence"
    conf_ok = candidate.confidence is None or candidate.confidence >= min_confidence
    if candidate.verification == "valid" and conf_ok and name_ok:
        return "B", "verified_by_finder"
    if agreement:
        return "B", "finder_agreement"
    if candidate.verification in NEEDS_VERIFY and v == "valid":
        if name_ok:
            return "B", "verified_by_verifier"
        return "C", "name_mismatch"
    if candidate.verification == "valid":
        return "C", ("name_mismatch" if not name_ok else "low_confidence")
    if not name_ok:
        return "C", "name_mismatch"
    final = v if v in ("accept_all", "unknown") else candidate.verification
    if final == "accept_all":
        return "C", "accept_all_no_evidence"
    return "C", "unknown_no_evidence"


def needs_verifier(candidate: EnrichResult, g: str, name_ok: bool) -> bool:
    """A C whose only weakness is the mailbox status (accept_all, unknown, none) goes to the verifier."""
    return g == "C" and candidate.verification in NEEDS_VERIFY and name_ok


# ---------------------------------------------------------------- database reads
def company_domains(conn, company_id: int | None) -> set:
    """Every dom: alias of the resolved company (its group), plus companies.domain."""
    if company_id is None:
        return set()
    from .. import companies
    try:
        ids = companies.group(conn, company_id)
    except Exception:
        ids = [company_id]
    q = ",".join("?" * len(ids))
    out = set()
    for (d,) in conn.execute("SELECT domain FROM companies WHERE id IN (%s) AND domain IS NOT NULL" % q, ids):
        reg = keys_mod.registrable_domain(d)
        if reg:
            out.add(reg)
    for (k,) in conn.execute("SELECT alias_key FROM company_aliases WHERE company_id IN (%s) AND alias_key LIKE 'dom:%%'"
                             % q, ids):
        out.add(k[4:])
    return out


def suppression(conn, email: str, company_id: int | None) -> str | None:
    """'excluded' when an exclusion hits the address or its domain, 'no_guess_domain' when the domain is on
    the no-guessing list; None otherwise."""
    from .. import exclusions
    from . import deps
    domain = email.rsplit("@", 1)[1]
    try:
        if exclusions.match(conn, email=email, domain=domain):
            return "excluded"
    except Exception:
        return "excluded"   # fail closed
    reg = keys_mod.registrable_domain(domain) or domain
    if deps.guess_blocked(conn, reg) or (reg != domain and deps.guess_blocked(conn, domain)):
        return "no_guess_domain"
    return None


def known_invalid(conn, address: str | None) -> bool:
    """Whether any provider call judged this address invalid: a finder's invalid status, or a verifier's
    invalid verdict (invalid, spamtrap, abuse, do_not_mail, blacklisted), which marks the candidate call X.
    Such an address is X for good (5.2): a later finder that returns it valid does not make it sendable."""
    a = (address or "").strip().lower()
    if not a:
        return False
    return conn.execute("SELECT 1 FROM enrich_calls WHERE email = ? AND (grade_hint = 'X' OR outcome = 'invalid' "
                        "OR verification = 'invalid') LIMIT 1", (a,)).fetchone() is not None


def lookup(conn, address: str) -> dict | None:
    """(hook, for U6 verify_address) {grade, provider, call_id, bounced} of the newest provider result for
    this address, or None. The grade is X when any call ever judged the address invalid."""
    a = (address or "").strip().lower()
    if not a:
        return None
    row = conn.execute("SELECT c.id, c.provider, c.grade_hint, c.bounced_at, r.grade AS req_grade, "
                       "r.result_call_id FROM enrich_calls c LEFT JOIN enrich_requests r ON r.id = c.request_id "
                       "WHERE c.email = ? ORDER BY c.id DESC LIMIT 1", (a,)).fetchone()
    if row is None:
        return None
    g = row["req_grade"] if row["result_call_id"] == row["id"] and row["req_grade"] else row["grade_hint"]
    if g == "rejected" or known_invalid(conn, a):
        g = "X"
    return {"grade": g, "provider": row["provider"], "call_id": row["id"], "bounced": row["bounced_at"] is not None}
