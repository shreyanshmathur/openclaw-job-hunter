"""`profile` commands (design 3.4, U4): import, salary-record, infer-record, questions, answer, interview,
feasibility, status, show, facts."""
from __future__ import annotations

import json

from .. import db
from .. import profile as P
from ..errors import Denied
from . import Result, add_command

# onboarding turns are not cycles and end with a plain final word (CLI route design 6.3, M6)
ONBOARD_NEXT = "reply with the single word ONBOARD_DONE"


def register(subparsers):
    p = add_command(subparsers, "profile import", cmd_import, callers="H",
                    help="copy the resume (and extra information) into private/ and extract its text")
    p.add_argument("--resume", required=True, metavar="<path>")
    p.add_argument("--extra", metavar="<path>")

    p = add_command(subparsers, "profile salary-record", cmd_salary_record, callers="A",
                    help="record the scout's salary research file (12.16)")
    p.add_argument("--file", required=True, metavar="<salary.json>")

    p = add_command(subparsers, "profile infer-record", cmd_infer_record, callers="A",
                    help="record the evaluator's profile inference file (12.9)")
    p.add_argument("--file", required=True, metavar="<inference.json>")

    p = add_command(subparsers, "profile questions", cmd_questions, callers="SH",
                    help="list the profile questions with their defaults")
    p.add_argument("--format", choices=("json", "text", "chat"), default="json")
    p.add_argument("--only-open", action="store_true")

    p = add_command(subparsers, "profile answer", cmd_answer, callers="HC",
                    help="store one answer (question id, key, or a form question task id)")
    p.add_argument("--field", required=True, metavar="<id>")
    p.add_argument("--value", required=True, metavar="<text>")

    p = add_command(subparsers, "profile interview", cmd_interview, callers="H",
                    help="answer the profile questions at the terminal")
    p.add_argument("--only-open", action="store_true")

    add_command(subparsers, "profile feasibility", cmd_feasibility, callers="SH",
                help="check the confirmed answers against each other and the salary research")
    add_command(subparsers, "profile status", cmd_status, callers="ASH",
                help="whether the profile is confirmed and which inputs exist")
    add_command(subparsers, "profile show", cmd_show, callers="H", help="show the confirmed profile")
    add_command(subparsers, "profile facts", cmd_facts, callers="SH", help="list the profile facts")


def cmd_import(args, ctx):
    resume_path = ctx.input_path(args.resume)
    extra = ctx.input_path(args.extra) if args.extra else None
    data = P.import_resume(resume_path, extra)
    msg = "resume imported (%s, %d characters, quality %s)" % (data["extract_method"], data["chars"], data["quality"])
    return Result(data=data, message=msg, next=data.get("next") or "run the salary research and inference turns")


def cmd_salary_record(args, ctx):
    payload = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        data = P.record_salary(conn, payload)
    return Result(data=data, message="salary research recorded (%d sources)" % data["sources_recorded"],
                  next=ONBOARD_NEXT)


def cmd_infer_record(args, ctx):
    payload = ctx.read_json(args.file)
    conn = ctx.connect()
    with db.tx(conn):
        data = P.record_inference(conn, payload)
    return Result(data=data, message="profile inference recorded; %d questions wait for the person"
                  % data["questions"], next=ONBOARD_NEXT)


def cmd_questions(args, ctx):
    qs = P.questions(only_open=args.only_open)
    text = P.questions_text(qs)
    data = {"questions": [{k: q[k] for k in ("id", "text", "default", "why", "required")} for q in qs]}
    if args.format in ("text", "chat"):
        data["text"] = text
    return Result(data=data, message="%d questions" % len(qs), human=text)


def cmd_answer(args, ctx):
    conn = ctx.connect()
    sensitive_ok = ctx.caller.cls == "human"
    by = "chat" if ctx.caller.cls == "chat" else "human"
    with db.tx(conn):
        data = P.answer(conn, args.field, args.value, "user_confirmed", sensitive_ok=sensitive_ok, by=by)
    left = data.get("remaining_required") or []
    msg = "stored %s" % data["field"]
    if left:
        msg += "; %d required questions left" % len(left)
    else:
        msg += "; the profile is confirmed"
    nxt = ""
    if data.get("requeue_suggested"):
        nxt = "the profile changed: run ./jobhunter eval requeue --since-profile-change to re-evaluate jobs"
    return Result(data=data, message=msg, next=nxt)


def _tty():
    try:
        f = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        raise Denied("E_PRECONDITION", "this command needs an interactive terminal")
    return f


def cmd_interview(args, ctx):
    if ctx.is_agent:
        raise Denied("E_HUMAN_ONLY", "the interview is for the person at a terminal")
    tty = _tty()
    try:
        data = P.interview(lambda: ctx.connect(), tty, tty, only_open=args.only_open)
        feas = P.feasibility()
        if feas["conflicts"]:
            tty.write("\nThings to check:\n")
            for c in feas["conflicts"]:
                tty.write("  - %s %s\n" % (c["text"], c["suggestion"]))
    finally:
        tty.close()
    data["feasibility"] = feas
    msg = "profile confirmed" if data.get("complete") else "%d required questions left" % len(data.get("missing") or [])
    return Result(data=data, message=msg)


def cmd_feasibility(args, ctx):
    data = P.feasibility()
    human = "\n".join("%s: %s %s" % (c["severity"], c["text"], c["suggestion"]) for c in data["conflicts"]) or \
        "no conflicts"
    return Result(data=data, message="%d conflicts" % len(data["conflicts"]), human=human)


def cmd_status(args, ctx):
    data = P.status()
    msg = "profile confirmed" if data["confirmed"] else "profile not confirmed (%d required questions open)" % \
        len(data["missing"])
    return Result(data=data, message=msg)


def cmd_show(args, ctx):
    data = P.load_confirmed()
    return Result(data=data, message="confirmed profile", human=json.dumps(data, indent=2, sort_keys=True))


def cmd_facts(args, ctx):
    fx = P.facts()
    human = "\n".join("%s %s" % (k, v) for k, v in sorted(fx.items(), key=lambda kv: int(kv[0][1:])))
    return Result(data={"facts": fx}, message="%d facts" % len(fx), human=human or "no facts")
