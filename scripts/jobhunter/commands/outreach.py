"""Outreach, contacts, research, address checks and history import commands (design 3.4, U6)."""
from __future__ import annotations

from jobhunter import contacts, db, emailcheck, outreach, research
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied
from jobhunter.threads import cfg


def register(subparsers):
    p = add_command(subparsers, "outreach next", cmd_next, callers="A", help="next people to research and write to")
    p.add_argument("--limit", type=int, default=3)
    p = add_command(subparsers, "outreach skip", cmd_skip, callers="A", help="do not pick a target for 30 days")
    p.add_argument("target", help="contact:<P...>, job:<J...> or company:<K...>")
    p.add_argument("--reason", required=True, choices=outreach.SKIP_REASONS)
    p = add_command(subparsers, "contact add", cmd_contact_add, callers="A", help="store a person (12.8 file)")
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "contact show", cmd_contact_show, callers="ASH",
                    help="one contact with keys and actions")
    p.add_argument("contact", help="contact uid")
    p = add_command(subparsers, "research add", cmd_research_add, callers="A", help="store research facts (12.2 file)")
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "research list", cmd_research_list, callers="ASH", help="facts of one subject")
    p.add_argument("--contact")
    p.add_argument("--company")
    p.add_argument("--job")
    p = add_command(subparsers, "email verify", cmd_email_verify, callers="A", help="MX check and grade of an address")
    p.add_argument("--address", required=True)
    p.add_argument("--grade", required=True, choices=("A", "B", "C"))
    p.add_argument("--evidence-file", dest="evidence_file")
    p = add_command(subparsers, "actions import", cmd_actions_import, callers="AH",
                    help="record past sends from a history scan as imported actions")
    p.add_argument("--file", required=True)


def cmd_next(args, ctx):
    conn = ctx.connect()
    limit = max(1, min(int(args.limit), 10))
    targets = outreach.work_list(conn, limit)
    if not targets:
        return Result(data={"targets": []}, code="NOTHING_TO_DO", message="no outreach target right now")
    return Result(data={"targets": targets}, message="%d target(s)" % len(targets),
                  next="dedup check, then research each target within the page budget")


def cmd_skip(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        outreach.skip(conn, args.target, args.reason)
    return Result(data={"target_key": args.target, "reason": args.reason}, message="target skipped for 30 days")


def cmd_contact_add(args, ctx):
    payload = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        res = contacts.add_from_file(conn, payload)
    if res["do_not_contact"]:
        raise Denied("E_CONTACT_DNC", "this person is on the do-not-contact list; drop the target", data=res)
    if res["company_blocked"]:
        raise Denied("E_EXCLUDED", "this company is excluded; drop the target", data=res)
    if res["already_contacted"]:
        raise Denied("E_DUP_PERSON", "this person already had a first touch; drop the target", data=res)
    return Result(data=res, message="contact %s stored" % res["contact_uid"])


def cmd_contact_show(args, ctx):
    conn = ctx.connect(write=False)
    return contacts.show(conn, args.contact)


def cmd_research_add(args, ctx):
    payload = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        res = research.add_research(conn, payload)
    flagged = sum(1 for f in res["facts"] if f["injection_flag"])
    msg = "%d fact(s) stored" % len(res["facts"])
    if flagged:
        msg += "; %d read like instructions to an AI and can never be a hook" % flagged
    return Result(data=res, message=msg)


def cmd_research_list(args, ctx):
    conn = ctx.connect(write=False)
    facts = research.list_research(conn, contact_uid=args.contact, company_uid=args.company, job_uid=args.job)
    return {"facts": facts}


def cmd_email_verify(args, ctx):
    evidence = None
    if args.evidence_file:
        evidence, _truncated = ctx.read_text(args.evidence_file)
    addr = (args.address or "").strip().lower()
    if not emailcheck.EMAIL_RE.match(addr):
        raise Denied("E_VALIDATION", "not a valid email address")
    try:
        mx = emailcheck.lookup_mx(addr.rsplit("@", 1)[1])
    except emailcheck.LookupFailed as exc:
        raise Denied("E_NETWORK", "MX lookup failed: %s" % exc)
    conn = ctx.connect()
    with db.tx(conn):
        res = emailcheck.verify_address(conn, addr, args.grade, None, mx_hosts=mx, evidence_text=evidence)
    if not res["allowed"]:
        code = "E_NO_MX" if "no_mx" in res["reasons"] else "E_ADDRESS_GRADE"
        raise Denied(code, "this address may not be used: %s" % ", ".join(res["reasons"]), data=res)
    return Result(data=res, message="address can be used")


def cmd_actions_import(args, ctx):
    if ctx.is_agent and cfg("gmail.route", "web_ui") != "web_ui":
        raise Denied("E_ROUTE_UNAVAILABLE", "history scans by the agent run only on the web_ui email route; "
                     "the app_password route uses ./jobhunter mail import-history")
    payload = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        res = outreach.import_actions(conn, payload, ctx.cycle_id)
    return Result(data=res, message="%d imported, %d already covered" % (res["imported"], res["already_covered"]))
