"""tools/check_gitignore.py and .gitignore (U7): runtime and personal files never reach git."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tests  # noqa: F401
from jobhunter import paths

sys.path.insert(0, os.path.join(paths.REPO, "tools"))
import check_gitignore  # noqa: E402

REFUSED = ("private/profile.json", "state/jobhunter.sqlite3", "logs/jh.log", "exports/jobs.csv",
           "workspaces/scout/AGENTS.md", "resumes/a.txt", "agent-templates/scout/AGENTS.md",
           "skills-src/jobhunter-gate/SKILL.md", "x/USER.md", "TOOLS.md", "HEARTBEAT.md", "a/MEMORY.md", "DREAMS.md",
           "BOOTSTRAP.md", "vendor/.git/config", "tests/fixtures/profile/resume.pdf", "docs/cv.docx",
           "tests/fixtures/shot.png", "img/logo.jpg", "data.db", "state.sqlite3-wal", ".env", ".env.local",
           "config/secrets.json")
ALLOWED = ("README.md", "agent-templates/scout/AGENTS.template.md", "skills-src/jobhunter-gate/SKILL.template.md",
           "docs/img/setup/terminal.png", "docs/img/sheets/menu.jpg", "private.example/profile.example.json",
           "tests/fixtures/install/fake-openclaw", "scripts/jobhunter/schema.sql", ".gitignore",
           ".github/workflows/ci.yml", "config.example.json")


class TestViolations(unittest.TestCase):
    def test_refused(self):
        for p in REFUSED:
            self.assertIsNotNone(check_gitignore.violation(p), p)

    def test_allowed(self):
        for p in ALLOWED:
            self.assertIsNone(check_gitignore.violation(p), p)
        self.assertIsNone(check_gitignore.violation("./README.md"))

    def test_main_paths(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check_gitignore.main(["--paths"] + list(ALLOWED)), 0)
            self.assertEqual(check_gitignore.main(["--paths", "README.md", "private/x.json"]), 1)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class TestGitignoreWithGit(unittest.TestCase):
    """The committed .gitignore keeps runtime files out of `git add -A` in a scratch repository."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="jh-gi-")
        shutil.copy(os.path.join(paths.REPO, ".gitignore"), os.path.join(self.d, ".gitignore"))
        self.git("init", "-q")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=self.d)
        return subprocess.run(["git", "-C", self.d] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=True, env=env).stdout.decode()

    def touch(self, rel):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("x\n")

    def test_add_all_keeps_runtime_out(self):
        for rel in REFUSED[:8] + ("state/jobhunter.sqlite3-wal", "notes.pdf", "shot.png", "node_modules/a.js",
                                  "scripts/__pycache__/a.pyc", ".DS_Store", "shared-skills/jobhunter-control/SKILL.md"):
            self.touch(rel)
        for rel in ALLOWED:
            if rel != ".gitignore":
                self.touch(rel)
        self.git("add", "-A")
        staged = self.git("ls-files").split()
        for rel in ALLOWED:
            self.assertIn(rel, staged)
        self.assertEqual(check_gitignore.check(staged), [])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check_gitignore.main(["--repo", self.d]), 0)

    def test_forced_add_is_caught(self):
        self.touch("private/profile.json")
        self.git("add", "-f", "private/profile.json")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check_gitignore.main(["--repo", self.d]), 1)


if __name__ == "__main__":
    unittest.main()
