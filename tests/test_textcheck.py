"""tools/textcheck.py rules (U7) and a clean run over the files U7 owns."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

import tests  # noqa: F401
from jobhunter import paths

sys.path.insert(0, os.path.join(paths.REPO, "tools"))
import textcheck  # noqa: E402

EN_DASH, EM_DASH, CURLY, ELLIPSIS, NBSP = chr(0x2013), chr(0x2014), chr(0x201C), chr(0x2026), chr(0xA0)
HOME_PREFIX = "/" + "Us" + "ers/"


def rules(rel: str, text: str) -> list[str]:
    return [f[2] for f in textcheck.check_text(rel, text)]


class TestRules(unittest.TestCase):
    def test_non_ascii_named(self):
        for ch, name in ((EN_DASH, "en dash"), (EM_DASH, "em dash"), (CURLY, "curly quote"),
                         (ELLIPSIS, "ellipsis"), (NBSP, "no-break space"), (chr(0x2212), "minus sign"),
                         (chr(0xE9), "non-ASCII character")):
            with self.subTest(name=name):
                f = textcheck.check_text("docs/x.md", "ok line\nbad %s here\n" % ch)
                self.assertEqual(len(f), 1)
                self.assertEqual(f[0][:3], (2, 5, "T-NONASCII"))
                self.assertIn(name, f[0][3])

    def test_home_paths(self):
        self.assertEqual(rules("README.md", "see %salex/repo" % HOME_PREFIX), ["T-HOMEPATH"])
        self.assertEqual(rules("README.md", "see /ho" + "me/alex/repo"), ["T-HOMEPATH"])
        self.assertEqual(rules("README.md", "see ~/openclaw-job-hunter and $HOME/x"), [])

    def test_dash_rules_only_in_templates(self):
        text = "Hi Alex - I saw your post -- nice.\n"
        self.assertEqual(sorted(rules("prompts/writer_brief.md", text)), ["T-DDASH", "T-SPACEDASH"])
        self.assertEqual(rules("agent-templates/scout/AGENTS.template.md", text).count("T-SPACEDASH"), 1)
        self.assertEqual(rules("openclaw/agents.patch.json5.tmpl", "a - b"), ["T-SPACEDASH"])
        self.assertEqual(rules("private.example/extra_info.example.md", "x - y"), ["T-SPACEDASH"])
        self.assertEqual(rules("docs/HOW-IT-WORKS.md", text), [])

    def test_dash_rule_exemptions(self):
        ok = ("- a bullet line\n  - nested bullet\n"
              "Run `jh.py preflight --lane scout` now.\n"
              "Inline `a - b` code is fine.\n"
              "```\ncmd -- arg - x\n```\n"
              "range 2024-2026 and well-known words\n")
        self.assertEqual(rules("skills-src/jobhunter-gate/SKILL.template.md", ok), [])

    def test_file_level(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "a.md"), "w", encoding="utf-8") as fh:
                fh.write("fine\n")
            with open(os.path.join(d, "b.png"), "wb") as fh:
                fh.write(b"\x89PNG\x00" + EM_DASH.encode("utf-8"))
            with open(os.path.join(d, "c.txt"), "w", encoding="utf-8") as fh:
                fh.write("bad " + EM_DASH + "\n")
            self.assertEqual(textcheck.check_file(d, "a.md"), [])
            self.assertEqual(textcheck.check_file(d, "b.png"), [])
            self.assertEqual(len(textcheck.check_file(d, "c.txt")), 1)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(textcheck.main(["--repo", d, os.path.join(d, "a.md")]), 0)
                self.assertEqual(textcheck.main(["--repo", d, os.path.join(d, "c.txt")]), 1)
        finally:
            shutil.rmtree(d)


class TestEncodingsAndIndex(unittest.TestCase):
    def test_utf16_text_is_decoded_and_flagged(self):
        d = tempfile.mkdtemp()
        try:
            for enc in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
                with open(os.path.join(d, "notes.txt"), "wb") as fh:
                    fh.write(("plain line\nbad " + EM_DASH + " here\n").encode(enc))
                got = [p.split(" ")[1] for p in textcheck.check_file(d, "notes.txt")]
                self.assertEqual(got, ["T-ENCODING", "T-NONASCII"], enc)
            self.assertIsNone(textcheck.text_encoding("x.bin", b"\x00\x01\x02\x03\xff\xfe\x00\x10abc"))
            self.assertEqual(textcheck.text_encoding("x.txt", b"hello\n"), "utf-8")
            self.assertIsNone(textcheck.text_encoding("x.png", b"hello\n"))
        finally:
            shutil.rmtree(d)

    @unittest.skipUnless(shutil.which("git"), "no git")
    def test_staged_reads_the_index(self):
        import subprocess
        d = tempfile.mkdtemp()
        env = dict(os.environ, HOME=d, GIT_CONFIG_NOSYSTEM="1")
        env.pop("GIT_INDEX_FILE", None)
        env.pop("GIT_DIR", None)
        try:
            subprocess.run(["git", "-C", d, "init", "-q"], env=env, check=True)
            with open(os.path.join(d, "a.md"), "w", encoding="utf-8") as fh:
                fh.write("bad " + EM_DASH + "\n")
            subprocess.run(["git", "-C", d, "add", "a.md"], env=env, check=True)
            with open(os.path.join(d, "a.md"), "w", encoding="utf-8") as fh:
                fh.write("fine\n")
            self.assertEqual(textcheck.staged_files(d), [("a.md", ("bad " + EM_DASH + "\n").encode("utf-8"))])
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(textcheck.main(["--repo", d]), 0)
                self.assertEqual(textcheck.main(["--repo", d, "--staged"]), 1)
            self.assertIn("a.md:1:5: T-NONASCII", out.getvalue())
            os.remove(os.path.join(d, "a.md"))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(textcheck.main(["--repo", d, "--staged"]), 1)
        finally:
            shutil.rmtree(d)


# Files and folders owned by U7 (design 11); they must pass every repo text rule.
U7_PATHS = ("README.md", "LICENSE", "SECURITY.md", "CHANGELOG.md", ".gitignore", "install.sh", "uninstall.sh",
            "jobhunter", "openclaw/agents.json", "openclaw/agents.patch.json5.tmpl", "openclaw/crons.json",
            "openclaw/exec-approvals.json5.tmpl", "scripts/jobhunter/install.py",
            "scripts/jobhunter/commands/install.py", "tools/sync_skills.py", "tools/textcheck.py",
            "tools/leakcheck.py", "tools/check_gitignore.py", "tools/install_hooks.sh", ".github/workflows/ci.yml",
            "macos", "docs/SETUP-macos.md", "docs/SETUP-linux-wsl.md", "docs/SAFETY-AND-TOS.md",
            "docs/HOW-IT-WORKS.md", "docs/TROUBLESHOOTING.md", "docs/PRIVACY.md", "docs/DEVELOPING.md",
            "docs/img/setup", "tests/test_install_render.py", "tests/test_textcheck.py", "tests/test_leakcheck.py",
            "tests/test_check_gitignore.py", "tests/test_repo_layout.py", "tests/test_install_sh.py",
            "tests/fixtures/install", "tests/fakes/u7")


def u7_files() -> list[str]:
    out = []
    for p in U7_PATHS:
        full = os.path.join(paths.REPO, p)
        if os.path.isdir(full):
            for dirpath, dirs, files in os.walk(full):
                dirs[:] = [x for x in dirs if x != "__pycache__"]
                out += [os.path.relpath(os.path.join(dirpath, f), paths.REPO) for f in files
                        if not f.endswith(".pyc") and f != ".DS_Store"]
        elif os.path.isfile(full):
            out.append(p)
    return out


class TestU7FilesClean(unittest.TestCase):
    def test_owned_files_exist_and_pass(self):
        files = u7_files()
        for must in ("README.md", "install.sh", "openclaw/crons.json", "tools/leakcheck.py"):
            self.assertIn(must, files)
        problems = []
        for rel in files:
            problems += textcheck.check_file(paths.REPO, rel)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
