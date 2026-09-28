"""Gate and reconcile commands [U1] (design 3.4 "Gate and reconcile", 2.3, 12.7)."""
from __future__ import annotations

from jobhunter import config, db, detect, gate, reconcile
from jobhunter.commands import Result, add_command
from jobhunter.commands.core import agent_of, by_of, contact_id, draft_id, job_id
from jobhunter.errors import Denied, exit_code

KIND_CHOICES = gate.KINDS


def _route_for(cfg: dict, kind: str) -> str:
    if kind in gate.EMAIL_KINDS:
        return gate.email_route(cfg)
    return "browser"


def cmd_precheck_plan(args, ctx):
    conn = ctx.connect(write=False)
    cfg = config.load(conn)
    return gate.precheck_plan(conn, args.kind, route=_route_for(cfg, args.kind), job_id=job_id(conn, args.job),
                              contact_id=contact_id(conn, args.contact), thread_key=args.thread)


def cmd_precheck(args, ctx):
    conn = ctx.connect()
    evidence = ctx.read_json(args.file)
    with db.tx(conn):
        res = gate.record_precheck(conn, args.kind, args.platform, evidence, "agent",
                                   job_id=job_id(conn, args.job), contact_id=contact_id(conn, args.contact),
                                   thread_key=args.thread, cycle_id=ctx.cycle_id)
    if res["result"] == "already_done":
        return Result(data=res, code="E_ALREADY_DONE", message="already done: %s (recorded)" % res.get("why"),
                      next="drop this item and move on")
    if res["result"] == "uncertain":
        return Result(data=res, message="uncertain: %s; do not reserve" % res.get("why"),
                      next="drop this item; a human task was opened")
    return res


def cmd_reserve(args, ctx):
    conn = ctx.connect()
    cfg = config.load(conn)
    agent = agent_of(ctx)
    lane = None
    if ctx.cycle_id:
        row = conn.execute("SELECT lane FROM cycles WHERE cycle_id = ?", (ctx.cycle_id,)).fetchone()
        lane = row[0] if row else None
    # without --cycle, reserve takes the agent's running cycle; duplicate denials count in that cycle too
    cycle = ctx.cycle_id or gate.running_cycle_id(conn, agent)
    try:
        with db.tx(conn):
            return gate.reserve(conn, kind=args.kind, draft_id=draft_id(conn, args.draft), precheck_id=args.precheck,
                                platform=args.platform, agent_id=agent, route=_route_for(cfg, args.kind),
                                job_id=job_id(conn, args.job), contact_id=contact_id(conn, args.contact),
                                thread_key=args.thread, cycle_id=ctx.cycle_id, lane=lane)
    except Denied as d:
        if exit_code(d.code) == 3 and cycle:
            try:
                with db.tx(conn):
                    gate.note_denial(conn, cycle, d)
            except Denied:
                pass
        raise


def cmd_arm(args, ctx):
    conn = ctx.connect()
    text, truncated = ctx.read_text(args.observed_file, max_chars=60000)
    with db.tx(conn):
        return gate.arm(conn, args.token, text, agent_id=ctx.caller.agent_id if ctx.is_agent else None)


def cmd_confirm(args, ctx):
    conn = ctx.connect()
    evidence, _t = ctx.read_text(args.evidence_file)
    ref = None
    if args.platform_ref_file:
        doc = ctx.read_json(args.platform_ref_file)
        if not isinstance(doc, dict) or set(doc) != {"url"} or not str(doc.get("url", "")).startswith("https://"):
            raise Denied("E_SCHEMA", 'platform ref file must be {"url": "https://..."}')
        ref = doc["url"]
    observed = None
    if args.observed_file:
        observed, _t = ctx.read_text(args.observed_file, max_chars=60000)
    with db.tx(conn):
        return gate.confirm(conn, args.token, evidence, post_detect_id=args.post_detect, platform_ref=ref,
                            agent_id=ctx.caller.agent_id if ctx.is_agent else None, observed_text=observed)


def cmd_fail(args, ctx):
    conn = ctx.connect()
    evidence, _t = ctx.read_text(args.evidence_file)
    with db.tx(conn):
        gate.fail(conn, args.token, args.reason, evidence, ctx.caller)
    return {"status": "failed", "token": args.token}


def cmd_unknown(args, ctx):
    conn = ctx.connect()
    note = ""
    if args.note_file:
        note, _t = ctx.read_text(args.note_file)
    with db.tx(conn):
        a = gate.action_by_token(conn, args.token)
        if ctx.is_agent and a["agent_id"] != ctx.caller.agent_id:
            raise Denied("E_NOT_FOUND", "the token belongs to another agent")
        after = detect.error_after_click(a["platform"], note) is not None
        return gate.mark_unknown(conn, args.token, note=note, after_click_error=after)


def cmd_status(args, ctx):
    conn = ctx.connect(write=False)
    agent = args.agent
    if ctx.is_agent and ctx.caller.agent_id and ctx.caller.agent_id.startswith("jobhunter-"):
        agent = ctx.caller.agent_id
    return gate.status_for(conn, agent)


def cmd_reconcile_list(args, ctx):
    conn = ctx.connect(write=False)
    agent = ctx.caller.agent_id if ctx.is_agent else None
    return {"tasks": reconcile.work_list(conn, args.route, agent)}


def cmd_reconcile_resolve(args, ctx):
    conn = ctx.connect()
    detail, _t = ctx.read_text(args.evidence_file)
    with db.tx(conn):
        return reconcile.record_check(conn, args.token, args.method, args.result, detail, by_of(ctx),
                                      agent_id=ctx.caller.agent_id if ctx.is_agent else None)


def cmd_reconcile_not_sent(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return reconcile.confirm_not_sent(conn, args.token, by_of(ctx))


def register(sub):
    def targets(p):
        p.add_argument("--job")
        p.add_argument("--contact")
        p.add_argument("--thread")

    p = add_command(sub, "gate precheck-plan", cmd_precheck_plan, callers="A", help="the checks to run before reserve")
    p.add_argument("--kind", required=True, choices=KIND_CHOICES)
    targets(p)
    p = add_command(sub, "gate precheck", cmd_precheck, callers="A", help="record the precheck evidence")
    p.add_argument("--kind", required=True, choices=KIND_CHOICES)
    p.add_argument("--platform", required=True)
    targets(p)
    p.add_argument("--file", required=True)
    p = add_command(sub, "gate reserve", cmd_reserve, callers="A", help="reserve a send slot (token)")
    p.add_argument("--kind", required=True, choices=KIND_CHOICES)
    p.add_argument("--draft", required=True)
    p.add_argument("--precheck", required=True, type=int)
    p.add_argument("--platform", required=True)
    targets(p)
    p = add_command(sub, "gate arm", cmd_arm, callers="A", help="compare the read-back with the approved text")
    p.add_argument("token")
    p.add_argument("--observed-file", required=True)
    p = add_command(sub, "gate confirm", cmd_confirm, callers="A", help="the page shows it was sent")
    p.add_argument("token")
    p.add_argument("--evidence-file", required=True)
    p.add_argument("--post-detect", type=int)
    p.add_argument("--platform-ref-file")
    p.add_argument("--observed-file", help="web-route email (required there): the Sent-folder read-back "
                   "(readback_text, with its Subject and To lines); a text other than the approved one, or other "
                   "recipients than the reserved address, makes the action unknown instead of sent")
    p = add_command(sub, "gate fail", cmd_fail, callers="A", help="free a reserved slot (listed reasons only)")
    p.add_argument("token")
    p.add_argument("--reason", required=True, choices=gate.AGENT_FAIL_REASONS)
    p.add_argument("--evidence-file", required=True)
    p = add_command(sub, "gate unknown", cmd_unknown, callers="SA", help="the outcome is not known")
    p.add_argument("token")
    p.add_argument("--note-file")
    p = add_command(sub, "gate status", cmd_status, callers="SHRA", help="open token of an agent")
    p.add_argument("--agent")
    p = add_command(sub, "reconcile list", cmd_reconcile_list, callers="SHA", help="checks for unknown actions")
    p.add_argument("--route", choices=("browser", "mailer"))
    p = add_command(sub, "reconcile resolve", cmd_reconcile_resolve, callers="A", help="report one check")
    p.add_argument("token")
    p.add_argument("--result", required=True, choices=("found", "not_found", "unknowable"))
    p.add_argument("--method", required=True, choices=reconcile.AGENT_METHODS)
    p.add_argument("--evidence-file", required=True)
    p = add_command(sub, "reconcile confirm-not-sent", cmd_reconcile_not_sent, callers="H",
                    help="the human confirms it was not sent (PIN)")
    p.add_argument("token")
