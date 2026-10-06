"""U2 template checks shared by tests/test_searches.py (scout) and tests/test_evaluate.py (evaluator).

The scout and evaluator AGENTS templates carry the common Tools paragraph of the CLI-ROUTE design (section 10):
OpenClaw's tools only, Claude Code's own tools off, never ask a person, one plain jh.py command per exec call,
never type --agent-proof, absolute workspace paths without ~ @ .. $, no edit tool (whole files only), refusals
by the guard or by OpenClaw end the cycle, and every cycle ends with the single word CYCLE_DONE."""
from __future__ import annotations

import os
import re

from jobhunter import install as ins

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TEMPLATES = os.path.join(REPO, "agent-templates")


def read(*parts: str) -> str:
    with open(os.path.join(TEMPLATES, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


def flat(text: str) -> str:
    """The text with every run of whitespace (line wraps included) as one space."""
    return re.sub(r"\s+", " ", text)


def section(text: str, title: str) -> str:
    """The body of the `## <title>` section (up to the next `## ` heading)."""
    m = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(title), text, re.S | re.M)
    if not m:
        raise AssertionError("no section %r" % title)
    return m.group(1)


COMMON = (
    "Claude Code's own tools (Bash, Read, Write, Edit, Glob, Grep, WebFetch, Task, TodoWrite, AskUserQuestion) "
    "are switched off for you.",
    "Never try them and never ask a person anything: nobody is there and nothing waits for approval.",
    "Run one plain jh.py command per exec call, with absolute paths and `timeoutSeconds: 90`.",
    "The safety plugin adds `-I` and an `--agent-proof` option to every jh.py command you run.",
    "Never type `--agent-proof` yourself and never copy one.",
    "Use absolute file paths that start with your workspace folder `__WS__/`.",
    "Never use `~`, `@`, `..` or `$` in a path.",
    "Read only inside your workspace.",
    "Write whole files only inside `__WS__/work/` and `__WS__/inbox/`: there is no edit tool, so to fix a file, "
    "write it again.",
    "A refused call is refused at once. A `G_*` code, or an OpenClaw message that a command is not allowed or a "
    "path is outside the workspace, means: end the cycle as your program says.",
    "Finish every cycle with the single word `CYCLE_DONE`.",
)


def check_agents_template(case, role: str, browser: bool) -> str:
    """Assert the common rules on agent-templates/<role>/AGENTS.template.md; returns the template text."""
    text = read(role, "AGENTS.template.md")
    tools = flat(section(text, "Tools"))
    for sentence in COMMON:
        case.assertIn(sentence, tools, "%s Tools section misses: %s" % (role, sentence))
    names = ["mcp__openclaw__exec", "mcp__openclaw__read", "mcp__openclaw__write"]
    for n in names:
        case.assertIn(n, tools)
    if browser:
        case.assertIn("mcp__openclaw__browser", tools)
        case.assertIn('Always pass `profile: "jobhunter"` to the browser tool.', tools)
    else:
        case.assertNotIn("mcp__openclaw__browser", text)
        case.assertIn("You have no browser.", tools)
    # the lane program ends with CYCLE_DONE everywhere, never with the old reply word
    case.assertNotIn("NO_REPLY", text)
    case.assertNotIn("NO_REPLY", read(role, "IDENTITY.md"))
    case.assertIn("`CYCLE_DONE`", read(role, "IDENTITY.md"))
    program = flat(section(text, "The cycle"))
    case.assertEqual(program.count("reply `CYCLE_DONE`"), 2, program)       # go false, and the normal end
    blocked = flat(text[:text.index("## The cycle")])
    case.assertIn("refused by OpenClaw (a command that is not allowed, a path outside the workspace), means stop",
                  blocked)
    case.assertIn("run `cycle end`, reply `CYCLE_DONE`.", blocked)
    # other turns end with the word their message names, never with CYCLE_DONE
    other = flat(section(text, "Other turns"))
    case.assertIn("Both end with the single word the message names", other)
    case.assertIn("Do not run `preflight` or `cycle end`.", other)
    # no wording that invites an edit tool, a home path or a typed proof
    case.assertNotRegex(text, r"(?i)\bedit (the|a|your) file\b")
    case.assertNotIn("~/", text)
    case.assertEqual(text.count("--agent-proof"), 2)                          # only in the two Tools sentences
    # it renders without leftovers and stays under the size limit the installer and sync_skills enforce
    mapping = ins.placeholders(repo="/opt/jh", py="/usr/bin/python3", ws_root="/opt/ws", role=role,
                               agent_id="jobhunter-" + role)
    rendered = ins.substitute(text, mapping)
    case.assertEqual(ins.PLACEHOLDER_RE.findall(rendered), [])
    case.assertLess(len(text), 12000)
    text.encode("ascii")
    return text
