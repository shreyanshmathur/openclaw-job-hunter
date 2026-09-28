"""Maintenance, exclusions, privacy and audit commands [U1] (design 3.4, section 9)."""
from __future__ import annotations

from jobhunter import audit, db, exclusions, hooks, housekeeping, keys, people
from jobhunter.canon import now
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied
from jobhunter.events import log_event


def cmd_housekeeping(args, ctx):
    conn = ctx.connect()
    return housekeeping.run(conn, args.only)


def cmd_audit(args, ctx):
    conn = ctx.connect()
    res = audit.run(conn, args.days or 2)      # IMAP first, then a short write transaction
    if res["mismatches"]:
        return Result(data=res, message="%d unledgered sends: everything is stopped for review"
                      % len(res["mismatches"]))
    return res


def cmd_exclusions_import(args, ctx):
    path = ctx.input_path(args.file) if args.file else None
    conn = ctx.connect()
    with db.tx(conn):
        res = exclusions.import_csv(conn, path, bool(args.deactivate), ctx.caller)
    if res["errors"] and not (res["added"] or res["reactivated"] or res["unchanged"]):
        return Result(data=res, code="E_VALIDATION", message="no valid lines; see errors")
    return res


def cmd_exclusions_list(args, ctx):
    conn = ctx.connect(write=False)
    return {"exclusions": exclusions.list_(conn, args.type)}


def cmd_exclusions_add(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return exclusions.add(conn, args.type, args.value, args.reason, source="human")


def cmd_exclusions_remove(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return exclusions.remove(conn, args.type, args.value, by="human")


def _queue_sheet_delete(conn, tab: str, row_id: str, ts: str) -> None:
    """sheets.queue_delete (U5) when installed, else the same sheet_deletes row directly."""
    try:
        from jobhunter import sheets
        fn = sheets.queue_delete
    except (ImportError, AttributeError):
        fn = None
    if fn is not None:
        fn(conn, tab, row_id)
        return
    conn.execute("INSERT INTO sheet_deletes (tab, row_id, created_at) VALUES (?, ?, ?) "
                 "ON CONFLICT (tab, row_id) DO UPDATE SET done_at = NULL", (tab, row_id, ts))


def forget(conn, *, email: str | None = None, linkedin: str | None = None, contact_uid: str | None = None) -> dict:
    """Delete a person's stored personal data, keep hashed suppression exclusions, queue Sheet deletes."""
    cid = None
    if contact_uid:
        cid = people.by_uid(conn, contact_uid)["id"]
    else:
        pairs = keys.person_keys(email=email, linkedin_url=linkedin)
        for k, _ in pairs:
            r = conn.execute("SELECT contact_id FROM contact_keys WHERE key = ?", (k,)).fetchone()
            if r:
                cid = r[0]
                break
    suppress = set()
    if email:
        suppress |= {k for k, kind in keys.email_keys(email) if kind == "email_norm"}
    if linkedin:
        suppress |= {k for k, _ in keys.linkedin_keys(linkedin)}
    ids = people.group(conn, cid) if cid else []
    if ids:
        q = ",".join("?" * len(ids))
        for (k, kind) in conn.execute("SELECT key, kind FROM contact_keys WHERE contact_id IN (%s)" % q, ids).fetchall():
            if kind in ("email_norm", "li_slug", "li_member", "li_legacy", "li_sales"):
                suppress.add(k)
    if not suppress and not ids:
        raise Denied("E_NOT_FOUND", "no such person")
    ts = now()
    added = 0
    for k in sorted(suppress):
        etype = "email" if k.startswith("email") else "linkedin"
        h = exclusions.hashed(k)
        if not conn.execute("SELECT 1 FROM exclusions WHERE type = ? AND value_key = ?", (etype, h)).fetchone():
            conn.execute("INSERT INTO exclusions (type, value_raw, value_key, reason, source, active, created_at, "
                         "updated_at) VALUES (?, ?, ?, 'forgotten', 'forget', 1, ?, ?)", (etype, h, h, ts, ts))
            added += 1
    deletes = 0
    if ids:
        # the email finder first (U10): its requests become `forgotten`, provider addresses and URLs are cleared
        hooks.on_forget(conn, ids[0])
        q = ",".join("?" * len(ids))
        rows = [("outreach", tok) for (tok,) in
                conn.execute("SELECT token FROM actions WHERE contact_id IN (%s)" % q, ids).fetchall()]
        rows += [("followups", tk) for (tk,) in
                 conn.execute("SELECT thread_key FROM threads WHERE contact_id IN (%s)" % q, ids).fetchall()]
        # the person's drafts show on the Approvals tab (row id draft_uid) with the name and the message
        rows += [("approvals", du) for (du,) in
                 conn.execute("SELECT draft_uid FROM drafts WHERE contact_id IN (%s)" % q, ids).fetchall()]
        for tab, row_id in rows:
            if row_id:
                _queue_sheet_delete(conn, tab, row_id, ts)
                deletes += 1
        conn.execute("DELETE FROM research_facts WHERE subject_kind = 'person' AND subject_id IN (%s)" % q, ids)
        conn.execute("DELETE FROM contact_keys WHERE contact_id IN (%s)" % q, ids)
        finder = [c for c in ("email_source", "email_enrich_call_id")
                  if c in {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}]
        conn.execute("UPDATE contacts SET full_name = NULL, first_name = NULL, title = NULL, email = NULL, "
                     "email_evidence_url = NULL, linkedin_url = NULL, li_slug = NULL, locale = NULL, do_not_contact = 1, "
                     "%sdnc_reason = 'forgotten', updated_at = ? WHERE id IN (%s)"
                     % ("".join("%s = NULL, " % c for c in finder), q), [ts] + ids)
        conn.execute("UPDATE actions SET recipient = NULL, evidence = NULL, note = NULL, updated_at = ? "
                     "WHERE contact_id IN (%s)" % q, [ts] + ids)
        conn.execute("UPDATE drafts SET recipient = NULL, subject = NULL, body = NULL, updated_at = ? "
                     "WHERE contact_id IN (%s)" % q, [ts] + ids)
        conn.execute("UPDATE threads SET reply_summary = NULL, subject = NULL, updated_at = ? WHERE contact_id IN (%s)"
                     % q, [ts] + ids)
    log_event(conn, "forget", contacts=len(ids), suppressions=added)
    return {"contacts": len(ids), "suppressions_added": added, "sheet_deletes": deletes}


def cmd_forget(args, ctx):
    given = [x for x in (args.email, args.linkedin, args.contact) if x]
    if len(given) != 1:
        raise Denied("E_USAGE", "give exactly one of --email, --linkedin, --contact")
    conn = ctx.connect()
    with db.tx(conn):
        return forget(conn, email=args.email, linkedin=args.linkedin, contact_uid=args.contact)


def register(sub):
    p = add_command(sub, "housekeeping", cmd_housekeeping, callers="SH", help="nightly maintenance and audit")
    p.add_argument("--only", choices=housekeeping.TASKS)
    p = add_command(sub, "audit run", cmd_audit, callers="SH", help="look for sends without a ledger entry")
    p.add_argument("--days", type=int)
    p = add_command(sub, "exclusions import", cmd_exclusions_import, callers="SH", help="import private/exclusions.csv")
    p.add_argument("--file")
    p.add_argument("--deactivate", action="store_true")
    p = add_command(sub, "exclusions list", cmd_exclusions_list, callers="SH", help="list exclusions")
    p.add_argument("--type", choices=exclusions.TYPES)
    p = add_command(sub, "exclusions add", cmd_exclusions_add, callers="SH", help="add one exclusion")
    p.add_argument("--type", required=True, choices=exclusions.TYPES)
    p.add_argument("--value", required=True)
    p.add_argument("--reason")
    p = add_command(sub, "exclusions remove", cmd_exclusions_remove, callers="H", help="deactivate one exclusion (PIN)")
    p.add_argument("--type", required=True, choices=exclusions.TYPES)
    p.add_argument("--value", required=True)
    p = add_command(sub, "forget", cmd_forget, callers="H", help="delete a person's data (PIN)")
    p.add_argument("--email")
    p.add_argument("--linkedin")
    p.add_argument("--contact")
