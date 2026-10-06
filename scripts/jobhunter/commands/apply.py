"""`apply next` and `apply release` (design 3.4, U6)."""
from __future__ import annotations

from jobhunter import applyq, db
from jobhunter.commands import Result, add_command


def register(subparsers):
    p = add_command(subparsers, "apply next", cmd_next, callers="A",
                    help="claim the next jobs to apply to (approved packages first)")
    p.add_argument("--limit", type=int, default=3, help="at most this many jobs (1 to 10)")
    p = add_command(subparsers, "apply release", cmd_release, callers="A", help="give a claimed job back")
    p.add_argument("--job", required=True, help="job uid")


def _holder(ctx) -> str:
    if ctx.cycle_id:
        return ctx.cycle_id
    agent = getattr(ctx.caller, "agent_id", None)
    return "manual:%s" % (agent or ctx.caller.cls)


def cmd_next(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        items = applyq.claim(conn, args.limit, _holder(ctx))
    if not items:
        return Result(data={"items": []}, code="NOTHING_TO_DO", message="no job to apply to right now",
                      next="run cycle end and reply CYCLE_DONE")
    return Result(data={"items": items}, message="%d job(s) claimed for this cycle" % len(items),
                  next="work each item by its needs field; apply release when you stop early")


def cmd_release(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        applyq.release(conn, args.job)
    return Result(data={"job_uid": args.job}, message="job released")
