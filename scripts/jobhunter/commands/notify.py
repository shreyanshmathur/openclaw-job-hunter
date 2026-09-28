"""`notify enqueue|flush|test` (design 1.5, 3.4, U5)."""
from __future__ import annotations

from jobhunter import notify, status
from jobhunter.canon import now
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied
from jobhunter.events import NOTIFY_KINDS, NOTIFY_PRIORITIES


def register(subparsers):
    p = add_command(subparsers, "notify enqueue", cmd_enqueue, callers="S", help="queue a message to the owner")
    p.add_argument("--kind", required=True, choices=list(NOTIFY_KINDS))
    p.add_argument("--priority", required=True, choices=list(NOTIFY_PRIORITIES))
    p.add_argument("--text-file", required=True)
    p.add_argument("--dedupe", required=True)
    p = add_command(subparsers, "notify flush", cmd_flush, callers="S", help="deliver queued messages")
    p.add_argument("--deliver", action="store_true", help="send (without it the text is only shown)")
    p.add_argument("--max", type=int, default=8, help="items per message (default 8)")
    add_command(subparsers, "notify test", cmd_test, callers="H", help="send a test message to your chat")


def cmd_enqueue(args, ctx):
    text, truncated = ctx.read_text(args.text_file)
    if len(args.dedupe) > 200:
        raise Denied("E_USAGE", "--dedupe is at most 200 characters")
    conn = ctx.connect()
    data = notify.enqueue(conn, args.kind, args.priority, text, args.dedupe)
    data["truncated"] = truncated
    return Result(data=data, message="queued" if data["queued"] else "already queued")


def cmd_flush(args, ctx):
    if args.max < 1 or args.max > 20:
        raise Denied("E_USAGE", "--max must be between 1 and 20")
    conn = ctx.connect()
    data = notify.flush(conn, args.deliver, args.max)
    if args.deliver and data.get("error") and not data.get("sent"):
        raise Denied("E_OPENCLAW_CALL", "the chat message was not delivered: %s" % data["error"],
                     data=data)
    if not data.get("count") and not data.get("delivered") and not data.get("suppressed"):
        return Result(data=data, code="NOTHING_TO_DO", message="nothing to deliver")
    if data.get("not_sent_reason"):
        return Result(data=data, message="%d not sent by chat: %s" % (data.get("suppressed", 0),
                                                                      data["not_sent_reason"]))
    return Result(data=data, message="%d delivered" % data.get("delivered", 0), human=data.get("text"))


def cmd_test(args, ctx):
    res = notify.send_text("Job Hunter test message (%s). If you can read this, messages reach you." % now(),
                           status.load_config())
    if not res.get("ok"):
        raise Denied("E_OPENCLAW_CALL", "the test message was not delivered: %s" % res.get("error"), data=res)
    return Result(data=res, message="test message delivered via %s" % res.get("channel"))
