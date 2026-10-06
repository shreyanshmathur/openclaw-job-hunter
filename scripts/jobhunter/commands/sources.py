"""`sources` and `searches` commands (design 3.4, U2)."""
from __future__ import annotations

from .. import db, jobs, searches, sources
from ..errors import Denied
from . import Result, add_command
from .eval import running_cycle


def register(subparsers):
    add_command(subparsers, "sources list", cmd_list, callers="SHR", help="API sources and browser sites")
    p = add_command(subparsers, "sources fetch", cmd_fetch, callers="SH", help="poll the public job APIs that are due")
    p.add_argument("--lane", required=True, choices=("api",))
    p.add_argument("--source", choices=tuple(sources.BY_ID))
    p.add_argument("--tenant")
    p.add_argument("--limit", type=int)
    p = add_command(subparsers, "sources add-tenant", cmd_add_tenant, callers="SH",
                    help="validate and add an ATS job board to poll")
    p.add_argument("--ats", required=True, choices=sources.ATS_IDS)
    p.add_argument("--tenant", required=True)
    p.add_argument("--company")
    p = add_command(subparsers, "searches generate", cmd_generate, callers="SH",
                    help="build browser searches from the confirmed profile into private/searches.json")
    p.add_argument("--overwrite", action="store_true")
    add_command(subparsers, "searches due", cmd_due, callers="A", help="searches for this scout cycle")


def cmd_list(args, ctx):
    conn = ctx.connect(write=False)
    rows = sources.list_sources(conn)
    return Result(data={"sources": rows}, message="%d source(s)" % len(rows))


def cmd_fetch(args, ctx):
    conn = ctx.connect()
    summary = sources.run_fetch(conn, lane=args.lane, source=args.source, tenant=args.tenant, limit=args.limit)
    if summary.get("skipped") == "profile_unconfirmed":
        return Result(data=summary, code="NOTHING_TO_DO",
                      message="the profile is not confirmed yet; run ./jobhunter profile first")
    attempted = summary["sources"] + len([e for e in summary["errors"] if "tenant" in e]) + summary["not_modified"]
    if attempted and summary["ok_sources"] == 0 and summary["errors"]:
        return Result(data=summary, code="E_NETWORK", message="every source failed; the next run retries")
    return Result(data=summary, message="fetched %d, new %d, queued %d, rejected by filters %d, errors %d" % (
        summary["fetched"], summary["new"], summary["eval_queued"], summary["prefilter_rejected"],
        len(summary["errors"])))


def cmd_add_tenant(args, ctx):
    conn = ctx.connect()
    res = sources.add_tenant(conn, args.ats, args.tenant, args.company)
    return Result(data=res, message="%s board %s added (%d jobs seen)" % (args.ats, args.tenant, res["jobs_seen"]))


def cmd_generate(args, ctx):
    conn = ctx.connect()
    prof = jobs.load_profile(required=True)
    cfg = jobs.load_config()
    items = searches.generate(prof, cfg)
    res = searches.write_file(items, jobs.profile_version(conn, prof), overwrite=bool(args.overwrite))
    res["generated"] = len(items)
    return Result(data=res, message="%d search(es) in private/searches.json (%d new)" % (res["searches"],
                                                                                       res["added"]))


def cmd_due(args, ctx):
    conn = ctx.connect()
    cycle_id = ctx.cycle_id or running_cycle(conn, "scout")
    if ctx.is_agent and not cycle_id:
        raise Denied("E_PRECONDITION", "no scout cycle: run preflight --lane scout and pass --cycle <id>")
    ctx.cycle_id = cycle_id
    with db.tx(conn):
        items = searches.due(conn, cycle_id)
        if items and ctx.is_agent:
            searches.mark_handed_out(conn, items, cycle_id)
    if not items:
        return Result(data={"searches": []}, code="NOTHING_TO_DO", message="no searches are due",
                      next="run cycle end and reply CYCLE_DONE")
    return Result(data={"searches": items}, message="%d search(es) due" % len(items),
                  next="open each URL, stay within page_budget, write one ingest file per site, run job add")
