"""CLI: QC (design 3.4 and 5.3, [U3]).

    qc lint --draft <id>                                   ap, ou, S, H   0, 6
    qc review start --draft <id>                           ap, ou         0, 6, 11
    qc review wait --job <qjob_uid> [--max <s, max 45>]    ap, ou         0, 6, 9
    qc worker [--drain] [--job <qjob_uid>] [--max-seconds <n>]   S        0, 12
    qc golden [--model <ref>]                              H              0, 1
"""
from __future__ import annotations

from .. import db, drafts
from ..errors import Denied
from ..events import enqueue_notification
from . import Result, add_command


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
    return Result(data=out, message="reviewer agreement %s with the golden labels" % out["agreement"],
                  human="Reviewer agreement: %s (need %s)" % (out["agreement"], "18/20"))
