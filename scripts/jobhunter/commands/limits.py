"""Limits, pacing, detection, breakers, pause, lanes and dispatch commands [U1] (design 3.4)."""
from __future__ import annotations

from jobhunter import breakers, ceilings, config, cycles, db, detect, dispatch, gate, identity, pacing
from jobhunter.commands import Result, add_command
from jobhunter.commands.core import agent_of, by_of
from jobhunter.errors import CYCLE_DONE, FINAL_WORD_HINT, Denied

LANES = ("scout", "evaluator", "applier", "outreach", "replies")


def _cycle_of(conn, ctx) -> str | None:
    """--cycle, or for an agent caller its running cycle, so per-cycle counts and LinkedIn cycle detection
    see every read, gauge and page check even when the agent left --cycle out."""
    if ctx.cycle_id:
        return ctx.cycle_id
    return gate.running_cycle_id(conn, agent_of(ctx)) if ctx.caller.cls == "agent" else None


def cmd_budget(args, ctx):
    conn = ctx.connect(write=False)
    res = ceilings.budget(conn, args.platform, args.kind, cycle_id=_cycle_of(conn, ctx))
    if not args.platform and not args.kind:
        enrich = _enrich_summary(conn)
        if enrich is not None:
            res = dict(res, enrich=enrich)
    return res


def _enrich_summary(conn):
    """The optional email finder's credit view (U10 enrich.budget.summary), or None when it is not installed.
    A summary that fails is reported, never raised: the budget view must always answer."""
    try:
        from jobhunter.enrich import budget as enrich_budget
    except ImportError:
        return None
    fn = getattr(enrich_budget, "summary", None)
    if fn is None:
        return None
    try:
        return fn(conn)
    except Exception as exc:   # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, getattr(exc, "message", exc))}


def cmd_pace_wait(args, ctx):
    conn = ctx.connect()
    return pacing.pace_wait(conn, args.platform, args.kind, agent_id=agent_of(ctx))


def cmd_usage_add(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        breakers.check_breakers(conn, breakers.scopes_for(args.platform, args.metric))
        identity.require_consent(conn, args.platform)     # a page of a login site needs the owner's consent
        return ceilings.usage_add(conn, args.platform, args.metric, args.n or 1, _cycle_of(conn, ctx))


def cmd_usage_gauge(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return ceilings.usage_gauge(conn, args.platform, args.metric, args.value, _cycle_of(conn, ctx))


def cmd_detect(args, ctx):
    source = args.source or "agent"
    if source == "guard" and ctx.caller.cls not in ("system", "human"):
        raise Denied("E_CALLER_NOT_ALLOWED", "only the guard (system caller) reports --source guard")
    payload = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        res = detect.detect(conn, payload, source, _cycle_of(conn, ctx),
                            agent_id=ctx.caller.agent_id if ctx.is_agent else None)
    if res.get("captcha"):
        from jobhunter import captcha
        captcha.finish_open(conn, res["captcha"])
        res["captcha"] = {k: res["captcha"][k] for k in ("captcha_code", "deadline_at", "token_outcome")}
        return Result(data=res, message="a CAPTCHA: the owner was asked to solve it (code %s)"
                      % res["captcha"]["captcha_code"], next="leave the tab open; move to the next job")
    if res.get("flow"):
        return Result(data=res, message="clear (%s step: account status, then the code steps)" % res["flow"])
    if res["verdict"] == "stop" and res["tripped"]:
        return Result(data=res, code="E_STOP_DETECTED", message="stop signature %s: %s is stopped" %
                      (res["matched"], res["scope"]), next="end the cycle now and " + FINAL_WORD_HINT)
    if res["verdict"] == "stop":
        return Result(data=res, message="this page needs you (%s)" % (res["job_needs_human"] or res["matched"]),
                      next="set the job needs_human and move on")
    return res


def cmd_identity(args, ctx):
    doc = ctx.read_json(args.file)
    if not isinstance(doc, dict) or set(doc) != {"platform", "observed"} or doc.get("platform") != args.platform:
        raise Denied("E_SCHEMA", 'identity file: {"platform": "%s", "observed": {...}}' % args.platform)
    conn = ctx.connect()
    with db.tx(conn):
        identity.require_consent(conn, args.platform)
        return identity.identity_check(conn, args.platform, doc["observed"], cycle_id=ctx.cycle_id)


def cmd_breaker_trip(args, ctx):
    detail = args.reason_code
    if args.detail_file:
        detail, _t = ctx.read_text(args.detail_file)
    if not breakers.valid_scope(args.scope) or args.scope.startswith("pause:"):
        raise Denied("E_VALIDATION", "unknown breaker scope %r" % args.scope)
    conn = ctx.connect()
    with db.tx(conn):
        res = breakers.trip(conn, args.scope, args.reason_code, detail, by=by_of(ctx), cycle_id=ctx.cycle_id)
    return res


def cmd_breaker_status(args, ctx):
    conn = ctx.connect(write=False)
    return {"breakers": breakers.status(conn), "paused": breakers.paused_info()}


def cmd_breaker_reset(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return breakers.reset(conn, args.scope, args.note, by="human")


def cmd_pause(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return breakers.pause(conn, args.scope or "all", args.reason, by=by_of(ctx))


def cmd_unpause(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return breakers.unpause(conn, args.scope or "all", by="human")


def cmd_preflight(args, ctx):
    conn = ctx.connect()
    agent = agent_of(ctx)
    with db.tx(conn):
        res = cycles.preflight(conn, args.lane, agent, env=ctx.env)
    ctx.cycle_id = res["cycle_id"]
    if not res["go"]:
        return Result(data=res, code=res["code"], message="no go: %s" % ", ".join(res["reasons"]),
                      retry_after_s=res.get("retry_after_s"), next="reply " + CYCLE_DONE)
    return Result(data=res, message="go")


def cmd_lock_renew(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return cycles.renew(conn, args.cycle, ctx.caller.agent_id if ctx.is_agent else None)


def cmd_cycle_end(args, ctx):
    summary = None
    if args.summary_file:
        summary = ctx.read_json(args.summary_file)
        if not isinstance(summary, dict) or set(summary) - {"counts", "notes"}:
            raise Denied("E_SCHEMA", 'summary file: {"counts": {...}, "notes": "..."}')
    conn = ctx.connect()
    with db.tx(conn):
        res = cycles.end(conn, args.cycle, summary, ctx.caller.agent_id if ctx.is_agent else None)
    return Result(data=res, message="cycle ended", next="reply " + CYCLE_DONE)


def cmd_dispatch_tick(args, ctx):
    conn = ctx.connect()
    res = dispatch.tick(conn)
    if res.get("failures"):
        return Result(data=res, code="E_OPENCLAW_CALL", message="openclaw cron run failed for %s"
                      % ", ".join(f["lane"] for f in res["failures"]))
    return res


def cmd_dispatch_plan(args, ctx):
    conn = ctx.connect()
    cfg = config.load(conn)
    date = args.date or config.local_date(None, cfg)
    with db.tx(conn):
        dispatch.plan_day(conn, date, cfg)
    return {"date": date, "slots": dispatch.slots(conn, date)}


def register(sub):
    p = add_command(sub, "budget", cmd_budget, callers="SHRA", help="ceilings and usage")
    p.add_argument("--platform")
    p.add_argument("--kind")
    p = add_command(sub, "pace wait", cmd_pace_wait, callers="A", help="wait out the pacing gap (at most 50 s)")
    p.add_argument("--platform", required=True)
    p.add_argument("--kind", required=True, choices=pacing.WAIT_KINDS)
    p = add_command(sub, "usage add", cmd_usage_add, callers="A", help="count a page view or search")
    p.add_argument("--platform", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--n", type=int)
    p = add_command(sub, "usage gauge", cmd_usage_gauge, callers="A", help="record a platform gauge")
    p.add_argument("--platform", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--value", required=True, type=int)
    p = add_command(sub, "detect", cmd_detect, callers="SA", help="check a page for stop signatures")
    p.add_argument("--file", required=True)
    p.add_argument("--source", choices=("agent", "guard"))
    p = add_command(sub, "identity check", cmd_identity, callers="A", help="the browser account is the owner's")
    p.add_argument("--platform", required=True, choices=("gmail", "linkedin"))
    p.add_argument("--file", required=True)
    p = add_command(sub, "breaker trip", cmd_breaker_trip, callers="SHA", help="stop an area")
    p.add_argument("--scope", required=True)
    p.add_argument("--reason-code", required=True)
    p.add_argument("--detail-file")
    add_command(sub, "breaker status", cmd_breaker_status, callers="SHRA", help="open and closed breakers")
    p = add_command(sub, "breaker reset", cmd_breaker_reset, callers="H", help="close a breaker (PIN)")
    p.add_argument("--scope", required=True)
    p.add_argument("--note", required=True)
    p = add_command(sub, "pause", cmd_pause, callers="SHC", help="pause everything or one area")
    p.add_argument("--scope")
    p.add_argument("--reason")
    p = add_command(sub, "unpause", cmd_unpause, callers="H", help="resume (PIN)")
    p.add_argument("--scope")
    p = add_command(sub, "preflight", cmd_preflight, callers="A", help="start a lane cycle")
    p.add_argument("--lane", required=True, choices=LANES)
    p = add_command(sub, "lock renew", cmd_lock_renew, callers="A", help="renew the browser lease")
    p.add_argument("--cycle", required=True)
    p = add_command(sub, "cycle end", cmd_cycle_end, callers="A", help="end a cycle and release its leases")
    p.add_argument("--cycle", required=True)
    p.add_argument("--summary-file")
    add_command(sub, "dispatch tick", cmd_dispatch_tick, callers="S", help="start due lane cycles")
    p = add_command(sub, "dispatch plan", cmd_dispatch_plan, callers="SH", help="the day plan")
    p.add_argument("--date")
