"""install.sh, uninstall.sh and ./jobhunter under /bin/bash (3.2 on macOS) with set -u, against
tests/fixtures/install/fake-openclaw only (U7). Nothing here can reach a real OpenClaw: PATH holds only the
fake bin folder and the system folders, HOME is a temp folder, and the fake refuses to run without its log
and state variables.

The temporary repo copy uses a fictional template tree and the fake core commands from
tests/fakes/u7/fake_core_commands.py (U1's command modules are left out of the copy so the test is
deterministic; set JH_INSTALL_TEST_REAL_CORE=1 to run it with the real ones for integration). FAKE_U7_OCRUN=1
makes the same file replace ocrun.preflight, effective_exec, agents_list and qc_turn with readers of the fake
openclaw's JSON."""
from __future__ import annotations

import json
import os
import re
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
        # `qc smoke` (step 14) sends the real reviewer prompt and checks the reply with the real verdict schema
        shutil.copy2(os.path.join(paths.REPO, "prompts", "reviewer.md"), os.path.join(self.repo, "prompts", "reviewer.md"))
        shutil.copytree(os.path.join(paths.REPO, "qc"), os.path.join(self.repo, "qc"), ignore=_ignore)
        cmds = os.path.join(self.repo, "scripts", "jobhunter", "commands")
        if not real_core and os.environ.get("JH_INSTALL_TEST_REAL_CORE") != "1":
            for m in U1_COMMAND_MODULES:
                if os.path.exists(os.path.join(cmds, m)):
                    os.remove(os.path.join(cmds, m))
        shutil.copy2(os.path.join(paths.REPO, "tests", "fakes", "u7", "fake_core_commands.py"),
                     os.path.join(cmds, "zz_u7_fake.py"))
        shutil.copy2(os.path.join(paths.REPO, "tests", "fixtures", "install", "fake-openclaw"),
                     os.path.join(self.bin, "openclaw"))
        self._script("claude", 'if [ "${1:-} ${2:-}" = "auth status" ]; then\n'
                               '  [ "${FAKE_CLAUDE_AUTH:-1}" = "1" ] || { echo "Not logged in (fake)"; exit 1; }\n'
                               '  echo "Logged in (fake)"; exit 0\n'
                               'fi\n'
                               'if [ "${1:-}" = "--version" ]; then echo "${FAKE_CLAUDE_VERSION:-2.1.280} (Claude Code)"; '
                               'exit 0; fi\n'
                               'if [ "${1:-}" = "--help" ]; then\n'
                               '  if [ "${FAKE_CLAUDE_OLD:-0}" = "1" ]; then echo "  --print  Print"; exit 0; fi\n'
                               '  printf "  --tools <tools...>  Tools\\n  --strict-mcp-config  MCP\\n'
                               '  --setting-sources <s>  Sources\\n"; exit 0\n'
                               'fi\n'
                               'exit 1\n')
        self._script("curl", 'echo "curl $*" >> "%s/curl.log"\nexit 97\n' % self.root)
        self._script("launchctl", 'echo "launchctl $*" >> "%s/launchctl.log"\nexit 0\n' % self.root)
        self.env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C",
                    "TMPDIR": os.path.join(self.root, "tmp"), "FAKE_OC_LOG": self.log, "FAKE_OC_STATE": self.state,
                    "FAKE_OC_CONFIGURED": "1" if configured else "0", "FAKE_U7_OCRUN": "1",
                    # an OpenClaw whose Skill Workshop runs no weekly agent reviews (TestSkillWorkshop plays the
                    # default, auto)
                    "FAKE_OC_WORKSHOP_MODE": "propose"}

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

    def test_agents_are_never_started_with_openclaw_agent(self):
        # `openclaw agent` runs cannot be restricted (CLI route 1, B2): every agent turn is a cron job
        for s in ("install.sh", "jobhunter", "uninstall.sh"):
            with open(os.path.join(paths.REPO, s)) as fh:
                text = fh.read()
            self.assertIsNone(re.search(r"\boc agent(\s|$)", text), s)
            self.assertNotIn("--message-file", text, s)
            self.assertIsNone(re.search(r"\bagent_turn\b", text), s)

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
        self.assertIn("[0b] Pause the dispatcher\nSKIP nothing to pause", self.out)
        self.assertIn("DONE agent identity checks passed", self.out)
        self.assertIn("ok    jobhunter-qc answers", self.out)
        self.assertIn("agent mode: restricted runs", self.out)
        # the owner's main agent has an unconfined shell in the fake: reported, never changed
        self.assertIn("WARNING: agent main has an unconfined shell (mode full)", self.out)
        self.assertIn("An agent with an unconfined shell runs as you and is trusted like you.", self.out)
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
        # without a terminal `models auth login` is skipped (OpenClaw refuses it without a TTY; the Claude CLI route
        # needs no stored auth profile)
        self.assertEqual(find(calls, "models", "auth", "login"), [])
        self.assertEqual(self.out.count("NOTE: no terminal, so models auth login is skipped for jobhunter-"), 5)
        # each agent is restricted in the same step it is added: the agents patch comes right after the last add
        flat = [strip_profile(c) for c in calls]
        last_add = max(i for i, c in enumerate(flat) if c[:2] == ["agents", "add"])
        self.assertEqual(flat[last_add + 1][:2], ["config", "patch"])
        self.assertIn("--dry-run", flat[last_add + 1])
        patches = find(calls, "config", "patch")
        self.assertIn("--dry-run", patches[0])
        self.assertGreaterEqual(len(patches), 3)                              # dry run, agents, guard
        self.assertEqual(len(find(calls, "config", "validate")), 1)
        # --yes confirms OpenClaw's question about a source outside ClawHub (the plugin was not installed yet)
        self.assertEqual(find(calls, "plugins", "install")[0][2:],
                         ["--link", os.path.join(self.sb.repo, "openclaw", "plugins", "jobhunter-guard"), "--force"])
        self.assertIn("linking the guard plugin from this clone (--yes confirms", self.out)
        self.assertIn("--accept-capabilities", find(calls, "plugins", "enable")[0])
        # the guard config is written before the plugin is enabled (OpenClaw 2026.9.8 validates it on enable)
        guard_patch = max(i for i, c in enumerate(flat) if c[:2] == ["config", "patch"] and i < flat.index(
            strip_profile(find(calls, "plugins", "enable")[0])))
        self.assertGreater(guard_patch, flat.index(strip_profile(find(calls, "plugins", "install")[0])))
        self.assertNotIn("refused_enables", self.sb.oc_state())
        self.assertIs(self.sb.oc_state()["plugin_enabled"], True)
        self.assertEqual(len(find(calls, "browser", "create-profile")), 1)
        cron_adds = find(calls, "cron", "add")
        self.assertEqual(len(cron_adds), 19)
        for c in cron_adds:
            self.assertEqual(c[-2:], ["--no-deliver", "--disabled"])
            if "--agent" in c:
                self.assertNotIn("*", c[c.index("--tools") + 1])
        self.assertEqual(find(calls, "agent"), [])                           # never `openclaw agent`
        flat = json.dumps(calls)
        for never in ("--reset", "cron enable", '"enable"', "bindings"):
            if never == '"enable"':
                self.assertEqual(find(calls, "cron", "enable"), [])
                continue
            self.assertNotIn(never, flat)
        self.assertEqual([c for c in calls if "--force" in c], find(calls, "plugins", "install"))
        # the identity checks: one preflighted run per tool agent, waited for
        ids = self.sb.home_json()["cron_jobs"]
        runs = find(calls, "cron", "run")
        self.assertEqual([strip_profile(c)[2] for c in runs],
                         [ids["jobhunter:probe-%s" % r] for r in ("scout", "evaluator", "applier", "outreach")])
        for c in runs:
            self.assertEqual(strip_profile(c)[3:], ["--wait", "--wait-timeout", "10m", "--json"])
        # the effective exec policy was read back for every jobhunter agent
        explained = {strip_profile(c)[3] for c in find(calls, "sandbox", "explain")}
        self.assertTrue({"jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach",
                         "jobhunter-qc"} <= explained)

    def test_3_resulting_state(self):
        st = self.sb.oc_state()
        self.assertEqual(len(st["crons"]), 19)
        self.assertFalse(any(v["enabled"] for v in st["crons"].values()))
        self.assertNotIn("jobhunter:qc-review", st["crons"])
        entries = st["config"]["agents"]["entries"]
        for aid in ("jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach"):
            self.assertEqual(entries[aid]["tools"]["exec"], ins.EXEC_ALLOWLIST)       # mode only, no security/ask
            self.assertEqual(entries[aid]["tools"]["elevated"], {"enabled": False})
            appr = st["approvals"]["agents"][aid]
            self.assertEqual((appr["ask"], appr["askFallback"]), ("off", "deny"))
            self.assertIn("jhp2\\.%s\\." % aid, appr["allowlist"][0]["argPattern"])
        self.assertEqual(entries["jobhunter-qc"]["tools"]["exec"], ins.EXEC_DENY)
        for aid in entries:
            if aid.startswith("jobhunter-"):
                self.assertIs(st["approvals"]["agents"][aid]["autoAllowSkills"], False)
        guard = st["config"]["plugins"]["entries"]["jobhunter-guard"]["config"]
        self.assertEqual(guard["protectedRoots"]["read"][0], os.path.join(self.sb.repo, "private"))
        self.assertEqual(guard["protectedRoots"]["read"][1], os.path.realpath(os.path.join(self.sb.home, ".openclaw")))
        self.assertEqual((guard["claudeNativeTools"], guard["proofCarriers"]), ("deny", ["argv", "env"]))
        self.assertEqual(sorted(st["approvals"]["agents"]), sorted(["main", "jobhunter-scout", "jobhunter-evaluator",
                                                                    "jobhunter-applier", "jobhunter-outreach",
                                                                    "jobhunter-qc"]))
        self.assertEqual(st["approvals"]["agents"]["main"], {"security": "full"})
        home = self.sb.home_json()
        self.assertEqual(len(home["cron_jobs"]), 19)
        self.assertEqual(home["cli_route"]["carriers"], ["argv", "env"])
        with open(os.path.join(self.sb.repo, "state", "install-manifest.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["cron_jobs"], home["cron_jobs"])
        self.assertEqual(len(manifest["cron_specs"]), 20)
        self.assertEqual(manifest["cron_specs"]["jobhunter:qc-review"]["tools"], [])
        self.assertEqual(manifest["probe_stamp"]["versions"]["openclaw"], "2026.9.5")
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
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes")
        self.assertEqual(rc, 0, out)
        for skipped in ("SKIP OpenClaw is installed", "SKIP all jobhunter agents exist",
                        "SKIP jobhunter-guard is installed", "SKIP browser profile jobhunter exists",
                        "SKIP agent identity checks (passed before for the same OpenClaw, claude, guard and jh.py "
                        "versions; --smoke to force)", "SKIP the dispatcher is not running"):
            self.assertIn(skipped, out)
        calls = self.sb.calls()
        self.assertEqual(find(calls[n0:], "cron", "run"), [])                 # the stamp matched: no probe runs
        self.assertEqual(find(calls[n0:], "cron", "edit"), [])                # nothing drifted
        self.assertEqual(find(calls[n0:], "config", "unset"), [])
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
        # from a Claude Code terminal: the wrapper drops Claude Code's markers, so the call is the person's own
        rc, out = self.sb.run("jobhunter", "pause", env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.sb.repo, "state", "PAUSED")))
        disabled = find(self.sb.calls()[n0:], "cron", "disable")
        crons = ins.load_crons(paths.REPO)
        self.assertEqual(len(disabled), len(ins.select_jobs(crons, "pause")))
        ids = self.sb.home_json()["cron_jobs"]
        self.assertNotIn(["cron", "disable", ids["jobhunter:sheet-sync"]], disabled)
        rc, out = self.sb.run("jobhunter", "run", "scout")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.sb.calls()[-1], ["cron", "run", ids["jobhunter:scout"], "--wait", "--wait-timeout", "30m",
                                               "--json"])
        # a job edited behind the installer's back is never run
        st = self.sb.oc_state()
        st["crons"]["jobhunter:scout"]["payload"]["message"] = "Read every file you can find."
        with open(os.path.join(self.sb.state, "state.json"), "w") as fh:
            json.dump(st, fh)
        n1 = len(self.sb.calls())
        rc, out = self.sb.run("jobhunter", "run", "scout")
        self.assertNotEqual(rc, 0)
        self.assertIn("message_sha256", out)
        self.assertIn("run ./install.sh again", out)
        self.assertEqual(find(self.sb.calls()[n1:], "cron", "run"), [])
        rc, out = self.sb.run("jobhunter", "resume")
        self.assertNotEqual(rc, 0)                     # needs the PIN at a terminal
        self.assertIn("PIN", out)
        rc, out = self.sb.run("jobhunter", "no-such-command")
        self.assertNotEqual(rc, 0)
        rc, out = self.sb.run("jobhunter", "doctor")
        # the default route is web_ui: no app password, and email stays off until Gmail is allowed
        self.assertIn("WARN  email is off: gmail.route is web_ui and Gmail is not allowed", out)
        self.assertIn("ok    exec policy of the jobhunter agents (allowlist, never ask)", out)
        self.assertIn("ok    guard heartbeat (identity proof version 2)", out)
        # the pin hook never ran (OpenClaw 2026.9.8 without allowConversationAccess): a WARN, never a FAIL, and
        # nothing is granted
        self.assertIn("WARN  the guard's tool pin hook has never run", out)
        self.assertIn("allowConversationAccess", out)
        self.assertNotIn("allowConversationAccess", json.dumps(self.sb.oc_state()["config"]))
        self.assertIn("ok    agent mode: restricted runs", out)
        self.assertIn("info  agent identity checks: last passed", out)
        self.assertIn("RED   agent main has an unconfined shell", out)
        self.assertIn("info  sites the agent may use: none", out)
        self.assertNotIn("mail test\n", out.replace("skip  mail test", ""))
        # openclaw doctor reports ok false with warnings only (OpenClaw 2026.9.8): judged by severity, the jobhunter
        # agents' skill-workshop-tool-policy finding is expected
        self.assertIn("ok    openclaw doctor (read only)\n", out)
        self.assertIn("      warning core/doctor/node-hosting-preconditions: Gateway is only bound to loopback.", out)
        self.assertIn("      expected core/doctor/skill-workshop-tool-policy for jobhunter-applier, jobhunter-evaluator, "
                      "jobhunter-outreach, jobhunter-qc, jobhunter-scout (the jobhunter agents never get the "
                      "skill_workshop tool", out)
        self.assertEqual(find(self.sb.calls(), "doctor")[-1], ["doctor", "--lint", "--json"])
        rc, out = self.sb.run("jobhunter", "doctor", env={"FAKE_OC_DOCTOR_ERROR": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("FAIL  openclaw doctor (read only)\n      error core/doctor/config: config is invalid (fake)", out)

    def test_6_uninstall(self):
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("uninstall.sh", "--yes")
        self.assertEqual(rc, 0, out)
        if sys.platform == "darwin":                  # keys may sit in the Keychain: asked, never removed silently
            self.assertIn("kept any email finder keys (./jobhunter enrich disconnect --all removes them)", out)
        self.assertIn("kept the jobhunter browser profile", out)
        calls = self.sb.calls()[n0:]
        self.assertEqual(len(find(calls, "cron", "rm")), 19)
        self.assertFalse(os.path.exists(os.path.join(self.sb.repo, "state", "probe")))
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
class TestFreshUserMessages(unittest.TestCase):
    """What a person sees: the model route is remembered across re-runs (./jobhunter update passes no options),
    a stopped install names its step and the way forward, and the wrapper says what to do next."""

    def setUp(self):
        self.sb = Sandbox(configured=False)

    def tearDown(self):
        self.sb.close()

    def test_the_api_key_route_is_kept_on_a_rerun_and_can_be_switched(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key", env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.sb.home_json()["model_route"], "api_key")
        # a re-run without options keeps the key route and asks for no key that onboarding would not use
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertIn("model route: Anthropic API key, as installed", out)
        self.assertIn("OpenClaw is already set up, so the key is not asked again", out)
        self.assertIn("models auth paste-api-key --provider anthropic", out)
        self.assertIn("DONE Anthropic API key route", out)
        self.assertNotIn("Claude CLI is signed in", out)
        self.assertEqual(len(find(self.sb.calls(), "onboard")), 1)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--cli-tools", "native",
                              "--i-accept-reduced-protection")
        self.assertNotEqual(rc, 0)
        self.assertIn("--cli-tools native is for the Claude subscription route only", out)
        # --claude-login switches back on purpose, and the switch is remembered
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--claude-login")
        self.assertEqual(rc, 0, out)
        self.assertIn("model route: switching from the API key to your Claude subscription", out)
        self.assertIn("DONE Claude CLI is signed in", out)
        self.assertEqual(self.sb.home_json()["model_route"], "cli")
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertIn("DONE Claude CLI is signed in", out)
        rc, out = self.sb.run("install.sh", "--api-key", "--claude-login")
        self.assertNotEqual(rc, 0)
        self.assertIn("choose one: --api-key or --claude-login", out)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key")
        self.assertEqual(rc, 0, out)                     # no key in the environment: OpenClaw already holds it
        self.assertEqual(self.sb.home_json()["model_route"], "api_key")

    def test_a_stopped_install_names_the_step_and_the_way_forward(self):
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_CLAUDE_AUTH": "0"})
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: Claude Code is not signed in, or its login expired", out)
        self.assertIn("claude auth login", out)
        self.assertIn("The install stopped at step 3 (Model route).", out)
        self.assertIn("then run ./install.sh again (finished steps print SKIP)", out)
        self.assertEqual(find(self.sb.calls(), "onboard"), [])
        rc, out = self.sb.run("install.sh", "--bogus")
        self.assertNotIn("The install stopped", out)     # an option error stops before any step
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key")
        self.assertNotEqual(rc, 0)
        self.assertIn("the API key is asked at a terminal", out)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", env={"FAKE_OC_VERSION": "OpenClaw 2026.1.1 (fake)"})
        self.assertNotEqual(rc, 0)
        self.assertIn("OpenClaw 2026.9.5 or newer is needed (this one is 2026.1.1). Update it with: openclaw update, "
                      "then run ./install.sh again", out)

    def test_install_output_and_next_steps(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertIn("created browser profile jobhunter", out)
        self.assertNotIn("L\no\na\nd", out)             # OpenClaw's spinner stays out of the install log
        self.assertNotIn("The install stopped", out)
        self.assertIn("./jobhunter pin set           your owner PIN (not set yet", out)
        self.assertIn("./jobhunter init              about 15 minutes", out)
        self.assertEqual(self.sb.home_json()["model_route"], "cli")

    def test_wrapper_messages(self):
        rc, out = self.sb.run("jobhunter", "status")
        self.assertNotEqual(rc, 0)
        self.assertIn("not installed yet: run ./install.sh first (in %s)" % self.sb.repo, out)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        rc, out = self.sb.run("jobhunter", "init")
        self.assertNotEqual(rc, 0)
        self.assertIn("run ./jobhunter init yourself in the Terminal app", out)
        rc, out = self.sb.run("jobhunter", "update")
        self.assertNotEqual(rc, 0)
        self.assertIn("this folder is not a git clone, so ./jobhunter update cannot download a new version", out)
        rc, out = self.sb.run("jobhunter", "doctor")
        self.assertIn("ok    Claude login (your Claude subscription)", out)
        rc, out = self.sb.run("jobhunter", "doctor", env={"FAKE_CLAUDE_AUTH": "0"})
        self.assertNotEqual(rc, 0)
        self.assertIn("FAIL  Claude login: Claude Code is missing or its login expired. Run: claude auth login", out)

    def test_update_pulls_reinstalls_and_keeps_the_route(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("no git")
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--api-key", env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(rc, 0, out)
        origin = os.path.join(self.sb.root, "origin.git")
        genv = dict(self.sb.env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                    GIT_COMMITTER_EMAIL="t@example.com", PATH="/usr/bin:/bin")

        def g(*a, cwd=None):
            p = subprocess.run([git] + list(a), cwd=cwd or self.sb.repo, env=genv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout.decode("utf-8", "replace"))
        with open(os.path.join(self.sb.repo, ".gitignore"), "w") as fh:
            fh.write("private/\nstate/\nlogs/\nexports/\n")
        g("init", "-q")
        g("add", "-A")
        g("commit", "-q", "-m", "fixture")
        g("clone", "-q", "--bare", self.sb.repo, origin, cwd=self.sb.root)
        g("remote", "add", "origin", origin)
        g("fetch", "-q", "origin")
        g("branch", "-q", "--set-upstream-to", "origin/" + subprocess.run(
            [git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.sb.repo, env=genv,
            stdout=subprocess.PIPE).stdout.decode().strip())
        rc, out = self.sb.run("jobhunter", "update", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertIn("[1/3] Downloading the new version (git pull)", out)
        self.assertIn("already the newest version", out)
        self.assertIn("model route: Anthropic API key, as installed", out)
        self.assertIn("[3/3] Self test", out)
        self.assertIn("update complete", out)


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


def set_state(sb, fn):
    st = sb.oc_state()
    fn(st)
    with open(os.path.join(sb.state, "state.json"), "w") as fh:
        json.dump(st, fh)


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestCliRouteInstall(unittest.TestCase):
    """Dispatcher pause, drift repair, exec policy read-back, identity checks and the CLI route switches."""

    def setUp(self):
        self.sb = Sandbox(configured=True)
        rc, out = self.sb.run("install.sh", "--yes")
        self.assertEqual(rc, 0, out)

    def tearDown(self):
        self.sb.close()

    def test_dispatch_paused_during_install_and_restarted_only_on_success(self):
        did = self.sb.home_json()["cron_jobs"]["jobhunter:dispatch"]
        set_state(self.sb, lambda st: st["crons"]["jobhunter:dispatch"].update(enabled=True))
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("DONE dispatcher paused until the install is complete", out)
        self.assertIn("the dispatcher runs again", out)
        calls = [strip_profile(c) for c in self.sb.calls()[n0:]]
        disable = calls.index(["cron", "disable", did])
        self.assertLess(disable, calls.index(["cron", "add"] + calls[[c[:2] for c in calls].index(["cron", "add"])][2:]))
        self.assertEqual(calls[-1] if calls[-1][:2] == ["cron", "enable"] else
                         [c for c in calls if c[:2] == ["cron", "enable"]][-1], ["cron", "enable", did])
        self.assertTrue(self.sb.oc_state()["crons"]["jobhunter:dispatch"]["enabled"])
        # a failing install leaves the dispatcher paused and says so
        rc, out = self.sb.run("install.sh", "--yes", "--smoke", env={"FAKE_OC_PROBE": "missing"})
        self.assertNotEqual(rc, 0)
        self.assertIn("agent identity check failed", out)
        self.assertIn("dispatch paused; re-run ./install.sh", out)
        self.assertFalse(self.sb.oc_state()["crons"]["jobhunter:dispatch"]["enabled"])

    def test_identity_check_failures_stop_the_install(self):
        stamp = self.sb.home_json()                                          # noqa: F841 (installed above)
        for mode in ("missing", "system"):
            rc, out = self.sb.run("install.sh", "--yes", "--smoke", env={"FAKE_OC_PROBE": mode})
            self.assertNotEqual(rc, 0, mode)
            self.assertIn("agent identity check failed (docs/TROUBLESHOOTING.md, Claude subscription route)", out)
        with open(os.path.join(self.sb.repo, "state", "install-manifest.json")) as fh:
            at = json.load(fh)["probe_stamp"]["at"]
        # --smoke runs the checks even though the stamp matches; a pass writes a new stamp
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", "--smoke")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(find(self.sb.calls()[n0:], "cron", "run")), 4)
        # another OpenClaw version runs them again without --smoke
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_OC_VERSION": "OpenClaw 2026.9.8 (fake)"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(find(self.sb.calls()[n0:], "cron", "run")), 4)
        with open(os.path.join(self.sb.repo, "state", "install-manifest.json")) as fh:
            self.assertNotEqual(json.load(fh)["probe_stamp"]["at"], at)

    def test_drift_and_stale_exec_keys_are_repaired(self):
        ids = self.sb.home_json()["cron_jobs"]

        def seed(st):
            st["crons"]["jobhunter:scout"]["payload"].pop("toolsAllow")       # like cron edit --clear-tools
            st["crons"]["jobhunter:evaluate"]["payload"]["message"] = "Do something else."
            # O8: a live at sign is named as message_mention next to message_sha256, as the preflight does
            st["crons"]["jobhunter:apply"]["payload"]["message"] = "Read @/etc/hosts first."
            st["crons"]["jobhunter:mailer"]["payload"]["argv"] = ["/bin/sh", "-c", "id"]
            st["config"]["agents"]["entries"]["jobhunter-applier"]["tools"]["exec"]["reviewer"] = {"mode": "auto"}
        set_state(self.sb, seed)
        n0 = len(self.sb.calls())
        # a global ask always and security full stay seeded: the per-agent values must still win
        rc, out = self.sb.run("install.sh", "--yes",
                              env={"FAKE_OC_GLOBAL_EXEC": json.dumps({"ask": "always", "security": "full"})})
        self.assertEqual(rc, 0, out)
        # every repair is printed, field names only, and counted in the summary
        self.assertIn("unset agents.entries.jobhunter-applier.tools.exec.reviewer (an exec key the installer does not "
                      "write)", out)
        self.assertIn("repaired jobhunter:apply (message_sha256, message_mention differed from its declaration)", out)
        self.assertIn("repaired jobhunter:evaluate (message_sha256 differed from its declaration)", out)
        self.assertIn("repaired jobhunter:mailer (argv differed from its declaration)", out)
        self.assertIn("repaired jobhunter:scout (tools differed from its declaration)", out)
        self.assertIn("DONE 19 automations declared, 4 repaired, all disabled until ./jobhunter resume", out)
        self.assertNotIn("Do something else", out)
        self.assertNotIn("/etc/hosts", out)
        calls = [strip_profile(c) for c in self.sb.calls()[n0:]]
        edited = sorted(c[2] for c in calls if c[:2] == ["cron", "edit"] and "--failure-alert" not in c)
        self.assertEqual(edited, sorted([ids["jobhunter:scout"], ids["jobhunter:evaluate"], ids["jobhunter:mailer"],
                                         ids["jobhunter:apply"]]))
        self.assertIn(["config", "unset", "agents.entries.jobhunter-applier.tools.exec.reviewer"], calls)
        st = self.sb.oc_state()
        self.assertEqual(st["crons"]["jobhunter:scout"]["payload"]["toolsAllow"], ["exec", "read", "write", "browser"])
        self.assertTrue(st["crons"]["jobhunter:evaluate"]["payload"]["message"].endswith("CYCLE_DONE."))
        self.assertNotIn("reviewer", st["config"]["agents"]["entries"]["jobhunter-applier"]["tools"]["exec"])
        # an edit that does not take: the job is removed and added again, the manifest follows
        set_state(self.sb, lambda st: st["crons"]["jobhunter:scout"]["payload"].pop("toolsAllow"))
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_OC_EDIT_IGNORES": "toolsAllow"})
        self.assertEqual(rc, 0, out)
        self.assertIn("replaced the automations an edit could not repair", out)
        calls = [strip_profile(c) for c in self.sb.calls()[n0:]]
        self.assertIn(["cron", "rm", ids["jobhunter:scout"]], calls)
        new_id = self.sb.home_json()["cron_jobs"]["jobhunter:scout"]
        self.assertNotEqual(new_id, ids["jobhunter:scout"])
        self.assertEqual(self.sb.oc_state()["crons"]["jobhunter:scout"]["id"], new_id)
        self.assertFalse(self.sb.oc_state()["crons"]["jobhunter:scout"]["enabled"])

    def test_repairs_by_cron_add_are_reported(self):
        """OpenClaw 2026.9.8: `cron add` of a known declaration key rewrites the job, so the repair pass finds
        nothing; the install still says what it brought back (live check T9)."""
        ids = self.sb.home_json()["cron_jobs"]

        def seed(st):
            st["crons"]["jobhunter:scout"]["payload"].pop("toolsAllow")
            st["crons"]["jobhunter:evaluate"]["payload"]["message"] = "Do something else."
            st["config"]["agents"]["entries"]["jobhunter-evaluator"]["tools"]["exec"]["pathPrepend"] = ["/tmp/x"]
        set_state(self.sb, seed)
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_OC_ADD_UPSERTS": "1"})
        self.assertEqual(rc, 0, out)
        calls = [strip_profile(c) for c in self.sb.calls()[n0:]]
        self.assertEqual([c for c in calls if c[:2] == ["cron", "edit"] and "--failure-alert" not in c], [])
        self.assertIn("unset agents.entries.jobhunter-evaluator.tools.exec.pathPrepend", out)
        self.assertIn("repaired jobhunter:evaluate (message_sha256 differed from its declaration)", out)
        self.assertIn("repaired jobhunter:scout (tools differed from its declaration)", out)
        self.assertIn("DONE 19 automations declared, 2 repaired,", out)
        self.assertEqual(self.sb.oc_state()["crons"]["jobhunter:scout"]["id"], ids["jobhunter:scout"])
        # a job removed by hand is added again and reported
        set_state(self.sb, lambda st: st["crons"].pop("jobhunter:replies"))
        rc, out = self.sb.run("install.sh", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("repaired jobhunter:replies (it was missing)", out)
        # nothing differs: no repair line
        rc, out = self.sb.run("install.sh", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("repaired", out)
        self.assertIn("DONE 19 automations declared, all disabled until ./jobhunter resume", out)

    def test_no_daemon_and_gateway_restart_before_2026_9_7(self):
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", "--no-daemon", env={"FAKE_OC_GATEWAY_DOWN": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("With --no-daemon the installer never installs or starts one", out)
        calls = self.sb.calls()[n0:]
        self.assertEqual(find(calls, "gateway", "start") + find(calls, "gateway", "install") +
                         find(calls, "gateway", "restart"), [])
        # OpenClaw 2026.9.6: plugin changes need a Gateway restart before the guard reports in
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_OC_VERSION": "OpenClaw 2026.9.6 (fake)",
                                                         "FAKE_OC_HEARTBEAT_ON": "restart"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(find(self.sb.calls()[n0:], "gateway", "restart")), 1)
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", "--no-daemon", env={"FAKE_OC_VERSION": "OpenClaw 2026.9.6 (fake)"})
        self.assertEqual(rc, 0, out)
        self.assertIn("loads plugin changes only after a Gateway restart", out)
        calls = self.sb.calls()[n0:]
        self.assertEqual(find(calls, "gateway", "restart") + find(calls, "gateway", "start") +
                         find(calls, "gateway", "install"), [])
        # a guard of the old identity proof never counts as reporting in
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_OC_GUARD_PROOF": "1", "FAKE_OC_VERSION":
                                                         "OpenClaw 2026.9.8 (fake)"})
        self.assertNotEqual(rc, 0)
        self.assertIn("identity proof version 2", out)

    def test_old_claude_and_mode_flags(self):
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_CLAUDE_OLD": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("this Claude Code is too old for restricted agent runs", out)
        # a Claude Code that has the flags but is older than a jobhunter model needs stops in step 3, not at the
        # first agent run
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", env={"FAKE_CLAUDE_VERSION": "2.1.270"})
        self.assertNotEqual(rc, 0)
        self.assertIn("ERROR: Claude Code 2.1.270 is too old for anthropic/claude-opus-5-5 of jobhunter-applier, "
                      "jobhunter-outreach (2.1.280 or newer is needed); update it: claude update", out)
        self.assertNotIn("[4] ", out)
        self.assertEqual(find(self.sb.calls()[n0:], "config", "patch"), [])
        rc, out = self.sb.run("install.sh", "--yes", "--cli-tools", "native")
        self.assertNotEqual(rc, 0)
        self.assertIn("Add --i-accept-reduced-protection to confirm", out)
        rc, out = self.sb.run("install.sh", "--yes", "--i-accept-reduced-protection")
        self.assertNotEqual(rc, 0)
        rc, out = self.sb.run("install.sh", "--yes", "--identity-carrier", "pin")
        self.assertNotEqual(rc, 0)
        # mode N needs the argv carrier alone: Claude Code's own Bash never gets the env proof
        for carrier in ("argv+env", "env"):
            rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--cli-tools", "native",
                                  "--i-accept-reduced-protection", "--identity-carrier", carrier)
            self.assertNotEqual(rc, 0)
            self.assertIn("--cli-tools native works only with --identity-carrier argv", out)
        # run from a Claude Code terminal: install.sh drops Claude Code's markers, so its jh.py calls stay system
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--cli-tools", "native", "--i-accept-reduced-protection",
                              env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: --cli-tools native uses the argv identity carrier only (--identity-carrier argv)", out)
        self.assertIn("WARNING: agent mode: Claude Code's own tools (reduced protection, --cli-tools native; identity "
                      "carrier argv).", out)
        self.assertEqual(self.sb.home_json()["cli_route"]["carriers"], ["argv"])
        st = self.sb.oc_state()
        self.assertEqual(st["config"]["agents"]["entries"]["jobhunter-scout"]["tools"]["exec"], ins.EXEC_FULL)
        self.assertEqual(st["config"]["agents"]["entries"]["jobhunter-qc"]["tools"]["exec"], ins.EXEC_DENY)
        self.assertEqual(st["config"]["plugins"]["entries"]["jobhunter-guard"]["config"]["claudeNativeTools"], "gate")
        self.assertEqual(st["config"]["plugins"]["entries"]["jobhunter-guard"]["config"]["proofCarriers"], ["argv"])
        rc, out = self.sb.run("jobhunter", "doctor")
        self.assertIn("FAIL  agent mode: Claude Code's own tools (reduced protection)", out)
        # a re-run without the flags returns to restricted runs
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--identity-carrier", "argv")
        self.assertEqual(rc, 0, out)
        st = self.sb.oc_state()
        self.assertEqual(st["config"]["agents"]["entries"]["jobhunter-scout"]["tools"]["exec"], ins.EXEC_ALLOWLIST)
        self.assertEqual(st["config"]["plugins"]["entries"]["jobhunter-guard"]["config"]["proofCarriers"], ["argv"])
        self.assertEqual(self.sb.home_json()["cli_route"]["carriers"], ["argv"])

    def test_doctor_probe(self):
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("jobhunter", "doctor", "--probe")
        self.assertIn("ok    agent identity (scout, evaluator, applier and outreach ran jh.py as themselves)", out)
        self.assertIn("ok    jobhunter-qc test turn", out)
        self.assertEqual(len(find(self.sb.calls()[n0:], "cron", "run")), 4)
        rc, out = self.sb.run("jobhunter", "doctor", "--probe", env={"FAKE_OC_PROBE": "system"})
        self.assertIn("FAIL  agent identity", out)
        rc, out = self.sb.run("jobhunter", "doctor", "--bogus")
        self.assertIn("usage: ./jobhunter doctor [--probe]", out)


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestGuardPluginLink(unittest.TestCase):
    """Step 10 on OpenClaw 2026.9.8: a plugin from a local path is installed only after a confirmation (--force),
    and the guard config is written before `plugins enable` (which validates it)."""

    def setUp(self):
        self.sb = Sandbox(configured=True)

    def tearDown(self):
        self.sb.close()

    def test_without_a_terminal_or_yes_the_link_stops_with_the_way_forward(self):
        rc, out = self.sb.run("install.sh", "--no-smoke")
        self.assertNotEqual(rc, 0)
        self.assertIn("Install cancelled; rerun with --force after reviewing the source.", out)
        self.assertIn("ERROR: openclaw plugins install --link failed. OpenClaw installs a plugin that is not from "
                      "ClawHub only after a confirmation: run ./install.sh at a terminal and answer yes, or run it "
                      "with --yes", out)
        calls = self.sb.calls()
        self.assertEqual([c for c in calls if "--force" in c], [])          # never confirmed for the person
        self.assertEqual(find(calls, "plugins", "enable"), [])
        self.assertEqual(self.sb.oc_state()["plugins"], [])
        # --yes confirms it; the guard config comes before enable
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.sb.oc_state()["plugins"], ["jobhunter-guard"])
        self.assertNotIn("refused_enables", self.sb.oc_state())

    def test_at_a_terminal_the_installer_asks(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.sb.repo, "private", "owner_pin.json"), "w") as fh:
            fh.write("{}")                                   # step 6 skips the PIN prompt; nothing reads it here
        set_state(self.sb, lambda st: st.update(plugins=[]))
        question = b"Link the guard plugin from this clone? [y/N] "
        rc, out = self.sb.run_tty("install.sh", "--no-smoke", answers=[(question, b"n\n")])
        self.assertNotEqual(rc, 0)
        self.assertIn("OpenClaw asks before it installs a plugin that is not from ClawHub", out)
        self.assertIn("ERROR: the guard plugin is required (it checks every agent action); nothing was linked", out)
        self.assertEqual(self.sb.oc_state()["plugins"], [])
        n0 = len(self.sb.calls())
        answers = [(question, b"y\n"), (b"Choose now which sites", b"n\n")]
        if sys.platform == "darwin":
            answers.append((b"Keep this Mac awake", b"n\n"))
        answers.append((b"Connect an email finder key now?", b"n\n"))
        rc, out = self.sb.run_tty("install.sh", "--no-smoke", answers=answers)
        self.assertEqual(rc, 0, out)
        self.assertIn("--force", find(self.sb.calls()[n0:], "plugins", "install")[0])
        self.assertEqual(self.sb.oc_state()["plugins"], ["jobhunter-guard"])

    def test_a_failed_plugin_listing_never_forces_a_link(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", env={"FAKE_OC_PLUGINS_LIST_FAIL": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("ERROR: openclaw plugins list failed", out)
        self.assertEqual(find(self.sb.calls(), "plugins", "install"), [])

    def test_enable_without_the_guard_config_is_refused_by_the_fake(self):
        # the fake plays OpenClaw 2026.9.8: enable validates the config (repo is required)
        env = dict(self.sb.env)
        p = subprocess.run([os.path.join(self.sb.bin, "openclaw"), "plugins", "enable", "jobhunter-guard"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"must have required property 'repo'", p.stdout)


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
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
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
        n0 = len(self.sb.calls())
        rc, out = self.sb.run_tty("jobhunter", "init", answers=ans)
        self.assertNotEqual(rc, 0, out)
        o = self.owner()
        self.assertEqual((o["first_name"], o["last_name"], o["gmail_address"]), ("Eve", "Quorrin", "eve.quorrin@example.com"))
        self.assertEqual(o["signature"]["full_name"], "Eve Quorrin")
        self.assertEqual(o["notify"], dict(o["notify"], channel="whatsapp", to=self.number))
        self.assertFalse(any("your-handle" in x for x in o["signature"]["links"]))
        self.assertIn("the guard now knows your chat number", out)
        guard_patch = [c for c in self.sb.calls()[n0:] if strip_profile(c)[:2] == ["config", "patch"]]
        self.assertEqual(len(guard_patch), 1)
        self.assertIn("salary research turn recorded nothing", out)
        self.assertIn("profile inference recorded nothing", out)
        # steps 6 and 7 run the onboarding automations after preflight, never `openclaw agent`
        ids = self.sb.home_json()["cron_jobs"]
        runs = [strip_profile(c) for c in find(self.sb.calls()[n0:], "cron", "run")]
        self.assertEqual(runs, [["cron", "run", ids["jobhunter:onboard-salary"], "--wait", "--wait-timeout", "20m",
                                 "--json"],
                                ["cron", "run", ids["jobhunter:onboard-profile"], "--wait", "--wait-timeout", "20m",
                                 "--json"]])
        self.assertEqual(find(self.sb.calls(), "agent"), [])
        with open(os.path.join(self.sb.home_json()["ws_root"], "scout", "work", "onboarding", "salary_inputs.json")) as fh:
            self.assertEqual(json.load(fh), {"role_titles": ["Data Analyst"], "city": "Remote"})
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


JH_AGENTS = ("jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach", "jobhunter-qc")


def oc_json(sb, *args, env=None):
    """One call of the fake openclaw (as install.sh makes it), parsed."""
    e = dict(sb.env, **(env or {}))
    p = subprocess.run([os.path.join(sb.bin, "openclaw")] + list(args), env=e, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=60)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip().startswith((b"{", b"[")) else None), p.stderr


def effective(sb, aid):
    rc, doc, _ = oc_json(sb, "sandbox", "explain", "--agent", aid, "--json")
    return doc["tools"]["exec"]


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestExecPolicySchema(unittest.TestCase):
    """OpenClaw 2026.9.5 and later refuse `mode` together with `security` or `ask` in one exec object; the rendered
    patch holds mode only and deletes an older install's keys in the same write."""

    def setUp(self):
        self.sb = Sandbox(configured=True)

    def tearDown(self):
        self.sb.close()

    def test_fake_refuses_mode_with_security_or_ask(self):
        f = os.path.join(self.sb.root, "bad.json")
        for exec_obj in ({"mode": "allowlist", "security": "allowlist", "ask": "off"}, {"mode": "deny", "ask": "off"}):
            with open(f, "w") as fh:
                json.dump({"agents": {"entries": {"jobhunter-scout": {"tools": {"exec": exec_obj}}}}}, fh)
            rc, doc, err = oc_json(self.sb, "config", "patch", "--file", f, "--dry-run", "--json")
            self.assertEqual(rc, 1, exec_obj)
            self.assertIn("mode cannot be combined with security or ask", doc["errors"][0]["message"])
        with open(f, "w") as fh:
            json.dump({"agents": {"entries": {"jobhunter-scout": {"tools": {"exec": {"mode": "allowlist"}}}}}}, fh)
        self.assertEqual(oc_json(self.sb, "config", "patch", "--file", f, "--dry-run", "--json")[0], 0)

    def test_older_install_with_security_and_ask_is_migrated_in_one_patch(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)

        def legacy(st):
            for aid in JH_AGENTS:
                ex = st["config"]["agents"]["entries"][aid]["tools"]["exec"]
                ex.pop("mode")
                ex.update(security="deny" if aid == "jobhunter-qc" else "allowlist", ask="off")
        set_state(self.sb, legacy)
        n0 = len(self.sb.calls())
        # a global ask always and security full: the per-agent mode must still win
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke",
                              env={"FAKE_OC_GLOBAL_EXEC": json.dumps({"ask": "always", "security": "full"})})
        self.assertEqual(rc, 0, out)
        self.assertEqual(find(self.sb.calls()[n0:], "config", "unset"), [])      # deleted by the patch itself
        st = self.sb.oc_state()
        self.assertEqual(st.get("refused_patches", []), [])
        for aid in JH_AGENTS:
            want = ins.EXEC_DENY if aid == "jobhunter-qc" else ins.EXEC_ALLOWLIST
            self.assertEqual(st["config"]["agents"]["entries"][aid]["tools"]["exec"], want, aid)
            eff = effective(self.sb, aid)
            self.assertEqual((eff["security"], eff["ask"]), ("deny" if aid == "jobhunter-qc" else "allowlist", "off"))


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestFailClosedAgents(unittest.TestCase):
    """An install that stops after `agents add` leaves no jobhunter agent with OpenClaw's fallback exec policy."""

    def setUp(self):
        self.sb = Sandbox(configured=True)

    def tearDown(self):
        self.sb.close()

    def assert_none_unconfined(self):
        st = self.sb.oc_state()
        for aid in JH_AGENTS:
            present = aid in [a["id"] for a in st["agents"]] or aid in st["config"]["agents"]["entries"]
            if not present:
                continue
            eff = effective(self.sb, aid)
            self.assertEqual(eff["security"], "deny", aid)
            t = st["config"]["agents"]["entries"][aid]["tools"]
            self.assertLessEqual({"exec", "read", "write", "browser"}, set(t["deny"]), aid)
            self.assertEqual((t["fs"], t["elevated"]), ({"workspaceOnly": True}, {"enabled": False}), aid)

    def test_fresh_install_that_stops_in_step_8_removes_its_agents(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", env={"FAKE_OC_APPROVALS_SET_FAIL": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("openclaw approvals set failed", out)
        for aid in JH_AGENTS:
            self.assertIn("NOTE: removed %s again: the install stopped before it was restricted" % aid, out)
        st = self.sb.oc_state()
        self.assertEqual(st["agents"], [])
        self.assertEqual(sorted(st["config"]["agents"]["entries"]), ["main"])
        self.assertEqual(sorted(st["deleted_agents"]), sorted(JH_AGENTS))
        self.assertEqual(find(self.sb.calls(), "models", "auth", "login"), [])
        # the next complete install adds and restricts them again
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.sb.oc_state()["agents"]), 5)

    def test_a_failed_add_removes_the_agents_added_before_it(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", env={"FAKE_OC_ADD_FAIL": "jobhunter-applier"})
        self.assertNotEqual(rc, 0)
        self.assertIn("openclaw agents add jobhunter-applier failed", out)
        st = self.sb.oc_state()
        self.assertEqual(st["agents"], [])
        self.assertEqual(sorted(st["deleted_agents"]), ["jobhunter-evaluator", "jobhunter-scout"])
        self.assertEqual(find(self.sb.calls(), "config", "patch"), [])           # never restricted, so removed

    def test_agents_that_cannot_be_removed_are_confined(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke",
                              env={"FAKE_OC_APPROVALS_SET_FAIL": "1", "FAKE_OC_DELETE_FAIL": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("ERROR: could not remove jobhunter-scout", out)
        self.assertIn("NOTE: the jobhunter agents are set to exec deny with every tool denied", out)
        self.assertEqual(len(self.sb.oc_state()["agents"]), 5)
        self.assert_none_unconfined()

    def test_a_rerun_that_stops_in_step_8_confines_the_existing_agents(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", env={"FAKE_OC_APPROVALS_SET_FAIL": "1"})
        self.assertNotEqual(rc, 0)
        self.assertNotIn("removed jobhunter-", out)                          # this run added none
        self.assertEqual(len(self.sb.oc_state()["agents"]), 5)
        self.assert_none_unconfined()
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertEqual(effective(self.sb, "jobhunter-scout")["security"], "allowlist")
        self.assertEqual(self.sb.oc_state()["config"]["agents"]["entries"]["jobhunter-scout"]["tools"]["deny"],
                         [a for a in ins.load_agents(paths.REPO) if a["id"] == "jobhunter-scout"][0]["tools_deny"])

    def test_wildcard_approvals_stop_the_install(self):
        oc_json(self.sb, "agents", "list", "--json")                         # the fake's first state
        set_state(self.sb, lambda st: st["approvals"]["agents"].update({"*": {"allowlist": [{"pattern": "/bin/cat"}]}}))
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertNotEqual(rc, 0)
        self.assertIn('agents["*"] adds 1 allowlist entries to every agent (/bin/cat)', out)
        self.assertEqual(self.sb.oc_state()["agents"], [])                   # added in this run: removed again
        # after a complete install, doctor turns red when a wildcard entry appears
        set_state(self.sb, lambda st: st["approvals"]["agents"].pop("*"))
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        set_state(self.sb, lambda st: st["approvals"]["agents"].update({"*": {"allowlist": ["/bin/cat"]}}))
        rc, out = self.sb.run("jobhunter", "doctor")
        self.assertIn("FAIL  exec policy of the jobhunter agents (allowlist, never ask)", out)
        self.assertIn('agents["*"] adds 1 allowlist entries', out)


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestSkillWorkshop(unittest.TestCase):
    """OpenClaw's default Skill Workshop mode (auto) keeps an enabled weekly review job for every agent, which no
    cron client may disable: install needs the owner's consent to the global mode propose before adding agents."""

    def setUp(self):
        self.sb = Sandbox(configured=True)
        self.sb.env["FAKE_OC_WORKSHOP_MODE"] = ""                         # OpenClaw's default: auto

    def tearDown(self):
        self.sb.close()

    def review_jobs(self):
        rc, doc, _ = oc_json(self.sb, "cron", "list", "--all", "--json")
        return {j["agentId"]: j["enabled"] for j in doc["jobs"] if j["declarationKey"].startswith("skill-collection-review:")}

    def test_no_consent_stops_before_any_agent_exists(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertNotEqual(rc, 0)
        self.assertIn("[7b] Skill Workshop reviews", out)
        self.assertIn("./install.sh --skill-workshop-propose", out)
        self.assertEqual(find(self.sb.calls(), "agents", "add"), [])
        self.assertEqual(self.sb.oc_state()["agents"], [])
        self.assertIsNone((self.sb.oc_state()["config"].get("skills") or {}).get("workshop"))

    def test_consent_sets_propose_and_the_review_jobs_stay_disabled(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--skill-workshop-propose")
        self.assertEqual(rc, 0, out)
        self.assertIn("DONE skills.workshop.autonomous.mode is propose", out)
        self.assertEqual(self.sb.oc_state()["config"]["skills"]["workshop"], {"autonomous": {"mode": "propose"}})
        jobs = self.review_jobs()
        self.assertEqual(sorted(a for a in jobs if a.startswith("jobhunter-")), sorted(JH_AGENTS))
        self.assertFalse(any(jobs.values()))
        self.assertIn("5 disabled automations outside the install name a jobhunter agent (skill_review)", out)
        with open(os.path.join(self.sb.repo, "state", "install-manifest.json")) as fh:
            self.assertEqual(json.load(fh)["skill_workshop_mode_before"], "auto")
        rc, out = self.sb.run("jobhunter", "doctor")
        self.assertIn("ok    no automation outside this install runs a jobhunter agent", out)
        # the owner turns the weekly reviews back on: doctor is red and a re-run stops before step 8
        set_state(self.sb, lambda st: st["config"]["skills"]["workshop"]["autonomous"].update(mode="auto"))
        self.assertTrue(all(self.review_jobs().values()))
        rc, out = self.sb.run("jobhunter", "doctor")
        self.assertIn("FAIL  no automation outside this install runs a jobhunter agent", out)
        self.assertIn("enabled automation skill-collection-review:jobhunter-", out)
        self.assertIn("FAIL  Skill Workshop autonomous mode is auto", out)
        n0 = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("[8] ", out)
        self.assertEqual(find(self.sb.calls()[n0:], "config", "patch"), [])

    def test_review_jobs_cannot_be_disabled_one_by_one(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--skill-workshop-propose")
        self.assertEqual(rc, 0, out)
        rc, _doc, err = oc_json(self.sb, "cron", "disable", "sys-review-jobhunter-scout")
        self.assertEqual(rc, 1)
        self.assertIn(b"system-owned monitor jobs cannot be edited", err)

    def test_an_enabled_job_added_by_hand_stops_the_install(self):
        self.sb.env["FAKE_OC_WORKSHOP_MODE"] = "off"
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP OpenClaw's Skill Workshop runs no weekly agent reviews (skills.workshop.autonomous.mode off)",
                      out)
        set_state(self.sb, lambda st: st["crons"].update({"custom:x": {
            "id": "job-999", "name": "nightly helper", "enabled": True, "agentId": "jobhunter-applier",
            "sessionTarget": "isolated", "payload": {"kind": "agentTurn", "message": "hi", "toolsAllow": ["exec"]},
            "delivery": {"mode": "none"}}}))
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertNotEqual(rc, 0)
        self.assertIn("enabled automation custom:x (job-999) runs jobhunter-applier outside the install", out)
        rc, out = self.sb.run("jobhunter", "doctor")
        self.assertIn("FAIL  no automation outside this install runs a jobhunter agent", out)

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "needs a pty")
    def test_at_a_terminal_models_auth_login_runs(self):
        rc, out = self.sb.run_tty("install.sh", "--yes", "--no-smoke", "--skill-workshop-propose")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(find(self.sb.calls(), "models", "auth", "login")), 5)
        self.assertNotIn("models auth login is skipped", out)


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestOpenclawBin(unittest.TestCase):
    """--openclaw-bin: a test install of another OpenClaw build (docs/DEVELOPING.md, live check)."""

    def setUp(self):
        self.sb = Sandbox(configured=True)
        self.alt_dir = os.path.join(self.sb.root, "oc-other", "bin")
        os.makedirs(self.alt_dir)
        self.alt = os.path.join(self.alt_dir, "openclaw")
        self.alt_log = os.path.join(self.sb.root, "alt.log")
        with open(self.alt, "w") as fh:
            fh.write('#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n'
                     % (self.alt_log, os.path.join(self.sb.bin, "openclaw")))
        os.chmod(self.alt, 0o755)

    def tearDown(self):
        self.sb.close()

    def alt_calls(self):
        if not os.path.exists(self.alt_log):
            return 0
        with open(self.alt_log) as fh:
            return len([l for l in fh if l.strip()])

    def test_the_named_binary_is_used_recorded_and_bound(self):
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke", "--profile", "jhtest", "--openclaw-bin", self.alt)
        self.assertEqual(rc, 0, out)
        self.assertIn("openclaw binary %s (--openclaw-bin)" % self.alt, out)
        self.assertEqual(self.alt_calls(), len(self.sb.calls()))          # every openclaw call went through it
        self.assertEqual(self.sb.home_json()["oc_bin"], self.alt)
        self.assertNotIn("Your terminal does not find openclaw by name", out)
        # a re-run without the flag keeps the recorded binary, like ./jobhunter
        n_alt, n_all = self.alt_calls(), len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", "--no-smoke")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.alt_calls() - n_alt, len(self.sb.calls()) - n_all)
        self.assertIn("openclaw binary %s\n" % self.alt, out)
        # one clone is one install: another binary is refused before any openclaw call
        n_all = len(self.sb.calls())
        rc, out = self.sb.run("install.sh", "--yes", "--openclaw-bin", os.path.join(self.sb.bin, "openclaw"))
        self.assertNotEqual(rc, 0)
        self.assertIn("this clone is installed for the OpenClaw binary %s" % self.alt, out)
        self.assertEqual(self.sb.calls()[n_all:], [])

    def test_bad_values_are_refused(self):
        for value, text in (("relative/openclaw", "--openclaw-bin needs an absolute path"),
                            (os.path.join(self.sb.root, "missing"), "--openclaw-bin is not an executable file")):
            rc, out = self.sb.run("install.sh", "--yes", "--openclaw-bin", value)
            self.assertNotEqual(rc, 0)
            self.assertIn(text, out)
        broken = os.path.join(self.sb.root, "broken-openclaw")
        with open(broken, "w") as fh:
            fh.write("#!/bin/bash\nexit 3\n")
        os.chmod(broken, 0o755)
        rc, out = self.sb.run("install.sh", "--yes", "--openclaw-bin", broken)
        self.assertNotEqual(rc, 0)
        self.assertIn("--openclaw-bin must name a working openclaw (nothing is installed over it)", out)
        self.assertFalse(os.path.exists(os.path.join(self.sb.root, "curl.log")))


if __name__ == "__main__":
    unittest.main()
