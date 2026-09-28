"""CLI: drafts and approvals (design 3.4, group "Drafts, QC, approvals" [U3]).

    draft create --file <draft.json>                      ap, ou          0, 3, 6, 7, 10
    draft revise <draft_uid> --file <draft.json>          ap, ou          0, 6, 10
    draft show <draft_uid or code> [--field ...]          ap, ou, S, H    0, 9
    draft list [--status <s>]                             ap, ou, S, H    0
    approvals list                                        ap, ou, S, H, R 0
    approve <code> [--by chat|cli]                        H, C            0, 6, 9
    skip <code> [--reason <text>]                         H, C            0, 9
    edit <code> --text-file <f> (H) | --text <text> (C)   H, C            0, 6
"""
from __future__ import annotations

import json

from .. import approvals, db, drafts, jobstate
from ..errors import Denied
from . import Result, add_command

STATUSES = jobstate.DRAFT_STATUSES


def register(subparsers):
    p = add_command(subparsers, "draft create", cmd_create, callers="A",
                    help="store a writer draft, run the dedup pre-check and lint")
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "draft revise", cmd_revise, callers="A", help="rewrite a draft after a QC failure")
    p.add_argument("draft", metavar="<draft_uid>")
    p.add_argument("--file", required=True)
    p = add_command(subparsers, "draft show", cmd_show, callers="ASH", help="show one draft")
    p.add_argument("draft", metavar="<draft_uid or code>")
    p.add_argument("--field", choices=("subject", "body", "send_text", "form"))
    p = add_command(subparsers, "draft list", cmd_list, callers="ASH", help="list drafts")
    p.add_argument("--status", choices=STATUSES)
    add_command(subparsers, "approvals list", cmd_approvals, callers="ASHR", help="drafts that wait for the person")
    p = add_command(subparsers, "approve", cmd_approve, callers="HC", help="approve a draft by its code")
    p.add_argument("code", metavar="<code>")
    p.add_argument("--by", choices=("chat", "cli"))
    p = add_command(subparsers, "skip", cmd_skip, callers="HC", help="skip a draft by its code")
    p.add_argument("code", metavar="<code>")
    p.add_argument("--reason")
    p = add_command(subparsers, "edit", cmd_edit, callers="HC", help="replace a draft's text with your own")
    p.add_argument("code", metavar="<code>")
    p.add_argument("--text-file")
    p.add_argument("--text")


def _agent_kinds(ctx):
    if ctx.caller.cls == "agent":
        return drafts.AGENT_KINDS.get(ctx.caller.agent_id or "", ())
    return None


def _check_agent_scope(ctx, row) -> None:
    kinds = _agent_kinds(ctx)
    if kinds is not None and row["kind"] not in kinds:
        raise Denied("E_NOT_FOUND", "no such draft for this agent")


def cmd_create(args, ctx):
    data = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        out = drafts.create_draft(conn, data, ctx.cycle_id, ctx.caller)
    out.pop("draft_id", None)
    if out["status"] == "dropped_qc":
        return Result(data=out, code="E_QC_BUDGET_EXHAUSTED", message="lint failed and no rewrite is allowed",
                      next="drop this item and move on")
    if not out["lint"]["pass"]:
        return Result(data=out, code="E_QC_LINT_FAILED", message="the draft failed lint",
                      next="fix every block in data.lint.blocks and run draft revise %s --file <f>" % out["draft_uid"])
    return Result(data=out, message="draft %s stored and lint passed" % out["draft_uid"],
                  next="run qc review start --draft %s" % out["draft_uid"])


def cmd_revise(args, ctx):
    data = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        row = drafts.get(conn, args.draft)
        _check_agent_scope(ctx, row)
        out = drafts.revise_draft(conn, args.draft, data)
    if out["status"] == "dropped_qc":
        return Result(data=out, code="E_QC_BUDGET_EXHAUSTED", message="the rewrite budget is used up; draft dropped",
                      next="drop this item and move on")
    if not out["lint"]["pass"]:
        return Result(data=out, code="E_QC_LINT_FAILED", message="the rewrite failed lint",
                      next="fix every block and run draft revise again (attempts left: %d)" % out["attempts_left"])
    return Result(data=out, message="revision %d stored and lint passed" % out["attempt"],
                  next="run qc review start --draft %s" % out["draft_uid"])


def cmd_show(args, ctx):
    conn = ctx.connect(write=False)
    row = drafts.get(conn, args.draft)
    _check_agent_scope(ctx, row)
    if args.field:
        value = drafts.field(conn, row, args.field)
        out = {"draft_uid": row["draft_uid"], "field": args.field, "value": value}
        # --human prints only the raw value (./jobhunter edit writes it to a file for $EDITOR)
        text = value if isinstance(value, str) else \
            ("" if value is None else json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True))
        return Result(data=out, human=text)
    return drafts.summary(conn, row, with_text=True)


def cmd_list(args, ctx):
    conn = ctx.connect(write=False)
    return {"drafts": drafts.list_drafts(conn, args.status, _agent_kinds(ctx))}


def cmd_approvals(args, ctx):
    conn = ctx.connect(write=False)
    items = approvals.pending(conn)
    human = "\n".join("%s  %s to %s (%s)  QC %s  expires %s" % (
        i["code"], i["kind"], i["to"], i["company"],
        ("%.2f" % i["qc_score"]) if i["qc_score"] is not None else "n/a", i["expires_at"]) for i in items)
    return Result(data={"pending": items}, message="%d waiting for you" % len(items),
                  human=human or "Nothing waits for your approval.")


def _by(ctx, flag) -> str:
    cls = ctx.caller.cls
    by = {"chat": "human:chat", "human": "human:cli"}.get(cls)
    if by is None:
        raise Denied("E_CALLER_NOT_ALLOWED", "only the person can decide on a draft")
    if flag and {"chat": "human:chat", "cli": "human:cli"}[flag] != by:
        raise Denied("E_USAGE", "--by %s does not match how you are signed in" % flag)
    return by


def cmd_approve(args, ctx):
    by = _by(ctx, args.by)
    conn = ctx.connect()
    with db.tx(conn):
        out = approvals.approve(conn, args.code, by)
    return Result(data=out, message=out["message"], human=out["message"])


def cmd_skip(args, ctx):
    by = _by(ctx, None)
    conn = ctx.connect()
    with db.tx(conn):
        out = approvals.skip(conn, args.code, args.reason or "", by)
    return Result(data=out, message=out["message"], human=out["message"])


def cmd_edit(args, ctx):
    by = _by(ctx, None)
    if bool(args.text_file) == bool(args.text):
        raise Denied("E_USAGE", "give exactly one of --text-file (terminal) or --text (chat)")
    if args.text_file:
        text, truncated = ctx.read_text(args.text_file, max_chars=6000)
        if truncated:
            raise Denied("E_VALIDATION", "the edit is longer than 6000 characters")
    else:
        text = args.text
    conn = ctx.connect()
    with db.tx(conn):
        out = approvals.human_edit(conn, args.code, text, by)
    msg = " ".join(out.get("messages") or [])
    if out.get("qjob_uid"):
        from ..qc import worker
        worker.spawn_worker(out["qjob_uid"])
        return Result(data=out, message=msg, human=msg)
    return Result(data=out, code="E_QC_LINT_FAILED", message=msg, human=msg,
                  next="send a corrected edit, or approve or skip the current text")
