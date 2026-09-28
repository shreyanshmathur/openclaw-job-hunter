"""`sheet connect|ping|sync|format` (design 3.4, U5)."""
from __future__ import annotations

import getpass

from jobhunter import sheets
from jobhunter import sheets_labels as L
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied


def register(subparsers):
    add_command(subparsers, "sheet connect", cmd_connect, callers="H",
                help="connect your Google Sheet (asks for the web app URL and the secret)")
    add_command(subparsers, "sheet ping", cmd_ping, callers="SH", help="check the Google Sheet connection")
    p = add_command(subparsers, "sheet sync", cmd_sync, callers="SH", help="update the Google Sheet mirror")
    p.add_argument("--full", action="store_true", help="resend every row")
    p.add_argument("--dry-run", action="store_true", help="build the rows without sending them")
    p.add_argument("--tab", choices=list(L.TABLE_ORDER), help="only this tab")
    add_command(subparsers, "sheet format", cmd_format, callers="SH", help="re-apply the sheet formatting")


def _tty_line(prompt: str) -> str:
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip()
    except OSError:
        raise Denied("E_PRECONDITION", "run ./jobhunter sheet connect in a terminal window")


def _tty_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except (OSError, EOFError):
        raise Denied("E_PRECONDITION", "run ./jobhunter sheet connect in a terminal window")


PROMPT_LINE = _tty_line
PROMPT_SECRET = _tty_secret


def cmd_connect(args, ctx):
    url = PROMPT_LINE("Paste the web app URL (it ends in /exec): ")
    secret = PROMPT_SECRET("Paste the connection secret (it stays hidden): ")
    conn = ctx.connect()
    data = sheets.connect(conn, url, secret)
    human = "Connected. The sheet uses time zone %s and the first full sync is done." % (data.get("tz") or "unknown")
    return Result(data=data, message=human, human=human)


def cmd_ping(args, ctx):
    data = sheets.ping()
    human = "The sheet answered: %s, script version %s, time zone %s." % (data.get("app"), data.get("schema_version"),
                                                                           data.get("tz"))
    return Result(data=data, message=human, human=human)


def cmd_sync(args, ctx):
    conn = ctx.connect()
    data = sheets.sync(conn, full=args.full, dry_run=args.dry_run, tab=args.tab)
    if data.get("skipped"):
        return Result(data=data, code="NOTHING_TO_DO", message="sheet sync skipped: %s" % data["skipped"])
    n = sum((data.get("pushed") or data.get("built") or {}).values())
    msg = "%s %d rows, %d deleted, %d of your edits applied" % (
        "Would send" if data.get("dry_run") else "Sent", n, int(data.get("deleted") or data.get("deletes") or 0),
        int(data.get("edits_applied") or 0))
    return Result(data=data, message=msg)


def cmd_format(args, ctx):
    return Result(data=sheets.format_sheet(), message="formatting re-applied")
