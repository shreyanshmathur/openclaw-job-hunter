"""Caller classes, owner PIN, chat grants and ACL argument classes (design 3.1, 3.3, 12.19, 13.2)."""
from __future__ import annotations

import io
import json
import os

import tests  # noqa: F401
from jobhunter import auth, canon, cli, paths
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase

PIN = "482915"


class AuthCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        auth.create_guard_key()

    def run_cli(self, argv, env=None, stdin=""):
        out = io.StringIO()
        rc = cli.main(argv, env=env or {}, stdin=io.StringIO(stdin), stdout=out)
        return rc, json.loads(out.getvalue())


class TestClassify(AuthCase):
    def test_classes(self):
        c = auth.classify(["budget"], {}, None)
        self.assertEqual((c.cls, c.agent_id), ("system", None))
        c = auth.classify(["budget"], {"OPENCLAW_SHELL": "exec", "JH_AGENT_ID": "jobhunter-scout"}, None)
        self.assertEqual((c.cls, c.agent_id), ("agent", "jobhunter-scout"))
        c = auth.classify(["budget"], {"OPENCLAW_SHELL": "1"}, None)
        self.assertEqual((c.cls, c.agent_id), ("agent", None))
        for argv in (["--pin-stdin", "unpause"], ["--grant", "1.2.3", "pause"]):
            with self.subTest(argv=argv):
                self.assertDenied("E_AUTH_FAILED", auth.classify, argv, {"OPENCLAW_SHELL": "1",
                                                                         "JH_AGENT_ID": "jobhunter-outreach"}, None)

    def test_agent_proof(self):
        """JH_AGENT_PROOF (13.1 #4): the guard's HMAC over the agent id; a bad proof, another agent's proof or an
        old one is refused, and after the first good proof a jobhunter-* id without one is refused too."""
        import hashlib
        import hmac as _hmac
        ap = "jobhunter-applier"
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": ap}
        # no proof on an install that never saw one: accepted (older guards, tests of other units)
        self.assertEqual(auth.classify(["budget"], env, None).agent_id, ap)
        proof = auth.agent_proof(ap)
        # the same message and key form as the guard's agentProof (src/grant.ts)
        ts, mac = proof.split(".")
        key = bytes.fromhex(auth.read_guard_key())
        self.assertEqual(mac, _hmac.new(key, ("agent\n%s\n%s" % (ap, ts)).encode(), hashlib.sha256).hexdigest())
        c = auth.classify(["budget"], dict(env, JH_AGENT_PROOF=proof), None)
        self.assertEqual((c.cls, c.agent_id), ("agent", ap))
        for bad in (auth.agent_proof("jobhunter-scout"), "123.abc", proof[:-1] + ("0" if proof[-1] != "0" else "1"),
                    auth.agent_proof(ap, ts=int(ts) - auth.AGENT_PROOF_MAX_AGE_S - 1),
                    auth.agent_proof(ap, key_hex="11" * 32)):
            with self.subTest(bad=bad):
                self.assertDenied("E_AUTH_FAILED", auth.classify, ["budget"], dict(env, JH_AGENT_PROOF=bad), None)
        # the install has now seen a good proof: an agent id without one is a spoof
        self.assertDenied("E_AUTH_FAILED", auth.classify, ["budget"], env, None)
        rc, out = self.run_cli(["budget"], env=env)
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"))
        rc, out = self.run_cli(["budget"], env=dict(env, JH_AGENT_PROOF=auth.agent_proof(ap)))
        self.assertEqual(rc, 0, out)
        # non-jobhunter agents get no proof from the guard and stay limited to read-only commands
        self.assertEqual(auth.classify(["budget"], {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"}, None).agent_id,
                         "main")

    def test_openclaw_shell_without_agent_id(self):
        rc, env = self.run_cli(["breaker", "status"], env={"OPENCLAW_SHELL": "1"})
        self.assertEqual(rc, 0)
        rc, env = self.run_cli(["pause"], env={"OPENCLAW_SHELL": "1"})
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))
        rc, env = self.run_cli(["approval", "set", "auto"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))
        rc, env = self.run_cli(["budget"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual(rc, 0)


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
    AG = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-outreach"}

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
        rc, env = self.run_cli(["pace", "wait", "--platform", "linkedin", "--kind", "write"],
                               env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-scout"})
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))
        rc, env = self.run_cli(["preflight", "--lane", "applier"], env={"OPENCLAW_SHELL": "1",
                                                                       "JH_AGENT_ID": "jobhunter-scout"})
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))


if __name__ == "__main__":
    import unittest
    unittest.main()
