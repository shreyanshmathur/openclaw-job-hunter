"""Repository layout (U7): every file has exactly one owning unit (the table in docs/DEVELOPING.md), and the
runtime folders are never part of the repo."""
from __future__ import annotations

import os
import re
import sys
import unittest

import tests  # noqa: F401
from jobhunter import paths

sys.path.insert(0, os.path.join(paths.REPO, "tools"))
import textcheck  # noqa: E402

DEVELOPING = os.path.join(paths.REPO, "docs", "DEVELOPING.md")
# design units U1..U10 plus INT, the integration owner of the end-to-end tests
UNIT_RE = re.compile(r"^(U(?:[1-9]|10)|INT)$")
DESIGN_UNITS = {"U%d" % i for i in range(1, 11)} | {"INT"}


def expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out += expand_braces(pattern[:m.start()] + alt + pattern[m.end():])
    return out


def glob_to_regex(pattern: str) -> re.Pattern:
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            rx += ".*"
            i += 2
        elif pattern[i] == "*":
            rx += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(pattern[i])
            i += 1
    return re.compile("^" + rx + "$")


def load_owner_table(path: str = DEVELOPING) -> list[tuple[str, str, re.Pattern]]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"<!-- owners:start -->\s*```text\n(.*?)```\s*<!-- owners:end -->", text, re.S)
    if not m:
        raise AssertionError("owner block not found in docs/DEVELOPING.md")
    rows = []
    for line in m.group(1).strip().split("\n"):
        unit, _, pattern = line.strip().partition(" ")
        if not UNIT_RE.match(unit) or not pattern:
            raise AssertionError("bad owner line: %r" % line)
        for p in expand_braces(pattern.strip()):
            rows.append((unit, p, glob_to_regex(p)))
    return rows


def owners(rel: str, table) -> list[str]:
    return sorted({unit for unit, _, rx in table if rx.match(rel)})


class TestMatcher(unittest.TestCase):
    def test_unit_names(self):
        for ok in ("U1", "U9", "U10", "INT"):
            self.assertTrue(UNIT_RE.match(ok), ok)
        for bad in ("U0", "U11", "U01", "int", "INTX", "X1"):
            self.assertFalse(UNIT_RE.match(bad), bad)

    def test_glob(self):
        self.assertTrue(glob_to_regex("a/**").match("a/b/c.txt"))
        self.assertTrue(glob_to_regex("a/*.py").match("a/b.py"))
        self.assertFalse(glob_to_regex("a/*.py").match("a/b/c.py"))
        self.assertEqual(expand_braces("x/{a,b}/{c,d}.py"), ["x/a/c.py", "x/a/d.py", "x/b/c.py", "x/b/d.py"])


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.table = load_owner_table()

    def test_table_covers_the_design_units(self):
        self.assertEqual({u for u, _, _ in self.table}, DESIGN_UNITS)
        for rel, unit in (("install.sh", "U7"), ("scripts/jobhunter/gate.py", "U1"),
                          ("scripts/jobhunter/migrations/0001_init.sql", "U1"),
                          ("scripts/jobhunter/migrations/0002_enrich.sql", "U10"),
                          ("scripts/jobhunter/enrich/providers.py", "U10"),
                          ("scripts/jobhunter/commands/enrich.py", "U10"),
                          ("docs/EMAIL-FINDER.md", "U10"), ("tests/test_enrich_chain.py", "U10"),
                          ("tests/fixtures/enrich/hunter/ok.json", "U10"), ("tests/fakes/u10/fake_mail.py", "U10"),
                          ("tests/test_e2e_pipeline.py", "INT"), ("tests/fixtures/e2e/day1/config.json", "INT"),
                          ("agent-templates/scout/skills/jobhunter-salary/SKILL.template.md", "U4"),
                          ("agent-templates/scout/skills/jobhunter-browser-search/SKILL.template.md", "U2"),
                          ("skills-src/jobhunter-gmail-web/SKILL.template.md", "U9"),
                          ("openclaw/plugins/jobhunter-guard/src/policy.ts", "U8"),
                          ("openclaw/crons.json", "U7"), ("tests/fakes/u3/fake_profile.py", "U3")):
            self.assertEqual(owners(rel, self.table), [unit], rel)

    def test_patterns_do_not_overlap(self):
        # probe every literal pattern (no wildcard) against the others
        for unit, pat, _ in self.table:
            if "*" in pat or "?" in pat:
                continue
            with self.subTest(pattern=pat):
                self.assertEqual(owners(pat, self.table), [unit])

    def test_every_repo_file_has_exactly_one_owner(self):
        problems = []
        for rel in textcheck.repo_files(paths.REPO):
            got = owners(rel.replace(os.sep, "/"), self.table)
            if len(got) != 1:
                problems.append("%s: %s" % (rel, ", ".join(got) or "no owner"))
        self.assertEqual(problems, [], "add the file to the owner table in docs/DEVELOPING.md or move it")

    def test_no_runtime_folders_or_rendered_files(self):
        files = textcheck.repo_files(paths.REPO)
        for rel in files:
            top = rel.split("/", 1)[0]
            self.assertNotIn(top, ("private", "state", "logs", "exports", "workspaces", "resumes"), rel)
            self.assertNotIn(os.path.basename(rel), ("AGENTS.md", "SKILL.md"), rel)

    def test_every_repo_check_runs_in_ci_the_hook_and_the_docs(self):
        checks = ("tools/textcheck.py", "tools/leakcheck.py", "tools/check_gitignore.py", "tools/gen_drivers.py --check")
        for rel in (".github/workflows/ci.yml", "tools/install_hooks.sh", "docs/DEVELOPING.md"):
            with open(os.path.join(paths.REPO, rel), "r", encoding="utf-8") as fh:
                text = fh.read()
            for check in checks:
                self.assertIn(check, text, "%s does not run %s" % (rel, check))
        with open(os.path.join(paths.REPO, ".github", "workflows", "ci.yml"), "r", encoding="utf-8") as fh:
            self.assertIn("tools/sync_skills.py --check", fh.read())

    def test_live_check_pin_steps(self):
        """The live check sets the first PIN at a terminal and runs every later PIN step with --pin-stdin; a PIN
        piped into `script -q /dev/null` is discarded by the echo-off flush and the prompt hangs (live run O6)."""
        with open(DEVELOPING, "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("## Live check on a test profile")
        section = text[start:text.index("\n## ", start + 1)]
        flat = " ".join(section.split())
        self.assertIn("jh.py --human --pin-stdin <command>", flat)
        self.assertIn("Do not pipe the PIN into `script -q /dev/null`", flat)
        steps = re.findall(r"(?m)^(\d+)\. ", section)
        self.assertEqual(steps, [str(i) for i in range(1, len(steps) + 1)], "live check steps are numbered in order")

    def test_executables(self):
        for rel in ("install.sh", "uninstall.sh", "jobhunter", "macos/stay-awake.sh", "tools/install_hooks.sh",
                    "tests/fixtures/install/fake-openclaw"):
            self.assertTrue(os.access(os.path.join(paths.REPO, rel), os.X_OK), rel)


if __name__ == "__main__":
    unittest.main()
