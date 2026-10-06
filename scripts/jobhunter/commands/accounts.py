"""Email codes, site accounts and the CAPTCHA hand-off [U1] (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.1, 3.3).

    account status   --job <id>                                   ap S H
    account create   --job <id> --tab <tab> [--token <token>]     ap
    account signin   --job <id> --tab <tab> [--token <token>]     ap
    code expect      --job <id> --tab <tab> --purpose <p> [--token <token>]   ap
    code submit      <otp_req> --tab <tab>                         ap
    code open-link   <otp_req> --tab <tab>                         ap
    code cancel      <otp_req>                                     ap H
    captcha open     --job <id> --tab <tab>                        ap
    captcha status   [--job <id>]                                  ap S H R
    captcha list                                                   S H R
    captcha expire                                                 S
    continue         <code>                                        C H (owner only)
    accounts list                                                  S H
    accounts forget  <host>                                        H

No envelope ever holds a password, a code or a sign-in link: outcomes, ids, site names and reason codes only.
"""
from __future__ import annotations

from jobhunter import accounts, captcha, db, gate, otp
from jobhunter.commands import Result, add_command
from jobhunter.commands.core import by_of
from jobhunter.errors import Denied


def _agent(ctx) -> str:
    return ctx.caller.agent_id if ctx.is_agent else ""


def _cycle(conn, ctx):
    return ctx.cycle_id or (gate.running_cycle_id(conn, _agent(ctx)) if ctx.is_agent else None)


def _next_for(outcome: str) -> str:
    return {
        "verify_code": "run code submit <request_uid> --tab <tab> until it gives an outcome",
        "verify_link": "run code open-link <request_uid> --tab <tab> until it gives an outcome",
        "code_needed": "run code submit <request_uid> --tab <tab> until it gives an outcome",
        "captcha": "the owner was asked to solve the CAPTCHA; leave the tab open and move to the next job",
        "rejected": "the job went to the owner; move to the next job",
        "exists": "the job went to the owner; move to the next job",
    }.get(outcome, "take a snapshot and continue the application")


def cmd_account_status(args, ctx):
    conn = ctx.connect(write=False)
    return accounts.status(conn, job_uid=args.job)


def cmd_account_create(args, ctx):
    conn = ctx.connect()
    res = accounts.create(conn, agent_id=_agent(ctx), cycle_id=_cycle(conn, ctx), job_uid=args.job, tab_id=args.tab,
                          token=args.token)
    _after_captcha(conn, res)
    return Result(data=res, message="account step: %s" % res["outcome"], next=_next_for(res["outcome"]))


def cmd_account_signin(args, ctx):
    conn = ctx.connect()
    res = accounts.signin(conn, agent_id=_agent(ctx), cycle_id=_cycle(conn, ctx), job_uid=args.job, tab_id=args.tab,
                          token=args.token)
    _after_captcha(conn, res)
    return Result(data=res, message="sign-in: %s" % res["outcome"], next=_next_for(res["outcome"]))


def cmd_code_expect(args, ctx):
    conn = ctx.connect()
    return otp.expect(conn, agent_id=_agent(ctx), cycle_id=_cycle(conn, ctx), job_uid=args.job, tab_id=args.tab,
                      purpose=args.purpose, token=args.token)


def _submit(args, ctx, link: bool):
    conn = ctx.connect()
    res = otp.submit(conn, request_uid=args.request, agent_id=_agent(ctx), cycle_id=_cycle(conn, ctx),
                     tab_id=args.tab, link=link)
    if isinstance(res, Result):
        return res
    _after_captcha(conn, res)
    return Result(data=res, message="%s: %s" % ("sign-in link" if link else "email code", res["outcome"]),
                  next=_next_for(res["outcome"]))


def cmd_code_submit(args, ctx):
    return _submit(args, ctx, False)


def cmd_code_open_link(args, ctx):
    return _submit(args, ctx, True)


def cmd_code_cancel(args, ctx):
    conn = ctx.connect()
    return otp.cancel(conn, args.request, _agent(ctx) or None)


def _after_captcha(conn, res: dict) -> None:
    code = res.get("captcha_code") if isinstance(res, dict) else None
    if not code:
        return
    row = conn.execute("SELECT * FROM captcha_tasks WHERE code = ? AND status = 'open'", (code,)).fetchone()
    if row is not None:
        captcha.finish_open(conn, captcha.task_view(row))


def cmd_captcha_open(args, ctx):
    conn = ctx.connect()
    agent = _agent(ctx)
    cyc = _cycle(conn, ctx)
    from jobhunter import cdp
    with db.tx(conn):
        job = conn.execute("SELECT * FROM jobs WHERE job_uid = ?", (args.job,)).fetchone()
        if job is None:
            raise Denied("E_NOT_FOUND", "no job %s" % args.job)
        row = conn.execute("SELECT * FROM captcha_tasks WHERE job_id = ? AND status = 'open'", (job["id"],)).fetchone()
        if row is not None:
            task = captcha.task_view(row)
        else:
            if job["claimed_by"] != cyc or job["status"] not in ("apply_queued", "applying"):
                raise Denied("E_PRECONDITION", "the job is not claimed by this cycle", data={"reason": "not_claimed"})
            tok = conn.execute("SELECT token FROM actions WHERE agent_id = ? AND job_id = ? AND kind = 'application' AND "
                               "status IN ('reserved','armed')", (agent, job["id"])).fetchone()
            url = title = None
            try:
                t = cdp.find_target(args.tab)
                if t is not None:
                    url, title = t["url"], t["title"]
            except Denied:
                pass
            task = captcha.open_task(conn, job_id=job["id"], tab_id=args.tab, token=tok[0] if tok else None,
                                     opened_by="agent", url=url, title=title, cycle_id=cyc)
    if task is None:
        return Result(data={"captcha_code": None, "handoff": False},
                      message="no CAPTCHA task (hand-off off or limits reached); the job went to the owner",
                      next="move to the next job")
    captcha.finish_open(conn, task)
    return Result(data={"captcha_code": task["code"], "deadline_at": task["deadline_at"],
                        "token_outcome": task["token_outcome"]},
                  message="the owner was asked to solve the CAPTCHA", next="leave the tab open; move to the next job")


def cmd_captcha_status(args, ctx):
    conn = ctx.connect(write=False)
    return {"tasks": captcha.status(conn, args.job)}


def cmd_captcha_list(args, ctx):
    conn = ctx.connect(write=False)
    return {"tasks": captcha.status(conn)}


def cmd_captcha_expire(args, ctx):
    conn = ctx.connect()
    return captcha.expire_all(conn)


def cmd_continue(args, ctx):
    conn = ctx.connect()
    res = captcha.continue_task(conn, args.code, by_of(ctx))
    if res.get("resumed"):
        msg = "Thanks. The application to %s continues in the next applier cycle (by %s UTC)." % (
            res["job_uid"], (res.get("next_cycle_at") or "")[11:16])
    else:
        msg = "Thanks. The CAPTCHA is gone; the job is checked by the next applier cycle."
    return Result(data=res, message=msg, human=msg)


def cmd_accounts_list(args, ctx):
    conn = ctx.connect(write=False)
    rows = accounts.accounts_list(conn)
    lines = ["%-9s %-12s %-40s %-15s %s" % (r["account_uid"], r["platform"], r["host"], r["status"],
                                            (r["last_used_at"] or "")[:16]) for r in rows] or ["no site accounts"]
    return Result(data={"accounts": rows}, human="\n".join(lines))


def cmd_accounts_forget(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        res = accounts.forget(conn, args.host, by_of(ctx))
    return Result(data=res, message="Forgot %d account(s). %s" % (len(res["forgotten"]), res["reminder"]))


def register(sub):
    p = add_command(sub, "account status", cmd_account_status, callers="SHA",
                    help="site account and consent state for a job (no secret)")
    p.add_argument("--job", required=True)
    for path, fn, hlp in (("account create", cmd_account_create, "create a site account by code (consent needed)"),
                          ("account signin", cmd_account_signin, "sign in to the site account by code")):
        p = add_command(sub, path, fn, callers="A", help=hlp)
        p.add_argument("--job", required=True)
        p.add_argument("--tab", required=True)
        p.add_argument("--token")
    p = add_command(sub, "code expect", cmd_code_expect, callers="A",
                    help="say a code or sign-in link was just asked for (before the click that sends it)")
    p.add_argument("--job", required=True)
    p.add_argument("--tab", required=True)
    p.add_argument("--purpose", required=True, choices=otp.PURPOSES)
    p.add_argument("--token")
    for path, fn, hlp in (("code submit", cmd_code_submit, "type the emailed code into the tab, by code"),
                          ("code open-link", cmd_code_open_link, "open the emailed sign-in link in the tab, by code")):
        p = add_command(sub, path, fn, callers="A", help=hlp)
        p.add_argument("request")
        p.add_argument("--tab", required=True)
    p = add_command(sub, "code cancel", cmd_code_cancel, callers="AH", help="cancel a code request")
    p.add_argument("request")
    p = add_command(sub, "captcha open", cmd_captcha_open, callers="A",
                    help="hand a CAPTCHA to the owner (the guard usually did it already)")
    p.add_argument("--job", required=True)
    p.add_argument("--tab", required=True)
    p = add_command(sub, "captcha status", cmd_captcha_status, callers="SHRA", help="CAPTCHA tasks")
    p.add_argument("--job")
    add_command(sub, "captcha list", cmd_captcha_list, callers="SHR", help="open and recent CAPTCHA tasks")
    add_command(sub, "captcha expire", cmd_captcha_expire, callers="SH", help="time out CAPTCHA tasks past the deadline")
    p = add_command(sub, "continue", cmd_continue, callers="CH",
                    help="after you solved a CAPTCHA in the agent's browser window (owner only)")
    p.add_argument("code")
    add_command(sub, "accounts list", cmd_accounts_list, callers="SH", help="site accounts the agent created")
    p = add_command(sub, "accounts forget", cmd_accounts_forget, callers="H",
                    help="delete the stored password of the accounts on a host (PIN)")
    p.add_argument("host")
