"""E2E (INT): the Claude subscription route (claude-cli) end to end through the real guard and the real jh.py.

CLI-ROUTE-DESIGN 11.3. The recorded calls of tests/fixtures/e2e/claude_cli_restricted.json (the restricted cron
runs of the onboarding pair and the evaluator probe) and claude_cli_native.json (a stray unrestricted run with
Claude Code's native tools) go to the real jobhunter-guard runtime (U8) through tests/fixtures/e2e/guard_bridge.ts.
For every exec the bridge plays OpenClaw's exec tool: before_tool_call (which may block or rewrite the command)
and then resolve_exec_env. The rewritten command then runs as a child `python -I` interpreter with exactly that
environment (OPENCLAW_SHELL plus the hook's JH_* values) and the rewritten workdir as its working directory, so
jh.py (U1) checks both identity carriers as it does in production.

Asserted: `profile salary-record` (scout) and `profile infer-record` (evaluator), the two calls that recorded
nothing before this fix, succeed as their agents; the same rewritten call run twice fails the second time; an
argv proof with another session's env proof fails; either carrier alone fails while both are required; the scout
cannot use the evaluator's argv proof, through the guard or around it; a call without -I fails; native tool calls
are refused by the guard, and the same native commands without the guard reach jh.py as unproven agents (public
read-only commands only); mode N (native gate, argv proof only) works only with the argv carrier; the carrier
switches of install (argv, env); the tool-surface pin. The layers next to the guard are checked as install renders
them: every allowed call needs no person (the guard never asks, the cron job has an explicit tools list, the exec
policy is allowlist with ask off, and the rewritten command is a hit in that agent's own approvals allowlist entry
under Python's re and node's RegExp, and a miss in every other agent's); shell metacharacters are refused by the
guard and, on their own, by the allowlist; out-of-workspace reads and writes are refused in every path form, and
jh.py's own --file confinement and ACL refuse them again with genuine proofs; the owner's main agent cannot use a
captured evaluator proof. No jh.py call of these tests is ever classified `system`.
Skipped without Node 24. Fictional data only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest

import tests  # noqa: F401
from jobhunter import auth, canon, paths
from jobhunter import install as ins
from jobhunter import profile as P
from tests import helpers
from tests.fixtures.e2e.support import (PROFILE_FIX, REPO, World, e2e_fixture, exec_tool_env, run_jh_child,
                                        split_rewritten)
from tests.test_e2e_guard_replay import NODE, GuardBridge, node_major

SC = "jobhunter-scout"
EV = "jobhunter-evaluator"
FAKE_PROOF = "jhp2.jobhunter-evaluator.1790000000.0123456789abcdef.0123456789abcdef." + "a" * 64
ARGPATTERN_JS = os.path.join(REPO, "tests", "fixtures", "install", "argpattern.mjs")
# Shell metacharacters after a plain jh.py command: chains, pipes, substitutions, redirects, background, a second
# line, a tab, quotes and a token starting with "=". None of them may reach a shell from an agent.
SHELL_SUFFIXES = ("; /bin/cat {ROOT}/private/guard.key", " && /usr/bin/id", " || /usr/bin/id", " | /bin/sh",
                  " $(/usr/bin/id)", " `/usr/bin/id`", " > {WS_ROOT}/scout/work/out.txt", " < /etc/hosts", " &",
                  "\n/usr/bin/id", "\t/usr/bin/id", " '/usr/bin/id'", ' "x"', " =x", " ${HOME}")
# The same command behind an env assignment, a wrapper or a second shell.
SHELL_PREFIXES = ("PYTHONPATH=/tmp ", "env -u OPENCLAW_SHELL ", "/usr/bin/env ", "/bin/sh -c ", "exec ", "nohup ")


@unittest.skipUnless(node_major() >= 24 and shutil.which("node"),
                     "needs node 24 or later (the guard plugin runs TypeScript directly)")
class ClaudeCliRouteBase(unittest.TestCase):
    carriers = ("argv", "env")
    native_tools = "deny"

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.stop)
        self.w.write_config()
        helpers.ensure_agent_setup(list(self.carriers))
        self.assertEqual(auth.required_carriers(), sorted(self.carriers))
        h = paths.home()
        self.root = paths.root()
        self.ws_root = h["ws_root"]
        self.py = h["python"]
        self.vars = {"PY": self.py, "REPO": REPO, "ROOT": self.root, "WS_ROOT": self.ws_root,
                     "JH": "%s %s/scripts/jh.py" % (self.py, REPO)}
        self.ran = []           # (what, classification records of the child)
        self.kept = {}
        self.allowed = []       # (agent, rewritten command) of every exec the guard allowed in a replay
        self.g = self.start_guard(self.native_tools, self.carriers)

    def tearDown(self):
        # no agent call of the claude-cli route is ever `system` (CLI-ROUTE-DESIGN 4.3)
        system = [(what, c) for what, recs in self.ran for c in recs if c.get("class") == "system"]
        self.assertEqual(system, [])

    # ------------------------------------------------------------ guard
    def guard_config(self, native: str, carriers) -> dict:
        """The guard config install writes (render-guard-config) for this mode and these carriers. The test
        install keeps private/ (home.json, guard.key) in its temp root instead of the repo, so the two paths that
        name it point there; every other key is used as rendered."""
        conf = ins.render_guard_config(repo=REPO, py=self.py, cfg={}, ws_root=self.ws_root,
                                       state_dir=os.path.join(self.root, "openclaw-state"), carriers=list(carriers),
                                       cli_tools="native" if native == "gate" else "restricted")
        conf = conf["plugins"]["entries"][ins.GUARD_PLUGIN_ID]["config"]
        self.assertEqual((conf["claudeNativeTools"], conf["pinToolSurface"], conf["proofCarriers"]),
                         (native, native == "deny", list(carriers)))
        repo_private, test_private = os.path.join(REPO, "private"), os.path.join(self.root, "private")
        self.assertEqual(conf["homeFile"], os.path.join(repo_private, "home.json"))
        self.assertIn(repo_private, conf["protectedRoots"]["read"])
        conf["homeFile"] = paths.home_file()
        conf["protectedRoots"] = {k: [test_private if p == repo_private else p for p in v] + (
            [self.root] if k == "write" else []) for k, v in conf["protectedRoots"].items()}
        return conf

    def start_guard(self, native: str = "deny", carriers=("argv", "env")) -> GuardBridge:
        g = GuardBridge(self.guard_config(native, carriers), canon.now())
        self.addCleanup(g.close)
        hb = os.path.join(paths.guard_dir(), "heartbeat.json")
        if os.path.exists(hb):
            os.remove(hb)
        self.assertTrue(g.ask({"op": "heartbeat"})["ok"])
        with open(hb, "r", encoding="utf-8") as fh:
            beat = json.load(fh)
        self.assertEqual((beat["proof_version"], beat["carriers"]), (2, list(carriers)))
        return g

    def sub(self, v):
        if isinstance(v, str):
            if v.startswith("{FIXTURE:") and v.endswith("}"):
                with open(os.path.join(PROFILE_FIX, v[len("{FIXTURE:"):-1]), "r", encoding="utf-8") as fh:
                    return fh.read()
            for k, val in self.vars.items():
                v = v.replace("{%s}" % k, val)
            return v
        if isinstance(v, list):
            return [self.sub(x) for x in v]
        if isinstance(v, dict):
            return {k: self.sub(x) for k, x in v.items()}
        return v

    def decide(self, agent: str, session: str, tool: str, params: dict, ctx: dict | None = None) -> dict:
        self.g.ask({"op": "clock", "now": canon.now()})
        return self.g.ask({"op": "exec" if tool == "exec" else "call", "agent": agent, "session": session,
                           "tool": tool, "params": params, "ctx": ctx or {}})

    def exec_ok(self, agent: str, session: str, command: str, role: str | None = None) -> dict:
        """A bridged exec the guard allows: {params, env, argv} (argv: the jh.py arguments it rewrote)."""
        role = role or agent[len("jobhunter-"):]
        ctx = {"runId": "run-" + role, "workspaceDir": os.path.join(self.ws_root, role)}
        dec = self.decide(agent, session, "exec", {"command": command, "timeoutSeconds": 90}, ctx)
        self.assertEqual(dec["outcome"], "allow", dec)
        params = dec["params"]
        self.assertEqual((params["workdir"], params["timeoutSeconds"]), (os.path.join(self.ws_root, role, "work"), 90))
        return {"params": params, "env": dec["env"], "argv": split_rewritten(params["command"], self.carriers)}

    # ------------------------------------------------------------ jh.py
    def jh(self, argv, env: dict, cwd: str | None = None, isolated: bool = True, what: str = "") -> dict:
        res = run_jh_child(argv, env, cwd=cwd, isolated=isolated)
        self.assertEqual(bool(res["isolated"]), isolated)
        self.ran.append((what or " ".join(str(a) for a in argv[2:4]), res["classify"]))
        return res

    def run_exec(self, ex: dict, env: dict | None = None, argv=None, isolated: bool = True) -> dict:
        """The rewritten exec as OpenClaw's exec tool starts it (env and argv can be swapped by a test)."""
        return self.jh(ex["argv"] if argv is None else argv, exec_tool_env(ex["env"] if env is None else env),
                       cwd=ex["params"]["workdir"], isolated=isolated)

    def assert_agent(self, res: dict, agent: str, code: str = "OK") -> dict:
        env = res["envelope"]
        self.assertEqual(env.get("code"), code, env)
        self.assertEqual(res["classify"][-1], {"class": "agent", "agent_id": agent}, res)
        return env.get("data") or {}

    def assert_refused(self, res: dict, code: str, message: str) -> None:
        env = res["envelope"]
        self.assertEqual((res["rc"] != 0, env.get("code")), (True, code), env)
        self.assertIn(message, env.get("message") or "", env)

    # ------------------------------------------------------------ recorded runs
    def replay(self, recording: dict) -> None:
        for run in recording["runs"]:
            agent, session, ctx = run["agent"], run["session"], self.sub(run["ctx"])
            for i, raw in enumerate(run["steps"]):
                step = self.sub(raw)
                where = "%s step %d" % (session, i + 1)
                dec = self.decide(agent, session, step["tool"], step["params"], ctx)
                want = step.get("expect", "allow")
                self.assertEqual(dec["outcome"], want, "%s: %s" % (where, dec.get("reason")))
                if want != "allow":
                    if step["tool"] == "exec":
                        self.assertIsNone(dec["env"], where)        # a blocked exec never gets an env proof
                    continue
                if step["tool"] == "exec":
                    params = dec["params"]
                    self.assertEqual(params["workdir"], os.path.join(ctx["workspaceDir"], "work"), where)
                    self.assertTrue(dec["env"]["JH_AGENT_PROOF"].startswith("jhe2.%s." % agent), where)
                    self.assertEqual((dec["env"]["JH_AGENT_ID"], dec["env"]["JH_SESSION_KEY"]), (agent, session))
                    ex = {"params": params, "env": dec["env"], "argv": split_rewritten(params["command"])}
                    self.assertTrue(ex["argv"][1].startswith("jhp2.%s." % agent), where)
                    self.allowed.append((agent, params["command"]))
                    res = self.run_exec(ex)
                    data = self.assert_agent(res, step["jh"]["agent_id"], step["jh"]["code"])
                    self.assertEqual(res["markers"], [["OPENCLAW_SHELL"]], where)
                    for k, v in (step["jh"].get("data") or {}).items():
                        self.assertEqual(data.get(k), v, "%s: %s" % (where, k))
                    if step.get("keep"):
                        self.kept[step["keep"]] = ex
                elif step["tool"] == "write":
                    path = step["params"]["path"]
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(step["params"]["content"])

    def guard_log(self) -> list:
        path = os.path.join(paths.logs_dir(), "guard-%s.jsonl" % canon.now()[:7])
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    # ------------------------------------------------------------ OpenClaw's own layers, as install renders them
    def approvals(self) -> dict:
        """agent id -> its exec approvals entry (install render-approvals for this carrier setting)."""
        doc = ins.merge_approvals(None, ins.load_agents(REPO), repo=REPO, py=self.py, root=REPO,
                                  carrier=list(self.carriers))
        return doc["agents"]

    def allowlist_hits(self, entry: dict, commands: list) -> list:
        """Whether each command is an allowlist hit of entry: argv[0] is the entry's program and the rest of the
        command text matches its argPattern, under Python's re and under node's RegExp (the engine OpenClaw
        uses); both engines must agree."""
        (item,) = entry["allowlist"]
        rests = [c[len(item["pattern"]) + 1:] if c.startswith(item["pattern"] + " ") else None for c in commands]
        py_hits = [r is not None and re.search(item["argPattern"], r) is not None for r in rests]
        out = subprocess.run([NODE, ARGPATTERN_JS], input=json.dumps({"pattern": item["argPattern"], "cases": [
            r if r is not None else "" for r in rests]}), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        js_hits = [r is not None and hit for r, hit in zip(rests, json.loads(out.stdout))]
        self.assertEqual(py_hits, js_hits, commands)
        return js_hits

    def pin(self, agent: str):
        return self.g.ask({"op": "prompt_tools", "agent": agent, "session": "agent:%s:main" % agent})["tools"]


class TestRestrictedRuns(ClaudeCliRouteBase):
    def test_onboarding_pair_and_probe_run_as_their_agents(self):
        self.replay(e2e_fixture("claude_cli_restricted.json"))
        # the reported failure: both onboarding records exist, written by the agents
        self.assertTrue(os.path.exists(P.salary_path()))
        st = self.w.ok(["profile", "status"])
        self.assertEqual(st["facts"], len(P.facts()))
        self.assertGreater(st["facts"], 0, st)
        # the probe file of the evaluator: both carriers, the probe session, the exec tool's marker
        with open(os.path.join(paths.state_dir(), "probe", EV + ".json"), "r", encoding="utf-8") as fh:
            probe = json.load(fh)
        self.assertEqual((probe["agent_id"], probe["carriers"], probe["session"]),
                         (EV, ["argv", "env"], auth.session_hash("agent:jobhunter-evaluator:cron:probe-evaluator")))
        self.assertEqual(probe["markers"], ["OPENCLAW_SHELL"])
        # restricted runs never show a native tool to the guard
        self.assertEqual([e for e in self.guard_log() if e.get("kind") == "native_tool"], [])
        # two single-use nonces per agent call, all recorded
        calls = sum(1 for _w, recs in self.ran for c in recs if c.get("class") == "agent")
        used = self.w.one("SELECT COUNT(*) FROM grants_used WHERE command LIKE 'agent-proof jobhunter-%'")[0]
        self.assertEqual(used, 2 * calls)

    def test_the_same_rewritten_call_twice_fails_the_second_time(self):
        self.replay(e2e_fixture("claude_cli_restricted.json"))
        for key in ("salary", "infer", "probe"):
            res = self.run_exec(self.kept[key])
            self.assert_refused(res, "E_AUTH_FAILED", "already used")
        # a fresh env proof of the same session does not revive the used argv proof
        ex = self.kept["salary"]
        fresh = self.g.ask({"op": "env", "agent": SC, "session": "agent:jobhunter-scout:cron:onboard-salary",
                            "runId": "run-sc-1"})["env"]
        self.assert_refused(self.run_exec(ex, env=fresh), "E_AUTH_FAILED", "already used")

    def test_argv_proof_with_another_sessions_env_proof_fails(self):
        a, b = "agent:jobhunter-scout:cron:onboard-salary", "agent:jobhunter-scout:cron:scout-lane"
        ex = self.exec_ok(SC, a, self.vars["JH"] + " whoami")
        other = self.g.ask({"op": "env", "agent": SC, "session": b, "runId": "run-x"})["env"]
        self.assert_refused(self.run_exec(ex, env=other), "E_AUTH_FAILED", "different sessions")
        # the other session's proof under this session's JH_SESSION_KEY does not verify at all
        self.assert_refused(self.run_exec(ex, env=dict(other, JH_SESSION_KEY=a)), "E_AUTH_FAILED",
                            "JH_SESSION_KEY does not match")
        # without the session key variable the two proofs still name different sessions
        bare = {k: v for k, v in other.items() if k != "JH_SESSION_KEY"}
        self.assert_refused(self.run_exec(ex, env=bare), "E_AUTH_FAILED", "different sessions")
        # a refused call burns nothing: the right pair still works, once
        self.assert_agent(self.run_exec(ex), SC)
        self.assert_refused(self.run_exec(ex), "E_AUTH_FAILED", "already used")

    def test_one_carrier_alone_fails_while_both_are_required(self):
        s = "agent:jobhunter-evaluator:cron:onboard-profile"
        ex = self.exec_ok(EV, s, self.vars["JH"] + " profile status")
        # env proof alone: the command without the argv proof pair
        self.assert_refused(self.run_exec(ex, argv=ex["argv"][2:]), "E_AUTH_FAILED", "missing argv proof")
        # argv proof alone: the exec env without JH_AGENT_PROOF
        no_env = {k: v for k, v in ex["env"].items() if k != "JH_AGENT_PROOF"}
        self.assert_refused(self.run_exec(ex, env=no_env), "E_AUTH_FAILED", "missing env proof")
        self.assert_agent(self.run_exec(ex), EV)

    def test_the_scout_cannot_use_the_evaluators_identity(self):
        ev_session, sc_session = "agent:jobhunter-evaluator:cron:onboard-profile", "agent:jobhunter-scout:cron:x"
        ev = self.exec_ok(EV, ev_session, self.vars["JH"] + " profile status")
        sc_ctx = {"runId": "run-sc", "workspaceDir": os.path.join(self.ws_root, "scout")}
        # (a) through the guard: the scout types the evaluator's rewritten command verbatim
        dec = self.decide(SC, sc_session, "exec", {"command": ev["params"]["command"], "timeoutSeconds": 90}, sc_ctx)
        self.assertEqual((dec["outcome"], dec["env"]), ("G_EXEC_PARAM", None), dec)
        dec = self.decide(SC, sc_session, "exec", {"command": "%s -I %s/scripts/jh.py --agent-proof %s profile status"
                                                    % (self.py, REPO, FAKE_PROOF)}, sc_ctx)
        self.assertEqual(dec["outcome"], "G_EXEC_PARAM", dec)
        # (b) around the guard: the evaluator's argv proof with the scout's own env proof
        sc_env = self.g.ask({"op": "env", "agent": SC, "session": sc_session, "runId": "run-sc"})["env"]
        self.assert_refused(self.run_exec(ev, env=sc_env), "E_AUTH_FAILED", "different agents")
        # ... also when it names the evaluator in JH_AGENT_ID and drops its session key
        dressed = dict(sc_env, JH_AGENT_ID=EV)
        dressed.pop("JH_SESSION_KEY")
        self.assert_refused(self.run_exec(ev, env=dressed), "E_AUTH_FAILED", "different agents")
        # (c) the evaluator's argv proof with no env proof, claiming the evaluator by name
        self.assert_refused(self.run_exec(ev, env={"JH_AGENT_ID": EV}), "E_AUTH_FAILED", "missing env proof")
        # (d) a name and the exec marker without any proof
        self.assert_refused(self.run_exec(ev, env={"JH_AGENT_ID": EV}, argv=ev["argv"][2:]), "E_AUTH_FAILED",
                            "without the guard's proof")
        # (e) a well-formed forged proof is refused by jh.py itself (L4; the allowlist checks the shape only)
        forged = ["--agent-proof", FAKE_PROOF] + ev["argv"][2:]
        self.assert_refused(self.run_exec(ev, argv=forged), "E_AUTH_FAILED", "proof")
        # the evaluator's own pair is untouched by all of that
        self.assert_agent(self.run_exec(ev), EV)

    def test_an_agent_call_without_python_I_is_refused(self):
        ex = self.exec_ok(SC, "agent:jobhunter-scout:cron:onboard-salary", self.vars["JH"] + " whoami")
        self.assert_refused(self.run_exec(ex, isolated=False), "E_AUTH_FAILED", "python -I")
        self.assert_agent(self.run_exec(ex), SC)

    def test_the_tool_surface_pin(self):
        ask = lambda agent: self.g.ask({"op": "prompt_tools", "agent": agent,  # noqa: E731
                                        "session": "agent:%s:main" % agent})["tools"]
        self.assertEqual(sorted(ask(EV)), ["exec", "read", "write"])
        self.assertEqual(sorted(ask(SC)), ["browser", "exec", "read", "write"])
        self.assertEqual(ask("jobhunter-qc"), [])
        self.assertIsNone(ask("main"))

    def test_allowed_calls_need_no_approval_at_any_layer(self):
        """Every call the recorded runs needed went through without a person: the guard allowed it (never
        requireApproval, which the bridge reports as REQUIRE_APPROVAL), the run is a restricted cron run with an
        explicit tools list, the agent's exec policy is allowlist with ask off on both config documents install
        writes (mode allowlist in the agents patch, explicit values in the approvals), and every rewritten command is a hit in that agent's own allowlist entry (and a miss in every
        other agent's), so OpenClaw runs it at once."""
        rec = e2e_fixture("claude_cli_restricted.json")
        self.replay(rec)
        self.assertEqual(sorted({a for a, _c in self.allowed}), [EV, SC])
        entries = self.approvals()
        patch = ins.render_agents_patch(ins.load_agents(REPO), ws_root=self.ws_root, repo=REPO, py=self.py,
                                        root=REPO)["agents"]["entries"]
        jobs = {j["key"]: j for j in ins.declared_jobs(ins.load_crons(REPO))}
        for run in rec["runs"]:
            agent = run["agent"]
            # L1: a restricted run (cron --tools with an explicit list, never "*"), the same list as the pin
            job = jobs["jobhunter:" + run["session"].split(":cron:", 1)[1]]
            self.assertEqual((job["kind"], job["agent"]), ("agent", agent))
            tools = ins.tools_list(job)
            args = ins.cron_add_args(job, py=self.py, repo=REPO, ws_root=self.ws_root, tz="UTC")
            self.assertEqual(args[args.index("--tools") + 1], ",".join(tools))
            self.assertNotIn("*", tools)
            self.assertEqual(sorted(tools), sorted(self.pin(agent)))
            used = {s["tool"] for s in run["steps"] if s.get("expect", "allow") == "allow"}
            self.assertLessEqual(used, set(tools), run["session"])
            # L2: explicit allowlist, never ask, no safe bins, no elevated exec, workspace-only files. `mode` only
            # (CLI route 6.1, D2): OpenClaw refuses `mode` next to `security`/`ask`, so the patch deletes those
            # keys of an older install (JSON null) and mode allowlist stands for security allowlist with ask off
            t = patch[agent]["tools"]
            self.assertEqual({k: t["exec"][k] for k in ("mode", "security", "ask", "safeBins", "safeBinTrustedDirs")},
                             {"mode": "allowlist", "security": None, "ask": None, "safeBins": [],
                              "safeBinTrustedDirs": []})
            spec = next(a for a in ins.load_agents(REPO) if a["id"] == agent)
            self.assertFalse({"security", "ask"} & ins.exec_rendered_keys(spec))
            # the effective policy OpenClaw derives from that mode passes the installer's own read-back
            eff = {"mode": "allowlist", "security": "allowlist", "ask": "off", "elevated": t["elevated"],
                   "approvals_ask": entries[agent]["ask"], "approvals_fallback": entries[agent]["askFallback"]}
            self.assertEqual(ins.exec_policy_problems(spec, eff), [])
            self.assertIsNone(ins.unconfined(eff))
            self.assertEqual((t["elevated"], t["fs"]), ({"enabled": False}, {"workspaceOnly": True}))
            self.assertLessEqual({"edit", "apply_patch"}, set(t["deny"]))
            e = entries[agent]
            self.assertEqual((e["security"], e["ask"], e["askFallback"]), ("allowlist", "off", "deny"))
        self.assertEqual((entries["jobhunter-qc"]["security"], entries["jobhunter-qc"]["allowlist"]), ("deny", []))
        # every command the guard allowed is a hit for its own agent only
        commands = [c for _a, c in self.allowed]
        for other, entry in entries.items():
            if not entry.get("allowlist"):
                continue
            want = [a == other for a, _c in self.allowed]
            self.assertEqual(self.allowlist_hits(entry, commands), want, other)

    def test_shell_metacharacters_are_refused_by_the_guard_and_by_the_allowlist(self):
        session = "agent:jobhunter-scout:cron:onboard-salary"
        ctx = {"runId": "run-sc", "workspaceDir": os.path.join(self.ws_root, "scout")}
        plain = self.vars["JH"] + " whoami"
        typed = [plain + self.sub(s) for s in SHELL_SUFFIXES] + [p + plain for p in SHELL_PREFIXES]
        typed += ["/bin/sh -c '%s'" % plain, "%s -c 'import os' %s/scripts/jh.py whoami" % (self.py, REPO)]
        for command in typed:
            dec = self.decide(SC, session, "exec", {"command": command, "timeoutSeconds": 90}, ctx)
            self.assertEqual((dec["outcome"], dec["env"]), ("G_EXEC_SHAPE", None), repr(command))
        # L2 on its own: the same text after a genuine guard-rewritten command (as if the guard had let it
        # through) is a miss in the agent's allowlist entry, while the rewritten command alone is a hit
        ex = self.exec_ok(SC, session, plain)
        cmd = ex["params"]["command"]
        cases = [cmd] + [cmd + self.sub(s) for s in SHELL_SUFFIXES] + [p + cmd for p in SHELL_PREFIXES]
        self.assertEqual(self.allowlist_hits(self.approvals()[SC], cases), [True] + [False] * (len(cases) - 1))
        # nothing ran: the one guard-rewritten call is still good, once
        self.assertFalse(os.path.exists(os.path.join(self.ws_root, "scout", "work", "out.txt")))
        self.assertEqual(self.w.one("SELECT COUNT(*) FROM grants_used")[0], 0)
        self.assert_agent(self.run_exec(ex), SC)

    def test_out_of_workspace_paths_are_refused(self):
        sc_ctx = {"runId": "run-sc", "workspaceDir": os.path.join(self.ws_root, "scout")}
        session = "agent:jobhunter-scout:cron:onboard-salary"
        work = os.path.join(self.ws_root, "scout", "work")
        os.makedirs(work, exist_ok=True)
        os.symlink(os.path.join(self.root, "private", "guard.key"), os.path.join(work, "notes.txt"))
        reads = ["/etc/hosts", "$HOME/.ssh/id_ed25519", "${HOME}/.ssh/id_ed25519", "~/.ssh/id_ed25519",
                 "~root/.profile", "file:///etc/hosts", "node://n/etc/hosts", "@/etc/hosts", "work\\..\\..\\x",
                 os.path.join(REPO, "scripts", "jobhunter", "auth.py"), os.path.join(self.root, "private", "home.json"),
                 os.path.join(self.root, "private", "guard.key"),
                 os.path.join(self.ws_root, "scout", "..", "evaluator", "ref", "profile_inference.md"),
                 os.path.join(self.ws_root, "evaluator", "work", "onboarding", "profile_inputs.json"),
                 os.path.join(work, "notes.txt")]
        writes = ["/tmp/jh-int-probe.txt", os.path.join(self.ws_root, "scout", "ref", "salary_research.md"),
                  os.path.join(self.ws_root, "evaluator", "work", "x.json"), os.path.join(REPO, "scripts", "x.py"),
                  os.path.join(self.ws_root, "scout", "skills", "x", "SKILL.md")]
        for path in reads:
            dec = self.decide(SC, session, "read", {"path": path}, sc_ctx)
            self.assertEqual(dec["outcome"], "G_PATH_DENIED", path)
        for path in writes:
            dec = self.decide(SC, session, "write", {"path": path, "content": "x"}, sc_ctx)
            self.assertEqual(dec["outcome"], "G_PATH_DENIED", path)
            self.assertFalse(os.path.exists(path), path)
        # jh.py's own --file confinement (L4), with genuine proofs as if the guard had allowed the call
        other = os.path.join(work, "onboarding", "salary.json")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("{}")
        for path in ("/etc/hosts", other, os.path.join(self.root, "private", "home.json")):
            argv = ["profile", "infer-record", "--file", path]
            res = self.jh(helpers.agent_argv(self.root, EV, argv), helpers.agent_env(self.root, EV),
                          cwd=os.path.join(self.ws_root, "evaluator", "work"))
            self.assertEqual(res["classify"], [{"class": "agent", "agent_id": EV}])
            self.assert_refused(res, "E_PATH_NOT_ALLOWED", "own work/ or inbox/")
        self.assertFalse(P.facts())

    def test_commands_outside_the_acl_are_refused_by_the_guard_and_by_jh_py(self):
        """L3 and L4 separately: the guard refuses the command, and jh.py refuses it even with a genuine pair of
        proofs (as if the guard had let it through)."""
        cases = ((SC, ["profile", "infer-record", "--file", "{WS_ROOT}/scout/work/onboarding/p.json"]),
                 (SC, ["eval", "next"]), (EV, ["dispatch", "tick"]), (EV, ["approve", "A7K2"]),
                 (EV, ["pause"]), (EV, ["unpause"]), (SC, ["profile", "status"]))
        for agent, argv in cases:
            argv = self.sub(argv)
            role = agent[len("jobhunter-"):]
            ctx = {"runId": "run-" + role, "workspaceDir": os.path.join(self.ws_root, role)}
            dec = self.decide(agent, "agent:%s:cron:x" % agent, "exec",
                              {"command": self.vars["JH"] + " " + " ".join(argv), "timeoutSeconds": 90}, ctx)
            self.assertEqual(dec["outcome"], "G_EXEC_ACL", (agent, argv, dec))
            self.assertIsNone(dec["env"])
            res = self.jh(helpers.agent_argv(self.root, agent, argv), helpers.agent_env(self.root, agent),
                          cwd=os.path.join(self.ws_root, role, "work"), what="%s %s" % (agent, argv[0]))
            self.assertEqual(res["classify"], [{"class": "agent", "agent_id": agent}])
            self.assert_refused(res, "E_CALLER_NOT_ALLOWED", "may not run")
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "paused")))

    def test_main_cannot_use_the_evaluators_identity(self):
        """The owner's main agent on claude-cli (native tools, OPENCLAW_MCP_TOKEN in its env): through the guard
        it cannot pass an --agent-proof, and around the guard a captured argv proof alone is refused; main itself
        is an unproven agent with public read-only commands, never system."""
        ev = self.exec_ok(EV, "agent:jobhunter-evaluator:cron:onboard-profile", self.vars["JH"] + " profile status")
        main_ctx = {"cwd": REPO, "workspaceDir": os.path.join(self.root, "main-ws")}
        for command in (ev["params"]["command"], "%s --agent-proof %s profile status" % (self.vars["JH"], FAKE_PROOF)):
            dec = self.decide("main", "agent:main:main", "exec", {"command": command}, main_ctx)
            self.assertEqual(dec["outcome"], "G_EXEC_PARAM", dec)
        dec = self.decide("main", "agent:main:main", "read", {"path": os.path.join(self.root, "private", "guard.key")},
                          main_ctx)
        self.assertEqual(dec["outcome"], "G_PATH_DENIED", dec)
        main_env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "OPENCLAW_MCP_TOKEN": "mcp-token-example"}
        self.assert_refused(self.jh(ev["argv"], main_env, cwd=REPO), "E_AUTH_FAILED", "missing env proof")
        self.assert_refused(self.jh(ev["argv"][2:], dict(main_env, JH_AGENT_ID=EV), cwd=REPO), "E_AUTH_FAILED",
                            "without the guard's proof")
        res = self.jh(["profile", "infer-record", "--file", "/etc/hosts"], main_env, cwd=REPO, isolated=False)
        self.assert_refused(res, "E_GUARD_MISSING", "")
        self.assertEqual(res["classify"], [{"class": "agent", "agent_id": None}])
        self.assertEqual(res["markers"], [["OPENCLAW_MCP_TOKEN", "CLAUDECODE"]])   # CLAUDECODE counts in any cwd
        res = self.jh(["status"], main_env, cwd=REPO, isolated=False)
        self.assertEqual((res["rc"], res["classify"]), (0, [{"class": "agent", "agent_id": None}]), res)
        # the evaluator's own pair is untouched
        self.assert_agent(self.run_exec(ev), EV)


class TestNativeTools(ClaudeCliRouteBase):
    def test_native_tool_calls_are_refused_by_the_guard(self):
        rec = self.sub(e2e_fixture("claude_cli_native.json"))
        for i, step in enumerate(rec["steps"]):
            dec = self.decide(rec["agent"], rec["session"], step["tool"], step["params"], rec["ctx"])
            self.assertEqual(dec["outcome"], step["expect"], "step %d: %s" % (i + 1, dec.get("reason")))
            self.assertIsNone(dec["env"] if step["tool"] == "exec" else None)
        native = [e for e in self.guard_log() if e.get("kind") == "native_tool"]
        self.assertEqual(len(native), len(rec["steps"]))
        self.assertTrue(all(e["agent"] == EV and e["decision"] == "block" for e in native), native)

    def test_native_commands_without_the_guard_are_unproven_agents(self):
        """The guard off or not loaded: Claude Code's Bash runs the text as typed in the agent's workspace with
        the harness variables, no proof of either kind. jh.py answers public read-only commands only."""
        rec = self.sub(e2e_fixture("claude_cli_native.json"))
        jh_path = os.path.join(REPO, "scripts", "jh.py")
        for item in rec["raw"]:
            toks = item["command"].split()
            isolated = toks[1] == "-I"
            argv = toks[3:] if isolated else toks[2:]
            self.assertEqual(toks[2 if isolated else 1], jh_path)
            res = self.jh(argv, dict(rec["native_env"]), cwd=rec["ctx"]["cwd"], isolated=isolated,
                          what="raw " + " ".join(argv))
            env = res["envelope"]
            self.assertEqual(env.get("code"), item["code"], "%s: %s" % (item["command"], env))
            # classified as an agent without identity by the harness markers, whatever the command
            self.assertEqual(res["classify"], [{"class": "agent", "agent_id": None}], item["command"])
            self.assertEqual(res["markers"], [["OPENCLAW_MCP_TOKEN", "CLAUDECODE"]], item["command"])
        self.assertFalse(os.path.exists(P.salary_path()))
        self.assertEqual(self.w.one("SELECT COUNT(*) FROM grants_used")[0], 0)


class TestGuardOffLayers(ClaudeCliRouteBase):
    """The guard-off variants of the live checks (CLI-ROUTE-DESIGN 13.3 T3b, T4b, T5c, T6 pin off, T10 and T13
    guard off) on
    the layers that stay when the guard plugin is disabled: OpenClaw's exec allowlist as install renders it (L2)
    and jh.py (L4). Without the guard nothing rewrites the command and resolve_exec_env adds no JH_* value, so
    the command runs as typed with the exec tool's marker only. These are offline stand-ins: they do not replace
    the live runs, which the release gate still requires (tests/fixtures/e2e/live_gate.py)."""

    def hits(self, agent: str, commands: list) -> list:
        return self.allowlist_hits(self.approvals()[agent], commands)

    def test_t3b_plain_and_forged_commands_without_the_guard(self):
        jh_i = "%s -I %s/scripts/jh.py" % (self.py, REPO)
        plain, bare = self.vars["JH"] + " whoami", jh_i + " whoami"
        forged = "%s --agent-proof %s whoami" % (jh_i, FAKE_PROOF)
        # L2: the plain command (no -I, no proof) and the -I command without a proof miss the allowlist; a
        # forged proof of the right shape naming this agent is a hit (L2 checks the shape only)
        self.assertEqual(self.hits(EV, [plain, bare, forged]), [False, False, True])
        # ... and a miss for every other agent: the argPattern pins the agent id in the proof
        for other, entry in self.approvals().items():
            if other != EV and entry.get("allowlist"):
                self.assertEqual(self.allowlist_hits(entry, [forged]), [False], other)
        # L4: the hit reaches jh.py as typed, with the exec marker only, and is refused there
        ws = os.path.join(self.ws_root, "evaluator", "work")
        for argv in (["--agent-proof", FAKE_PROOF, "whoami"], ["--agent-proof", FAKE_PROOF, "dispatch", "tick"]):
            res = self.jh(argv, exec_tool_env(None), cwd=ws, what="guard off " + argv[-1])
            self.assert_refused(res, "E_AUTH_FAILED", "proof")
        self.assertEqual(self.w.one("SELECT COUNT(*) FROM grants_used")[0], 0)
        self.assertEqual([e for e in self.guard_log() if e.get("agent") == EV], [])

    def test_t5c_another_agents_minted_command_misses_the_allowlist(self):
        ev = self.exec_ok(EV, "agent:jobhunter-evaluator:cron:onboard-profile", self.vars["JH"] + " profile status")
        cmd = ev["params"]["command"]
        self.assertEqual(self.hits(SC, [cmd]), [False])
        self.assertEqual(self.hits(EV, [cmd]), [True])
        # had it run anyway, the scout's exec carries no env proof of the evaluator: refused, nothing burnt
        self.assert_refused(self.run_exec(ev, env={}), "E_AUTH_FAILED", "missing env proof")
        self.assert_agent(self.run_exec(ev), EV)

    def test_t6_pin_off_native_shapes_miss_every_allowlist(self):
        rec = self.sub(e2e_fixture("claude_cli_native.json"))
        native = ["/bin/ls /", "/bin/cat /etc/hosts", "/usr/bin/id"] + [i["command"] for i in rec["raw"]]
        for agent, entry in self.approvals().items():
            if entry.get("allowlist"):
                self.assertEqual(self.allowlist_hits(entry, native), [False] * len(native), agent)
            else:
                self.assertEqual(entry["security"], "deny", agent)

    def test_t10_guard_off_main_is_an_unproven_agent(self):
        main_env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "OPENCLAW_MCP_TOKEN": "mcp-token-example"}
        res = self.jh(["dispatch", "tick"], main_env, cwd=REPO, isolated=False, what="main dispatch tick")
        self.assert_refused(res, "E_GUARD_MISSING", "")
        self.assertEqual(res["classify"], [{"class": "agent", "agent_id": None}])
        self.assertIn("OPENCLAW_MCP_TOKEN", res["markers"][0])
        res = self.jh(["status"], main_env, cwd=REPO, isolated=False, what="main status")
        self.assertEqual((res["rc"], res["classify"]), (0, [{"class": "agent", "agent_id": None}]), res)
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "paused")))

    def agents_patch(self, route: str = "cli") -> dict:
        return ins.render_agents_patch(ins.load_agents(REPO), ws_root=self.ws_root, repo=REPO, py=self.py,
                                       route=route)["agents"]["entries"]

    def test_t4b_workspace_only_is_the_file_layer_without_the_guard(self):
        """T4b offline: with the guard off, OpenClaw's workspaceOnly (L2) is the only file layer. Every agent
        gets it on both routes, with its own workspace, nodes denied (no node for a node:// form to reach) and
        elevated off; every T4 read target lies outside that workspace, so workspaceOnly must refuse it (whether
        it does for every path form is V5, live only). The in-workspace writes of T4 are inside: OpenClaw alone
        allows them and only the guard refuses them (the single-layer rows of 4.1)."""
        agents = ins.load_agents(REPO)
        for route in ("cli", "api_key"):
            entries = self.agents_patch(route)
            self.assertEqual(sorted(entries), sorted(a["id"] for a in agents))
            for a in agents:
                tools = entries[a["id"]]["tools"]
                self.assertEqual(entries[a["id"]]["workspace"], os.path.join(self.ws_root, a["role"]), route)
                self.assertIs(tools["fs"]["workspaceOnly"], True, (route, a["id"]))
                self.assertIn("nodes", tools["deny"], (route, a["id"]))
                self.assertEqual(tools["elevated"], {"enabled": False}, (route, a["id"]))
        ws = os.path.realpath(os.path.join(self.ws_root, "evaluator"))
        os.makedirs(os.path.join(ws, "work"), exist_ok=True)

        def inside(path: str) -> bool:
            p = path[len("file://"):] if path.startswith("file://") else path
            p = p[1:] if p.startswith("@") else p
            p = os.path.expanduser(os.path.expandvars(p))
            p = os.path.realpath(os.path.join(ws, p))      # relative forms resolve against the workspace
            return os.path.commonpath([p, ws]) == ws

        key = os.path.join(self.root, "private", "guard.key")
        home_rel = os.path.relpath(key, os.path.expanduser("~"))
        reads = ["/etc/hosts", key, "~/" + home_rel, "$HOME/" + home_rel, "@" + key, "file://" + key,
                 os.path.join(self.root, "private", "home.json"), os.path.join(REPO, "scripts", "jobhunter", "auth.py"),
                 os.path.join(self.ws_root, "scout", "ref", "salary_research.md"), "work/../../scout/ref/x",
                 os.path.join(ws, "work", "..", "..", "scout", "ref", "x"), "../../../etc/hosts"]
        self.assertEqual([p for p in reads if inside(p)], [])
        single = [os.path.join(ws, "ref", "x"), "work/AGENTS.md", os.path.join(ws, "skills", "x", "SKILL.md")]
        self.assertEqual([p for p in single if not inside(p)], [])
        ctx = {"runId": "run-evaluator", "workspaceDir": ws}
        for path in single:
            dec = self.decide(EV, "agent:jobhunter-evaluator:cron:t4", "write", {"path": path, "content": "x"}, ctx)
            self.assertEqual(dec["outcome"], "G_PATH_DENIED", path)
        self.assertFalse(os.path.exists(os.path.join(ws, "ref", "x")))

    def test_t13_guard_off_browser_profile_is_a_single_layer(self):
        """T13 off offline: OpenClaw has no per-agent browser profile control, so without the guard the
        `jobhunter` profile rests on the agents' instructions alone (the single-layer row of 4.1). Checked: only
        the three browsing agents get the browser tool, the installer may add the jobhunter profile but never
        change browser.defaultProfile, every browsing agent is told to pass the profile, and every complete
        browser call example in the templates names it. With the guard on, a call without it is blocked."""
        agents = ins.load_agents(REPO)
        browsing = sorted(a["id"] for a in agents if "browser" in a["tools_allow"])
        self.assertEqual(browsing, ["jobhunter-applier", "jobhunter-outreach", "jobhunter-scout"])
        for route in ("cli", "api_key"):
            entries = self.agents_patch(route)
            for a in agents:
                tools = entries[a["id"]]["tools"]
                self.assertEqual(("browser" in tools["allow"], "browser" in tools["deny"]),
                                 (a["id"] in browsing, a["id"] not in browsing), (route, a["id"]))
        ins.assert_patch_scope(ins.headless_patch(), [])
        for bad in ({"browser": {"defaultProfile": ins.BROWSER_PROFILE}},
                    {"browser": {"profiles": {"openclaw": {"headless": False}}}}):
            with self.assertRaises(ins.Denied):
                ins.assert_patch_scope(bad, [])
        for a in agents:
            if a["id"] in browsing:
                with open(os.path.join(REPO, a["template_dir"], "AGENTS.template.md"), "r", encoding="utf-8") as fh:
                    self.assertIn('`profile: "jobhunter"`', fh.read(), a["id"])
        checked = 0
        for top in ("agent-templates", "skills-src"):
            for dirpath, _dirs, files in os.walk(os.path.join(REPO, top)):
                for name in files:
                    if not name.endswith(".md"):
                        continue
                    with open(os.path.join(dirpath, name), "r", encoding="utf-8") as fh:
                        text = fh.read()
                    for m in re.finditer(r'`(\{"action":[^`]*\})`', text):
                        try:
                            call = json.loads(m.group(1))
                        except ValueError:
                            continue            # a shape with placeholders, not a call to copy
                        if len(call) > 1:       # {"action": "click"} alone names an action kind only
                            checked += 1
                            self.assertEqual(call.get("profile"), ins.BROWSER_PROFILE, (name, m.group(1)))
        self.assertGreater(checked, 10)
        ctx = {"runId": "run-scout", "workspaceDir": os.path.join(self.ws_root, "scout")}
        session = "agent:jobhunter-scout:cron:t13"
        for params, want in (({"action": "status"}, "G_BROWSER_PROFILE"),
                             ({"action": "status", "profile": "openclaw"}, "G_BROWSER_PROFILE"),
                             ({"action": "status", "profile": ins.BROWSER_PROFILE}, "allow")):
            self.assertEqual(self.decide(SC, session, "browser", params, ctx)["outcome"], want, params)


class ModeNBase(ClaudeCliRouteBase):
    """Mode N (--cli-tools native --i-accept-reduced-protection): the guard gates native Bash and inserts -I and
    the argv proof; resolve_exec_env never fires for native Bash, so no env proof arrives."""
    native_tools = "gate"

    def native_exec(self) -> dict:
        rec = self.sub(e2e_fixture("claude_cli_native.json"))
        for step in rec["gate"]:
            dec = self.g.ask({"op": "call", "agent": rec["agent"], "session": rec["session"], "tool": step["tool"],
                              "params": step["params"], "ctx": rec["ctx"]})
            self.assertEqual(dec["outcome"], step["expect"], dec.get("reason"))
        first = rec["gate"][0]
        dec = self.g.ask({"op": "call", "agent": rec["agent"], "session": rec["session"], "tool": "exec",
                          "params": first["params"], "ctx": rec["ctx"]})
        self.assertEqual(dec["outcome"], "allow", dec)
        self.assertEqual(dec["params"]["timeout"], 90000)
        argv = split_rewritten(dec["params"]["command"], ("argv",))
        self.assertTrue(argv[1].startswith("jhp2.%s." % EV))
        return {"argv": argv, "env": dict(rec["native_env"]), "cwd": rec["ctx"]["cwd"]}


class TestModeNGateBothCarriers(ModeNBase):
    """Mode N with the env carrier: the installer no longer writes it (install.check_cli_tools), because native
    Bash never gets the env proof. A guard config edited by hand, or left by an older install, still proves
    nothing: jh.py refuses the call."""

    def guard_config(self, native: str, carriers) -> dict:
        conf = super().guard_config(native, ("argv",))
        conf["proofCarriers"] = list(carriers)
        return conf

    def test_the_installer_refuses_mode_n_with_the_env_carrier(self):
        for carriers in (["argv", "env"], ["env"]):
            with self.subTest(carriers=carriers):
                with self.assertRaises(ins.Denied) as cm:
                    ins.render_guard_config(repo=REPO, py=self.py, cfg={}, carriers=carriers, cli_tools="native")
                self.assertIn("argv identity carrier", cm.exception.message)

    def test_with_both_carriers_required_a_native_call_is_refused(self):
        n = self.native_exec()
        res = self.jh(n["argv"], n["env"], cwd=n["cwd"])
        self.assert_refused(res, "E_AUTH_FAILED", "missing env proof")


class TestModeNGateArgvCarrier(ModeNBase):
    carriers = ("argv",)

    def test_with_the_argv_carrier_a_native_call_is_proven(self):
        n = self.native_exec()
        data = self.assert_agent(self.jh(n["argv"], n["env"], cwd=n["cwd"]), EV)
        self.assertEqual((data["carriers"], data["isolated"]), (["argv"], True))
        self.assertEqual(data["markers"], ["OPENCLAW_MCP_TOKEN", "CLAUDECODE"])
        self.assert_refused(self.jh(n["argv"], n["env"], cwd=n["cwd"]), "E_AUTH_FAILED", "already used")


class TestArgvCarrierOnly(ClaudeCliRouteBase):
    """--identity-carrier argv (V2 fails: resolve_exec_env does not fire for bridged exec)."""
    carriers = ("argv",)

    def test_argv_carrier(self):
        ex = self.exec_ok(SC, "agent:jobhunter-scout:cron:onboard-salary", self.vars["JH"] + " whoami")
        self.assertNotIn("JH_AGENT_PROOF", ex["env"])
        data = self.assert_agent(self.run_exec(ex), SC)
        self.assertEqual(data["carriers"], ["argv"])
        self.assert_refused(self.run_exec(ex), "E_AUTH_FAILED", "already used")
        # an env proof that is there must still verify and agree
        other = self.g.ask({"op": "env", "agent": EV, "session": "agent:jobhunter-evaluator:x", "runId": "r"})["env"]
        self.assertIsNone(other.get("JH_AGENT_PROOF"))
        ex2 = self.exec_ok(SC, "agent:jobhunter-scout:cron:onboard-salary", self.vars["JH"] + " whoami")
        forged = dict(ex2["env"], JH_AGENT_PROOF="jhe2.jobhunter-scout.1790000000.0123456789abcdef."
                                                  "0123456789abcdef." + "b" * 64)
        self.assert_refused(self.run_exec(ex2, env=forged), "E_AUTH_FAILED", "proof")


class TestEnvCarrierOnly(ClaudeCliRouteBase):
    """--identity-carrier env (F-ENV: V3 fails, the guard's rewrite of bridged exec is not applied as a whole)."""
    carriers = ("env",)

    def test_env_carrier(self):
        ex = self.exec_ok(EV, "agent:jobhunter-evaluator:cron:onboard-profile", self.vars["JH"] + " whoami")
        self.assertEqual(ex["argv"], ["whoami"])
        data = self.assert_agent(self.run_exec(ex), EV)
        self.assertEqual(data["carriers"], ["env"])
        self.assert_refused(self.run_exec(ex), "E_AUTH_FAILED", "already used")
        # still no system: the exec marker without a proof is an unproven agent
        res = self.run_exec(ex, env={"JH_AGENT_ID": EV})
        self.assert_refused(res, "E_AUTH_FAILED", "without the guard's proof")


if __name__ == "__main__":
    unittest.main()
