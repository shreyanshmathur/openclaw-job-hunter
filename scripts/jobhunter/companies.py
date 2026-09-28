"""Company identity resolution and merges (design 2.4.2, B3). Hook-safe: runs inside the caller's
transaction, never commits, no network.

resolve(): every key of the input (alphanumeric name, loose name, registrable domain and its label, ATS
tenant) is looked up in company_aliases; merged rows are followed to their survivor. No match creates a
company; one match adds the missing keys; several matches merge into the lowest id in the same
transaction (unless a pair is recorded in company_distinct, then E_COMPANY_AMBIGUOUS and a
confirm_company_merge task). So "Kestrel Labs", "KestrelLabs Pvt Ltd" and kestrellabs.com are one row
and every trigger sees a single company_id.
"""
from __future__ import annotations

import json

from . import db, jobstate, keys
from .canon import new_uid, now
from .errors import Denied
from .events import log_event, open_human_task

EXACT_KINDS = frozenset(("name", "dom", "ats"))
STATE_RANK = {"none": 0, "contacted": 1, "active_thread": 2, "do_not_contact": 3}
OPEN_DRAFT = ("drafted", "lint_failed", "review_pending", "review_failed", "qc_passed", "awaiting_approval",
              "approved")
ALIAS_SOURCES = ("ats", "email", "careers_url", "job_board", "private_aliases", "human", "exclusions", "merge")


def survivor(conn, company_id: int | None) -> int | None:
    """Follow merged_into to the surviving row."""
    seen = set()
    cid = company_id
    while cid is not None and cid not in seen:
        seen.add(cid)
        row = conn.execute("SELECT merged_into FROM companies WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no company with id %r" % cid)
        if row[0] is None:
            return cid
        cid = row[0]
    raise Denied("E_INTERNAL", "company merge chain loops at %r" % company_id)


def group(conn, company_id: int) -> list[int]:
    """The survivor and every row merged into it (directly or through other merged rows)."""
    top = survivor(conn, company_id)
    out, todo = [top], [top]
    while todo:
        cur = todo.pop()
        for (cid,) in conn.execute("SELECT id FROM companies WHERE merged_into = ?", (cur,)):
            if cid not in out:
                out.append(cid)
                todo.append(cid)
    return out


def by_uid(conn, uid: str):
    row = conn.execute("SELECT * FROM companies WHERE company_uid = ?", (uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no company %s" % uid)
    return row


def _insert_keys(conn, company_id: int, pairs, source: str) -> list[str]:
    added = []
    for key, kind in pairs:
        cur = conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) "
                           "VALUES (?, ?, ?, ?, ?) ON CONFLICT (alias_key) DO NOTHING",
                           (key, company_id, kind, source, now()))
        if cur.rowcount:
            added.append(key)
    return added


def resolve(conn, *, name=None, domain=None, ats=None, tenant=None, source: str, create: bool = True):
    """Company id for the given identity (see module doc). With create=False and no match: None."""
    return _resolve(conn, name=name, domain=domain, ats=ats, tenant=tenant, source=source, create=create)


def add_keys(conn, company_id: int, *, name=None, domain=None, ats=None, tenant=None, source: str) -> int:
    """Attach more identity keys to a known company (for example the email domain of a company known only by
    a name-less domain or ATS tenant), so it is never created a second time. Keys that already belong to
    another company merge the two by the resolve rules (lowest id survives; a pair marked distinct is
    E_COMPANY_AMBIGUOUS with a question for you). Returns the surviving company id. Hook-safe."""
    return _resolve(conn, name=name, domain=domain, ats=ats, tenant=tenant, source=source, create=False,
                    seed=survivor(conn, company_id))


def _resolve(conn, *, name, domain, ats, tenant, source: str, create: bool, seed: int | None = None):
    if source not in ALIAS_SOURCES:
        raise Denied("E_VALIDATION", "unknown alias source %r" % source)
    pairs = keys.company_keys(name=name, domain=domain, ats=ats, tenant=tenant)
    if not pairs:
        raise Denied("E_VALIDATION", "no usable company identity (name, domain or ATS tenant)",
                     data={"name": name, "domain": domain})
    found: dict[int, list[tuple[str, str, str]]] = {}
    if seed is not None:
        # the caller vouches for this company: it counts as an exact match
        found[seed] = [("", "seed", "name")]
    for key, kind in pairs:
        row = conn.execute("SELECT company_id, kind FROM company_aliases WHERE alias_key = ?", (key,)).fetchone()
        if row is None:
            continue
        cid = survivor(conn, row[0])
        found.setdefault(cid, []).append((key, kind, row[1]))
    if not found:
        if not create:
            return None
        ts = now()
        reg = keys.company_domain(domain) if domain else None
        display = (name or "").strip() or reg or (tenant or "").strip()
        agency = keys.is_agency_name(name)
        cur = conn.execute(
            "INSERT INTO companies (company_uid, display_name, domain, is_agency, agency_source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_uid("K"), display[:200], reg, 1 if agency else 0, "bundled_list" if agency else None, ts, ts))
        cid = cur.lastrowid
        _insert_keys(conn, cid, pairs, source)
        _record_tenant(conn, cid, ats, tenant, source)
        log_event(conn, "company_created", company_id=cid, keys=[k for k, _ in pairs])
        return cid
    ids = sorted(found)
    if len(ids) > 1:
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if conn.execute("SELECT 1 FROM company_distinct WHERE a_id = ? AND b_id = ?",
                                (min(a, b), max(a, b))).fetchone():
                    uids = [r[0] for r in conn.execute(
                        "SELECT company_uid FROM companies WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids)),
                        ids)]
                    question = "Are these one company? %s (input: %s)" % (", ".join(uids), name or domain or tenant)

                    def _task(c, question=question, first=ids[0]):
                        open_human_task(c, "confirm_company_merge", question, company_id=first)
                    db.defer_write(conn, _task)
                    raise Denied("E_COMPANY_AMBIGUOUS", "the input matches companies marked as distinct",
                                 data={"companies": uids})
        target = ids[0]
        for other in ids[1:]:
            matched = [k for k, _kind, _stored in found[other] if k]
            strength = "exact" if any(kind in EXACT_KINDS or stored in EXACT_KINDS
                                      for _k, kind, stored in found[other]) \
                and any(kind in EXACT_KINDS or stored in EXACT_KINDS for _k, kind, stored in found[target]) \
                else "fuzzy"
            merge(conn, other, target, by="system:resolve", strength=strength, matched_keys=matched)
    else:
        target = ids[0]
    _insert_keys(conn, target, pairs, source)
    _record_tenant(conn, target, ats, tenant, source)
    if name and keys.is_agency_name(name):
        conn.execute("UPDATE companies SET is_agency = 1, agency_source = 'bundled_list', updated_at = ? "
                     "WHERE id = ? AND agency_source IS NULL", (now(), target))
    if domain:
        reg = keys.company_domain(domain)
        if reg:
            conn.execute("UPDATE companies SET domain = ?, updated_at = ? WHERE id = ? AND domain IS NULL",
                         (reg, now(), target))
    return target


def _record_tenant(conn, cid: int, ats, tenant, source: str) -> None:
    """Record the ATS tenant as given: SmartRecruiters and Workday site ids are case-sensitive, and U2 polls the
    stored spelling, so the tenant is never folded. An existing row that differs only in case is reused (it
    gets the company when it has none) instead of adding a second spelling that would be polled twice."""
    if not (ats and tenant):
        return
    a = keys.fold(ats).strip()
    t = str(tenant).strip()
    if not a or not t:
        return
    row = conn.execute("SELECT tenant FROM ats_tenants WHERE ats = ? AND lower(tenant) = lower(?) "
                       "ORDER BY (tenant = ?) DESC, rowid LIMIT 1", (a, t, t)).fetchone()
    if row is not None:
        conn.execute("UPDATE ats_tenants SET company_id = COALESCE(company_id, ?) WHERE ats = ? AND tenant = ?",
                     (cid, a, row[0]))
        return
    conn.execute("INSERT INTO ats_tenants (ats, tenant, company_id, active, source) VALUES (?, ?, ?, 1, ?)",
                 (a, t, cid, source))


def _supersede_open_email_draft(conn, loser: int, winner: int) -> None:
    ph = ",".join("?" * len(OPEN_DRAFT))
    w = conn.execute("SELECT id FROM drafts WHERE company_id = ? AND kind IN ('cold_email','application_email') "
                     "AND status IN (%s)" % ph, (winner,) + OPEN_DRAFT).fetchone()
    if not w:
        return
    for (did,) in conn.execute("SELECT id FROM drafts WHERE company_id = ? AND kind IN ('cold_email',"
                               "'application_email') AND status IN (%s)" % ph, (loser,) + OPEN_DRAFT).fetchall():
        jobstate.set_draft_status(conn, did, "superseded", "company_merge", "system:merge")


def merge(conn, from_id: int, to_id: int, by: str, strength: str, matched_keys=None) -> None:
    """Merge company from_id into to_id (2.4.2 step 4). Never commits."""
    if strength not in ("exact", "fuzzy"):
        raise Denied("E_VALIDATION", "strength must be exact or fuzzy")
    from_id, to_id = survivor(conn, from_id), survivor(conn, to_id)
    if from_id == to_id:
        return
    a = conn.execute("SELECT * FROM companies WHERE id = ?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM companies WHERE id = ?", (to_id,)).fetchone()
    ts = now()
    _supersede_open_email_draft(conn, from_id, to_id)
    conn.execute("UPDATE company_aliases SET company_id = ? WHERE company_id = ?", (to_id, from_id))
    for table in ("jobs", "contacts", "drafts", "actions", "threads", "inbound_messages", "prechecks", "ats_tenants",
                  "human_tasks"):
        if table in ("jobs", "contacts", "drafts", "actions", "threads", "inbound_messages"):
            conn.execute("UPDATE %s SET company_id = ?, updated_at = ? WHERE company_id = ?" % table,
                         (to_id, ts, from_id))
        else:
            conn.execute("UPDATE %s SET company_id = ? WHERE company_id = ?" % table, (to_id, from_id))
    conn.execute("UPDATE research_facts SET subject_id = ? WHERE subject_kind = 'company' AND subject_id = ?",
                 (to_id, from_id))
    for (x,) in conn.execute("SELECT CASE WHEN a_id = ? THEN b_id ELSE a_id END FROM company_distinct "
                             "WHERE a_id = ? OR b_id = ?", (from_id, from_id, from_id)).fetchall():
        if x != to_id:
            conn.execute("INSERT INTO company_distinct (a_id, b_id, by, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT DO NOTHING", (min(x, to_id), max(x, to_id), by, ts))
    # pname keys carry the company uid: rewrite them and merge people who then collide
    from . import people
    suffix = "@" + a["company_uid"]
    for (key, cid) in conn.execute("SELECT key, contact_id FROM contact_keys WHERE kind = 'pname' AND key LIKE ?",
                                   ("pname:%" + suffix,)).fetchall():
        new_key = key[: -len(suffix)] + "@" + b["company_uid"]
        hit = conn.execute("SELECT contact_id FROM contact_keys WHERE key = ?", (new_key,)).fetchone()
        if hit is None:
            conn.execute("UPDATE contact_keys SET key = ? WHERE key = ?", (new_key, key))
        else:
            conn.execute("DELETE FROM contact_keys WHERE key = ?", (key,))
            keep, lose = sorted((people.survivor(conn, hit[0]), people.survivor(conn, cid)))
            if keep != lose:
                people.merge(conn, lose, keep, by=by, fuzzy=True)
    state = a["contact_state"] if STATE_RANK[a["contact_state"]] > STATE_RANK[b["contact_state"]] else b["contact_state"]
    reason = a["contact_state_reason"] if state == a["contact_state"] and state != b["contact_state"] \
        else b["contact_state_reason"]
    if "human" in (a["agency_source"], b["agency_source"]):
        human_vals = [r["is_agency"] for r in (a, b) if r["agency_source"] == "human"]
        is_agency = 1 if all(v == 1 for v in human_vals) else 0
        agency_source = "human" if is_agency else None
    else:
        is_agency = 1 if (a["is_agency"] and b["is_agency"]) else 0
        agency_source = "bundled_list" if is_agency else None
    conn.execute("UPDATE companies SET contact_state = ?, contact_state_reason = ?, is_agency = ?, agency_source = ?, "
                 "domain = COALESCE(domain, ?), updated_at = ? WHERE id = ?",
                 (state, reason, is_agency, agency_source, a["domain"], ts, to_id))
    conn.execute("UPDATE companies SET merged_into = ?, updated_at = ? WHERE id = ?", (to_id, ts, from_id))
    conn.execute("INSERT INTO company_merges (from_id, to_id, matched_keys, strength, by, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (from_id, to_id, json.dumps(sorted(matched_keys or [])), strength, by, ts))
    log_event(conn, "company_merged", from_id=from_id, to_id=to_id, strength=strength, by=by,
              matched_keys=sorted(matched_keys or []))
    if strength == "fuzzy":
        open_human_task(conn, "confirm_company_merge",
                        "Merged %s into %s (%s). Run ./jobhunter companies split %s --keys ... if they differ."
                        % (a["display_name"], b["display_name"], ", ".join(sorted(matched_keys or [])) or "fuzzy",
                           b["company_uid"]), company_id=to_id)


def split(conn, company_id: int, keys_to_move: list[str], by: str) -> int:
    """Move alias keys to a new company and record the pair as distinct (undoes a fuzzy merge)."""
    cid = survivor(conn, company_id)
    if not keys_to_move:
        raise Denied("E_VALIDATION", "name at least one alias key to move")
    have = {r[0]: r[1] for r in conn.execute("SELECT alias_key, kind FROM company_aliases WHERE company_id = ?", (cid,))}
    missing = [k for k in keys_to_move if k not in have]
    if missing:
        raise Denied("E_VALIDATION", "keys not held by this company: %s" % ", ".join(missing))
    if len(set(keys_to_move)) >= len(have):
        raise Denied("E_VALIDATION", "a split must leave at least one key on the company")
    ts = now()
    first = keys_to_move[0]
    display = first.split(":", 1)[1] if ":" in first else first
    cur = conn.execute("INSERT INTO companies (company_uid, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       (new_uid("K"), display, ts, ts))
    new_id = cur.lastrowid
    for k in set(keys_to_move):
        conn.execute("UPDATE company_aliases SET company_id = ?, source = 'human' WHERE alias_key = ?", (new_id, k))
        if k.startswith("dom:"):
            conn.execute("UPDATE companies SET domain = ? WHERE id = ?", (k[4:], new_id))
    conn.execute("INSERT INTO company_distinct (a_id, b_id, by, created_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                 (min(cid, new_id), max(cid, new_id), by, ts))
    log_event(conn, "company_split", company_id=cid, new_company_id=new_id, keys=sorted(set(keys_to_move)), by=by)
    return new_id


def _q(term: str) -> str:
    term = term.strip().lower()
    return '"%s"' % term.replace('"', "") if (" " in term) else term


def precheck_query(conn, company_id: int) -> str:
    """Gmail search expression covering every alias of the company, e.g.
    ("kestrel labs" OR kestrellabs OR kestrellabs.com OR kestrellabs.in). Callers prefix in:sent etc."""
    ids = group(conn, company_id)
    ph = ",".join("?" * len(ids))
    terms = set()
    for (name,) in conn.execute("SELECT display_name FROM companies WHERE id IN (%s)" % ph, ids):
        n = " ".join(keys.name_tokens(name))
        if n:
            terms.add(_q(n))
    for (key,) in conn.execute("SELECT alias_key FROM company_aliases WHERE company_id IN (%s)" % ph, ids):
        if key.startswith("id:") or key.startswith("dom:"):
            body = key.split(":", 1)[1]
            if len(body) >= 3:
                terms.add(_q(body))
    return "(" + " OR ".join(sorted(terms)) + ")"


def set_agency(conn, company_id: int, on: bool, by: str) -> None:
    cid = survivor(conn, company_id)
    conn.execute("UPDATE companies SET is_agency = ?, agency_source = 'human', updated_at = ? WHERE id = ?",
                 (1 if on else 0, now(), cid))
    log_event(conn, "company_agency", company_id=cid, is_agency=bool(on), by=by)


def clear_state(conn, company_id: int, by: str) -> None:
    """contact_state back to none (not for exclusions: do_not_contact from an exclusion stays)."""
    cid = survivor(conn, company_id)
    row = conn.execute("SELECT contact_state, contact_state_reason FROM companies WHERE id = ?", (cid,)).fetchone()
    if row["contact_state"] == "do_not_contact" and (row["contact_state_reason"] or "").startswith("exclusion"):
        raise Denied("E_PRECONDITION", "this company is excluded; remove the exclusion instead")
    conn.execute("UPDATE companies SET contact_state = 'none', contact_state_reason = ?, updated_at = ? WHERE id = ?",
                 ("cleared by " + by, now(), cid))
    log_event(conn, "company_cleared", company_id=cid, by=by)


def aliases_file() -> str:
    import os
    from . import paths
    return os.path.join(paths.private_dir(), "company_aliases.csv")


def import_aliases(conn, path: str | None = None) -> dict:
    """private/company_aliases.csv (header alias,company): each alias (a name or a domain) is resolved together
    with its company, so both end up on one row (source private_aliases). Runs inside the caller's tx."""
    import csv
    import hashlib
    from . import db
    path = path or aliases_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"linked": 0, "errors": [], "missing": True}
    out = {"linked": 0, "errors": []}
    lines = [(i + 1, ln) for i, ln in enumerate(text.splitlines()) if ln.strip() and not ln.lstrip().startswith("#")]
    if lines and lines[0][1].strip().lower().replace(" ", "").startswith("alias,company"):
        lines = lines[1:]
    for n, ln in lines:
        try:
            alias, company = [x.strip() for x in next(csv.reader([ln]))[:2]]
        except (ValueError, csv.Error):
            out["errors"].append({"line": n, "reason": "needs alias,company"})
            continue
        is_domain = "." in alias and " " not in alias
        try:
            if is_domain:
                resolve(conn, name=company, domain=alias, source="private_aliases")
            else:
                base = resolve(conn, name=company, source="private_aliases")
                pairs = keys.company_keys(name=alias)
                for k, _kind in pairs:
                    row = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", (k,)).fetchone()
                    other = survivor(conn, row[0]) if row else base
                    if other != base:
                        keep, lose = min(other, base), max(other, base)
                        merge(conn, lose, keep, by="private_aliases", strength="exact", matched_keys=[k])
                        base = keep
                _insert_keys(conn, base, pairs, "private_aliases")
            out["linked"] += 1
        except Denied as d:
            out["errors"].append({"line": n, "reason": d.message})
    db.meta_set(conn, "aliases_sha256", hashlib.sha256(text.encode("utf-8")).hexdigest(), "system")
    return out


def aliases_changed(conn) -> bool:
    import hashlib
    try:
        with open(aliases_file(), "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return False
    row = conn.execute("SELECT value FROM meta WHERE key = 'aliases_sha256'").fetchone()
    return (row[0] if row else None) != sha
