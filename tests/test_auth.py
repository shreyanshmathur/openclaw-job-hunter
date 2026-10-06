"""Caller classes, owner PIN, chat grants and ACL argument classes (design 3.1, 3.3, 12.19, 13.2)."""
from __future__ import annotations

import io
import json
import os
from unittest import mock

import tests  # noqa: F401
from jobhunter import auth, canon, cli, paths
from tests import helpers
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase

PIN = "482915"


class AuthCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        auth.create_guard_key()

    def run_cli(self, argv, env=None, stdin="", cwd=None):
        out = io.StringIO()
        before = os.getcwd()
        if cwd:
            os.chdir(cwd)
        try:
            rc = cli.main(argv, env=env or {}, stdin=io.StringIO(stdin), stdout=out)
        finally:
            os.chdir(before)
        return rc, json.loads(out.getvalue())


class TestClassify(AuthCase):
    def test_classes(self):
        c = auth.classify(["budget"], {}, None)
        self.assertEqual((c.cls, c.agent_id), ("system", None))
        # a harness marker without a proof is an unproven agent: public read-only commands only
        c = auth.classify(["budget"], {"OPENCLAW_SHELL": "1"}, None)
        self.assertEqual((c.cls, c.agent_id, c.detail["markers"]), ("agent", None, ["OPENCLAW_SHELL"]))
        c = auth.classify(["budget"], {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"}, None)
        self.assertEqual((c.cls, c.agent_id), ("agent", None))
        # a jobhunter agent id without the guard's proof is a spoof
        for env in ({"OPENCLAW_SHELL": "exec", "JH_AGENT_ID": "jobhunter-scout"}, {"JH_AGENT_ID": "jobhunter-qc"}):
            with self.subTest(env=env):
                self.assertDenied("E_AUTH_FAILED", auth.classify, ["budget"], env, None)
        for argv in (["--pin-stdin", "unpause"], ["--grant", "1.2.3", "pause"]):
            with self.subTest(argv=argv):
                self.assertDenied("E_AUTH_FAILED", auth.classify, argv, {"OPENCLAW_SHELL": "1"}, None)

    def test_harness_markers(self):
        self.assertEqual(auth.harness_markers({}), [])
        self.assertEqual(auth.harness_markers({"OPENCLAW_SHELL": ""}), [])
        self.assertEqual(auth.harness_markers({"OPENCLAW_MCP_TOKEN": ""}), ["OPENCLAW_MCP_TOKEN"])
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            with self.subTest(key=key):
                self.assertEqual(auth.harness_markers({key: "1"}), ["CLAUDECODE"])
                self.assertEqual(auth.harness_markers({key: ""}), ["CLAUDECODE"])
        c = auth.classify(["status"], {"CLAUDECODE": "1", "OPENCLAW_MCP_TOKEN": "t"}, None)
        self.assertEqual((c.cls, c.agent_id, c.detail["markers"]), ("agent", None, ["OPENCLAW_MCP_TOKEN", "CLAUDECODE"]))

    def test_claude_code_marks_a_call_from_any_working_directory(self):
        """A Claude Code Bash call is an unproven agent wherever it runs (it used to count only inside WS_ROOT, so a
        call from the repo or /tmp was `system`). The human and system entry points drop the marker themselves."""
        for cwd in (paths.ws_root(), self.home.dir, paths.REPO, os.path.dirname(self.home.dir)):
            for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                with self.subTest(cwd=cwd, key=key):
                    rc, env = self.run_cli(["pause"], env={key: "1"}, cwd=cwd)
                    self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"), env)
                    rc, env = self.run_cli(["budget"], env={key: "1"}, cwd=cwd)        # public read-only
                    self.assertEqual(rc, 0, env)
        # after the entry point's scrub, the same call is `system` again
        env = auth.scrub_agent_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/usr/bin"})
        self.assertEqual(auth.classify(["pause"], env, None).cls, "system")
        self.assertDenied("E_AUTH_FAILED", auth.classify, ["--pin-stdin", "unpause"], {"CLAUDECODE": "1"}, None)

    def test_unproven_agents_get_public_read_only_commands(self):
        rc, env = self.run_cli(["breaker", "status"], env={"OPENCLAW_SHELL": "1"})
        self.assertEqual(rc, 0)
        rc, env = self.run_cli(["pause"], env={"OPENCLAW_SHELL": "1"})
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))
        self.assertIn("no verified agent identity", env["message"])
        rc, env = self.run_cli(["approval", "set", "auto"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))
        rc, env = self.run_cli(["budget"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual(rc, 0)
        rc, env = self.run_cli(["whoami"], env={"OPENCLAW_MCP_TOKEN": "x"})
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))
        rc, env = self.run_cli(["whoami"])
        self.assertEqual((rc, env["data"]["class"], env["data"]["carriers"]), (0, "system", []))

    def test_scrub_agent_env(self):
        env = {"PATH": "/usr/bin", "HOME": "/h", "OPENCLAW_SHELL": "exec", "OPENCLAW_CHANNEL_CONTEXT": "x",
               "OPENCLAW_MCP_TOKEN": "t", "OPENCLAW_MCP_URL": "u", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
               "CLAUDE_CODE_SSE_PORT": "1", "JH_AGENT_ID": "jobhunter-scout", "JH_AGENT_PROOF": "p",
               "JH_SESSION_KEY": "s", "JH_RUN_ID": "r", "JOBHUNTER_HOME": "/x", "JOBHUNTER_DB": "/y",
               "OPENCLAW_PROFILE": "jhtest", "CLAUDE_CONFIG_DIR": "/c"}
        self.assertEqual(auth.scrub_agent_env(env), {"PATH": "/usr/bin", "HOME": "/h", "OPENCLAW_PROFILE": "jhtest",
                                                     "CLAUDE_CONFIG_DIR": "/c"})
        self.assertEqual(auth.classify(["budget"], auth.scrub_agent_env(env), None).cls, "system")


class ProofCase(AuthCase):
    EV = "jobhunter-evaluator"
    SC = "jobhunter-scout"

    def setUp(self):
        super().setUp()
        helpers.ensure_agent_setup()
        self.now = int(canon.utcnow().timestamp())

    def carriers(self, carriers):
        helpers.ensure_agent_setup(carriers)

    def call(self, rest, agent=EV, session=None, argv_proof=True, env_proof=True, env=None, **mint):
        """classify() of one agent call (in process, as python -I). mint: ts, nonce, key_hex, argv_agent,
        env_agent, argv_session, env_session, argv_rest (the tokens the argv proof was minted for)."""
        session = session or helpers.default_session(agent)
        e = {"OPENCLAW_SHELL": "exec", "JH_AGENT_ID": agent, "JH_SESSION_KEY": mint.get("env_session", session)}
        proof = None
        if argv_proof:
            proof = auth.argv_proof(mint.get("argv_agent", agent), list(mint.get("argv_rest", rest)),
                                    mint.get("argv_session", session), ts=mint.get("ts"), nonce=mint.get("nonce"),
                                    key_hex=mint.get("key_hex"))
        if env_proof:
            e["JH_AGENT_PROOF"] = auth.env_proof(mint.get("env_agent", agent), mint.get("env_session", session),
                                                 ts=mint.get("ts"), nonce=mint.get("env_nonce"),
                                                 key_hex=mint.get("key_hex"))
        e.update(env or {})
        argv = (["--agent-proof", proof] if proof else []) + list(rest)
        with helpers.as_isolated():
            return auth.classify(argv, e, None, proof_arg=proof, proof_rest=list(rest))

    def nonces(self):
        return sorted(r[0] for r in self.conn.execute("SELECT nonce FROM grants_used"))


class TestAgentProofs(ProofCase):
    def test_both_carriers(self):
        c = self.call(["eval", "stats"], nonce="00000000000000a1", env_nonce="00000000000000e1")
        self.assertEqual((c.cls, c.agent_id, c.detail["carriers"]), ("agent", self.EV, ["argv", "env"]))
        self.assertEqual(c.detail["session"], auth.session_hash(helpers.default_session(self.EV)))
        self.assertEqual(c.detail["markers"], ["OPENCLAW_SHELL"])
        self.assertEqual(self.nonces(), ["ap:00000000000000a1", "ep:00000000000000e1"])
        row = self.conn.execute("SELECT command FROM grants_used WHERE nonce = 'ap:00000000000000a1'").fetchone()
        self.assertEqual(row[0], "agent-proof " + self.EV)

    def test_every_configured_carrier_is_required(self):
        d = self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], env_proof=False)
        self.assertIn("missing env proof", d.message)
        d = self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], argv_proof=False)
        self.assertIn("missing argv proof", d.message)
        self.assertEqual(self.nonces(), [])
        self.carriers(["argv"])
        self.assertEqual(self.call(["eval", "stats"], env_proof=False).detail["carriers"], ["argv"])
        self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], argv_proof=False)
        # an env proof that is present must still verify, and is consumed with the argv one
        self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], env={"JH_AGENT_PROOF": "jhe2.bad"})
        self.assertEqual(self.call(["eval", "stats"]).detail["carriers"], ["argv", "env"])
        self.carriers(["env"])
        self.assertEqual(self.call(["eval", "stats"], argv_proof=False).detail["carriers"], ["env"])
        self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], env_proof=False)
        for bad in ([], ["argv", "pin"], "nothing", 3):
            with self.subTest(carriers=bad):
                h = paths.home()
                h["cli_route"] = {"carriers": bad}
                with open(paths.home_file(), "w") as fh:
                    json.dump(h, fh)
                self.assertDenied("E_CONFIG_INVALID", self.call, ["eval", "stats"])

    def test_the_env_carrier_alone_needs_the_session_key(self):
        """With env as the only carrier nothing else names the session: JH_SESSION_KEY must be set and match."""
        self.carriers(["env"])
        no_key = {"JH_SESSION_KEY": ""}
        d = self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], argv_proof=False, env=no_key)
        self.assertIn("JH_SESSION_KEY is missing", d.message)
        d = self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], argv_proof=False,
                              env={"JH_SESSION_KEY": "agent:jobhunter-evaluator:other"})
        self.assertIn("JH_SESSION_KEY does not match", d.message)
        self.assertEqual(self.nonces(), [])
        self.assertEqual(self.call(["eval", "stats"], argv_proof=False).detail["carriers"], ["env"])
        # with argv+env the argv proof names the session: both proofs must name the same one, the variable may be
        # absent, and a present one must still match
        self.carriers(["argv", "env"])
        self.assertEqual(self.call(["eval", "stats"], env=no_key).detail["carriers"], ["argv", "env"])
        self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"], env_session="agent:jobhunter-evaluator:x",
                          env=no_key)
        self.assertDenied("E_AUTH_FAILED", self.call, ["eval", "stats"],
                          env={"JH_SESSION_KEY": "agent:jobhunter-evaluator:other"})

    def test_the_guard_key_is_read_once_per_call(self):
        rest, session = ["eval", "stats"], helpers.default_session(self.EV)
        proof = auth.argv_proof(self.EV, rest, session)
        env = {"OPENCLAW_SHELL": "exec", "JH_AGENT_ID": self.EV, "JH_SESSION_KEY": session,
               "JH_AGENT_PROOF": auth.env_proof(self.EV, session)}
        with mock.patch.object(auth, "read_guard_key", wraps=auth.read_guard_key) as spy, helpers.as_isolated():
            c = auth.classify(["--agent-proof", proof] + rest, env, None, proof_arg=proof, proof_rest=rest)
        self.assertEqual((c.cls, c.detail["carriers"]), ("agent", ["argv", "env"]))
        self.assertEqual(spy.call_count, 1)
        with mock.patch.object(auth, "read_guard_key", wraps=auth.read_guard_key) as spy:
            self.assertEqual(auth.classify(["budget"], {}, None).cls, "system")
        self.assertEqual(spy.call_count, 0)                # no proof, no key read

    def test_bad_proofs(self):
        rest = ["eval", "record", "--job", "JAAAAAAA", "--file", "/w/x.json"]
        cases = {
            "wrong key": dict(key_hex="22" * 32),
            "121 s old": dict(ts=self.now - 121),
            "11 s in the future": dict(ts=self.now + 11),
            "changed token": dict(argv_rest=["eval", "record", "--job", "JAAAAAAB", "--file", "/w/x.json"]),
            "added token": dict(argv_rest=rest[:-1]),
            "reordered tokens": dict(argv_rest=rest[2:4] + rest[:2] + rest[4:]),
            "agents differ": dict(argv_agent=self.SC),
            "env agent differs": dict(env_agent=self.SC),
            "sessions differ": dict(argv_session="agent:jobhunter-evaluator:other"),
            "env session differs": dict(env_session="agent:jobhunter-evaluator:other"),
        }
        for name, mint in cases.items():
            with self.subTest(name):
                self.assertDenied("E_AUTH_FAILED", self.call, rest, **mint)
        for name, env in (("v1 env proof", {"JH_AGENT_PROOF": "%d.%s" % (self.now, "ab" * 32)}),
                          ("regex miss", {"JH_AGENT_PROOF": "jhe2.jobhunter-evaluator.1.2.3.4"}),
                          ("JH_AGENT_ID mismatch", {"JH_AGENT_ID": self.SC}),
                          ("JH_SESSION_KEY mismatch", {"JH_SESSION_KEY": "agent:jobhunter-evaluator:x"})):
            with self.subTest(name):
                d = self.assertDenied("E_AUTH_FAILED", self.call, rest, env=env)
                if name == "v1 env proof":
                    self.assertIn("old guard", d.message)
        self.assertEqual(self.nonces(), [])
        # the boundaries: 120 s old and 10 s in the future still verify
        self.assertEqual(self.call(rest, ts=self.now - 120).cls, "agent")
        self.assertEqual(self.call(rest, ts=self.now + 10).cls, "agent")
        bad = auth.argv_proof(self.EV, rest, "s")
        for token in (bad.replace("jhp2.", "jhp1."), bad[:-1], "jhp2.main.%d.0123456789abcdef.%s.%s" % (
                self.now, "0" * 16, "0" * 64)):
            with self.subTest(token=token):
                self.assertDenied("E_AUTH_FAILED", auth.verify_argv_proof, token, rest)

    def test_single_use_rolls_back_every_nonce(self):
        self.call(["budget"], nonce="00000000000000a2", env_nonce="00000000000000e2")
        # the argv nonce was used: the fresh env nonce is not recorded either
        self.assertDenied("E_AUTH_FAILED", self.call, ["budget"], nonce="00000000000000a2", env_nonce="00000000000000e3")
        # the env nonce was used: the fresh argv nonce is not recorded either
        d = self.assertDenied("E_AUTH_FAILED", self.call, ["budget"], nonce="00000000000000a3",
                              env_nonce="00000000000000e2")
        self.assertIn("already used", d.message)
        self.assertEqual(self.nonces(), ["ap:00000000000000a2", "ep:00000000000000e2"])

    def test_isolated_pin_and_grant(self):
        rest = ["budget"]
        session = helpers.default_session(self.EV)
        proof = auth.argv_proof(self.EV, rest, session)
        env = {"OPENCLAW_SHELL": "exec", "JH_AGENT_PROOF": auth.env_proof(self.EV, session)}
        d = self.assertDenied("E_AUTH_FAILED", auth.classify, ["--agent-proof", proof] + rest, env, None,
                              proof_arg=proof, proof_rest=rest)          # not under python -I
        self.assertIn("python -I", d.message)
        for extra in (["--pin-stdin"], ["--grant", "1.2.3"]):
            with self.subTest(extra=extra):
                r = rest + extra
                self.assertDenied("E_AUTH_FAILED", self.call, r, argv_rest=r)
        self.assertEqual(self.nonces(), [])

    def test_shared_vectors(self):
        """The same constants as the guard's test/grant.test.ts (tests/fixtures/core/proof_vectors.json)."""
        import re
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "core", "proof_vectors.json")) as fh:
            vec = json.load(fh)
        self.assertEqual((vec["argv_regex"], vec["env_regex"]), (auth.ARGV_PROOF_RE.pattern, auth.ENV_PROOF_RE.pattern))
        key = vec["hmac_sha256_key_hex"]
        for case in vec["cases"]:
            with self.subTest(case=case["token_hmac_sha256"][:30]):
                self.assertEqual(auth.session_hash(case["session_key"]), case["sk"])
                if case["kind"] == "argv":
                    self.assertEqual(auth.argv_digest(case["rest"]), case["digest_sha256"])
                    tok = auth.argv_proof(case["agent_id"], case["rest"], case["session_key"], ts=case["ts"],
                                          nonce=case["nonce"], key_hex=key)
                    self.assertRegex(tok, vec["argv_regex"])
                    self.assertTrue(re.fullmatch(r"[A-Za-z0-9_./:@+=,%-]+", tok))     # the guard's token class
                else:
                    tok = auth.env_proof(case["agent_id"], case["session_key"], ts=case["ts"], nonce=case["nonce"],
                                         key_hex=key)
                    self.assertRegex(tok, vec["env_regex"])
                self.assertEqual(tok, case["token_hmac_sha256"])
        # verification with the vectors' key and clock
        with open(auth.guard_key_file(), "w") as fh:
            fh.write(key + "\n")
        argv_case = vec["cases"][0]
        self.clock.set(canon.fmt_ts(__import__("datetime").datetime.fromtimestamp(argv_case["ts"] + 5,
                                                                                    tz=__import__("datetime").timezone.utc)))
        self.assertEqual(auth.verify_argv_proof(argv_case["token_hmac_sha256"], argv_case["rest"]),
                         (argv_case["agent_id"], argv_case["nonce"], argv_case["sk"]))
        env_case = [c for c in vec["cases"] if c["kind"] == "env"][0]
        self.assertEqual(auth.verify_env_proof(env_case["token_hmac_sha256"], env_case["session_key"]),
                         (env_case["agent_id"], env_case["nonce"], env_case["sk"]))


class TestAgentProofCli(ProofCase):
    def test_agent_call_through_the_cli(self):
        rc, env = helpers.agent_cli(self.EV, ["eval", "stats"])
        self.assertEqual(rc, 0, env)
        rc, env = helpers.agent_cli(self.EV, ["whoami"])
        self.assertEqual((rc, env["data"]["class"], env["data"]["agent_id"], env["data"]["carriers"]),
                         (0, "agent", self.EV, ["argv", "env"]), env)
        self.assertTrue(env["data"]["isolated"])
        probe = os.path.join(paths.state_dir(), "probe", self.EV + ".json")
        self.assertEqual(oct(os.stat(probe).st_mode & 0o777), "0o600")
        with open(probe) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["agent_id"], rec["carriers"], rec["markers"]), (self.EV, ["argv", "env"],
                                                                              ["OPENCLAW_SHELL"]))
        self.assertNotIn("JH_AGENT_PROOF", json.dumps(rec))
        # the same argv and env replayed: refused
        argv = helpers.agent_argv(paths.root(), self.EV, ["eval", "stats"])
        e = helpers.agent_env(paths.root(), self.EV)
        with helpers.as_isolated():
            rc, out = self.run_cli(argv, env=e)
            self.assertEqual(rc, 0, out)
            rc, out = self.run_cli(argv, env=e)
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"))
        # outside python -I: refused before any nonce is used
        rc, out = self.run_cli(helpers.agent_argv(paths.root(), self.EV, ["eval", "stats"]),
                               env=helpers.agent_env(paths.root(), self.EV))
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"))
        # the ACL still applies to a proven agent
        rc, out = helpers.agent_cli(self.EV, ["searches", "due"])
        self.assertEqual((rc, out["code"]), (11, "E_CALLER_NOT_ALLOWED"))

    def test_reserved_option_and_abbreviations(self):
        proof = auth.argv_proof(self.EV, ["budget"], "s")
        for argv in (["budget", "--agent-proof", proof], ["--agent-proof=" + proof, "budget"],
                     ["--agent-p", proof, "budget"], ["--quiet", "--agent-proof", proof, "budget"],
                     ["--agent-proof"], ["budget", "--agent-proofx"],
                     ["pace", "wait", "--plat", "linkedin", "--kind", "write"], ["--cyc", "C20260929T120000ZABCD", "budget"],
                     ["eval", "stats", "--day", "3"]):
            with self.subTest(argv=argv):
                with helpers.as_isolated():
                    rc, out = self.run_cli(argv, env=helpers.agent_env(paths.root(), self.EV))
                self.assertEqual((rc, out["code"]), (2, "E_USAGE"), out)
        self.assertEqual(self.nonces(), [])
        parser = cli.build_parser()
        self.assertFalse(parser.allow_abbrev)
        self.assertTrue(all(not p.allow_abbrev for p in cli.registered_commands(parser).values()))

    def test_real_python_i_child_and_nonce_race(self):
        """A real `python -I` child classifies as the agent; the same proofs in two processes at once: exactly
        one call succeeds (one BEGIN IMMEDIATE transaction per call)."""
        rc, out = helpers.run_jh(helpers.agent_argv(paths.root(), self.EV, ["whoami"]),
                                 helpers.agent_env(paths.root(), self.EV))
        self.assertEqual((rc, out["data"]["class"], out["data"]["isolated"]), (0, "agent", True), out)
        rc, out = helpers.run_jh(helpers.agent_argv(paths.root(), self.EV, ["whoami"]),
                                 helpers.agent_env(paths.root(), self.EV), isolated=False)
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"), out)
        self.assertIn("python -I", out["message"])
        argv = helpers.agent_argv(paths.root(), self.EV, ["eval", "stats"])
        env = helpers.agent_env(paths.root(), self.EV)
        procs = [helpers.start_jh(argv, env) for _ in range(2)]
        results = [helpers.finish_jh(p) for p in procs]
        codes = sorted(r[1]["code"] for r in results)
        self.assertEqual(codes, ["E_AUTH_FAILED", "OK"], results)
        self.assertEqual(len([n for n in self.nonces() if n.startswith(("ap:", "ep:"))]), 4)    # whoami + one call

    def test_system_class_stays_for_plain_calls(self):
        rc, out = helpers.run_jh(["whoami"], {})
        self.assertEqual((rc, out["data"]["class"], out["data"]["markers"]), (0, "system", []), out)


class TestPin(AuthCase):
    def test_set_and_check_pin_with_lockout(self):
        self.assertDenied("E_AUTH_FAILED", auth.check_pin, PIN, "unpause")        # no PIN yet
        self.assertDenied("E_VALIDATION", auth.set_pin, None, "12ab")
        auth.set_pin(None, PIN)
        with open(auth.pin_file()) as fh:
            rec = json.load(fh)
        self.assertNotIn(PIN, json.dumps(rec))
        self.assertIn(rec["algo"], ("scrypt", "pbkdf2_sha256"))
        self.assertEqual(oct(os.stat(auth.pin_file()).st_mode & 0o777), "0o600")
        auth.check_pin(PIN, "unpause")
        self.assertDenied("E_AUTH_FAILED", auth.set_pin, "000000", "123456")
        for i in range(5):
            self.assertDenied("E_AUTH_FAILED", auth.check_pin, "000000", "unpause")
        d = self.assertDenied("E_AUTH_LOCKED", auth.check_pin, PIN, "unpause")
        self.assertGreater(d.retry_after, 0)
        self.assertTrue(self.conn.execute("SELECT 1 FROM notifications WHERE dedupe_key LIKE 'auth_locked:%'").fetchone())
        self.clock.advance(minutes=61)
        auth.check_pin(PIN, "unpause")

    def test_parallel_wrong_pins_hit_the_lockout(self):
        """The lockout check and the attempt row are one transaction before the hash: a burst of guesses
        started together is still limited to five failures, the rest are E_AUTH_LOCKED."""
        import threading
        from jobhunter.errors import Denied
        auth.set_pin(None, PIN)
        codes: list = []
        lock = threading.Lock()
        start = threading.Barrier(12)

        def guess(i):
            start.wait()
            try:
                auth.check_pin("%06d" % (100000 + i), "unpause")
                code = "OK"
            except Denied as d:
                code = d.code
            with lock:
                codes.append(code)
        threads = [threading.Thread(target=guess, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(codes), 12)
        self.assertEqual(codes.count("E_AUTH_FAILED"), 5, codes)
        self.assertEqual(codes.count("E_AUTH_LOCKED"), 7, codes)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM auth_attempts WHERE ok = 0").fetchone()[0], 5)
        self.assertDenied("E_AUTH_LOCKED", auth.check_pin, PIN, "unpause")

    def test_human_command_through_cli(self):
        auth.set_pin(None, PIN)
        rc, env = self.run_cli(["unpause"])
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"))
        rc, env = self.run_cli(["--pin-stdin", "unpause"], stdin="999999\n")
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))
        rc, env = self.run_cli(["--pin-stdin", "unpause"], stdin=PIN + "\n")
        self.assertEqual((rc, env["code"]), (0, "OK"))
        rc, env = self.run_cli(["--pin-stdin", "auth", "set-pin"], stdin=PIN + "\n111222\n111222\n")
        self.assertEqual(rc, 0, env)
        auth.check_pin("111222", "x")

    def test_first_pin_needs_a_terminal(self):
        rc, env = self.run_cli(["auth", "set-pin"], stdin="123456\n123456\n")
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"))


class TestGrant(AuthCase):
    def test_grant_hmac_expiry_and_replay(self):
        now_epoch = int(canon.utcnow().timestamp())
        g = auth.sign_grant("pause", ["--scope", "linkedin"], ts=now_epoch)
        rc, env = self.run_cli(["pause", "--scope", "linkedin", "--grant", g])
        self.assertEqual((rc, env["code"]), (0, "OK"))
        rc, env = self.run_cli(["pause", "--scope", "linkedin", "--grant", g])
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))            # replay
        g2 = auth.sign_grant("pause", ["--scope", "gmail"], ts=now_epoch)
        rc, env = self.run_cli(["pause", "--scope", "linkedin", "--grant", g2])
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))            # args differ
        old = auth.sign_grant("pause", [], ts=now_epoch - 121)
        rc, env = self.run_cli(["pause", "--grant", old])
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))            # expired
        g3 = auth.sign_grant("unpause", [], ts=now_epoch)
        rc, env = self.run_cli(["unpause", "--grant", g3])
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))     # not a chat command

    def test_compact_json_form_is_accepted(self):
        import hashlib
        import hmac
        key = bytes.fromhex(auth.read_guard_key())
        ts = int(canon.utcnow().timestamp())
        nonce = "0123456789abcdef"
        msg = "config lower\n%s\n%d\n%s" % (json.dumps(["gmail.max_links", "1"], separators=(",", ":")), ts, nonce)
        g = "%d.%s.%s" % (ts, nonce, hmac.new(key, msg.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(auth.verify_grant(g, "config lower", ["gmail.max_links", "1"]), nonce)
        self.assertEqual(auth.grant_args(["--quiet", "config", "lower", "a", "b", "--grant", g], "config lower"),
                         ["a", "b"])


class TestAclArgumentClasses(AuthCase):

    def test_argument_classes(self):
        req = auth.require
        ag = auth.Caller("agent", "jobhunter-outreach")
        req(ag, "reconcile resolve", {"_1": "TAAAAAAAAAAA", "--result": "found", "--method": "li_conversation",
                                      "--evidence-file": os.path.join(paths.ws_dir("outreach"), "work", "e.txt")})
        cases = [
            ("reconcile resolve", {"_1": "TAAAA", "--result": "found", "--method": "li_conversation",
                                   "--evidence-file": os.path.join(paths.ws_dir("outreach"), "work", "e.txt")},
             "E_CALLER_NOT_ALLOWED"),
            ("reconcile resolve", {"_1": "TAAAAAAAAAAA", "--result": "found", "--method": "imap_message_id",
                                   "--evidence-file": os.path.join(paths.ws_dir("outreach"), "work", "e.txt")},
             "E_CALLER_NOT_ALLOWED"),
            ("reconcile resolve", {"_1": "TAAAAAAAAAAA", "--result": "found", "--method": "li_conversation",
                                   "--evidence-file": "/etc/passwd"}, "E_PATH_NOT_ALLOWED"),
            ("gate reserve", {"--kind": "application", "--draft": "DAAAAAAA", "--precheck": 1, "--platform": "x"},
             "E_CALLER_NOT_ALLOWED"),
            ("approve", {"_1": "A7K2"}, "E_CALLER_NOT_ALLOWED"),
            ("usage add", {"--platform": "linkedin", "--metric": "page_view", "--extra": "1"}, "E_CALLER_NOT_ALLOWED"),
        ]
        for command, args, code in cases:
            with self.subTest(command=command, args=args):
                self.assertDenied(code, req, ag, command, args)

    def test_system_human_only_with_flag(self):
        sysc = auth.Caller("system")
        auth.require(sysc, "exclusions import", {})
        self.assertDenied("E_HUMAN_ONLY", auth.require, sysc, "exclusions import", {"--deactivate": True})
        auth.require(auth.Caller("human"), "exclusions import", {"--deactivate": True})
        self.assertDenied("E_CALLER_NOT_ALLOWED", auth.require, auth.Caller("chat"), "unpause", {})
        auth.require(auth.Caller("chat"), "pause", {"--scope": "all"})

    def test_agent_cli_argument_rejected(self):
        rc, env = helpers.agent_cli("jobhunter-scout", ["pace", "wait", "--platform", "linkedin", "--kind", "write"])
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))
        rc, env = helpers.agent_cli("jobhunter-scout", ["preflight", "--lane", "applier"])
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))


if __name__ == "__main__":
    import unittest
    unittest.main()
