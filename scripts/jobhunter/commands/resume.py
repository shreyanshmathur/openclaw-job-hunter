"""`resume` commands (design 3.4, 6.4, U4): plan, build, base-build, base-review, stage, unstage."""
from __future__ import annotations

from .. import db
from .. import resume as R
from ..errors import Denied
from . import Result, add_command


def register(subparsers):
    p = add_command(subparsers, "resume plan", cmd_plan, callers="A",
                    help="copy the base resume and the JD to work/ and describe the tailoring rules")
    p.add_argument("--job", required=True, metavar="<job_uid>")

    p = add_command(subparsers, "resume build", cmd_build, callers="A",
                    help="build a tailored resume variant from a tailor file (12.5)")
    p.add_argument("--job", required=True, metavar="<job_uid>")
    p.add_argument("--file", required=True, metavar="<tailor.json>")

    add_command(subparsers, "resume base-build", cmd_base_build, callers="SH",
                help="build the base variant and its resume draft (once per base version)")
    add_command(subparsers, "resume base-review", cmd_base_review, callers="H",
                help="show the base resume with every replaced dash and confirm it")

    p = add_command(subparsers, "resume stage", cmd_stage, callers="A",
                    help="copy the approved PDF to the browser upload folder for an open token")
    p.add_argument("--variant", required=True, metavar="<variant_uid>")
    p.add_argument("--token", required=True, metavar="<token>")

    p = add_command(subparsers, "resume unstage", cmd_unstage, callers="AS", help="delete a staged copy")
    p.add_argument("--token", required=True, metavar="<token>")


def cmd_plan(args, ctx):
    conn = ctx.connect()
    data = R.plan(conn, args.job, cycle_id=ctx.cycle_id)
    if data["mode"] == "off":
        nxt = ("tailoring is off: stage variant %s" % data["base_variant_uid"]) if data["base_variant_uid"] else \
            "tailoring is off and the base variant is not approved yet: leave this job"
    else:
        nxt = "write the tailor file (12.5) and run resume build"
    return Result(data=data, message="resume plan for %s (mode %s)" % (args.job, data["mode"]), next=nxt)


def _draft_result(data: dict, what: str) -> Result:
    status = data.get("draft_status")
    if status == "lint_failed":
        return Result(data=data, code="E_QC_LINT_FAILED",
                      message="%s built but its draft failed lint" % what,
                      next="read the lint findings and build again with a corrected tailor file")
    return Result(data=data, message="%s %s built (draft %s, %s)" % (what, data["variant_uid"], data["draft_uid"],
                                                                     status or "created"),
                  next="start QC review of draft %s" % data["draft_uid"])


def cmd_build(args, ctx):
    tailor = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        data = R.build(conn, args.job, tailor, cycle_id=ctx.cycle_id, caller=ctx.caller)
    return _draft_result(data, "resume variant")


def cmd_base_build(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        data = R.build_base(conn, cycle_id=ctx.cycle_id, caller=ctx.caller)
    if data.get("reused"):
        return Result(data=data, code="NOTHING_TO_DO", message="the base variant %s is current" % data["variant_uid"])
    return _draft_result(data, "base variant")


def cmd_base_review(args, ctx):
    if ctx.is_agent:
        raise Denied("E_HUMAN_ONLY", "the base review is for the person at a terminal")
    base = R.load_base()
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        raise Denied("E_PRECONDITION", "this command needs an interactive terminal")
    try:
        tty.write(R.review_text(base))
        from .. import profile as P
        original = P._read_text(P.resume_text_path())
        if original:
            tty.write("\nExtracted text of your original resume:\n\n%s\n" % original)
        tty.write("\nIs the base resume above correct and complete? Type yes to confirm: ")
        tty.flush()
        ans = (tty.readline() or "").strip().lower()
    finally:
        tty.close()
    if ans not in ("yes", "y"):
        return Result(data={"reviewed": False}, code="NOTHING_TO_DO",
                      message="not confirmed; edit private/resume/base.json or run the inference again")
    rec = R.mark_reviewed(base)
    return Result(data={"reviewed": True, "base_sha256": rec["sha256"]}, message="base resume confirmed",
                  next="run ./jobhunter resume base-build")


def cmd_stage(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        data = R.stage(conn, args.variant, args.token)
    return Result(data=data, message="staged %s" % data["filename"],
                  next="browser upload exactly %s, then check that the page shows %s" % (data["upload_path"],
                                                                                          data["filename"]))


def cmd_unstage(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        row = conn.execute("SELECT removed_at FROM staged_files WHERE token = ?", (args.token,)).fetchone()
        R.unstage(conn, args.token)
    if row is None or row["removed_at"] is not None:
        return Result(data={"removed": False}, code="NOTHING_TO_DO", message="nothing staged for this token")
    return Result(data={"removed": True}, message="staged copy deleted")
