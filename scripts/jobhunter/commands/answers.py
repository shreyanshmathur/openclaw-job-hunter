"""`answers` commands (design 3.4, 6.5, U4): get (applier) and add (the person)."""
from __future__ import annotations

import json

from .. import answers as A
from .. import db
from ..errors import Denied
from . import Result, add_command


def register(subparsers):
    p = add_command(subparsers, "answers get", cmd_get, callers="A",
                    help="answer one form field from the answer bank, or open a question for the person")
    p.add_argument("--file", required=True, metavar="<question.json>")

    p = add_command(subparsers, "answers add", cmd_add, callers="H", help="store an answer for form fields")
    p.add_argument("--key", required=True, metavar="<k>")
    p.add_argument("--value", required=True, metavar="<v>")
    p.add_argument("--patterns", metavar="<json list>")
    p.add_argument("--sensitive", action="store_true")


def cmd_get(args, ctx):
    question = ctx.read_json(args.file)
    res = A.lookup(question)
    if res["found"]:
        return Result(data=res, message="answer found (%s)" % res["key"], next="use the value exactly as given")
    conn = ctx.connect()
    with db.tx(conn):
        task = A.request_human(conn, question, res.get("reason") or "no_match")
    data = dict(res, **task)
    return Result(data=data, code="E_NOT_FOUND",
                  message="no confirmed answer for this field; the person was asked (%s)" % task["human_task"],
                  next="leave this job for the person: do not guess the field; move on to the next job")


def cmd_add(args, ctx):
    patterns = None
    if args.patterns:
        try:
            patterns = json.loads(args.patterns)
        except ValueError:
            raise Denied("E_VALIDATION", "--patterns must be a JSON list of regular expressions")
    data = A.add(args.key, args.value, patterns=patterns, sensitive=args.sensitive)
    return Result(data=data, message="answer %s stored" % args.key)
