"""`mail` command group (design 3.4, Email transport) [U9].

    mail connect [--store keychain|file]   H     optional: log in to SMTP and IMAP with a new app password, store it
    mail test                              S H   SMTP and IMAP login check (nothing sent, nothing read)
    mail run [--max-sends 1] [--no-fetch]  S     the mailer: fetch, reconcile, send at most one approved email
    mail import-history --days <n>         H     record past Sent mail as imported actions
    mail audit [--days 2] [--file <f>]     S H A Sent messages to known people or companies without a ledger entry

gmail.route = web_ui (the default) needs no app password: `mail run`, `mail test` and `mail import-history`
answer NOTHING_TO_DO (or queue the work for the browser lane) with `handled_by: browser_lane` instead of
failing, and `mail audit` tells the replies lane when the browser Sent read is due and records it (`--file`).
The app_password route is optional and works as before.
"""
from __future__ import annotations

import getpass
import sys

from ..commands import Result, add_command
from ..errors import Denied


def register(subparsers):
    p = add_command(subparsers, "mail connect", cmd_connect, callers="H",
                    help="optional: connect Gmail with an app password (prompts with echo off)")
    p.add_argument("--store", choices=["keychain", "file"], default=None,
                   help="where the app password is kept (default: the macOS Keychain when available)")
    add_command(subparsers, "mail test", cmd_test, callers="SH", help="test the SMTP and IMAP logins")
    p = add_command(subparsers, "mail run", cmd_run, callers="S", help="the mailer run (cron every 5 minutes)")
    p.add_argument("--max-sends", type=int, default=1, help="at most this many emails in this run (0 to 3)")
    p.add_argument("--no-fetch", action="store_true", help="skip the IMAP fetch of replies")
    p = add_command(subparsers, "mail import-history", cmd_import_history, callers="H",
                    help="record the Sent folder of the last N days as imported actions")
    p.add_argument("--days", type=int, required=True)
    p = add_command(subparsers, "mail audit", cmd_audit, callers="SHA",
                    help="Sent messages to known people or companies that have no ledger entry; on the web_ui "
                         "route also whether the browser Sent read is due, and --file records one")
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--file", default=None,
                   help="web_ui route: a browser read of the Sent folder (purpose audit or history) to record")


def _cfg(conn=None) -> dict:
    from .. import config
    return config.load(conn)


def _read_app_password(ctx) -> str:
    """The app password, typed with echo off (getpass reads the terminal even when stdin carries the PIN)."""
    try:
        pw = getpass.getpass("Google app password (16 letters, spaces are fine): ", stream=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        raise Denied("E_USAGE", "no app password entered")
    if not pw or not pw.strip():
        raise Denied("E_USAGE", "no app password entered")
    return pw


def cmd_connect(args, ctx):
    from .. import mail
    conn = ctx.connect()
    cfg = _cfg(conn)
    account = mail.owner_address(cfg)
    pw = _read_app_password(ctx)
    res = mail.connect(conn, account, pw, store=args.store)
    del pw
    res["route"] = mail.route(cfg)
    msg = "Gmail connected for %s (password kept in the %s)." % (
        res["account"], "Keychain" if res["store"] == "keychain" else "secrets file")
    if res["route"] == mail.ROUTE_WEB:
        msg += (" gmail.route is still web_ui: the browser lane keeps sending, and code uses this connection only "
                "to double-check unknown sends and to read the Sent folder for the audit. Set gmail.route to "
                "app_password in private/config.json if you want code to send.")
    else:
        msg += " If a gmail breaker is open, reset it with ./jobhunter breaker reset gmail."
    return Result(data=res, message=msg)


def cmd_test(args, ctx):
    from .. import mail
    if mail.is_web_route(_cfg()) and not mail.stored_account():
        lane = mail.browser_lane("test")
        return Result(data=dict(lane, smtp_ok=None, imap_ok=None, account=None), code="NOTHING_TO_DO",
                      message=lane["message"])
    res = mail.test_connection()
    if res.get("error"):
        raise Denied("E_PRECONDITION", res["error"], data={k: v for k, v in res.items() if k != "error"})
    if not (res["smtp_ok"] and res["imap_ok"]):
        raise Denied("E_MAIL_TRANSPORT", "Gmail login failed (SMTP %s, IMAP %s)"
                     % ("ok" if res["smtp_ok"] else "failed", "ok" if res["imap_ok"] else "failed"), data=res)
    return Result(data=res, message="SMTP and IMAP logins work for %s" % res["account"])


def cmd_run(args, ctx):
    from ..mail import outbox
    conn = ctx.connect()
    res = outbox.run_once(conn, max_sends=args.max_sends, fetch=not args.no_fetch)
    if res.get("handled_by") == "browser_lane":
        n = len(res.get("reconciled") or [])
        msg = "mailer: %s" % res["blocked"]["message"]
        if n:
            msg += "; %d unknown web sends checked over IMAP" % n
        if res.get("errors"):
            msg += "; the optional IMAP check failed (%s)" % (res["errors"][0].get("message") or "")[:120]
        return Result(data=res, code="OK" if n else "NOTHING_TO_DO", message=msg)
    transport = [e for e in res.get("errors") or [] if e.get("code") == "E_MAIL_TRANSPORT"]
    if transport and not res["sent"]:
        raise Denied("E_MAIL_TRANSPORT", transport[0].get("message") or "Gmail connection failed", data=res)
    if res.get("blocked") and not res["sent"]:
        return Result(data=res, code="NOTHING_TO_DO", message="mailer: %s" % (res["blocked"].get("reason") or
                                                                              res["blocked"].get("scope")))
    busy = res["sent"] or res["fetched"] or res["reconciled"] or res["attempts"]
    return Result(data=res, code="OK" if busy else "NOTHING_TO_DO",
                  message="mailer: %d sent, %d fetched, %d packets" % (len(res["sent"]), res["fetched"],
                                                                       res["packets_written"]))


def cmd_import_history(args, ctx):
    from .. import mail
    from ..mail import audit
    conn = ctx.connect()
    if mail.is_web_route(_cfg(conn)) and not mail.is_connected(conn):
        st = audit.request_web_history(conn, args.days)
        return Result(data=st, code="PENDING",
                      message="gmail.route is web_ui: the outreach agent reads your Gmail Sent folder of the last %d "
                              "days in the browser during its next replies cycles and records it. No app password "
                              "is needed." % args.days)
    res = audit.import_history(conn, args.days)
    return Result(data=res, message="%d addresses from %d Sent messages recorded as imported (%d already known)"
                  % (res["imported"], res["messages"], res["skipped"]))


def cmd_audit(args, ctx):
    from .. import mail
    from ..mail import audit
    if args.days < 1 or args.days > 60:
        raise Denied("E_USAGE", "--days must be between 1 and 60")
    conn = ctx.connect()
    cfg = _cfg(conn)
    if args.file:
        payload = ctx.read_json(args.file)
        res = audit.record_web_read(conn, payload, days=args.days)
        if res["purpose"] == "history":
            return Result(data=res, message="%d addresses from %d Sent messages recorded as imported (%d already "
                                            "known)%s" % (res["imported"], res["messages"], res["skipped"],
                                                          "" if res["complete"] else "; more to read"))
        return Result(data=res, message="%d Sent messages read, %d without a ledger entry%s"
                      % (res["messages"], len(res["unledgered"]),
                         "; everything is stopped until the person looks" if res["tripped"] else ""))
    if ctx.is_agent:
        # an agent never triggers an IMAP read here; it only learns what the browser lane owes
        st = audit.web_status(conn, cfg)
        due = [w for w, on in (("the Sent audit", st["audit_due"]), ("a Sent history read", st["history_scan"]),
                               ("the delivery-failure search", st["bounce_check"])) if on]
        return Result(data=st, code="OK" if due else "NOTHING_TO_DO",
                      message=("due: %s" % " and ".join(due)) if due else st["message"])
    if mail.is_web_route(cfg) and not mail.is_connected(conn):
        st = audit.web_status(conn, cfg)
        try:
            rows = audit.web_sent_since(conn, args.days, cfg)
        except Denied as d:
            st["error"] = d.message
            rows = []
        st["unledgered"] = rows
        return Result(data=st, message="%d Sent messages without a ledger entry in the last browser read (%s)"
                      % (len(rows), st["last_audit_at"] or "none yet"))
    rows = audit.sent_since(conn, args.days)
    return Result(data={"unledgered": rows, "route": mail.route(cfg), "source": "imap"},
                  message="%d Sent messages without a ledger entry" % len(rows))
