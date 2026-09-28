"""install.sh, uninstall.sh and ./jobhunter under /bin/bash (3.2 on macOS) with set -u, against
tests/fixtures/install/fake-openclaw only (U7). Nothing here can reach a real OpenClaw: PATH holds only the
fake bin folder and the system folders, HOME is a temp folder, and the fake refuses to run without its log
and state variables.

The temporary repo copy uses a fictional template tree and the fake core commands from
tests/fakes/u7/fake_core_commands.py (U1's command modules are left out of the copy so the test is
deterministic; set JH_INSTALL_TEST_REAL_CORE=1 to run it with the real ones for integration)."""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import tests  # noqa: F401
from jobhunter import install as ins, paths
from tests.test_install_render import make_template_tree

BASH = "/bin/bash"
SCRIPTS = ("install.sh", "uninstall.sh", "jobhunter", "macos/stay-awake.sh", "tools/install_hooks.sh")
U1_COMMAND_MODULES = ("core.py", "limits.py", "maint.py", "gate.py")
FAKE_KEY = "test-key-not-real-0000"


def _ignore(_dir, names):
    return [n for n in names if n in ("__pycache__", ".DS_Store") or n.endswith(".pyc")]


class Sandbox:
    """A temp folder with a repo copy, a fake HOME and a bin folder holding only fakes."""

    def __init__(self, configured: bool = True, real_core: bool = False):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="jh-sh-"))
        self.repo = os.path.join(self.root, "openclaw-job-hunter")
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        self.log = os.path.join(self.root, "oc.log")
        self.state = os.path.join(self.root, "oc-state")
        for d in (self.repo, self.home, self.bin, os.path.join(self.root, "tmp")):
            os.makedirs(d)
        for name in ("install.sh", "uninstall.sh", "jobhunter", "config.example.json"):
            src = os.path.join(paths.REPO, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(self.repo, name))
        for d in ("scripts", "tools", "macos", "private.example", "shared-skills"):
            src = os.path.join(paths.REPO, d)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(self.repo, d), ignore=_ignore)
        shutil.copytree(os.path.join(paths.REPO, "openclaw"), os.path.join(self.repo, "openclaw"),
                        ignore=shutil.ignore_patterns("plugins", "__pycache__"))
        plugin = os.path.join(self.repo, "openclaw", "plugins", "jobhunter-guard")
        os.makedirs(plugin)
        with open(os.path.join(plugin, "openclaw.plugin.json"), "w") as fh:
            json.dump({"id": "jobhunter-guard", "note": "stub for the install test"}, fh)
        make_template_tree(self.repo, ins.load_agents(paths.REPO))
        cmds = os.path.join(self.repo, "scripts", "jobhunter", "commands")
        if not real_core and os.environ.get("JH_INSTALL_TEST_REAL_CORE") != "1":
            for m in U1_COMMAND_MODULES:
                if os.path.exists(os.path.join(cmds, m)):
                    os.remove(os.path.join(cmds, m))
        shutil.copy2(os.path.join(paths.REPO, "tests", "fakes", "u7", "fake_core_commands.py"),
                     os.path.join(cmds, "zz_u7_fake.py"))
        shutil.copy2(os.path.join(paths.REPO, "tests", "fixtures", "install", "fake-openclaw"),
                     os.path.join(self.bin, "openclaw"))
        self._script("claude", 'if [ "${1:-} ${2:-}" = "auth status" ]; then echo "Logged in (fake)"; exit 0; fi\n'
                               'exit 1\n')
        self._script("curl", 'echo "curl $*" >> "%s/curl.log"\nexit 97\n' % self.root)
        self._script("launchctl", 'echo "launchctl $*" >> "%s/launchctl.log"\nexit 0\n' % self.root)
        self.env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C",
                    "TMPDIR": os.path.join(self.root, "tmp"), "FAKE_OC_LOG": self.log, "FAKE_OC_STATE": self.state,
                    "FAKE_OC_CONFIGURED": "1" if configured else "0"}

    def _script(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as fh:
            fh.write("#!/bin/bash\n" + body)
        os.chmod(p, 0o755)

    def run(self, script, *args, env=None):
        e = dict(self.env, **(env or {}))
        p = subprocess.run([BASH, os.path.join(self.repo, script)] + list(args), cwd=self.repo, env=e,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def run_tty(self, script, *args, answers=(), env=None, timeout=120):
        """Run a script with stdin, stdout and stderr on a pseudo terminal. answers: (prompt, reply) pairs
        typed in order, each once its prompt has appeared. A reply to a hidden prompt (read -s) waits until the
        terminal echo is off, so it is not flushed by the mode switch."""
        import pty
        import termios
        e = dict(self.env, **(env or {}))
        m, s_fd = pty.openpty()
        p = subprocess.Popen([BASH, os.path.join(self.repo, script)] + list(args), cwd=self.repo, env=e,
                             stdin=s_fd, stdout=s_fd, stderr=s_fd, start_new_session=True)
        os.close(s_fd)
        pending = list(answers)
        out, pos = b"", 0
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if pending:
                    i = out.find(pending[0][0], pos)
                    if i >= 0:
                        prompt, reply = pending.pop(0)
                        hidden = b"PIN" in prompt
                        until = time.monotonic() + 5
                        while hidden and time.monotonic() < until and termios.tcgetattr(m)[3] & termios.ECHO:
                            time.sleep(0.02)
                        time.sleep(0.05)
                        os.write(m, reply)
                        pos = i + len(prompt)
                        continue
                r, _, _ = select.select([m], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(m, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        out += chunk
                        continue
                if p.poll() is not None:
                    while select.select([m], [], [], 0.05)[0]:
                        try:
                            chunk = os.read(m, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        out += chunk
                    break
            else:
                p.kill()
            rc = p.wait(timeout=10)
        finally:
            os.close(m)
        return rc, out.decode("utf-8", "replace").replace("\r\n", "\n")

    def jh(self, *args, stdin=b""):
        e = dict(self.env)
        p = subprocess.run([sys.executable, os.path.join(self.repo, "scripts", "jh.py")] + list(args), cwd=self.repo,
                           env=e, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def calls(self) -> list[list[str]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(l)["argv"] for l in fh if l.strip()]

    def log_entries(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def oc_state(self) -> dict:
        with open(os.path.join(self.state, "state.json")) as fh:
            return json.load(fh)

    def home_json(self) -> dict:
        with open(os.path.join(self.repo, "private", "home.json")) as fh:
            return json.load(fh)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def strip_profile(argv):
    return argv[2:] if argv[:1] == ["--profile"] else argv


def find(calls, *prefix):
    return [c for c in calls if strip_profile(c)[:len(prefix)] == list(prefix)]


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestSyntax(unittest.TestCase):
    def test_bash_parses_every_script(self):
        for s in SCRIPTS:
            p = subprocess.run([BASH, "-n", os.path.join(paths.REPO, s)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            self.assertEqual(p.returncode, 0, "%s: %s" % (s, p.stdout.decode()))

    @unittest.skipUnless(sys.platform == "darwin", "macOS ships bash 3.2 as /bin/bash")
    def test_macos_bash_is_3_2(self):
        out = subprocess.run([BASH, "--version"], stdout=subprocess.PIPE).stdout.decode()
        self.assertIn("version 3.2", out)

    def test_scripts_use_strict_mode_and_no_bash4_features(self):
        for s in ("install.sh", "uninstall.sh", "jobhunter"):
            with open(os.path.join(paths.REPO, s)) as fh:
                text = fh.read()
            self.assertIn("set -euo pipefail", text, s)
            for bash4 in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "coproc", "${!OC_ARGS"):
                self.assertNotIn(bash4, text, "%s uses %s" % (s, bash4))


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestInstallFlow(unittest.TestCase):
    """One sandbox, run in order: install, re-install, wrapper, uninstall."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox(configured=True)
        cls.rc, cls.out = cls.sb.run("install.sh", "--yes")
        cls.calls_after_install = cls.sb.calls()

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def test_1_install_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn("DONE install complete", self.out)
        for n in range(1, 18):
            self.assertIn("[%d] " % n, self.out)
        # without a terminal the optional and consent steps are skipped, never answered for the person
        self.assertIn("SKIP site consent", self.out)
        self.assertIn("SKIP email finder keys", self.out)
        self.assertNotIn("app password", self.out.split("[17] Finish")[1].split("Optional:")[0])
        self.assertFalse(os.path.exists(os.path.join(self.sb.root, "curl.log")), "curl must not run")
        self.assertFalse(os.path.exists(os.path.join(self.sb.root, "launchctl.log")), "stay-awake is opt-in")

    def test_2_openclaw_calls(self):
        calls = self.calls_after_install
        self.assertEqual(find(calls, "browser", "import-profile"), [])        # no copy without the owner's consent
        self.assertEqual(find(calls, "browser", "cookies"), [])
        self.assertTrue(all(c[:1] != ["--profile"] for c in calls))          # default profile
        self.assertEqual(find(calls, "onboard"), [])                          # already configured: never re-onboard
        adds = find(calls, "agents", "add")
        self.assertEqual([c[2] for c in adds], ["jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier",
                                                "jobhunter-outreach", "jobhunter-qc"])
        ws_root = self.sb.home_json()["ws_root"]
        self.assertTrue(ws_root.startswith(os.path.join(self.sb.home, ".openclaw-job-hunter")))
        for c in adds:
            self.assertIn("--non-interactive", c)
            self.assertTrue(c[c.index("--workspace") + 1].startswith(ws_root + "/"))
        self.assertEqual(len(find(calls, "models", "auth", "login")), 5)
        patches = find(calls, "config", "patch")
        self.assertIn("--dry-run", patches[0])
        self.assertGreaterEqual(len(patches), 3)                              # dry run, agents, guard
        self.assertEqual(len(find(calls, "config", "validate")), 1)
        self.assertEqual(find(calls, "plugins", "install")[0][2:],
                         ["--link", os.path.join(self.sb.repo, "openclaw", "plugins", "jobhunter-guard")])
        self.assertIn("--accept-capabilities", find(calls, "plugins", "enable")[0])
        self.assertEqual(len(find(calls, "browser", "create-profile")), 1)
        cron_adds = find(calls, "cron", "add")
        self.assertEqual(len(cron_adds), 13)
        for c in cron_adds:
            self.assertEqual(c[-2:], ["--no-deliver", "--disabled"])
        self.assertEqual(len(find(calls, "agent")), 5)
        flat = json.dumps(calls)
        for never in ("--reset", "cron enable", '"enable"', "bindings", "--force"):
            if never == '"enable"':
                self.assertEqual(find(calls, "cron", "enable"), [])
                continue
            self.assertNotIn(never, flat)
        self.assertEqual(find(calls, "cron", "run"), [])

    def test_3_resulting_state(self):
        st = self.sb.oc_state()
        self.assertEqual(len(st["crons"]), 13)
        self.assertFalse(any(v["enabled"] for v in st["crons"].values()))
        self.assertEqual(sorted(st["approvals"]["agents"]), sorted(["main", "jobhunter-scout", "jobhunter-evaluator",
                                                                    "jobhunter-applier", "jobhunter-outreach",
                                                                    "jobhunter-qc"]))
        self.assertEqual(st["approvals"]["agents"]["main"], {"security": "full"})
        home = self.sb.home_json()
        self.assertEqual(len(home["cron_jobs"]), 13)
        with open(os.path.join(self.sb.repo, "state", "install-manifest.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["cron_jobs"], home["cron_jobs"])
        self.assertIn("jobhunter-qc", manifest["agents"])
        agents_md = os.path.join(home["ws_root"], "applier", "AGENTS.md")
        with open(agents_md) as fh:
            text = fh.read()
        self.assertIn(self.sb.repo + "/scripts/jh.py", text)
        self.assertFalse(os.path.exists(os.path.join(self.sb.repo, "workspaces")))
        self.assertEqual(os.stat(os.path.join(self.sb.repo, "private")).st_mode & 0o777, 0o700)
        self.assertTrue(os.path.isabs(home["upload_root"]))                  # resolved upload root (13.1 item 11)
        self.assertIn("browser upload folder: " + home["upload_root"], self.out)
        # without --chat-control the shared skill is neither rendered nor added to extraDirs
        self.assertFalse(os.path.exists(os.path.join(self.sb.repo, "shared-skills", "jobhunter-control", "SKILL.md")))
        self.assertIsNone(st.get("extradirs"))

    def test_4_rerun_is_idempotent(self):
        before = self.sb.oc_state()
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        for skipped in ("SKIP OpenClaw is installed", "SKIP all jobhunter agents exist",
                        "SKIP jobhunter-guard is installed", "SKIP browser profile jobhunter exists",
                        "SKIP model test turns"):
            self.assertIn(skipped, out)
        calls = self.sb.calls()
        self.assertEqual(len(find(calls, "agents", "add")), 5)
        self.assertEqual(len(find(calls, "plugins", "install")), 1)
        self.assertEqual(len(find(calls, "browser", "create-profile")), 1)
        after = self.sb.oc_state()
        self.assertEqual({k: v["id"] for k, v in after["crons"].items()},
                         {k: v["id"] for k, v in before["crons"].items()})
        self.assertEqual(after["approvals"], before["approvals"])

    def test_5_wrapper(self):
        rc, out = self.sb.run("jobhunter", "help")
        self.assertEqual(rc, 0, out)
        self.assertIn("stop-now", out)
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("jobhunter", "pause")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.sb.repo, "state", "PAUSED")))
        disabled = find(self.sb.calls()[n0:], "cron", "disable")
        crons = ins.load_crons(paths.REPO)
        self.assertEqual(len(disabled), len(ins.select_jobs(crons, "pause")))
        ids = self.sb.home_json()["cron_jobs"]
        self.assertNotIn(["cron", "disable", ids["jobhunter:sheet-sync"]], disabled)
        rc, out = self.sb.run("jobhunter", "run", "scout")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.sb.calls()[-1], ["cron", "run", ids["jobhunter:scout"], "--wait", "--wait-timeout", "30m"])
        rc, out = self.sb.run("jobhunter", "resume")
        self.assertNotEqual(rc, 0)                     # needs the PIN at a terminal
        self.assertIn("PIN", out)
        rc, out = self.sb.run("jobhunter", "no-such-command")
        self.assertNotEqual(rc, 0)
        rc, out = self.sb.run("jobhunter", "doctor")
        # the default route is web_ui: no app password, and email stays off until Gmail is allowed
        self.assertIn("WARN  email is off: gmail.route is web_ui and Gmail is not allowed", out)
        self.assertIn("info  sites the agent may use: none", out)
        self.assertNotIn("mail test\n", out.replace("skip  mail test", ""))

    def test_6_uninstall(self):
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("uninstall.sh", "--yes")
        self.assertEqual(rc, 0, out)
        if sys.platform == "darwin":                  # keys may sit in the Keychain: asked, never removed silently
            self.assertIn("kept any email finder keys (./jobhunter enrich disconnect --all removes them)", out)
        self.assertIn("kept the jobhunter browser profile", out)
        calls = self.sb.calls()[n0:]
        self.assertEqual(len(find(calls, "cron", "rm")), 13)
        self.assertEqual(len(find(calls, "agents", "delete")), 5)
        self.assertEqual(len(find(calls, "plugins", "uninstall")), 1)
        st = self.sb.oc_state()
        self.assertEqual(st["crons"], {})
        self.assertEqual(st["agents"], [])
        self.assertEqual(sorted(st["approvals"]["agents"]), ["main"])
        self.assertIn("jobhunter", st["profiles"])                          # kept unless asked
        last_patch = st["patches"][-1]
        self.assertIsNone(last_patch["agents"]["entries"]["jobhunter-applier"])
        self.assertTrue(os.path.isdir(os.path.join(self.sb.repo, "private")))   # data kept without --purge
        self.assertEqual(find(calls, "browser", "delete-profile"), [])


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestProfileAndApiKey(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(configured=False)

    def tearDown(self):
        self.sb.close()

    def test_profile_is_used_everywhere_and_bound(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key", "--profile", "jhtest",
                              env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(rc, 0, out)
        calls = self.sb.calls()
        for c in calls:
            if c != ["--version"]:
                self.assertEqual(c[:2], ["--profile", "jhtest"], c)
        onboard = find(calls, "onboard")
        self.assertEqual(len(onboard), 1)
        self.assertIn("apiKey", onboard[0])
        # the key goes to onboarding through its environment, never on a command line
        self.assertNotIn(FAKE_KEY, json.dumps(calls))
        self.assertNotIn("--anthropic-api-key", onboard[0])
        entry = [e for e in self.sb.log_entries() if strip_profile(e["argv"])[:1] == ["onboard"]][0]
        self.assertTrue(entry["env_api_key"])
        self.assertEqual(find(calls, "models", "auth", "login"), [])
        self.assertEqual(self.sb.home_json()["oc_profile"], "jhtest")
        for dirpath, _dirs, files in os.walk(self.sb.repo):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    self.assertNotIn(FAKE_KEY.encode(), fh.read(), os.path.join(dirpath, f))
        n0 = len(calls)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--profile", "other")
        self.assertNotEqual(rc, 0)
        self.assertIn("One clone is one install", out)
        self.assertEqual([c for c in self.sb.calls()[n0:] if c != ["--version"]], [])
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key", env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(rc, 0, out)                                          # re-run keeps the recorded profile
        self.assertTrue(all(c[:2] == ["--profile", "jhtest"] for c in self.sb.calls()[n0:] if c != ["--version"]))

    def test_api_key_flag_only_as_a_fallback(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key",
                              env={"ANTHROPIC_API_KEY": FAKE_KEY, "FAKE_OC_ONBOARD_NEEDS_FLAG": "1"})
        self.assertEqual(rc, 0, out)
        onboard = find(self.sb.calls(), "onboard")
        self.assertEqual(len(onboard), 2)
        self.assertNotIn("--anthropic-api-key", onboard[0])
        self.assertIn("--anthropic-api-key", onboard[1])
        self.assertIn("needs the key as an argument", out)
        self.assertNotIn(FAKE_KEY, out)

    def test_bad_arguments_and_missing_claude(self):
        rc, out = self.sb.run("install.sh", "--bogus")
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown option", out)
        rc, out = self.sb.run("install.sh", "--profile", "Bad Name")
        self.assertNotEqual(rc, 0)
        os.remove(os.path.join(self.sb.bin, "claude"))
        rc, out = self.sb.run("install.sh", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("claude auth login", out)
        self.assertEqual(find(self.sb.calls(), "onboard"), [])


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestChatControlAndPurge(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(configured=True)
        self.skill = os.path.join(self.sb.repo, "shared-skills", "jobhunter-control", "SKILL.md")

    def tearDown(self):
        self.sb.close()

    def test_chat_control_renders_the_skill_and_uninstall_removes_it(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--chat-control")
        self.assertEqual(rc, 0, out)
        with open(self.skill) as fh:
            text = fh.read()
        py = self.sb.home_json()["python"]
        self.assertIn("%s %s/scripts/jh.py status --human" % (py, self.sb.repo), text)
        self.assertNotIn("__REPO__", text)
        self.assertNotIn("__PY__", text)
        shared = os.path.join(self.sb.repo, "shared-skills")
        self.assertEqual(self.sb.oc_state()["extradirs"], [shared])
        self.assertTrue(os.path.isfile(os.path.join(shared, "jobhunter-control", "SKILL.md")))
        # a later re-run without the flag (./jobhunter update) re-renders it
        os.remove(self.skill)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(self.skill))
        rc, out = self.sb.run("uninstall.sh", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.skill))
        self.assertTrue(os.path.isfile(os.path.join(shared, "jobhunter-control", "SKILL.template.md")))
        self.assertEqual(self.sb.oc_state()["extradirs"], [])

    def test_upload_root_option(self):
        up = os.path.join(self.sb.root, "gw-tmp", "openclaw", "uploads")
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--upload-root", up)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.sb.home_json()["upload_root"], os.path.realpath(up))
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--upload-root", "relative/dir")
        self.assertNotEqual(rc, 0)
        self.assertIn("upload folder", out)

    def test_old_sqlite_stops_the_preflight(self):
        # a python3 wrapper whose sqlite3 reports 3.23: preflight refuses before touching openclaw
        fake = os.path.join(self.sb.root, "oldpy")
        os.makedirs(fake)
        with open(os.path.join(fake, "sitecustomize.py"), "w") as fh:
            fh.write("import sqlite3\nsqlite3.sqlite_version_info = (3, 23, 1)\nsqlite3.sqlite_version = '3.23.1'\n")
        self.sb._script("python3", 'PYTHONPATH="%s" exec "%s" "$@"\n' % (fake, sys.executable))
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertNotEqual(rc, 0)
        self.assertIn("SQLite 3.24.0 or newer is needed", out)
        self.assertEqual([c for c in self.sb.calls() if c != ["--version"]], [])

    def test_purge_forgets_the_mail_secret_before_deleting_private(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.sb.repo, "private", "secrets.json"), "w") as fh:
            json.dump({"mail_account": "alex.rivera@example.com", "mail_store": "file",
                       "mail_app_password": "abcdefghijklmnop"}, fh)
        rc, out = self.sb.run_tty("uninstall.sh", "--purge", "--yes",
                                  answers=((b"Type delete to confirm", b"delete\n"),
                                           (b"Type delete private to confirm", b"delete private\n")))
        self.assertEqual(rc, 0, out)
        self.assertIn("no Keychain item to delete", out)          # file store: nothing in the Keychain
        self.assertLess(out.index("no Keychain item to delete"), out.index("deleted private/"))
        self.assertFalse(os.path.exists(os.path.join(self.sb.repo, "private")))


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestPinChangeWithRealCore(unittest.TestCase):
    """./jobhunter pin set against U1's real auth set-pin (the fakes hid a protocol mismatch)."""

    def setUp(self):
        self.sb = Sandbox(configured=True, real_core=True)
        rc, out = self.sb.jh("--human", "init")
        self.assertEqual(rc, 0, out)
        code = ("import sys; sys.path.insert(0, %r); from jobhunter import auth; auth.set_pin(None, '135790')"
                % os.path.join(self.sb.repo, "scripts"))
        p = subprocess.run([sys.executable, "-c", code], env=self.sb.env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        self.assertEqual(p.returncode, 0, p.stdout)

    def tearDown(self):
        self.sb.close()

    def check(self, pin):
        return self.sb.jh("--human", "--pin-stdin", "auth", "check", stdin=pin.encode() + b"\n")

    def test_change_pin(self):
        self.assertEqual(self.check("135790")[0], 0)
        rc, out = self.sb.run_tty("jobhunter", "pin", "set",
                                  answers=((b"Current PIN: ", b"135790\n"), (b"New PIN (6 to 12 digits): ", b"246801\n"),
                                           (b"New PIN again: ", b"246801\n")))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("differ", out)
        self.assertEqual(self.check("246801")[0], 0)
        self.assertNotEqual(self.check("135790")[0], 0)

    def test_mismatched_new_pins_change_nothing(self):
        rc, out = self.sb.run_tty("jobhunter", "pin", "set",
                                  answers=((b"Current PIN: ", b"135790\n"), (b"New PIN (6 to 12 digits): ", b"246801\n"),
                                           (b"New PIN again: ", b"246802\n")))
        self.assertNotEqual(rc, 0)
        self.assertIn("the two new PINs differ", out)
        self.assertEqual(self.check("135790")[0], 0)



def _real_core_sandbox(test) -> Sandbox:
    sb = Sandbox(configured=True, real_core=True)
    rc, out = sb.jh("--human", "init")
    test.assertEqual(rc, 0, out)
    code = ("import sys; sys.path.insert(0, %r); from jobhunter import auth; auth.set_pin(None, '135790')"
            % os.path.join(sb.repo, "scripts"))
    p = subprocess.run([sys.executable, "-c", code], env=sb.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    test.assertEqual(p.returncode, 0, p.stdout)
    return sb


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestWrapperPassThrough(unittest.TestCase):
    """Commands that product messages and docs name reach jh.py (with the PIN when they need the owner)."""

    @classmethod
    def setUpClass(cls):
        cls.sb = _real_core_sandbox(cls())

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def run_wrapper(self, *args):
        return self.sb.run("jobhunter", *args)

    def test_owner_commands_ask_for_the_pin(self):
        for args in (("qc", "golden"), ("answers", "add", "--key", "k", "--value", "v"),
                     ("profile", "answer", "--field", "Q4", "--value", "3"), ("resume", "base-review"),
                     ("mail", "import-history", "--days", "365"), ("mail", "connect", "--store", "file"),
                     ("tier", "set", "--platform", "gmail", "--tier", "moderate"), ("tier", "set", "gmail", "moderate"),
                     ("companies", "split", "C1", "--keys", "a,b"), ("contacts", "split", "P1", "--keys", "a,b"),
                     ("unpause", "--scope", "linkedin"), ("edit", "A7K2", "--text-file", "/dev/null")):
            rc, out = self.run_wrapper(*args)
            self.assertNotEqual(rc, 0, args)
            self.assertIn("needs your owner PIN at a terminal", out, args)
            self.assertNotIn("unknown command", out, args)
            self.assertNotIn("usage:", out, args)

    def test_open_commands_run(self):
        rc, out = self.run_wrapper("eval", "requeue", "--since-profile-change")
        self.assertEqual(rc, 0, out)
        self.assertIn("requeued", out)
        rc, out = self.run_wrapper("approvals")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_wrapper("config", "validate")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_wrapper("profile", "status")
        self.assertEqual(rc, 0, out)
        self.assertIn("input_files", out)

    def test_resume_steps_are_not_areas(self):
        rc, out = self.run_wrapper("resume", "base-build")
        self.assertIn("no base resume yet", out)        # the resume step ran (and needs the inference first)
        self.assertNotIn("not paused", out)
        rc, out = self.run_wrapper("pause", "--scope", "linkedin")
        self.assertEqual(rc, 0, out)
        self.assertIn("pause:linkedin", out)

    def test_refusals_and_usage(self):
        rc, out = self.run_wrapper("gate", "reserve", "--kind", "cold_email")
        self.assertNotEqual(rc, 0)
        self.assertIn("run by the agents or the automations", out)
        rc, out = self.run_wrapper("breaker")
        self.assertNotEqual(rc, 0)
        self.assertIn("usage: ./jobhunter breaker reset|status|trip", out)
        rc, out = self.run_wrapper("no-such-command")
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown command: no-such-command", out)

    def test_pin_commands_work_at_a_terminal(self):
        rc, out = self.sb.run_tty("jobhunter", "profile", "answer", "--field", "Q4", "--value", "3",
                                  answers=((b"Owner PIN: ", b"135790\n"),))
        self.assertEqual(rc, 0, out)
        self.assertIn("stored Q4", out)
        rc, out = self.sb.run_tty("jobhunter", "mail", "connect", "--store", "file",
                                  answers=((b"Owner PIN: ", b"135790\n"),))
        self.assertNotIn("unrecognized arguments", out)   # --store file reached mail connect
        self.assertIn("owner.gmail_address", out)          # which refuses the placeholder address

    def test_help_lists_the_pass_through_commands(self):
        rc, out = self.run_wrapper("help")
        self.assertEqual(rc, 0)
        for s in ("qc golden", "profile answer --field", "answers add", "eval requeue", "mail import-history",
                  "mail connect --store file", "companies split", "resume base-review"):
            self.assertIn(s, out)


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestInitWizard(unittest.TestCase):
    """./jobhunter init: the owner details step, and steps 6 and 7 judged by the files they must record."""

    def setUp(self):
        self.sb = _real_core_sandbox(self)
        rdir = os.path.join(self.sb.repo, "private", "resume")
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, "original.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 fictional")
        self.number = "+1000" + "0000001"

    def tearDown(self):
        self.sb.close()

    def owner(self):
        with open(os.path.join(self.sb.repo, "private", "config.json")) as fh:
            return json.load(fh)["owner"]

    def test_owner_step_and_agent_turns_that_record_nothing(self):
        ans = ((b"Your first name: ", b"Eve\n"), (b"Your last name: ", b"Quorrin\n"), (b"email signature", b"\n"),
               (b"send from: ", b"eve.quorrin@example.com\n"), (b"Links for your signature", b"\n"),
               (b"Chat app", b"\n"), (b"international form", self.number.encode() + b"\n"),
               (b"extra_info.md to add", b"n\n"), (b"Job titles", b"Data Analyst\n"), (b"City", b"Remote\n"))
        rc, out = self.sb.run_tty("jobhunter", "init", answers=ans)
        self.assertNotEqual(rc, 0, out)
        o = self.owner()
        self.assertEqual((o["first_name"], o["last_name"], o["gmail_address"]), ("Eve", "Quorrin", "eve.quorrin@example.com"))
        self.assertEqual(o["signature"]["full_name"], "Eve Quorrin")
        self.assertEqual(o["notify"], dict(o["notify"], channel="whatsapp", to=self.number))
        self.assertFalse(any("your-handle" in x for x in o["signature"]["links"]))
        self.assertIn("the guard now knows your chat number", out)
        guard_patch = [c for c in self.sb.calls() if strip_profile(c)[:2] == ["config", "patch"]]
        self.assertEqual(len(guard_patch), 1)
        self.assertIn("salary research turn recorded nothing", out)
        self.assertIn("profile inference recorded nothing", out)
        onb = os.path.join(self.sb.repo, "state", "onboarding")
        self.assertFalse(os.path.exists(os.path.join(onb, "infer.done")))
        self.assertFalse(os.path.exists(os.path.join(onb, "salary.done")))
        # a re-run (even with a stale marker of an older wizard) tries again instead of skipping
        with open(os.path.join(onb, "infer.done"), "w"):
            pass
        rc, out = self.sb.run_tty("jobhunter", "init", answers=((b"extra_info.md to add", b"n\n"),
                                                                (b"Job titles", b"Data Analyst\n"), (b"City", b"Remote\n")))
        self.assertNotEqual(rc, 0, out)
        self.assertIn("SKIP your details are set", out)
        self.assertNotIn("SKIP profile inferred", out)
        self.assertIn("profile inference recorded nothing", out)

    def test_recorded_steps_are_skipped(self):
        priv = os.path.join(self.sb.repo, "private")
        for rel in ("salary_research.json", "profile_inference.json", os.path.join("resume", "base.json")):
            with open(os.path.join(priv, rel), "w") as fh:
                fh.write("{}\n")
        ans = ((b"Your first name: ", b"Eve\n"), (b"Your last name: ", b"Quorrin\n"), (b"email signature", b"\n"),
               (b"send from: ", b"eve.quorrin@example.com\n"), (b"Links for your signature", b"\n"),
               (b"Chat app", b"none\n"), (b"extra_info.md to add", b"n\n"), (b"Owner PIN: ", b"\n"))
        rc, out = self.sb.run_tty("jobhunter", "init", answers=ans)
        self.assertIn("SKIP salary research recorded", out)
        self.assertIn("SKIP profile inferred", out)
        self.assertIn("== 8. Your preferences", out)           # stopped at the interview's PIN prompt
        self.assertEqual(self.owner()["notify"]["channel"], "none")


LOCAL_STATE = {"profile": {"profiles_order": ["Default", "Profile 1"], "info_cache": {
    "Default": {"name": "Personal", "user_name": "eve.quorrin@example.com", "hosted_domain": "NO_HOSTED_DOMAIN"},
    "Profile 1": {"name": "Kestrel", "user_name": "eve@kestrel.example", "hosted_domain": "kestrel.example"}}}}
GMAIL_DOMAINS = ["google.com", "mail.google.com", "accounts.google.com"]


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestBrowserConsent(unittest.TestCase):
    """./jobhunter browser consent|forget|check|login against the fake openclaw and the real core: the plain
    explanation comes first, every site defaults to No, the Chrome profile is picked by name (a work profile only
    with a typed word), the consent is recorded (PIN) before the cookies of only the allowed sites are copied, a
    read-only login check follows, and forget takes the consent back and clears the cookies."""

    def setUp(self):
        self.sb = _real_core_sandbox(self)
        for rel in (("Library", "Application Support", "Google", "Chrome"), (".config", "google-chrome")):
            d = os.path.join(self.sb.home, *rel)
            os.makedirs(d)
            with open(os.path.join(d, "Local State"), "w") as fh:
                json.dump(LOCAL_STATE, fh)
        self.consent_file = os.path.join(self.sb.repo, "private", "consent.json")
        self.sb.env["FAKE_OC_CONSENT_FILE"] = self.consent_file
        self.sb.env["FAKE_OC_GMAIL_ACCOUNT"] = "eve.quorrin@example.com"

    def tearDown(self):
        self.sb.close()

    def consent(self) -> dict:
        with open(self.consent_file) as fh:
            return json.load(fh)["sites"]

    def site_answers(self, yes=("gmail", "naukri")):
        rc, text = self.sb.jh("--human", "install", "consent-sites")
        self.assertEqual(rc, 0, text)
        out = []
        for line in text.strip().split("\n"):
            site, _status, _doms, label = line.split("\t")
            out.append((("Allow the agent to use your %s login? [y/N] " % label).encode(),
                        b"y\n" if site in yes else b"\n"))
        return out

    def grant(self, env=None):
        ans = self.site_answers() + [
            (b"Copy from Chrome (c) or log in by hand (m)? [c] ", b"\n"),
            (b"Type its number (Enter to cancel): ", b"2\n"),                  # the work profile first
            (b"Type work to use it anyway", b"\n"),                           # not confirmed: pick again
            (b"Type its number (Enter to cancel): ", b"1\n"),
            (b"Owner PIN: ", b"135790\n")]
        return self.sb.run_tty("jobhunter", "browser", "consent", answers=ans, env=env)

    def test_consent_needs_a_terminal_and_the_pin(self):
        rc, out = self.sb.run("jobhunter", "browser", "consent")
        self.assertNotEqual(rc, 0)
        self.assertIn("needs a terminal", out)
        rc, out = self.sb.run("jobhunter", "browser", "forget", "gmail")
        self.assertNotEqual(rc, 0)
        self.assertIn("needs your owner PIN at a terminal", out)
        self.assertFalse(os.path.exists(self.consent_file))
        self.assertEqual(find(self.sb.calls(), "browser", "import-profile"), [])
        rc, out = self.sb.run("jobhunter", "browser", "login", "linkedin")
        self.assertNotEqual(rc, 0)
        self.assertIn("may not use linkedin yet", out)
        rc, out = self.sb.run("jobhunter", "browser", "import")
        self.assertNotEqual(rc, 0)
        self.assertIn("no allowed site", out)

    def test_consent_copy_check_and_forget(self):
        rc, out = self.grant()
        self.assertEqual(rc, 0, out)
        # the explanation comes before the first question, and the work profile is flagged
        self.assertLess(out.index("Nothing is sent anywhere else"), out.index("Allow the agent to use your Gmail"))
        self.assertLess(out.index("Your normal Chrome is never opened"), out.index("Allow the agent to use your"))
        self.assertIn("WORK OR SCHOOL PROFILE", out)
        self.assertIn('"Kestrel" looks like a work or school profile', out)
        self.assertNotIn("app password", out.lower())
        rows = self.consent()
        self.assertEqual(os.stat(self.consent_file).st_mode & 0o777, 0o600)
        self.assertEqual(sorted(k for k, v in rows.items() if v["status"] == "granted"), ["gmail", "naukri"])
        self.assertEqual((rows["gmail"]["method"], rows["gmail"]["chrome_profile"], rows["gmail"]["chrome_profile_name"]),
                         ("chrome_import", "Default", "Personal"))
        self.assertEqual(rows["linkedin"]["status"], "declined")             # Enter means No
        st = self.sb.oc_state()
        self.assertEqual(len(st["imports"]), 1)
        imp = st["imports"][0]
        self.assertEqual((imp["system"], imp["browser"]), ("Default", "chrome"))
        self.assertEqual(imp["domains"], GMAIL_DOMAINS + ["naukri.com"])     # only the allowed sites
        self.assertEqual(imp["granted_at_copy"], ["gmail", "naukri"])        # recorded before the copy ran
        self.assertIn("ok    Gmail is logged in as eve.quorrin@example.com", out)
        self.assertIn("ok    Naukri looks logged in", out)
        # init asks only about sites nobody answered yet
        rc, out = self.sb.run_tty("jobhunter", "browser", "consent", "--new")
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP Gmail: answered before (granted)", out)
        self.assertIn("SKIP every site is answered", out)
        # forget one site: consent off, every cookie cleared, the sites still allowed copied again
        rc, out = self.sb.run_tty("jobhunter", "browser", "forget", "naukri", answers=((b"Owner PIN: ", b"135790\n"),))
        self.assertEqual(rc, 0, out)
        rows = self.consent()
        self.assertEqual(rows["naukri"]["status"], "revoked")
        self.assertEqual(rows["gmail"]["status"], "granted")
        st = self.sb.oc_state()
        self.assertEqual(st["cookie_clears"], 1)
        self.assertEqual(st["imports"][-1]["domains"], GMAIL_DOMAINS)
        self.assertEqual(st["cookie_domains"], GMAIL_DOMAINS)
        rc, out = self.sb.run("jobhunter", "browser", "sites")
        self.assertIn("taken back", out)
        rc, out = self.sb.run_tty("jobhunter", "browser", "forget", "--all", answers=((b"Owner PIN: ", b"135790\n"),))
        self.assertEqual(rc, 0, out)
        self.assertEqual({v["status"] for v in self.consent().values()} & {"granted"}, set())
        st = self.sb.oc_state()
        self.assertEqual((st["cookie_clears"], st["cookie_domains"], len(st["imports"])), (2, [], 2))

    def test_gmail_disclosure_comes_first_and_enter_keeps_gmail_off(self):
        ans = self.site_answers(yes=()) + [(b"Owner PIN: ", b"135790\n")]
        rc, out = self.sb.run_tty("jobhunter", "browser", "consent", answers=ans)
        self.assertEqual(rc, 0, out)
        flat = " ".join(out.split())
        ask = flat.index("Allow the agent to use your Gmail login? [y/N]")
        for words in ("Gmail is the exception: its login is your whole Google account session",
                      "What Yes gives the agent's profile: your Google account session",
                      "the google.com cookies are copied", "That session could open other Google services",
                      "The guard lets the agents use only mail.google.com and blocks every other Google service",
                      "./jobhunter browser forget gmail (PIN) clears those cookies"):
            self.assertLess(flat.index(words), ask, words)
        rows = self.consent()
        self.assertEqual(rows["gmail"]["status"], "declined")                  # Enter means No
        self.assertEqual({v["status"] for v in rows.values()}, {"declined"})
        self.assertEqual(find(self.sb.calls(), "browser", "import-profile"), [])
        self.assertIn("No site is allowed", out)

    def test_verification_prompt_stops_the_check(self):
        rc, out = self.grant(env={"FAKE_OC_CHECKPOINT": "mail.google"})
        self.assertEqual(rc, 0, out)
        self.assertIn("STOP  Gmail shows a CAPTCHA or a verification prompt", out)
        self.assertNotIn("Naukri looks logged in", out)                     # nothing more is checked
        self.assertIn("Some sites need you", out)

    def test_wrong_gmail_account_stops(self):
        rc, out = self.sb.jh("--human", "install", "owner", "--gmail", "eve.sender@example.com")
        self.assertEqual(rc, 0, out)
        rc, out = self.grant()
        self.assertIn("STOP  the Gmail account in the agent profile is eve.quorrin@example.com", out)
        self.assertIn("./jobhunter browser forget gmail", out)


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestEmailFinderWrapper(unittest.TestCase):
    """./jobhunter enrich: keys and paid-tier lookups are owner actions (PIN); connect needs a terminal."""

    @classmethod
    def setUpClass(cls):
        cls.sb = _real_core_sandbox(cls())

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def test_owner_actions(self):
        rc, out = self.sb.run("jobhunter", "enrich", "connect", "hunter")
        self.assertNotEqual(rc, 0)
        self.assertIn("enrich connect needs a terminal", out)
        for args in (("enrich", "disconnect", "--all"), ("enrich", "retry", "P1"), ("enrich", "test", "hunter"),
                     ("enrich", "find", "--contact", "P1", "--include-reserve"),
                     ("enrich", "find", "--contact", "P1", "--include-res")):
            rc, out = self.sb.run("jobhunter", *args)
            self.assertNotEqual(rc, 0, args)
            self.assertIn("needs your owner PIN at a terminal", out, args)
        rc, out = self.sb.run("jobhunter", "enrich")
        self.assertIn("usage: ./jobhunter enrich connect", out)
        rc, out = self.sb.run("jobhunter", "enrich", "budget")
        self.assertNotIn("PIN", out)

    def test_help_lists_the_new_commands(self):
        rc, out = self.sb.run("jobhunter", "help")
        self.assertIn("enrich connect <provider>", out)
        self.assertIn("browser forget <site>|--all", out)


if __name__ == "__main__":
    unittest.main()
