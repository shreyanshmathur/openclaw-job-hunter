"""`digest`, `inbox`, `status`, `export` (design 3.4, U5)."""
from __future__ import annotations

from jobhunter import digest, export, status
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied


def register(subparsers):
    p = add_command(subparsers, "digest", cmd_digest, callers="SH", help="what changed since the last digest")
    p.add_argument("--since-last", action="store_true", help="only since the last delivered digest")
    p.add_argument("--deliver", action="store_true", help="send it to your chat")
    add_command(subparsers, "inbox", cmd_inbox, callers="SHCR", help="everything waiting for you")
    add_command(subparsers, "status", cmd_status, callers="SHCR", help="state, limits, queues and stops")
    p = add_command(subparsers, "export", cmd_export, callers="SH", help="CSV copies of every Sheet tab")
    p.add_argument("--out", help="folder (default exports/)")


def cmd_digest(args, ctx):
    conn = ctx.connect()
    data = digest.run(conn, since_last=args.since_last, deliver=args.deliver)
    if data.get("nothing_new"):
        return Result(data=data, code="NOTHING_TO_DO", message="nothing new since the last digest",
                      human="Nothing new since the last digest.")
    flush = data.get("flush") or {}
    if args.deliver and not data.get("sent") and flush.get("failed"):
        raise Denied("E_OPENCLAW_CALL", "the digest is queued but was not delivered yet: %s"
                     % (flush.get("error") or "not delivered"), data=data)
    if args.deliver and not data.get("sent"):
        msg = "digest queued; it is sent after quiet hours" if flush.get("quiet_hours") else "digest queued"
    else:
        msg = "digest %s" % ("delivered" if data.get("sent") else "built")
    return Result(data=data, message=msg, human=data.get("text"))


def cmd_inbox(args, ctx):
    conn = ctx.connect(write=False)
    config = status.load_config()
    data = status.build_inbox(conn, config)
    n = len(data["approvals"]) + len(data["questions"]) + len(data["tasks"])
    return Result(data=data, message="%d items waiting for you" % n,
                  human=status.render_inbox_text(data, status.tzinfo(config)))


def cmd_status(args, ctx):
    conn = ctx.connect(write=False)
    config = status.load_config()
    data = status.build_status(conn, config)
    return Result(data=data, message=data["state"], human=status.render_status_text(data, status.tzinfo(config)))


def cmd_export(args, ctx):
    out = None
    if args.out:
        out = ctx.input_path(args.out)
    conn = ctx.connect(write=False)
    data = export.export_all(conn, out)
    return Result(data=data, message="exported %d files to %s" % (len(data["files"]), data["out_dir"]))
