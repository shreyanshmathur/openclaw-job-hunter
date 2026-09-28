"""`job` commands (design 3.4, U2): add, show, list, alias add, set-status, human-call."""
from __future__ import annotations

from .. import db, jobs
from ..errors import Denied
from . import Result, add_command


def register(subparsers):
    p = add_command(subparsers, "job add", cmd_add, callers="ASH",
                    help="ingest a job file (12.1): keys, dedup, exclusions, pre-filter, evaluation queue")
    p.add_argument("--file", required=True, help="ingest JSON under the agent's work/ folder")
    p = add_command(subparsers, "job show", cmd_show, callers="ASH", help="one job with keys, evaluation, actions")
    p.add_argument("job_uid")
    p = add_command(subparsers, "job list", cmd_list, callers="SH", help="recent jobs")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p = add_command(subparsers, "job alias add", cmd_alias_add, callers="A",
                    help="record another URL of a known job (12.15 URL file)")
    p.add_argument("job_uid")
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "job set-status", cmd_set_status, callers="AH",
                    help="needs_human (applier: listed reasons only) or closed (human)")
    p.add_argument("job_uid")
    p.add_argument("--status", required=True, choices=("needs_human", "closed"))
    p.add_argument("--reason", required=True)
    p = add_command(subparsers, "job human-call", cmd_human_call, callers="SH",
                    help="your call on a job: apply_anyway or never")
    p.add_argument("job_uid")
    p.add_argument("call", choices=jobs.HUMAN_CALLS)


def _by(ctx) -> str:
    if ctx.caller.cls == "agent":
        return ctx.caller.agent_id or "agent"
    return {"human": "human:cli", "chat": "human:chat"}.get(ctx.caller.cls, "system")


def cmd_add(args, ctx):
    payload = ctx.read_json(args.file)
    if ctx.is_agent:
        via = "browser"
    else:
        via = payload.get("discovered_via") if isinstance(payload, dict) else None
        via = via if via in ("browser", "human") else "human"
    conn = ctx.connect()
    with db.tx(conn):
        results = jobs.ingest(conn, payload, ctx.cycle_id, via)
    counts: dict = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    msg = ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items()))
    return Result(data={"results": results, "counts": counts}, message="ingested %d job(s): %s" % (len(results), msg),
                  next="continue with the next site" if ctx.is_agent else "")


def cmd_show(args, ctx):
    conn = ctx.connect(write=False)
    return {"job": jobs.show(conn, args.job_uid)}


def cmd_list(args, ctx):
    conn = ctx.connect(write=False)
    rows = jobs.list_jobs(conn, args.status, args.limit)
    return Result(data={"jobs": rows}, message="%d job(s)" % len(rows))


def cmd_alias_add(args, ctx):
    data = ctx.read_json(args.file)
    if not isinstance(data, dict) or set(data) != {"url"} or not isinstance(data.get("url"), str):
        raise Denied("E_SCHEMA", 'the URL file must be {"url": "https://..."}')
    conn = ctx.connect()
    with db.tx(conn):
        res = jobs.add_alias(conn, args.job_uid, data["url"])
    return Result(data=res, message="alias recorded for %s" % args.job_uid)


def cmd_set_status(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        res = jobs.set_status(conn, args.job_uid, args.status, args.reason, _by(ctx), agent=ctx.is_agent)
    return Result(data=res, message="job %s is now %s" % (args.job_uid, args.status),
                  next="move on to the next job" if ctx.is_agent else "")


def cmd_human_call(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        jobs.set_human_call(conn, args.job_uid, args.call, _by(ctx))
        row = conn.execute("SELECT status, human_call FROM jobs WHERE job_uid = ?", (args.job_uid,)).fetchone()
    return Result(data={"job_uid": args.job_uid, "human_call": row["human_call"], "status": row["status"]},
                  message="recorded %s for %s" % (args.call.replace("_", " "), args.job_uid))
