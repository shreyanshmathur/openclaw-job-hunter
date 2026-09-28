"""Threads, replies, follow-ups and outcomes (design 3.4, U6)."""
from __future__ import annotations

from jobhunter import db, replies, threads
from jobhunter.commands import Result, add_command
from jobhunter.threads import cfg


def register(subparsers):
    p = add_command(subparsers, "thread list", cmd_list, callers="ASH", help="conversation threads")
    p.add_argument("--needs-check", dest="needs_check", action="store_true",
                   help="only threads the replies lane should check in the browser")
    p.add_argument("--state", choices=threads.THREAD_STATES)
    p = add_command(subparsers, "reply pending", cmd_pending, callers="A", help="reply packets to classify")
    p.add_argument("--limit", type=int, default=10)
    p = add_command(subparsers, "reply record", cmd_record, callers="AS",
                    help="record a classified reply (12.13) or an accepted invitation")
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "followup due", cmd_followup_due, callers="A", help="threads due for their follow-up")
    p.add_argument("--limit", type=int, default=5)
    p = add_command(subparsers, "outcome set", cmd_outcome, callers="HS", help="record the outcome of a thread or job")
    p.add_argument("--thread")
    p.add_argument("--job")
    p.add_argument("--outcome", required=True,
                   choices=sorted(set(threads.THREAD_OUTCOMES) | set(threads.APPLICATION_OUTCOMES)))


def cmd_list(args, ctx):
    if args.needs_check:
        conn = ctx.connect()
        with db.tx(conn):
            items = threads.needs_check(conn)
            if ctx.is_agent:   # handed out: not handed out again for CHECK_EVERY_HOURS
                threads.mark_checked(conn, [i["thread_key"] for i in items])
        return {"threads": items}
    conn = ctx.connect(write=False)
    return {"threads": threads.list_threads(conn, state=args.state)}


def cmd_pending(args, ctx):
    conn = ctx.connect(write=False)
    limit = max(1, min(int(args.limit), 50))
    if cfg("gmail.route", "web_ui") == "web_ui":
        out = {"checks": replies.web_checks(conn, limit), "packets": replies.pending(conn, limit)}
        out.update(replies.web_lane(conn, getattr(ctx.caller, "agent_id", None) if ctx.is_agent else None))
        return out
    items = replies.pending(conn, limit)
    if not items:
        return Result(data={"packets": []}, code="NOTHING_TO_DO", message="no reply to classify")
    return {"packets": items}


def cmd_record(args, ctx):
    payload = ctx.read_json(args.file)
    by = getattr(ctx.caller, "agent_id", None) or ctx.caller.cls
    conn = ctx.connect()
    with db.tx(conn):
        res = replies.record(conn, payload, by)
    replies.cleanup_packet(res.pop("packet_path", None))
    return Result(data=res, message="recorded")


def cmd_followup_due(args, ctx):
    conn = ctx.connect(write=False)
    items = threads.due_followups(conn, max(1, min(int(args.limit), 20)))
    if not items:
        return Result(data={"items": []}, code="NOTHING_TO_DO", message="no follow-up is due")
    return {"items": items}


def cmd_outcome(args, ctx):
    conn = ctx.connect()
    by = "human:cli" if ctx.caller.cls == "human" else "system"
    with db.tx(conn):
        res = threads.set_outcome(conn, thread_key=args.thread, job_uid=args.job, outcome=args.outcome, by=by)
    return Result(data=res, message="outcome recorded")
