"""`eval` commands (design 3.4, U2): next, record, release, requeue, stats."""
from __future__ import annotations

from .. import db, evaluate, paths
from ..errors import Denied
from . import Result, add_command


def register(subparsers):
    p = add_command(subparsers, "eval next", cmd_next, callers="A",
                    help="claim queued jobs and write one packet per job to work/<cycle>/")
    p.add_argument("--limit", type=int, default=None)
    p = add_command(subparsers, "eval record", cmd_record, callers="A", help="record a scorecard (12.3)")
    p.add_argument("--job", required=True)
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "eval release", cmd_release, callers="A", help="give a claimed job back")
    p.add_argument("--job", required=True)
    p = add_command(subparsers, "eval requeue", cmd_requeue, callers="SH",
                    help="send jobs back to evaluation (after a profile change, or one job)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--since-profile-change", action="store_true")
    g.add_argument("--job")
    p = add_command(subparsers, "eval stats", cmd_stats, callers="ASH", help="rejection share per reason code")
    p.add_argument("--days", type=int, default=14)


def running_cycle(conn, lane: str) -> str | None:
    row = conn.execute("SELECT cycle_id FROM cycles WHERE lane = ? AND status = 'running' ORDER BY started_at DESC "
                       "LIMIT 1", (lane,)).fetchone()
    return row[0] if row else None


def cmd_next(args, ctx):
    conn = ctx.connect()
    cycle_id = ctx.cycle_id or running_cycle(conn, "evaluator")
    if not cycle_id:
        raise Denied("E_PRECONDITION", "no evaluator cycle: run preflight --lane evaluator and pass --cycle <id>")
    paths.work_dir("evaluator", cycle_id)   # validates the cycle id format
    ctx.cycle_id = cycle_id
    with db.tx(conn):
        packets = evaluate.claim(conn, args.limit, cycle_id)
    if not packets:
        return Result(data={"packets": []}, code="NOTHING_TO_DO", message="no jobs are waiting for evaluation",
                      next="run cycle end and reply NO_REPLY")
    return Result(data={"packets": packets}, message="%d packet(s) written" % len(packets),
                  next="read the brief once, then write one scorecard per packet and run eval record")


def cmd_record(args, ctx):
    sc = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        res = evaluate.record(conn, args.job, sc)
    return Result(data=res, message="%s scored %d (%s)" % (args.job, res["score"], res["verdict"]),
                  next="continue with the next packet")


def cmd_release(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        evaluate.release(conn, args.job)
    return Result(data={"job_uid": args.job}, message="released %s" % args.job)


def cmd_requeue(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        n = evaluate.requeue(conn, since_profile_change=bool(args.since_profile_change), job_uid=args.job)
    return Result(data={"requeued": n}, message="%d job(s) sent back to evaluation" % n)


def cmd_stats(args, ctx):
    conn = ctx.connect(write=False)
    return {"stats": evaluate.stats(conn, args.days)}
