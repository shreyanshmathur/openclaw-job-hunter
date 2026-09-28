"""Contact files (`contact add --file`, design 12.8 and 2.4.3).

The agent reports one person; code validates the file, resolves the company (companies.resolve) and the
person (people.resolve, U1) from every identity key, so an opaque LinkedIn member URL and the vanity URL of
the same profile, a second address or a Gmail dot variant all land on one contact row. Exclusions are
applied to the stored contact (do_not_contact), and the person is linked to the job whose posting named
them (job_hiring_team) when `source_url` is that posting. An address the agent reports records where it came
from (`email_source`): `published` for grade A, `pattern` for grade B; the file can never set `provider`.

set_address (hook-safe, ENRICH-SPEC 11.1) is how the email finder (U10) and the pattern step write an address
to an existing contact: it never replaces an A or B address, resolves the new address keys through
people.resolve (another contact holding the address merges in), applies email and domain exclusions and
stores the grade, evidence URL and provenance. The database trigger t_contact_provider_email checks the shape.

Runs inside the caller's transaction; never commits.
"""
from __future__ import annotations

import re

from . import canon
from .errors import Denied
from .events import log_event
from .threads import LIVE_SQL, dep

CONTACT_KEYS = {"full_name", "first_name", "title", "company", "company_domain", "role_type", "locale", "email",
                "email_grade", "email_evidence_url", "linkedin_url", "linkedin_member_url", "source_url"}
ROLE_TYPES = ("hiring_manager", "recruiter", "founder", "employee", "role_inbox", "other")
RELATION_BY_ROLE = {"hiring_manager": "hiring_manager", "recruiter": "recruiter", "founder": "founder"}
ROLE_INBOX_LOCALS = frozenset({"careers", "career", "jobs", "job", "hr", "hiring", "recruitment", "recruiting",
                               "recruiter", "talent", "apply", "applications", "resume", "resumes", "cv", "people",
                               "team", "info", "hello", "contact", "work", "joinus", "join"})
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
VANITY_RE = re.compile(r"^https://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#\s]+)/?(?:[?#].*)?$", re.I)
MEMBER_RE = re.compile(r"^https://(?:[a-z]{2,3}\.)?linkedin\.com/(?:in/ACoA[A-Za-z0-9_-]+|pub/[^?#\s]+|"
                       r"sales/(?:lead|people)/[^?#\s]+)/?(?:[?#].*)?$", re.I)
LOCALE_RE = re.compile(r"^[A-Za-z]{2}(?:[-_][A-Za-z]{2})?$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _text(data: dict, key: str, max_len: int, required: bool = False) -> str | None:
    v = data.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise Denied("E_SCHEMA", "%s is required" % key)
        return None
    if not isinstance(v, str):
        raise Denied("E_SCHEMA", "%s must be a string" % key)
    v = " ".join(canon.normalize_text(v).split())
    if len(v) > max_len or _CTRL_RE.search(v):
        raise Denied("E_VALIDATION", "%s is too long or has control characters" % key)
    return v


def _https(data: dict, key: str) -> str | None:
    v = _text(data, key, 2000)
    if v is not None and not v.lower().startswith("https://"):
        raise Denied("E_VALIDATION", "%s must be an https URL" % key)
    if v is not None and "lnkd.in" in v.lower():
        raise Denied("E_VALIDATION", "lnkd.in short links are refused: open it and use the profile URL")
    return v


def validate(data: dict) -> dict:
    """Check a contact file (12.8) and return the normalized fields. Denied E_SCHEMA or E_VALIDATION."""
    if not isinstance(data, dict):
        raise Denied("E_SCHEMA", "contact file must be a JSON object")
    extra = set(data) - CONTACT_KEYS
    if extra:
        raise Denied("E_SCHEMA", "unknown keys in contact file", data={"unknown": sorted(extra)})
    out = {
        "full_name": _text(data, "full_name", 120), "first_name": _text(data, "first_name", 60),
        "title": _text(data, "title", 200), "company": _text(data, "company", 200),
        "company_domain": _text(data, "company_domain", 253), "locale": _text(data, "locale", 10),
        "email": _text(data, "email", 254), "email_grade": _text(data, "email_grade", 1),
        "email_evidence_url": _https(data, "email_evidence_url"), "linkedin_url": _https(data, "linkedin_url"),
        "linkedin_member_url": _https(data, "linkedin_member_url"), "source_url": _https(data, "source_url"),
        "role_type": _text(data, "role_type", 20) or "other",
    }
    if out["role_type"] not in ROLE_TYPES:
        raise Denied("E_VALIDATION", "role_type must be one of %s" % ", ".join(ROLE_TYPES))
    if out["locale"] is not None and not LOCALE_RE.match(out["locale"]):
        raise Denied("E_VALIDATION", "locale must look like IN or en-IN")
    if out["email"] is not None:
        out["email"] = out["email"].lower()
        if not EMAIL_RE.match(out["email"]):
            raise Denied("E_VALIDATION", "email is not a valid address")
        local = out["email"].split("@", 1)[0].split("+", 1)[0]
        if local in ROLE_INBOX_LOCALS and out["role_type"] != "role_inbox":
            raise Denied("E_VALIDATION", "%s@ is a role inbox: set role_type to role_inbox" % local)
    if out["email_grade"] is not None:
        if out["email_grade"] not in ("A", "B", "C"):
            raise Denied("E_VALIDATION", "email_grade must be A, B or C")
        if out["email"] is None:
            raise Denied("E_VALIDATION", "email_grade needs an email")
    if out["email_evidence_url"] is not None and out["email"] is None:
        raise Denied("E_VALIDATION", "email_evidence_url needs an email")
    if out["linkedin_url"] is not None:
        m = VANITY_RE.match(out["linkedin_url"])
        if not m or MEMBER_RE.match(out["linkedin_url"]):
            raise Denied("E_VALIDATION", "linkedin_url must be the vanity /in/<slug> URL read from the profile page; "
                         "put an opaque /in/ACoA..., /pub/ or /sales/ URL in linkedin_member_url")
    if out["linkedin_member_url"] is not None and not MEMBER_RE.match(out["linkedin_member_url"]):
        raise Denied("E_VALIDATION", "linkedin_member_url must be an opaque /in/ACoA..., /pub/ or /sales/lead/ URL")
    if not (out["email"] or out["linkedin_url"] or out["linkedin_member_url"]):
        raise Denied("E_VALIDATION", "a contact needs an email, linkedin_url or linkedin_member_url")
    if out["role_type"] != "role_inbox" and not out["full_name"]:
        raise Denied("E_VALIDATION", "full_name is required for a person")
    if out["first_name"] is None and out["full_name"] and out["role_type"] != "role_inbox":
        out["first_name"] = out["full_name"].split()[0]
    return out


def vanity_slug(url: str | None) -> str | None:
    if not url:
        return None
    m = VANITY_RE.match(url)
    if not m or MEMBER_RE.match(url):
        return None
    from urllib.parse import unquote
    return unquote(m.group(1)).strip().lower() or None


def _find_job(conn, url: str | None):
    if not url:
        return None
    row = conn.execute("SELECT id FROM jobs WHERE source_url = ? OR apply_url = ? ORDER BY id LIMIT 1",
                       (url, url)).fetchone()
    if row is not None:
        return row[0]
    try:
        key, aliases = dep("keys").job_key(url, {}, "")
    except (ImportError, NotImplementedError, AttributeError, Denied, ValueError):
        return None
    for k in [key] + list(aliases or []):
        row = conn.execute("SELECT job_id FROM job_keys WHERE key = ?", (k,)).fetchone()
        if row is not None:
            return row[0]
    return None


def _surviving(conn, contact_id: int) -> int:
    seen = set()
    while contact_id not in seen:
        seen.add(contact_id)
        row = conn.execute("SELECT merged_into FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None or row[0] is None:
            return contact_id
        contact_id = row[0]
    return contact_id


def _exclusion_hits(conn, f: dict, contact_id: int, company_id: int | None) -> list:
    """exclusions.match (U1) over every identity of the stored person and the company."""
    try:
        excl = dep("exclusions")
    except ImportError:
        return []
    targets = {"contact_id": contact_id}
    if f["email"]:
        targets["email"] = f["email"]
    if f["linkedin_url"] or f["linkedin_member_url"]:
        targets["linkedin_url"] = f["linkedin_url"] or f["linkedin_member_url"]
    if company_id:
        targets["company_id"] = company_id
    try:
        return list(excl.match(conn, **targets) or [])
    except NotImplementedError:
        return []


def _attach_email_domain(conn, company_id: int, name: str | None, company_domain: str, email: str) -> int:
    """Give the company the keys of the contact's email domain too (2.4.2: a recipient address is company
    evidence) when it differs from `company_domain`, so a colleague known only by an address at that domain
    resolves to the same company and the company rules see both. Anchored on the company name (the file's, or
    the stored display name when it is one of the company's keys), so it never creates a second company; a
    company holding the email domain is merged in unless a human marked the pair distinct. Returns the
    surviving company id."""
    keys_mod = dep("keys")
    companies = dep("companies")
    try:
        email_dom = keys_mod.company_domain(email)
        given = keys_mod.company_domain(company_domain)
    except (NotImplementedError, AttributeError):
        return company_id
    if not email_dom or email_dom == given:
        return company_id   # free-mail or hosting address, or the same domain: nothing to add
    company_id = companies.survivor(conn, company_id) or company_id
    anchor = name
    if not anchor:
        row = conn.execute("SELECT display_name FROM companies WHERE id = ?", (company_id,)).fetchone()
        anchor = row[0] if row else None
    name_keys = [k for k, _kind in keys_mod.company_keys(name=anchor)] if anchor else []
    anchored = False
    for k in name_keys:
        r = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", (k,)).fetchone()
        if r is not None and companies.survivor(conn, r[0]) == company_id:
            anchored = True
            break
    if not anchored:
        return company_id   # a domain-only company: no key ties the email domain to it without a new row
    held = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", ("dom:" + email_dom,)).fetchone()
    other = companies.survivor(conn, held[0]) if held else None
    if other is not None and other != company_id and conn.execute(
            "SELECT 1 FROM company_distinct WHERE a_id = ? AND b_id = ?",
            (min(other, company_id), max(other, company_id))).fetchone():
        return company_id   # a human said these are different companies
    try:
        return companies.resolve(conn, name=anchor, domain=email_dom, source="email", create=True)
    except Denied as exc:
        if exc.code != "E_COMPANY_AMBIGUOUS":
            raise
        return company_id   # the confirm_company_merge task is already queued; keep the named company


def add_from_file(conn, data: dict) -> dict:
    """Store one contact (12.8). Returns {contact_uid, merged_with, existing, do_not_contact, needs_vanity,
    company_uid, already_contacted, job_linked}. Runs in the caller's transaction."""
    f = validate(data)
    keys_mod = dep("keys")
    company_id = None
    company_uid = None
    domain = f["company_domain"]
    if domain is None and f["email"]:
        try:
            domain = keys_mod.registrable_domain(f["email"])
        except (NotImplementedError, AttributeError):
            domain = None
    if f["company"] or domain:
        company_id = dep("companies").resolve(conn, name=f["company"], domain=domain,
                                              source="email" if f["email"] else "careers_url", create=True)
        if f["company_domain"] and f["email"]:
            company_id = _attach_email_domain(conn, company_id, f["company"], f["company_domain"], f["email"])
        row = conn.execute("SELECT company_uid FROM companies WHERE id = ?", (company_id,)).fetchone()
        company_uid = row[0] if row else None
    keys: list = []
    keys += keys_mod.person_keys(email=f["email"], linkedin_url=f["linkedin_url"], full_name=f["full_name"],
                                 company_uid=company_uid)
    if f["linkedin_member_url"]:
        keys += keys_mod.person_keys(linkedin_url=f["linkedin_member_url"])
    seen, uniq = set(), []
    for k in keys:
        if k[0] not in seen:
            seen.add(k[0])
            uniq.append(tuple(k))
    before = {}
    if uniq:
        q = "SELECT DISTINCT k.contact_id, c.contact_uid FROM contact_keys k JOIN contacts c ON c.id = k.contact_id " \
            "WHERE k.key IN (%s)" % ",".join("?" for _ in uniq)
        before = {r[0]: r[1] for r in conn.execute(q, [k[0] for k in uniq])}
    slug = vanity_slug(f["linkedin_url"])
    member_only = bool(f["linkedin_member_url"]) and not slug
    fields = {"full_name": f["full_name"], "first_name": f["first_name"], "title": f["title"],
              "company_id": company_id, "role_type": f["role_type"], "locale": f["locale"], "email": f["email"],
              "email_grade": f["email_grade"], "email_evidence_url": f["email_evidence_url"],
              "linkedin_url": f["linkedin_url"], "li_slug": slug, "needs_vanity": 1 if member_only else 0}
    contact_id = _surviving(conn, dep("people").resolve(conn, keys=uniq, fields=fields, create=True))
    stamp = canon.now()
    # fill what the stored row does not know yet (never overwrite a known value)
    conn.execute(
        "UPDATE contacts SET full_name = COALESCE(full_name, ?), first_name = COALESCE(first_name, ?), "
        "title = COALESCE(title, ?), company_id = COALESCE(company_id, ?), locale = COALESCE(locale, ?), "
        "email = COALESCE(email, ?), email_grade = COALESCE(email_grade, ?), "
        "email_evidence_url = COALESCE(email_evidence_url, ?), linkedin_url = COALESCE(linkedin_url, ?), "
        "li_slug = COALESCE(li_slug, ?), role_type = CASE WHEN role_type = 'other' THEN ? ELSE role_type END, "
        "updated_at = ? WHERE id = ?",
        (f["full_name"], f["first_name"], f["title"], company_id, f["locale"], f["email"], f["email_grade"],
         f["email_evidence_url"], f["linkedin_url"], slug, f["role_type"], stamp, contact_id))
    has_opaque = conn.execute("SELECT 1 FROM contact_keys WHERE contact_id = ? AND kind IN "
                              "('li_member','li_legacy','li_sales') LIMIT 1", (contact_id,)).fetchone() is not None
    conn.execute("UPDATE contacts SET needs_vanity = CASE WHEN li_slug IS NOT NULL THEN 0 WHEN ? THEN 1 "
                 "ELSE needs_vanity END WHERE id = ?", (1 if has_opaque else 0, contact_id))
    source = {"A": "published", "B": "pattern"}.get(f["email_grade"] or "")
    if source and f["email"]:
        conn.execute("UPDATE contacts SET email_source = ? WHERE id = ? AND email_source IS NULL AND lower(email) = ? "
                     "AND email_grade = ?", (source, contact_id, f["email"], f["email_grade"]))
    hits = _exclusion_hits(conn, f, contact_id, company_id)
    if hits:
        first = hits[0]
        reason = "excluded:%s" % (first.get("type") if isinstance(first, dict) else "match")
        conn.execute("UPDATE contacts SET do_not_contact = 1, dnc_reason = COALESCE(dnc_reason, ?) WHERE id = ?",
                     (reason, contact_id))
    job_linked = None
    job_id = _find_job(conn, f["source_url"])
    rel = RELATION_BY_ROLE.get(f["role_type"])
    if job_id is not None and rel:
        conn.execute("INSERT OR IGNORE INTO job_hiring_team (job_id, contact_id, relation) VALUES (?, ?, ?)",
                     (job_id, contact_id, rel))
        job_linked = conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    row = conn.execute("SELECT c.*, co.company_uid, co.contact_state FROM contacts c "
                       "LEFT JOIN companies co ON co.id = c.company_id WHERE c.id = ?", (contact_id,)).fetchone()
    already = conn.execute(
        "SELECT 1 FROM actions WHERE contact_id = ? AND first_touch = 1 AND status IN %s LIMIT 1" % LIVE_SQL,
        (contact_id,)).fetchone() is not None
    accepted = conn.execute("SELECT 1 FROM threads WHERE contact_id = ? AND state = 'invite_accepted' LIMIT 1",
                            (contact_id,)).fetchone() is not None
    merged_with = sorted(uid for cid, uid in before.items() if cid != contact_id)
    log_event(conn, "contact_added", contact_uid=row["contact_uid"], existing=bool(before), merged_with=merged_with)
    return {"contact_uid": row["contact_uid"], "merged_with": merged_with, "existing": bool(before),
            "do_not_contact": bool(row["do_not_contact"]), "needs_vanity": bool(row["needs_vanity"]),
            "company_uid": row["company_uid"], "company_blocked": row["contact_state"] == "do_not_contact",
            "already_contacted": already and not accepted, "job_linked": job_linked}


EMAIL_SOURCES = ("published", "pattern", "provider", "human")


def set_address(conn, contact_id: int, *, email: str, grade: str, source: str, evidence_url: str | None,
                enrich_call_id: int | None = None) -> dict:
    """Write an address found for an existing contact (ENRICH-SPEC 11.1). Hook-safe: runs in the caller's
    transaction, never commits, no network. Returns {contact_id, contact_uid, email, grade, source,
    do_not_contact, merged_with}. Denied: E_VALIDATION, E_NOT_FOUND, E_ADDRESS_GRADE (an A or B address is
    already stored, or this address bounced before)."""
    addr = (email or "").strip().lower()
    if not EMAIL_RE.match(addr):
        raise Denied("E_VALIDATION", "not a valid email address")
    if grade not in ("A", "B", "C"):
        raise Denied("E_VALIDATION", "grade must be A, B or C")
    if source not in EMAIL_SOURCES:
        raise Denied("E_VALIDATION", "source must be one of %s" % ", ".join(EMAIL_SOURCES))
    if evidence_url is not None and not (isinstance(evidence_url, str) and evidence_url.startswith("https://")):
        raise Denied("E_VALIDATION", "evidence_url must be an https URL")
    if source == "provider" and (enrich_call_id is None or grade not in ("B", "C")):
        raise Denied("E_ADDRESS_GRADE", "a provider address needs its call id and grade B or C")
    cid = _surviving(conn, int(contact_id))
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no contact %s" % contact_id)
    old = (row["email"] or "").lower()
    if old and old != addr and row["email_grade"] in ("A", "B") and not row["email_invalid"]:
        raise Denied("E_ADDRESS_GRADE", "the contact already has a grade %s address" % row["email_grade"])
    if old == addr and row["email_invalid"]:
        raise Denied("E_ADDRESS_GRADE", "this address bounced before")
    if conn.execute("SELECT 1 FROM contacts WHERE lower(email) = ? AND email_invalid = 1 LIMIT 1",
                    (addr,)).fetchone() is not None:
        raise Denied("E_ADDRESS_GRADE", "this address bounced before")
    before_uid = row["contact_uid"]
    company_uid = None
    if row["company_id"]:
        cu = conn.execute("SELECT company_uid FROM companies WHERE id = ?", (row["company_id"],)).fetchone()
        company_uid = cu[0] if cu else None
    new_keys = [tuple(k) for k in dep("keys").person_keys(email=addr, company_uid=company_uid)
                if str(k[0]).startswith(("email:", "email_norm:"))]
    own = [(r[0], r[1]) for r in conn.execute("SELECT key, kind FROM contact_keys WHERE contact_id = ?", (cid,))]
    held = set()
    for k, _kind in new_keys:
        r = conn.execute("SELECT contact_id FROM contact_keys WHERE key = ?", (k,)).fetchone()
        if r is not None:
            held.add(_surviving(conn, r[0]))
    merged_with = []
    if own and (held - {cid}):
        others = sorted(held - {cid})
        merged_with = [r[0] for r in conn.execute("SELECT contact_uid FROM contacts WHERE id IN (%s)"
                                                  % ",".join("?" * len(others)), others)]
        cid = _surviving(conn, dep("people").resolve(conn, keys=own + new_keys, fields={}, create=False) or cid)
    elif not held:
        stamp = canon.now()
        for k, kind in new_keys:
            conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (key) DO NOTHING", (k, cid, kind, stamp))
    elif held != {cid}:
        raise Denied("E_ADDRESS_GRADE", "the address belongs to another contact")
    stamp = canon.now()
    changed = old != addr
    conn.execute("UPDATE contacts SET email = ?, email_grade = ?, email_evidence_url = ?, email_source = ?, "
                 "email_enrich_call_id = ?, email_invalid = 0, "
                 "email_mx_ok = CASE WHEN ? THEN NULL ELSE email_mx_ok END, updated_at = ? WHERE id = ?",
                 (addr, grade, evidence_url, source, enrich_call_id, 1 if changed else 0, stamp, cid))
    dnc = False
    try:
        hits = list(dep("exclusions").match(conn, email=addr, domain=addr.rsplit("@", 1)[1], contact_id=cid) or [])
    except (ImportError, NotImplementedError, AttributeError):
        hits = []
    if hits:
        first = hits[0]
        conn.execute("UPDATE contacts SET do_not_contact = 1, dnc_reason = COALESCE(dnc_reason, ?) WHERE id = ?",
                     ("excluded:%s" % (first.get("type") if isinstance(first, dict) else "match"), cid))
    r = conn.execute("SELECT contact_uid, do_not_contact FROM contacts WHERE id = ?", (cid,)).fetchone()
    dnc = bool(r["do_not_contact"])
    log_event(conn, "contact_address_set", contact_uid=r["contact_uid"], domain=addr.rsplit("@", 1)[1], grade=grade,
              source=source, merged_with=[u for u in merged_with if u != before_uid])
    return {"contact_id": cid, "contact_uid": r["contact_uid"], "email": addr, "grade": grade, "source": source,
            "do_not_contact": dnc, "merged_with": merged_with}


def show(conn, contact_uid: str) -> dict:
    row = conn.execute("SELECT c.*, co.company_uid, co.display_name AS company_name, co.contact_state FROM contacts c "
                       "LEFT JOIN companies co ON co.id = c.company_id WHERE c.contact_uid = ?",
                       (contact_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no contact %s" % contact_uid)
    keys = [{"key": r[0], "kind": r[1]} for r in
            conn.execute("SELECT key, kind FROM contact_keys WHERE contact_id = ? ORDER BY kind, key", (row["id"],))]
    actions = [dict(r) for r in conn.execute(
        "SELECT token, kind, status, platform, reserved_at, sent_at FROM actions WHERE contact_id = ? ORDER BY id",
        (row["id"],))]
    threads = [dict(r) for r in conn.execute(
        "SELECT thread_key, channel, state, reply_class, followup_due_at FROM threads WHERE contact_id = ?",
        (row["id"],))]
    merged = conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (row["merged_into"],)).fetchone() \
        if row["merged_into"] else None
    out = {k: row[k] for k in ("contact_uid", "full_name", "first_name", "title", "role_type", "locale", "email",
                               "email_grade", "email_source", "email_mx_ok", "email_invalid", "linkedin_url", "li_slug",
                               "needs_vanity", "do_not_contact", "dnc_reason", "company_uid", "company_name",
                               "contact_state")}
    out.update({"merged_into": merged[0] if merged else None, "keys": keys, "actions": actions, "threads": threads})
    return out
