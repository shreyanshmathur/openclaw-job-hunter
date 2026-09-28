"""Who may be looked up: target, privacy, exclusion and suppression checks before any call (U10,
ENRICH-SPEC section 6). There is no "look up anyone": only a person named on the hiring team of an
eligible or applied job, in a target role, who passes every exclusion, suppression and dedup rule.

`check` raises Denied with the code of the first failing rule. The volume rules (day, cycle and backlog
caps) are separate (`check_volume`), because a cached answer or a resumed request costs no new lookup.
"""
from __future__ import annotations

import re

from .. import keys as keys_mod
from .. import people
from ..canon import now, parse_ts, ts_add, utcnow
from ..errors import Denied
from . import deps, settings

TARGET_JOB_STATUSES = ("eligible", "apply_queued", "awaiting_approval", "applying", "applied")
LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")
OPEN_DRAFT = ("drafted", "lint_failed", "review_pending", "review_failed", "qc_passed", "awaiting_approval", "approved")
HONORIFICS = keys_mod.HONORIFICS
TARGET_RE = re.compile(r"^contact:(P[A-Z2-7]{7})$")


def _q(n: int) -> str:
    return ",".join("?" * n)


def name_parts(full_name: str | None, first_name: str | None) -> tuple:
    """(first, last, full) with honorifics removed and whitespace trimmed (NFKC)."""
    import unicodedata
    full = unicodedata.normalize("NFKC", full_name or "").strip()
    toks = [t for t in re.split(r"\s+", full) if t]
    toks = [t for t in toks if keys_mod.fold(t).strip(".") not in HONORIFICS]
    first = unicodedata.normalize("NFKC", first_name or "").strip()
    if not first or keys_mod.fold(first).strip(".") in HONORIFICS:
        first = toks[0] if toks else ""
    last = toks[-1] if len(toks) >= 2 else ""
    if first and last and keys_mod.fold(first) == keys_mod.fold(last):
        last = ""
    full_clean = " ".join(toks) if toks else (first or "")
    return (first or None), (last or None), (full_clean or None)


def check(conn, contact_id: int, *, target: str | None, cycle_id: str | None, s: dict | None = None) -> dict:
    """Every rule of the section 6 table except the volume caps. Returns the facts the chain needs:
    {contact_id, contact_uid, company_id, domain, first, last, full, li_slug, company_name}."""
    s = s if s is not None else settings.load(conn)
    cid = people.survivor(conn, contact_id)
    c = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    if c is None:
        raise Denied("E_NOT_FOUND", "no such contact")
    pids = people.group(conn, cid)
    company_id = c["company_id"]
    cids: list = []
    if company_id is not None:
        from .. import companies
        company_id = companies.survivor(conn, company_id)
        cids = companies.group(conn, company_id)
    # selected outreach target: on the hiring team of an eligible or applied job of this company
    linked = False
    if cids:
        linked = conn.execute(
            "SELECT 1 FROM job_hiring_team h JOIN jobs j ON j.id = h.job_id WHERE h.contact_id IN (%s) AND "
            "j.status IN (%s) AND j.company_id IN (%s) LIMIT 1" % (_q(len(pids)), _q(len(TARGET_JOB_STATUSES)),
                                                                   _q(len(cids))),
            list(pids) + list(TARGET_JOB_STATUSES) + list(cids)).fetchone() is not None
    if not linked:
        raise Denied("E_NOT_TARGET", "this contact is not on the hiring team of an eligible or applied job",
                     data={"reason": "no_eligible_job"})
    if c["role_type"] not in s["_targets"]:
        raise Denied("E_NOT_TARGET", "the contact's role is not an outreach target", data={"reason": "role"})
    if target is not None:
        m = TARGET_RE.match(target or "")
        if not m or m.group(1) not in {r[0] for r in conn.execute(
                "SELECT contact_uid FROM contacts WHERE id IN (%s)" % _q(len(pids)), pids)}:
            raise Denied("E_USAGE", "--target must be contact:<this contact's id>")
    # not suppressed
    if conn.execute("SELECT 1 FROM contacts WHERE id IN (%s) AND do_not_contact = 1" % _q(len(pids)),
                    pids).fetchone():
        raise Denied("E_CONTACT_DNC", "the person is marked do not contact")
    comp = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    domain = keys_mod.company_domain(comp["domain"]) if comp is not None and comp["domain"] else None
    from .. import exclusions
    ex = exclusions.match(conn, company_id=company_id, contact_id=cid, domain=domain) if domain else \
        exclusions.match(conn, company_id=company_id, contact_id=cid)
    if ex:
        raise Denied("E_EXCLUDED", "an exclusion matches this person or company", data={"type": ex[0]["type"]})
    if comp is not None and comp["contact_state"] in ("active_thread", "do_not_contact"):
        raise Denied("E_COMPANY_BLOCKED", "the company is %s" % comp["contact_state"])
    # worth sending (the rules behind `dedup check`, plus open drafts)
    res = deps.dedup_check(conn, "cold_email", contact_id=cid, company_id=company_id)
    if not res.get("allowed", False):
        hits = res.get("hits") or [{"code": "E_DUP_PERSON", "detail": "dedup refused"}]
        h = hits[0]
        raise Denied(h.get("code") or "E_DUP_PERSON", h.get("detail") or "dedup refused", data={"hits": hits})
    if conn.execute("SELECT 1 FROM drafts WHERE ((contact_id IN (%s) AND kind IN ('cold_email','li_invite_note','inmail')) "
                    "OR (company_id IN (%s) AND kind IN ('cold_email','application_email'))) AND status IN (%s) LIMIT 1"
                    % (_q(len(pids)), _q(len(cids)), _q(len(OPEN_DRAFT))),
                    list(pids) + list(cids) + list(OPEN_DRAFT)).fetchone():
        raise Denied("E_DUP_DRAFT", "an open first-touch draft exists for this person or company")
    # inputs
    if not domain:
        raise Denied("E_PRECONDITION", "the company has no usable domain", data={"reason": "no_company_domain"})
    first, last, full = name_parts(c["full_name"], c["first_name"])
    li_slug = str(c["li_slug"]).strip().lower() if c["li_slug"] else None
    li_ok = bool(s.get("use_linkedin_identifier")) and bool(li_slug) and not li_slug.startswith("acoa")
    if not (first and last) and not li_ok:
        raise Denied("E_PRECONDITION", "first and last name are needed", data={"reason": "no_name"})
    if deps.guess_blocked(conn, domain):
        raise Denied("E_ENRICH_UNAVAILABLE", "the domain is on the no-guessing list after a bounce",
                     data={"reason": "no_guess_domain"})
    loc = (c["locale"] or "").strip().upper()[:2]
    if loc and loc in s.get("skip_locales", []):
        raise Denied("E_ENRICH_UNAVAILABLE", "lookups are off for this locale", data={"reason": "locale_skipped"})
    return {"contact_id": cid, "contact_uid": c["contact_uid"], "company_id": company_id, "domain": domain,
            "first": first, "last": last, "full": full, "li_slug": li_slug if li_ok else None,
            "company_name": comp["display_name"] if comp is not None else None}


def lookups_since(conn, since: str, cycle_id: str | None = None, exclude_request: int | None = None) -> list:
    sql = ("SELECT r.id, min(c.started_at) AS first_call FROM enrich_requests r JOIN enrich_calls c ON "
           "c.request_id = r.id WHERE c.op <> 'account' AND c.started_at > ?")
    args: list = [since]
    if cycle_id is not None:
        sql += " AND r.cycle_id = ?"
        args.append(cycle_id)
    if exclude_request is not None:
        sql += " AND r.id <> ?"
        args.append(exclude_request)
    sql += " GROUP BY r.id ORDER BY first_call"
    return [dict(r) for r in conn.execute(sql, args)]


def unsent_found(conn) -> int:
    """Provider-found addresses on contacts with no live email action yet."""
    return conn.execute(
        "SELECT count(*) FROM contacts c WHERE c.email_source = 'provider' AND c.merged_into IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM actions a WHERE a.contact_id = c.id AND a.kind IN ('cold_email','application_email') "
        "AND a.status IN (%s))" % _q(len(LIVE)), LIVE).fetchone()[0]


def check_volume(conn, s: dict, cycle_id: str | None, exclude_request: int | None = None) -> None:
    """Day, cycle and backlog caps (only before a new lookup). Denied(E_ENRICH_UNAVAILABLE, reason)."""
    at = now()
    day = lookups_since(conn, ts_add(at, days=-1), exclude_request=exclude_request)
    if len(day) >= int(s["max_lookups_per_day"]):
        oldest = day[0]["first_call"]
        retry = max(1, int((parse_ts(ts_add(oldest, days=1)) - utcnow()).total_seconds()) + 1)
        raise Denied("E_ENRICH_UNAVAILABLE", "the daily lookup cap is reached", retry_after=retry,
                     data={"reason": "day_cap"})
    if cycle_id:
        cyc = lookups_since(conn, "0000", cycle_id=cycle_id, exclude_request=exclude_request)
        if len(cyc) >= int(s["max_lookups_per_cycle"]):
            raise Denied("E_ENRICH_UNAVAILABLE", "the lookup cap for this cycle is reached",
                         data={"reason": "cycle_cap"})
    if unsent_found(conn) >= int(s["max_unsent_found"]):
        raise Denied("E_ENRICH_UNAVAILABLE", "too many found addresses are still unused",
                     data={"reason": "unsent_backlog"})
