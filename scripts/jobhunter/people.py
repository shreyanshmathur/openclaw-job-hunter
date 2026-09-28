"""Person identity resolution and merges (design 2.4.3, M2). Hook-safe: runs inside the caller's
transaction, never commits, no network.

A match on any key (email, Gmail-folded email, LinkedIn slug, opaque member id, legacy or Sales Navigator
id, pname name-at-company key) is the same person (fail closed). Several matched contacts merge into the
lowest id. A match that rests only on pname keys is fuzzy: it is recorded and opens a human task
(`contacts split`, PIN, undoes it).
"""
from __future__ import annotations

from . import jobstate
from .canon import new_uid, now
from .errors import Denied
from .events import log_event, open_human_task

KEY_KINDS = ("email", "email_norm", "li_slug", "li_member", "li_legacy", "li_sales", "pname")
FIELDS = ("full_name", "first_name", "title", "company_id", "role_type", "locale", "email", "email_grade",
          "email_evidence_url", "email_mx_ok", "linkedin_url", "li_slug", "do_not_contact", "dnc_reason")
# the address travels as one group on a merge: the survivor keeps the stronger one (address_rank)
ADDRESS_FIELDS = ("email", "email_grade", "email_evidence_url", "email_mx_ok")
FINDER_FIELDS = ("email_source", "email_enrich_call_id")      # migrations/0002_enrich.sql
GRADE_RANK = {"A": 3, "B": 2, "C": 1}
SOURCE_RANK = {"published": 5, "pattern": 4, "human": 3, None: 2, "provider": 1}
OPEN_DRAFT = ("drafted", "lint_failed", "review_pending", "review_failed", "qc_passed", "awaiting_approval",
              "approved")


def survivor(conn, contact_id: int | None) -> int | None:
    seen = set()
    cid = contact_id
    while cid is not None and cid not in seen:
        seen.add(cid)
        row = conn.execute("SELECT merged_into FROM contacts WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no contact with id %r" % cid)
        if row[0] is None:
            return cid
        cid = row[0]
    raise Denied("E_INTERNAL", "contact merge chain loops at %r" % contact_id)


def group(conn, contact_id: int) -> list[int]:
    """The survivor and every contact merged into it (dedup checks look at all of them)."""
    top = survivor(conn, contact_id)
    out, todo = [top], [top]
    while todo:
        cur = todo.pop()
        for (cid,) in conn.execute("SELECT id FROM contacts WHERE merged_into = ?", (cur,)):
            if cid not in out:
                out.append(cid)
                todo.append(cid)
    return out


def by_uid(conn, uid: str):
    row = conn.execute("SELECT * FROM contacts WHERE contact_uid = ?", (uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no contact %s" % uid)
    return row


def _add_keys(conn, contact_id: int, pairs) -> None:
    for key, kind in pairs:
        if kind not in KEY_KINDS:
            raise Denied("E_VALIDATION", "unknown contact key kind %r" % kind)
        conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (key) DO NOTHING", (key, contact_id, kind, now()))


def _refresh_vanity(conn, contact_id: int) -> None:
    kinds = {}
    for key, kind in conn.execute("SELECT key, kind FROM contact_keys WHERE contact_id = ?", (contact_id,)):
        kinds.setdefault(kind, []).append(key)
    slug = kinds["li_slug"][0][3:] if kinds.get("li_slug") else None
    opaque = bool(kinds.get("li_member") or kinds.get("li_legacy") or kinds.get("li_sales"))
    needs = 1 if (opaque and not slug) else 0
    conn.execute("UPDATE contacts SET needs_vanity = ?, li_slug = COALESCE(?, li_slug), updated_at = ? WHERE id = ?",
                 (needs, slug, now(), contact_id))


def resolve(conn, *, keys: list, fields: dict, create: bool = True):
    """Contact id for the given keys (see module doc). With create=False and no match: None."""
    pairs = [(k, kind) for k, kind in (keys or [])]
    if not pairs:
        raise Denied("E_VALIDATION", "no person identity (email, LinkedIn URL or name at a company)")
    fields = {k: v for k, v in (fields or {}).items() if k in FIELDS and v is not None}
    found: dict[int, list[str]] = {}
    for key, kind in pairs:
        row = conn.execute("SELECT contact_id, kind FROM contact_keys WHERE key = ?", (key,)).fetchone()
        if row:
            found.setdefault(survivor(conn, row[0]), []).append(row[1])
    if not found:
        if not create:
            return None
        ts = now()
        cols = ["contact_uid", "created_at", "updated_at"] + list(fields)
        vals = [new_uid("P"), ts, ts] + [fields[c] for c in fields]
        cur = conn.execute("INSERT INTO contacts (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))), vals)
        cid = cur.lastrowid
        _add_keys(conn, cid, pairs)
        _refresh_vanity(conn, cid)
        log_event(conn, "contact_created", contact_id=cid)
        return cid
    ids = sorted(found)
    target = ids[0]
    for other in ids[1:]:
        fuzzy = all(k == "pname" for k in found[other]) or all(k == "pname" for k in found[target])
        merge(conn, other, target, by="system:resolve", fuzzy=fuzzy)
    if len(ids) == 1 and all(k == "pname" for k in found[target]):
        row = conn.execute("SELECT contact_uid, full_name FROM contacts WHERE id = ?", (target,)).fetchone()
        new_keys = [k for k, kind in pairs if kind != "pname"]
        if new_keys and not conn.execute(
                "SELECT 1 FROM contact_keys WHERE contact_id = ? AND key IN (%s)" % ",".join("?" * len(new_keys)),
                [target] + new_keys).fetchone():
            log_event(conn, "contact_fuzzy_match", contact_id=target, keys=new_keys)
            open_human_task(conn, "confirm_company_merge",
                            "New identity %s was matched to %s (%s) by name at the same company only. "
                            "If they differ run ./jobhunter contacts split %s --keys %s"
                            % (", ".join(new_keys), row["full_name"] or "", row["contact_uid"], row["contact_uid"],
                               ",".join(new_keys)))
    _add_keys(conn, target, pairs)
    cur = conn.execute("SELECT * FROM contacts WHERE id = ?", (target,)).fetchone()
    updates = {k: v for k, v in fields.items() if k not in ("do_not_contact", "dnc_reason") and cur[k] is None}
    if fields.get("do_not_contact"):
        updates["do_not_contact"] = 1
        updates["dnc_reason"] = fields.get("dnc_reason") or cur["dnc_reason"]
    if updates:
        updates["updated_at"] = now()
        conn.execute("UPDATE contacts SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in updates),
                     list(updates.values()) + [target])
    _refresh_vanity(conn, target)
    return target


def address_rank(row) -> tuple:
    """How strong a contact's address is: no address or a bounced one is weakest, then grade A > B > C, then
    source published > pattern > human > (unknown) > provider."""
    if row is None or not row["email"] or row["email_invalid"]:
        return (0, 0, 0)
    source = row["email_source"] if "email_source" in row.keys() else None
    return (1, GRADE_RANK.get(row["email_grade"], 0), SOURCE_RANK.get(source, 0))


def merge(conn, from_id: int, to_id: int, by: str, fuzzy: bool) -> None:
    """Merge contact from_id into to_id. Live actions that would break a person-level unique index stay on
    the loser (merged_into keeps them visible to people.group() dedup checks)."""
    from_id, to_id = survivor(conn, from_id), survivor(conn, to_id)
    if from_id == to_id:
        return
    ts = now()
    ph = ",".join("?" * len(OPEN_DRAFT))
    if conn.execute("SELECT 1 FROM drafts WHERE contact_id = ? AND kind IN ('cold_email','li_invite_note','inmail') "
                    "AND status IN (%s)" % ph, (to_id,) + OPEN_DRAFT).fetchone():
        for (did,) in conn.execute("SELECT id FROM drafts WHERE contact_id = ? AND kind IN ('cold_email',"
                                   "'li_invite_note','inmail') AND status IN (%s)" % ph,
                                   (from_id,) + OPEN_DRAFT).fetchall():
            jobstate.set_draft_status(conn, did, "superseded", "contact_merge", "system:merge")
    for (aid,) in conn.execute("SELECT id FROM actions WHERE contact_id = ?", (from_id,)).fetchall():
        conn.execute("SAVEPOINT jh_merge_action")
        try:
            conn.execute("UPDATE actions SET contact_id = ?, updated_at = ? WHERE id = ?", (to_id, ts, aid))
            conn.execute("RELEASE jh_merge_action")
        except Exception:
            conn.execute("ROLLBACK TO jh_merge_action")
            conn.execute("RELEASE jh_merge_action")
    conn.execute("UPDATE drafts SET contact_id = ?, updated_at = ? WHERE contact_id = ?", (to_id, ts, from_id))
    conn.execute("UPDATE threads SET contact_id = ?, updated_at = ? WHERE contact_id = ?", (to_id, ts, from_id))
    conn.execute("INSERT INTO job_hiring_team (job_id, contact_id, relation) SELECT job_id, ?, relation "
                 "FROM job_hiring_team WHERE contact_id = ? ON CONFLICT DO NOTHING", (to_id, from_id))
    conn.execute("DELETE FROM job_hiring_team WHERE contact_id = ?", (from_id,))
    conn.execute("UPDATE research_facts SET subject_id = ? WHERE subject_kind = 'person' AND subject_id = ?",
                 (to_id, from_id))
    conn.execute("UPDATE contact_keys SET contact_id = ? WHERE contact_id = ?", (to_id, from_id))
    a = conn.execute("SELECT * FROM contacts WHERE id = ?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM contacts WHERE id = ?", (to_id,)).fetchone()
    updates = {k: a[k] for k in FIELDS if k not in ("do_not_contact", "dnc_reason") + ADDRESS_FIELDS
               and b[k] is None and a[k] is not None}
    if a["do_not_contact"] and not b["do_not_contact"]:
        updates["do_not_contact"] = 1
        updates["dnc_reason"] = a["dnc_reason"] or "merged"
    group = ADDRESS_FIELDS + tuple(f for f in FINDER_FIELDS if f in a.keys())
    if address_rank(a) > address_rank(b):
        # the merged contact's address is stronger: it moves as one group (grade, evidence, MX, finder source)
        updates.update({k: a[k] for k in group})
        updates["email_invalid"] = a["email_invalid"]
    elif a["email_invalid"] and not b["email_invalid"] and (a["email"] == b["email"]):
        updates["email_invalid"] = 1
    updates["updated_at"] = ts
    conn.execute("UPDATE contacts SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in updates),
                 list(updates.values()) + [to_id])
    from . import hooks
    hooks.on_contact_merge(conn, from_id, to_id)      # email finder requests move to the survivor (U10)
    conn.execute("UPDATE contacts SET merged_into = ?, updated_at = ? WHERE id = ?", (to_id, ts, from_id))
    _refresh_vanity(conn, to_id)
    log_event(conn, "contact_merged", from_id=from_id, to_id=to_id, fuzzy=bool(fuzzy), by=by)
    if fuzzy:
        open_human_task(conn, "confirm_company_merge",
                        "Merged contact %s into %s by name at the same company only. If they are two people run "
                        "./jobhunter contacts split %s --keys <keys>" % (a["contact_uid"], b["contact_uid"],
                                                                        b["contact_uid"]))


def split(conn, contact_id: int, keys_to_move: list[str], by: str) -> int:
    """Move identity keys to a new contact (undoes a fuzzy merge)."""
    cid = survivor(conn, contact_id)
    have = {r[0] for r in conn.execute("SELECT key FROM contact_keys WHERE contact_id = ?", (cid,))}
    missing = [k for k in keys_to_move if k not in have]
    if not keys_to_move or missing:
        raise Denied("E_VALIDATION", "keys not held by this contact: %s" % ", ".join(missing or ["(none given)"]))
    if len(set(keys_to_move)) >= len(have):
        raise Denied("E_VALIDATION", "a split must leave at least one key on the contact")
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    ts = now()
    cur = conn.execute("INSERT INTO contacts (contact_uid, full_name, first_name, company_id, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (new_uid("P"), row["full_name"], row["first_name"],
                                                     row["company_id"], ts, ts))
    new_id = cur.lastrowid
    for k in set(keys_to_move):
        conn.execute("UPDATE contact_keys SET contact_id = ? WHERE key = ?", (new_id, k))
        if k.startswith("email:"):
            conn.execute("UPDATE contacts SET email = ? WHERE id = ?", (k[6:], new_id))
    _refresh_vanity(conn, cid)
    _refresh_vanity(conn, new_id)
    log_event(conn, "contact_split", contact_id=cid, new_contact_id=new_id, keys=sorted(set(keys_to_move)), by=by)
    return new_id
