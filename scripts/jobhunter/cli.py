"""`jh.py` command line (design 3): discovery, global options, caller checks, envelope, exit codes.

    <PY> $REPO/scripts/jh.py [--cycle <id>] [--quiet] [--human] [--pin-stdin] [--grant <g>] <group> [<cmd>] [args]

- Commands are auto-discovered: every module in jobhunter/commands/ exposes register(subparsers)
  (see jobhunter.commands for the contract). A module that fails to import does not take the rest of
  the CLI down; its error is logged and reported by `selftest` (discovery_errors()).
- Global options: --quiet, --human, --pin-stdin and --grant are accepted anywhere on the line (cron
  jobs put --quiet last); --cycle is global only before the command words, because `cycle end` and
  `lock renew` have their own --cycle. There is no --home.
- Every run prints exactly one JSON envelope (or NO_REPLY with --quiet on success, or text with
  --human) and exits with the number frozen in errors.CODES.
- Caller class (auth.classify: agent, chat grant, human PIN, system) and permission (the command's caller
  letters, then auth.require with acl.json and its argument classes) are checked before the handler runs.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import pkgutil
import sqlite3
import sys
import traceback
from dataclasses import dataclass

from . import paths
from .canon import now
from .commands import Result
from .errors import CODES, Denied, exit_code, map_sqlite_error

FREE_TEXT_MAX = 4000
_GLOBAL_FLAGS = ("--quiet", "--human", "--pin-stdin")
_discovery_errors: list[dict] = []


# ---------------------------------------------------------------- parser
class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print usage and exit 2; we want the envelope
        raise Denied("E_USAGE", message, data={"usage": self.format_usage().strip()})

    def exit(self, status=0, message=None):
        if status:
            raise Denied("E_USAGE", (message or "usage error").strip())
        raise _HelpExit()


class _HelpExit(Exception):
    pass


def discovery_errors() -> list[dict]:
    return list(_discovery_errors)


def _command_modules() -> list:
    from . import commands as pkg
    mods = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        name = "%s.%s" % (pkg.__name__, info.name)
        try:
            mods.append(importlib.import_module(name))
        except Exception as exc:  # a broken module must not disable pause, status or breaker commands
            _discovery_errors.append({"module": name, "error": "%s: %s" % (type(exc).__name__, exc)})
    return mods


def build_parser(modules: list | None = None) -> argparse.ArgumentParser:
    """The full argparse tree. `modules` replaces discovery (tests)."""
    del _discovery_errors[:]
    parser = _Parser(prog="jh.py", description="openclaw-job-hunter command line", add_help=True)
    parser.add_argument("--cycle", dest="_g_cycle", metavar="<cycle_id>", help="attach the call to a cycle")
    sub = parser.add_subparsers(dest="_jh_sub_0", metavar="<command>")
    sub.required = True
    sub._jh_depth = 0
    for mod in (_command_modules() if modules is None else modules):
        reg = getattr(mod, "register", None)
        if reg is None:
            continue
        try:
            reg(sub)
        except Exception as exc:
            _discovery_errors.append({"module": getattr(mod, "__name__", repr(mod)),
                                      "error": "%s: %s" % (type(exc).__name__, exc)})
    return parser


def registered_commands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """{'gate reserve': leaf parser, ...} for every registered command (test_acl, selftest)."""
    out: dict[str, argparse.ArgumentParser] = {}

    def walk(p, prefix):
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    path = (prefix + " " + name).strip()
                    if child.get_default("_jh_func") is not None:
                        out[path] = child
                    walk(child, path)
    walk(parser, "")
    return out


# ---------------------------------------------------------------- global options
@dataclass
class GlobalOptions:
    quiet: bool = False
    human: bool = False
    pin_stdin: bool = False
    grant: str | None = None


def _split_globals(argv: list[str]) -> tuple[GlobalOptions, list[str]]:
    g = GlobalOptions()
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--quiet":
            g.quiet = True
        elif tok == "--human":
            g.human = True
        elif tok == "--pin-stdin":
            g.pin_stdin = True
        elif tok == "--grant" or tok.startswith("--grant="):
            if tok == "--grant":
                if i + 1 >= len(argv):
                    raise Denied("E_USAGE", "--grant needs a value")
                i += 1
                value = argv[i]
            else:
                value = tok.split("=", 1)[1]
            if g.grant is not None:
                raise Denied("E_USAGE", "--grant given twice")
            g.grant = value
        elif tok == "--home" or tok.startswith("--home="):
            raise Denied("E_USAGE", "there is no --home option; the database path is fixed")
        else:
            rest.append(tok)
        i += 1
    return g, rest


# ---------------------------------------------------------------- callers
def classify(argv: list[str], env: dict, stdin, g: GlobalOptions, command: str = "", rest: list | None = None):
    """Caller class (jobhunter.auth.classify). A chat grant signs the command words and the tokens after
    them (auth.grant_args)."""
    from . import auth
    args = auth.grant_args(list(rest if rest is not None else argv), command) if command else None
    return auth.classify(argv, env, stdin, command=command or None, args=args)


def check_callers(caller, callers: str, command: str) -> None:
    """The command's caller letters (3.4 tables) against the caller class."""
    cls = caller.cls
    if cls == "agent":
        agent = getattr(caller, "agent_id", None)
        if agent and agent.startswith("jobhunter-"):
            ok = "A" in callers or "R" in callers
        elif "R" in callers:
            ok = True   # public read-only (guard rule R7)
        elif not agent:
            raise Denied("E_GUARD_MISSING", "agent call without JH_AGENT_ID; the jobhunter-guard plugin is not active")
        else:
            ok = False
    elif cls == "system":
        ok = "S" in callers
        if not ok and "H" in callers:
            raise Denied("E_HUMAN_ONLY", "%r needs the owner PIN (./jobhunter %s)" % (command, command))
    elif cls == "human":
        ok = "H" in callers or "S" in callers
    elif cls == "chat":
        ok = "C" in callers
    else:
        ok = False
    if not ok:
        raise Denied("E_CALLER_NOT_ALLOWED", "caller class %s may not run %r" % (cls, command))


def require(caller, command: str, args: dict) -> None:
    from . import auth
    auth.require(caller, command, args)


def acl_args(parser: argparse.ArgumentParser, ns: argparse.Namespace) -> dict:
    """The given arguments in acl.json form: {'--file': '/x', '_1': 'J...', '--needs-check': True}.
    Only arguments that differ from their default are included."""
    out: dict = {}
    pos = 0
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.dest.startswith("_") or action.dest == "help":
            continue
        value = getattr(ns, action.dest, None)
        if action.option_strings:
            longs = [o for o in action.option_strings if o.startswith("--")]
            name = longs[0] if longs else action.option_strings[0]
            if value is not None and value != action.default:
                out[name] = value
        else:
            pos += 1
            if value is not None:
                out["_%d" % pos] = value
    return out


# ---------------------------------------------------------------- context
class Context:
    """What a handler gets besides its argparse namespace."""

    def __init__(self, caller, command: str, argv: list[str], cycle_id: str | None, g: GlobalOptions,
                 env: dict, stdin):
        self.caller = caller
        self.command = command
        self.argv = argv
        self.cycle_id = cycle_id
        self.quiet = g.quiet
        self.human = g.human
        self.pin_stdin = g.pin_stdin
        self.grant = g.grant
        self.env = env
        self.stdin = stdin
        self._conns: list = []

    @property
    def is_agent(self) -> bool:
        return self.caller.cls == "agent"

    def connect(self, write: bool = True):
        from . import db
        conn = db.connect(write=write)
        self._conns.append(conn)
        return conn

    def close(self) -> None:
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        self._conns = []

    def input_path(self, path: str) -> str:
        """Resolve a --file style argument. Agent callers are confined to their own work/ and inbox/
        (E_PATH_NOT_ALLOWED); other callers get the realpath."""
        if self.is_agent:
            return paths.ensure_agent_path(path, self.caller.agent_id)
        if not path:
            raise Denied("E_USAGE", "a file path is required")
        return os.path.realpath(path)

    def read_text(self, path: str, max_chars: int = FREE_TEXT_MAX) -> tuple[str, bool]:
        """Free-text file (12.14): UTF-8, at most max_chars (longer is truncated and flagged)."""
        real = self.input_path(path)
        try:
            with open(real, "r", encoding="utf-8") as fh:
                text = fh.read(max_chars + 1)
        except FileNotFoundError:
            raise Denied("E_NOT_FOUND", "file not found", data={"path": path})
        except (OSError, UnicodeDecodeError) as exc:
            raise Denied("E_VALIDATION", "file is not readable UTF-8 text: %s" % exc, data={"path": path})
        if len(text) > max_chars:
            return text[:max_chars], True
        return text, False

    def read_json(self, path: str, max_bytes: int = 2_000_000):
        real = self.input_path(path)
        try:
            with open(real, "rb") as fh:
                raw = fh.read(max_bytes + 1)
        except FileNotFoundError:
            raise Denied("E_NOT_FOUND", "file not found", data={"path": path})
        except OSError as exc:
            raise Denied("E_VALIDATION", "file is not readable: %s" % exc, data={"path": path})
        if len(raw) > max_bytes:
            raise Denied("E_VALIDATION", "file is too large", data={"path": path, "max_bytes": max_bytes})
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise Denied("E_SCHEMA", "file is not valid UTF-8 JSON: %s" % exc, data={"path": path})


# ---------------------------------------------------------------- output
def _envelope(code: str, data: dict, message: str, next_: str, retry_after_s, cycle_id) -> dict:
    return {"ok": exit_code(code) == 0, "code": code, "data": data if data is not None else {},
            "message": message or "", "next": next_ or "", "retry_after_s": retry_after_s, "cycle_id": cycle_id}


def _default_next(code: str) -> str:
    n = exit_code(code)
    return {
        0: "", 1: "stop the cycle and reply NO_REPLY", 2: "fix the call once; do not loop",
        3: "drop this item and move on", 4: "skip this action type for now", 5: "end the cycle now and reply NO_REPLY",
        6: "rewrite if budget is left, else drop", 7: "drop this item", 8: "reply NO_REPLY",
        9: "re-read the work list", 10: "fix the input file once", 11: "do the missing step first, or end the cycle",
        12: "the next run retries",
    }.get(n, "")


def _write(stdout, text: str) -> None:
    stdout.write(text + "\n")
    try:
        stdout.flush()
    except Exception:
        pass


def _log_line(caller, command: str, code: str, rc: int, extra: str = "") -> None:
    if not os.path.isdir(paths.logs_dir()):
        return   # before `init` nothing is created in the repo
    try:
        who = getattr(caller, "cls", "unknown") if caller is not None else "unknown"
        agent = getattr(caller, "agent_id", None) if caller is not None else None
        if agent:
            who += ":" + agent
        line = "%s %s %s %s %d%s\n" % (now(), who, (command or "-").replace(" ", "."), code, rc,
                                       (" " + extra) if extra else "")
        with open(os.path.join(paths.logs_dir(), "jh.log"), "a", encoding="utf-8") as fh:
            fh.write(line.encode("ascii", "replace").decode("ascii"))
    except OSError:
        pass


def _render_human(env: dict, human: str | None) -> str:
    if human:
        return human
    head = env["message"] or env["code"]
    if env["data"]:
        return head + "\n" + json.dumps(env["data"], indent=2, sort_keys=True, default=str)
    return head


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None, env: dict | None = None, stdin=None, stdout=None,
         modules: list | None = None) -> int:
    """Run one command; returns the exit number. `modules` replaces discovery (tests)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    g = GlobalOptions()
    caller = None
    command = ""
    cycle_id = None
    ctx = None
    code, data, message, next_, retry, human = "E_INTERNAL", {}, "", "", None, None
    try:
        g, rest = _split_globals(argv)
        parser = build_parser(modules)
        try:
            ns = parser.parse_args(rest)
        except _HelpExit:
            return 0
        except Denied as d:
            if _discovery_errors:
                d.data = dict(d.data or {}, discovery_errors=discovery_errors())
            raise
        command = ns._jh_command
        leaf = registered_commands(parser)[command]
        cycle_id = ns._g_cycle or getattr(ns, "cycle", None)
        caller = classify(argv, env, stdin, g, command, rest)
        check_callers(caller, ns._jh_callers, command)
        require(caller, command, acl_args(leaf, ns))
        ctx = Context(caller, command, argv, cycle_id, g, env, stdin)
        result = ns._jh_func(ns, ctx)
        if result is None:
            result = Result()
        elif isinstance(result, dict):
            result = Result(data=result)
        elif not isinstance(result, Result):
            raise Denied("E_INTERNAL", "handler returned %s" % type(result).__name__)
        if result.code not in CODES:
            raise Denied("E_INTERNAL", "handler returned unknown code %r" % result.code)
        code, data, message, next_ = result.code, result.data, result.message, result.next
        retry, human = result.retry_after_s, result.human
        if ctx.cycle_id and not cycle_id:
            cycle_id = ctx.cycle_id
    except Denied as d:
        code, data, message, retry = d.code, d.data, d.message, d.retry_after
        next_ = _default_next(code)
    except sqlite3.DatabaseError as exc:
        d = map_sqlite_error(exc)
        code, data, message, retry = d.code, d.data, d.message, d.retry_after
        next_ = _default_next(code)
    except KeyboardInterrupt:
        code, data, message, next_ = "E_INTERNAL", {}, "interrupted", _default_next("E_INTERNAL")
    except Exception as exc:
        tb = traceback.format_exc()
        _log_line(caller, command, "E_INTERNAL", 1, "traceback=" + json.dumps(tb[-2000:]))
        code, data, message = "E_INTERNAL", {"error": type(exc).__name__}, "internal error: %s" % exc
        next_ = _default_next(code)
    finally:
        if ctx is not None:
            ctx.close()
    rc = exit_code(code)
    envelope = _envelope(code, data, message, next_, retry, cycle_id)
    try:
        if g.quiet and rc == 0:
            _write(stdout, "NO_REPLY")
        elif g.human:
            _write(stdout, _render_human(envelope, human))
        else:
            _write(stdout, json.dumps(envelope, sort_keys=False, default=str, ensure_ascii=True))
    except (OSError, ValueError, io.UnsupportedOperation):
        pass
    _log_line(caller, command, code, rc)
    return rc
