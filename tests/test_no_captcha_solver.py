"""U1: there is no CAPTCHA-solving code path anywhere (FEATURES-OTP-ACCOUNTS-CAPTCHA 3.5). Every committed text file
(Python, TypeScript, JavaScript, templates, skills, JSON, docs) is searched for solver services, audio-challenge and
automation-hiding words; the code-owned page scripts never click, dispatch events, post messages or reach into a
frame. A CAPTCHA is always handed to the owner."""
from __future__ import annotations

import os
import re
import subprocess
import unittest

import tests  # noqa: F401
from jobhunter import pagefill, paths

# Assembled from parts, so this file itself does not match the scan below.
DENY = ["2" + "captcha", "anti" + "captcha", "anti-" + "captcha", "cap" + "solver", "cap" + "monster",
        "deathby" + "captcha", "nope" + "cha", "bus" + "ter", "audio " + "challenge", "solve" + "Captcha",
        "recaptcha-" + "solver", "hcaptcha-" + "solver", "navigator." + "webdriver", "ste" + "alth",
        "puppeteer-" + "extra", "unde" + "tected"]
TEXT_EXT = (".py", ".ts", ".js", ".mjs", ".json", ".md", ".gs", ".sh", ".txt", ".tmpl", ".json5", ".yml", ".csv",
            ".toml", "")


def repo_files() -> list:
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=paths.REPO,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        names = [os.path.relpath(os.path.join(d, n), paths.REPO) for d, _s, ns in os.walk(paths.REPO)
                 if "/.git" not in d and "node_modules" not in d for n in ns]
    return [n for n in names if os.path.splitext(n)[1] in TEXT_EXT and os.path.isfile(os.path.join(paths.REPO, n))]


class TestNoSolver(unittest.TestCase):
    def test_no_solver_words_in_the_repo(self):
        rx = re.compile("|".join(re.escape(w) for w in DENY), re.I)
        hits = []
        for name in repo_files():
            if name == "tests/test_no_captcha_solver.py":
                continue
            with open(os.path.join(paths.REPO, name), "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
            for m in rx.finditer(text):
                hits.append("%s: %s" % (name, m.group(0)))
        self.assertEqual(hits, [])

    def test_page_scripts_never_act_on_the_page(self):
        for name, script in pagefill.SCRIPTS.items():
            for bad in ("click(", "dispatchEvent", "postMessage", "contentWindow", "contentDocument", ".value =",
                        "submit(", "focus("):
                self.assertNotIn(bad, script, "%s uses %s" % (name, bad))
            self.assertTrue(script.startswith("() => {"), name)
            self.assertIn("jobhunter pagefill %s" % name, script)

    def test_page_scripts_compile(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node not found")
        for name, script in pagefill.SCRIPTS.items():
            res = subprocess.run([node, "-e", "new Function('return (' + process.argv[1] + ')')", script],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(res.returncode, 0, "%s: %s" % (name, res.stderr[-500:]))


if __name__ == "__main__":
    unittest.main()
