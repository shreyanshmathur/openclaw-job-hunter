"""CLI command modules (design 3). `jobhunter.cli` imports every module in this package whose name does
not start with an underscore and calls its `register(subparsers)`. Each unit owns its own modules
(11 rule 4); nobody edits a shared dispatcher.

Contract for a command module:

    from jobhunter.commands import add_command, Result

    def register(subparsers):
        p = add_command(subparsers, "gate reserve", cmd_reserve, callers="A",
                        help="reserve a send slot")
        p.add_argument("--kind", required=True)

    def cmd_reserve(args, ctx):          # args: argparse.Namespace, ctx: jobhunter.cli.Context
        conn = ctx.connect()             # db.connect(write=True), closed by the CLI afterwards
        ...
        return {"token": token}          # or Result(...); raise errors.Denied(code, message) to refuse

Rules:
- `path` is the command as typed ("budget", "gate reserve", "qc review start"); groups are created on
  demand and may be shared between modules; registering the same path twice is an error.
- `callers` uses the letters of the 3.4 tables: S system, H human (PIN), C chat (/jh grant), R public
  read-only, A jobhunter agents (acl.json decides which agent and which argument classes). Human callers
  may also run every S command.
- argument dests must not start with an underscore (reserved for the CLI).
- the handler returns a dict (the envelope `data`, code OK), a Result, or None.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

CALLER_LETTERS = frozenset("SHCRA")


@dataclass
class Result:
    """What a handler returns when it needs more than `data` (code NOTHING_TO_DO or PENDING, a message
    for the human, a hint for the agent, a retry time, or a text rendering for --human)."""
    data: dict = field(default_factory=dict)
    code: str = "OK"
    message: str = ""
    next: str = ""
    retry_after_s: int | None = None
    human: str | None = None


def _group(subparsers, name: str):
    """Return the nested subparsers action of group `name`, creating the group if needed."""
    existing = subparsers.choices.get(name)
    if existing is not None:
        nested = getattr(existing, "_jh_subparsers", None)
        if nested is None:
            raise ValueError("command %r is already registered as a leaf; it cannot also be a group" % name)
        return nested
    parser = subparsers.add_parser(name, help="%s commands" % name)
    depth = getattr(subparsers, "_jh_depth", 0) + 1
    nested = parser.add_subparsers(dest="_jh_sub_%d_%s" % (depth, name.replace("-", "_")), metavar="<command>")
    nested.required = True
    nested._jh_depth = depth
    parser._jh_subparsers = nested
    return nested


def add_command(subparsers, path: str, func, callers: str, help: str | None = None,
                description: str | None = None) -> argparse.ArgumentParser:
    """Register one command and return its parser (add the arguments to it)."""
    words = path.split()
    if not words:
        raise ValueError("empty command path")
    letters = set(callers)
    if not letters or not letters <= CALLER_LETTERS:
        raise ValueError("callers must use the letters S, H, C, R, A (got %r)" % callers)
    sp = subparsers
    for w in words[:-1]:
        sp = _group(sp, w)
    leaf = words[-1]
    if leaf in sp.choices:
        raise ValueError("command %r is registered twice" % path)
    parser = sp.add_parser(leaf, help=help, description=description or help)
    parser.set_defaults(_jh_func=func, _jh_command=" ".join(words), _jh_callers="".join(sorted(letters)))
    return parser
