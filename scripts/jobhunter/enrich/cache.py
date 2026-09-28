"""Hashed identity cache: a person is never looked up twice (U10, ENRICH-SPEC section 9).

Keys per request: `lookup:<first>|<last>|<domain>`, `pname:<first>.<last>@<company_uid>`, `li:<slug>`,
`li_member:<id>`, and after a hit `email_norm:<address>`. Each is stored only as
sha256(install_id + "|" + key), so the cache table holds no names, slugs or addresses in clear.
One request per person, forever; `grant_retry` (human, PIN) allows exactly one more.
"""
from __future__ import annotations

import hashlib

from .. import keys as keys_mod
from .. import locks, paths
from ..canon import new_uid, now
from ..errors import Denied
from ..events import log_event

KINDS = ("lookup", "pname", "li", "li_member", "email_norm")
LOCK_TTL_S = 120


def _install_id(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'install_id'").fetchone()
    if row and row[0]:
        return str(row[0])
    return str(paths.home().get("install_id") or "")


def key_hash(conn, key: str) -> str:
    return hashlib.sha256(("%s|%s" % (_install_id(conn), key)).encode("utf-8")).hexdigest()


def _group(conn, contact_id: int) -> list:
    from .. import people
    try:
        return people.group(conn, contact_id)
    except Denied:
        return [contact_id]


def raw_keys(conn, contact_row, query) -> list:
    """[(clear key, kind)] before hashing (never stored or logged)."""
    out: list = []
    first = keys_mod.fold(query.first_name or "").strip()
    last = keys_mod.fold(query.last_name or "").strip()
    if first and last and query.domain:
        out.append(("lookup:%s|%s|%s" % (first, last, query.domain), "lookup"))
    company_uid = None
    if contact_row["company_id"] is not None:
        r = conn.execute("SELECT company_uid FROM companies WHERE id = ?", (contact_row["company_id"],)).fetchone()
        company_uid = r[0] if r else None
    pk = keys_mod.pname_key(contact_row["full_name"], company_uid)
    if pk:
        out.append((pk, "pname"))
    ids = _group(conn, contact_row["id"])
    q = ",".join("?" * len(ids))
    for key, kind in conn.execute("SELECT key, kind FROM contact_keys WHERE contact_id IN (%s) AND kind IN "
                                  "('li_slug','li_member') ORDER BY key" % q, ids):
        out.append((key, "li" if kind == "li_slug" else "li_member"))
    if contact_row["li_slug"]:
        out.append(("li:" + str(contact_row["li_slug"]).strip().lower(), "li"))
    seen = set()
    return [(k, kind) for k, kind in out if not (k in seen or seen.add(k))]


def keys_for(conn, contact_row, query) -> list:
    """[(key_hash, kind)] for a contact and its query."""
    return [(key_hash(conn, k), kind) for k, kind in raw_keys(conn, contact_row, query)]


def email_key(conn, email: str) -> tuple:
    norm = [k for k, kind in keys_mod.email_keys(email) if kind == "email_norm"][0]
    return key_hash(conn, norm), "email_norm"


def find(conn, key_hashes: list) -> dict | None:
    """The request that owns any of these hashes (newest first), or None."""
    hs = [h for h in key_hashes if h]
    if not hs:
        return None
    row = conn.execute("SELECT r.* FROM enrich_request_keys k JOIN enrich_requests r ON r.id = k.request_id "
                       "WHERE k.key_hash IN (%s) ORDER BY r.id DESC LIMIT 1" % ",".join("?" * len(hs)), hs).fetchone()
    return dict(row) if row is not None else None


def lock_name(request_uid: str) -> str:
    return "enrich:req:" + request_uid


def _add_keys(conn, request_id: int, key_hashes: list) -> None:
    import sqlite3
    ts = now()
    for h, kind in key_hashes:
        if kind not in KINDS:
            raise Denied("E_INTERNAL", "unknown cache key kind %r" % kind)
        try:
            conn.execute("INSERT INTO enrich_request_keys (key_hash, request_id, kind, created_at) VALUES (?, ?, ?, ?)",
                         (h, request_id, kind, ts))
        except sqlite3.IntegrityError:
            owner = conn.execute("SELECT request_id FROM enrich_request_keys WHERE key_hash = ?", (h,)).fetchone()
            if owner is None or owner[0] != request_id:
                raise Denied("E_ALREADY_DONE", "this person already has an email lookup")


def claim(conn, contact_id: int, key_hashes: list, by: str, cycle_id: str | None, holder: str,
          company_id: int | None = None) -> dict:
    """Insert a running request with its key hashes and take its lock. Runs in the caller's transaction.
    Denied(E_ALREADY_DONE) when a key already belongs to another request, E_LOCKED when the contact has a
    running request."""
    import sqlite3
    if not key_hashes:
        raise Denied("E_PRECONDITION", "no identity to look up", data={"reason": "no_name"})
    ts = now()
    uid = new_uid("E")
    try:
        cur = conn.execute("INSERT INTO enrich_requests (request_uid, contact_id, company_id, status, next_step, "
                           "created_by, cycle_id, started_at, created_at, updated_at) VALUES (?, ?, ?, 'running', 0, ?, "
                           "?, ?, ?, ?)", (uid, contact_id, company_id, by, cycle_id, ts, ts, ts))
    except sqlite3.IntegrityError:
        raise Denied("E_LOCKED", "a lookup for this contact is already running")
    rid = cur.lastrowid
    _add_keys(conn, rid, key_hashes)
    if not locks.acquire(conn, lock_name(uid), holder, LOCK_TTL_S):
        raise Denied("E_LOCKED", "the lookup is held by another process")
    log_event(conn, "enrich_claim", request_uid=uid)
    return {"id": rid, "request_uid": uid}


def add_email_key(conn, request_id: int, email: str) -> None:
    """After a hit: the found address joins the request's keys (never twice for that address either)."""
    import sqlite3
    h, kind = email_key(conn, email)
    try:
        conn.execute("INSERT INTO enrich_request_keys (key_hash, request_id, kind, created_at) VALUES (?, ?, ?, ?)",
                     (h, request_id, kind, now()))
    except sqlite3.IntegrityError:
        pass   # another request already found the same address: both stay answered


def release_keys(conn, request_id: int) -> int:
    return conn.execute("DELETE FROM enrich_request_keys WHERE request_id = ?", (request_id,)).rowcount


def state(conn, contact_id: int) -> str | None:
    """Latest request status for the person (hook-safe; for U6 outreach hints): running | found | not_found |
    unavailable | forgotten | None."""
    ids = _group(conn, contact_id)
    row = conn.execute("SELECT status, sendable FROM enrich_requests WHERE contact_id IN (%s) ORDER BY id DESC "
                       "LIMIT 1" % ",".join("?" * len(ids)), ids).fetchone()
    return row["status"] if row is not None else None


def on_merge(conn, from_id: int, to_id: int) -> None:
    """(hook) Requests of the merged contact move to the survivor. A second running request is finished."""
    ts = now()
    both = conn.execute("SELECT id FROM enrich_requests WHERE contact_id = ? AND status = 'running'", (to_id,)).fetchone()
    if both is not None:
        conn.execute("UPDATE enrich_requests SET status = 'not_found', reason = 'merged', next_step = 3, "
                     "finished_at = ?, updated_at = ? WHERE contact_id = ? AND status = 'running'", (ts, ts, from_id))
    conn.execute("UPDATE enrich_requests SET contact_id = ?, updated_at = ? WHERE contact_id = ?", (to_id, ts, from_id))


def purge_calls(conn, call_ids: list) -> int:
    """Clear address and URLs of calls (outcome, credits and key hashes stay). Contact fields first."""
    if not call_ids:
        return 0
    ts = now()
    q = ",".join("?" * len(call_ids))
    conn.execute("UPDATE contacts SET email = NULL, email_grade = NULL, email_evidence_url = NULL, email_mx_ok = NULL, "
                 "email_source = NULL, email_enrich_call_id = NULL, updated_at = ? "
                 "WHERE email_source = 'provider' AND email_enrich_call_id IN (%s)" % q, [ts] + list(call_ids))
    return conn.execute("UPDATE enrich_calls SET email = NULL, email_domain = NULL, source_url = NULL, "
                        "source_urls_json = '[]', purged_at = COALESCE(purged_at, ?), updated_at = ? "
                        "WHERE id IN (%s)" % q, [ts, ts] + list(call_ids)).rowcount


def forget(conn, contact_id: int) -> None:
    """(hook, U1 forget) Requests lose the contact and become `forgotten`; call addresses and URLs are
    cleared; the salted key hashes stay (the forget exclusion already blocks the person)."""
    ids = _group(conn, contact_id)
    q = ",".join("?" * len(ids))
    req_ids = [r[0] for r in conn.execute("SELECT id FROM enrich_requests WHERE contact_id IN (%s)" % q, ids)]
    call_ids = []
    if req_ids:
        rq = ",".join("?" * len(req_ids))
        call_ids = [r[0] for r in conn.execute("SELECT id FROM enrich_calls WHERE request_id IN (%s)" % rq, req_ids)]
    extra = [r[0] for r in conn.execute("SELECT email_enrich_call_id FROM contacts WHERE id IN (%s) AND "
                                        "email_enrich_call_id IS NOT NULL" % q, ids)]
    purge_calls(conn, sorted(set(call_ids) | set(extra)))
    ts = now()
    conn.execute("UPDATE enrich_requests SET contact_id = NULL, status = 'forgotten', next_step = 3, "
                 "finished_at = COALESCE(finished_at, ?), updated_at = ? WHERE contact_id IN (%s)" % q, [ts, ts] + ids)
    log_event(conn, "enrich_forget", requests=len(req_ids))


def grant_retry(conn, contact_id: int, by: str) -> dict:
    """(human) A new request for the person with retry_of set; the key hashes move to it. One retry per
    person; refused while running and when the earlier result bounced. Runs in the caller's transaction."""
    import sqlite3
    ids = _group(conn, contact_id)
    q = ",".join("?" * len(ids))
    last = conn.execute("SELECT * FROM enrich_requests WHERE contact_id IN (%s) AND status IN "
                        "('found','not_found','running') ORDER BY id DESC LIMIT 1" % q, ids).fetchone()
    if last is None:
        raise Denied("E_NOT_FOUND", "this person has no finished email lookup to retry")
    if last["status"] == "running":
        raise Denied("E_PRECONDITION", "the lookup is still running", data={"reason": "running"})
    if last["retry_of"] is not None or conn.execute("SELECT 1 FROM enrich_requests WHERE retry_of = ?",
                                                    (last["id"],)).fetchone():
        raise Denied("E_ALREADY_DONE", "this person already had their one retry")
    bounced = conn.execute("SELECT 1 FROM enrich_calls WHERE request_id = ? AND bounced_at IS NOT NULL",
                           (last["id"],)).fetchone()
    if bounced is not None:
        raise Denied("E_PRECONDITION", "the earlier address bounced; the domain is on the no-guessing list",
                     data={"reason": "bounced"})
    ts = now()
    uid = new_uid("E")
    try:
        cur = conn.execute("INSERT INTO enrich_requests (request_uid, contact_id, company_id, status, next_step, "
                           "retry_of, created_by, started_at, created_at, updated_at) VALUES (?, ?, ?, 'running', 0, ?, "
                           "?, ?, ?, ?)", (uid, contact_id, last["company_id"], last["id"], by, ts, ts, ts))
    except sqlite3.IntegrityError:
        raise Denied("E_ALREADY_DONE", "this person already had their one retry")
    conn.execute("UPDATE enrich_request_keys SET request_id = ? WHERE request_id = ?", (cur.lastrowid, last["id"]))
    log_event(conn, "enrich_retry_granted", request_uid=uid, by=by)
    return {"request_uid": uid, "retry_of": last["request_uid"]}
