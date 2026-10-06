"""CLI: QC (design 3.4 and 5.3, [U3]).

    qc lint --draft <id>                                   ap, ou, S, H   0, 6
    qc review start --draft <id>                           ap, ou         0, 6, 11
    qc review wait --job <qjob_uid> [--max <s, max 45>]    ap, ou         0, 6, 9
    qc worker [--drain] [--job <qjob_uid>] [--max-seconds <n>]   S        0, 12
    qc golden [--model <ref>]                              H              0, 1
    qc smoke                                               S, H           0, 12

`qc smoke` (CLI-ROUTE-DESIGN 9, install step 14) runs one jobhunter-qc turn, exactly as reviews run, on a real
review packet: the reviewer prompt filled in with a fictional cold email (SMOKE_ITEM) that breaks most rules, so
the verdict lists many claims, AI tells and issues and runs well past the 2000 characters OpenClaw 2026.9.8 keeps
in the run record (D13). It passes when the reply is a valid verdict for that packet (review.parse_verdict, nonce
and draft sha256 echoed) and, when it is read from the run record, longer than that cap. The jobhunter-qc role
answers review packets only (D14), so a test message of any other shape is refused by design. When the run
finished but its verdict could not be read from the run record (V13: no text, a text cut at the cap with or
without U+2026, or a valid verdict too short to show the record keeps long replies), it sets cli_route.qc_reply
to "file" (fallback F-QC) and tries once more. The switch stays even when that try fails too: the run record
cannot carry the reply, and install step 14 then applies the F-QC agent and guard config (write tool for
jobhunter-qc, its verdict file only) and runs `qc smoke` again (data.switched tells it so).
"""
from __future__ import annotations

import secrets

from .. import db, drafts, paths
from ..errors import Denied
from ..events import enqueue_notification
from . import Result, add_command

# The smoke packet: a fictional cold email (every name, company and URL is made up) with many claims the facts do
# not support, several hooks, AI-voice phrases and a salary and referral ask. The reviewer has to list and quote
# each one, so its verdict is several thousand characters long (the run record keeps 2000).
SMOKE_ITEM = {
    "channel": "email_cold",
    "subject": "Empty miles, pricing and a quick favour",
    "body": ("Hi Morgan,\n\nI hope this finds you well. I came across your profile and I am thrilled to reach out "
             "about the pivotal work your team is doing.\n\nYour post on route planning said Harbor Lane Freight cut "
             "empty miles by 30% in 2025. Congratulations on the Series C round you closed in March, and I noticed we "
             "both studied at Lakemont College.\n\nAt Copperline Logistics I led a team of 12 analysts and owned the "
             "company-wide pricing model. I cut fuel costs by 40% in one quarter and saved the business over 5 million "
             "dollars. I also built the dispatch dashboard that 300 drivers use every day, and I was promoted twice in "
             "two years.\n\nAs a passionate, results-driven leader, I can leverage my robust skill set to deliver "
             "seamless, cutting-edge insights for your team. Ultimately, I am confident I would be a great fit for "
             "your Senior Analyst opening, and I would also love a referral and to hear the salary range.\n\nWould "
             "you have 30 minutes this week, or next week, or the week after, for a call, a coffee chat and a quick "
             "look at my portfolio?\n\nBest,"),
    "recipient": {"first_name": "Morgan", "last_name": "Vale", "title": "Director of Analytics",
                  "company": "Harbor Lane Freight", "locale": "US"},
    "research_facts": {
        "R1": {"text": "Harbor Lane Freight engineering blog (team post): route planning changes reduced empty miles "
                       "by about 12% in 2025.",
               "snippet": "reduced empty miles by about 12% in 2025", "source_type": "engineering_blog",
               "source_url": "https://harborlane.example/blog/route-planning", "published_at": "2026-02-03",
               "retrieved_at": "2026-02-10"},
        "R2": {"text": "Harbor Lane Freight job post, Senior Analyst: owns pricing and route analytics.",
               "snippet": "Senior Analyst: owns pricing and route analytics", "source_type": "job_post",
               "source_url": "https://jobs.example.com/harborlane/senior-analyst", "published_at": "2026-02-05",
               "retrieved_at": "2026-02-10"}},
    "profile_facts": {
        "P1": "At Copperline Logistics, worked on the pricing model with two other analysts; fuel cost per route fell "
              "about 8% over a year.",
        "P2": "3 years as a data analyst at Copperline Logistics.",
        "P3": "Built a dispatch report used by the Copperline operations team."},
}

def register(subparsers):
    p = add_command(subparsers, "qc lint", cmd_lint, callers="ASH", help="re-lint a stored draft (nothing is written)")
    p.add_argument("--draft", required=True)
    p = add_command(subparsers, "qc review start", cmd_review_start, callers="A",
                    help="queue the independent review of a lint-clean draft")
    p.add_argument("--draft", required=True)
    p = add_command(subparsers, "qc review wait", cmd_review_wait, callers="A",
                    help="wait up to 45 s for a review verdict")
    p.add_argument("--job", required=True)
    p.add_argument("--max", type=int, default=45)
    p = add_command(subparsers, "qc worker", cmd_worker, callers="S", help="run queued reviews (system job)")
    p.add_argument("--drain", action="store_true")
    p.add_argument("--job")
    p.add_argument("--max-seconds", type=int, default=250)
    p = add_command(subparsers, "qc golden", cmd_golden, callers="H",
                    help="calibrate the reviewer on the golden set (owner PIN)")
    p.add_argument("--model")
    add_command(subparsers, "qc smoke", cmd_smoke, callers="SH",
                help="one jobhunter-qc test turn (install step 14); picks how the reply is read")


def _scope(ctx, row) -> None:
    if ctx.caller.cls == "agent" and row["kind"] not in drafts.AGENT_KINDS.get(ctx.caller.agent_id or "", ()):
        raise Denied("E_NOT_FOUND", "no such draft for this agent")


def cmd_lint(args, ctx):
    conn = ctx.connect(write=False)
    row = drafts.get(conn, args.draft)
    _scope(ctx, row)
    res = drafts.run_lint(conn, row)
    data = {"draft_uid": row["draft_uid"], "pass": res["pass"], "blocks": res["blocks"], "warns": res["warns"],
            "metrics": res["metrics"]}
    if not res["pass"]:
        return Result(data=data, code="E_QC_LINT_FAILED", message="lint failed",
                      next="fix every block and run draft revise")
    return Result(data=data, message="lint passed")


def cmd_review_start(args, ctx):
    from ..qc import review, worker
    conn = ctx.connect()
    try:
        with db.tx(conn):
            row = drafts.get(conn, args.draft)
            _scope(ctx, row)
            out = review.start(conn, args.draft)
    except Denied as d:
        if d.code == "E_REVIEWER_TAMPERED":
            with db.tx(conn):
                enqueue_notification(conn, "reviewer_tampered:%s" % db.now()[:13], "high", "alert",
                                     "QC reviews stopped: the reviewer prompt or the QC agent's AGENTS.md does not "
                                     "match the installed hashes. Run ./jobhunter doctor.")
        raise
    if out.get("state") == "queued" and not out.get("reused"):
        worker.spawn_worker(out["qjob_uid"])
    return Result(data=out, message="review %s is %s" % (out.get("qjob_uid"), out.get("state")),
                  next="run qc review wait --job %s --max 45; do other work while it is PENDING" % out.get("qjob_uid"))


def cmd_review_wait(args, ctx):
    from ..qc import review
    conn = ctx.connect(write=False)
    job = conn.execute("SELECT draft_id FROM qc_jobs WHERE qjob_uid = ?", (args.job,)).fetchone()
    if job is None:
        raise Denied("E_NOT_FOUND", "unknown QC job %s" % args.job)
    _scope(ctx, drafts.get_by_id(conn, job[0]))
    st = review.wait(conn, args.job, max(0, min(int(args.max), 45)))
    if st["state"] in ("queued", "running"):
        return Result(data=st, code="PENDING", message="the review is still %s" % st["state"],
                      next="call qc review wait --job %s again later" % st["qjob_uid"])
    if st["state"] in ("failed", "timeout"):
        return Result(data=st, code="E_QC_REVIEW_FAILED", message="the reviewer did not answer (not a verdict)",
                      next="leave the draft; run qc review start --draft %s in a later cycle" % st["draft_uid"])
    if st.get("verdict") == "pass":
        return Result(data=st, message="the review passed; draft is %s" % st["draft_status"],
                      next="approved items are sent by the mailer or the gate; awaiting_approval waits for the person")
    if st["draft_status"] == "dropped_qc":
        return Result(data=st, code="E_QC_BUDGET_EXHAUSTED", message="the review failed on the last attempt; dropped",
                      next="drop this item and move on")
    return Result(data=st, code="E_QC_REVIEW_FAILED", message="the review failed",
                  next="rewrite with data.issues and data.rewrite_brief (no new facts), then draft revise %s"
                  % st["draft_uid"])


def cmd_worker(args, ctx):
    from ..qc import worker
    if args.job:
        out = worker.run_job(args.job)
        return {"ran": out.get("ran") or [out]}
    if not args.drain:
        raise Denied("E_USAGE", "give --drain or --job <qjob_uid>")
    out = worker.drain(max(0, int(args.max_seconds)))
    if not out["ran"] and not out["recovered"]:
        return Result(data=out, code="NOTHING_TO_DO", message="no queued reviews")
    return out


def cmd_golden(args, ctx):
    from ..qc import worker
    out = worker.golden(model=args.model)
    conn = ctx.connect()
    with db.tx(conn):
        db.meta_set(conn, "golden_last", out["golden_last"], "human")
    human = "Reviewer agreement: %s (need %s)" % (out["agreement"], "18/20")
    if out.get("errors"):
        human += "\n%d replies could not be read or parsed (%d cut by the run record); they count as " \
                 "disagreements, so fix the reply route first (./install.sh --smoke)" % (out["errors"], out.get("cut", 0))
    if out.get("disagreements"):
        human += "\nDisagreements:\n" + "\n".join("  " + d for d in out["disagreements"])
    return Result(data=out, message="reviewer agreement %s with the golden labels" % out["agreement"], human=human)


# ---------------------------------------------------------------- qc smoke
def smoke_packet(nonce: str) -> tuple:
    """(packet text, draft sha256) of the smoke test: SMOKE_ITEM through the real reviewer prompt, reviewed today."""
    from ..qc import review, worker
    return worker.golden_packet(SMOKE_ITEM, nonce, review.today())


def smoke_check(res, nonce: str, sha: str) -> dict:
    """What one smoke reply shows: {valid (a verdict for this packet, parse_verdict), long (longer than the run
    record's cap), cut (cut by the run record: with U+2026 (qc._run_turn), or a text that stops at the cap and
    does not parse), chars, error}."""
    from .. import qc
    from ..qc import review
    out = {"valid": False, "long": False, "cut": False, "chars": 0, "error": None}
    if not isinstance(res, dict):
        return dict(out, error="bad reviewer result")
    if res.get("cut"):
        out["cut"] = True
    text = str(res.get("text") or "")
    out["chars"] = qc.js_len(text.strip())
    if not res.get("ok"):
        return dict(out, error=str(res.get("error") or "the reviewer turn failed"))
    if qc.reply_cut(text):
        return dict(out, cut=True, error="the reply was cut at %d characters by the run record" % qc.RUN_TEXT_CAP)
    parsed = review.parse_verdict(text, nonce, sha)
    if parsed["ok"]:
        return dict(out, valid=True, long=out["chars"] > qc.RUN_TEXT_CAP)
    if qc.RUN_TEXT_CAP - qc.RUN_TEXT_CUT_SLACK <= out["chars"] <= qc.RUN_TEXT_CAP + 1:
        out["cut"] = True
    return dict(out, error="the reply is not a valid verdict for the test packet (%s)" % parsed["error"])


def _smoke_turn(timeout_s: int) -> tuple:
    """One reviewer turn on the smoke packet (written with the review packets, mode 600): (result, check)."""
    from .. import qc
    from ..qc import review
    nonce = secrets.token_hex(8)
    text, sha = smoke_packet(nonce)
    path = review.write_packet("smoke-" + nonce, text)
    try:
        res = qc.agent_turn(qc.QC_AGENT, "smoke-" + nonce, path, timeout_s)
    except Denied as d:
        res = {"ok": False, "text": None, "raw": "", "error": "%s: %s" % (d.code, d.message)}
    finally:
        review.remove_packet(path)
    return res, smoke_check(res, nonce, sha)


def _smoke_try(mode: str, res, chk: dict) -> dict:
    return {"reply_mode": mode, "ok": bool(res.get("ok")), "empty": bool(res.get("empty")), "cut": chk["cut"],
            "valid": chk["valid"], "long": chk["long"], "chars": chk["chars"]}


def set_qc_reply(value: str) -> None:
    """cli_route.qc_reply in private/home.json, through the installer's helper (U7 install.set_cli_route) when
    it is there, else as an atomic merge that keeps every other key."""
    from .. import install as _install
    from ..qc import QC_REPLY_MODES
    if value not in QC_REPLY_MODES:
        raise Denied("E_VALIDATION", "qc_reply must be one of %s" % ", ".join(QC_REPLY_MODES))
    helper = getattr(_install, "set_cli_route", None)
    if callable(helper):
        helper(qc_reply=value)
        return
    h = paths.home()
    route = dict(h.get("cli_route")) if isinstance(h.get("cli_route"), dict) else {}
    route["qc_reply"] = value
    h["cli_route"] = route
    _install.write_json(paths.home_file(), h)


def cmd_smoke(args, ctx):
    from .. import qc
    timeout = int(qc.settings()["qc"]["review"]["timeout_s"])
    first_mode = qc.qc_reply_mode()
    res, chk = _smoke_turn(timeout)
    tries = [_smoke_try(first_mode, res, chk)]
    passed = chk["valid"] and (first_mode == "file" or chk["long"])
    switched = False
    # run mode: a reply the run record lost or cut, or a valid verdict too short to show the record keeps long
    # replies, moves the reviewer to the verdict file (F-QC), which no run record cap can cut
    if not passed and first_mode == "run" and (res.get("empty") or chk["cut"] or chk["valid"]):
        set_qc_reply("file")
        switched = True
        res, chk = _smoke_turn(timeout)
        tries.append(_smoke_try("file", res, chk))
        passed = chk["valid"]
    mode = qc.qc_reply_mode()
    text = str(res.get("text") or "")
    data = {"reply_mode": mode, "switched": switched, "tries": tries, "reply_chars": chk["chars"],
            "reply": text[:80] + (" ... " + text[-40:] if len(text) > 120 else text[80:])}
    where = "the verdict file (F-QC)" if mode == "file" else "the run record"
    if passed:
        return Result(data=data, message="the jobhunter-qc test review returned a whole, valid verdict; replies are "
                                         "read from %s" % where,
                      human="QC reviewer: OK (replies are read from %s)" % where)
    err = str(chk["error"] or res.get("error") or "the reviewer reply could not be read")[:300]
    data["error"] = err
    if switched:
        return Result(data=data, code="E_OPENCLAW_CALL",
                      message="the jobhunter-qc reply cannot be read from the run record, so replies now go to a "
                              "verdict file, and that test turn failed too: %s" % err,
                      next="run ./install.sh again: it applies the verdict-file settings and repeats this test")
    return Result(data=data, code="E_OPENCLAW_CALL", message="the jobhunter-qc test turn failed: %s" % err,
                  next="run ./jobhunter doctor; docs/TROUBLESHOOTING.md explains the QC reviewer checks")
