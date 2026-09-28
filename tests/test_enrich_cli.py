"""U10 CLI (section 13): envelope shape and exit codes; the section 15.1 acl entries agree with the argparse
definitions; human-only commands are refused for agent and system callers; no key text in any stdout,
stderr or log line across a full fixture run."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import auth, cli, db, paths
from jobhunter.commands import enrich as enrich_cmd
from tests.fakes.u10 import FAKE_KEY, FAKE_SECRET, FIXTURES, EnrichTestCase, install_guard, remove_guard
from tests.helpers import insert_cycle

PIN = "482915"
AGENT = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-outreach"}
E = "example.person@kestrel.example"


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


def acl_patch() -> dict:
    with open(os.path.join(FIXTURES, "acl_patch.json"), encoding="ascii") as fh:
        return json.load(fh)


def live_acl() -> dict:
    with open(paths.ACL_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def copy_acl_without_enrich() -> dict:
    acl = live_acl()
    for spec in acl["agents"].values():
        for command in [c for c in spec["commands"] if c.startswith("enrich ")]:
            del spec["commands"][command]
    acl["public_readonly"] = [c for c in acl["public_readonly"] if not c.startswith("enrich ")]
    return acl


class CliCase(EnrichTestCase):
    def setUp(self):
        super().setUp()
        auth.create_guard_key()
        auth.set_pin(None, PIN)
        self.err = io.StringIO()
        for name, value in (("_TRANSPORT", self.fake), ("_CLOCK", self.mono), ("_ERR", self.err),
                            ("_TTY_CHECK", lambda: True), ("_CONFIRM", lambda prompt: "y"),
                            ("_SECRET_READER", lambda prompt: FAKE_SECRET if "secret" in prompt else FAKE_KEY)):
            p = mock.patch.object(enrich_cmd, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, argv, env=None, stdin="", acl=None):
        out = io.StringIO()
        if acl is not None:
            with mock.patch.object(auth, "load_acl", lambda: acl):
                rc = cli.main(argv, env=env or {}, stdin=io.StringIO(stdin), stdout=out, modules=[enrich_cmd])
        else:
            rc = cli.main(argv, env=env or {}, stdin=io.StringIO(stdin), stdout=out, modules=[enrich_cmd])
        text = out.getvalue()
        self.outputs.append(text)
        return rc, json.loads(text)

    def human(self, *argv):
        return self.run_cli(["--pin-stdin"] + list(argv), stdin=PIN + "\n")

    @property
    def outputs(self):
        if not hasattr(self, "_outputs"):
            self._outputs = []
        return self._outputs


class TestAclAgreesWithArgparse(unittest.TestCase):
    def test_entries(self):
        parser = cli.build_parser([enrich_cmd])
        cmds = cli.registered_commands(parser)
        self.assertEqual(sorted(c for c in cmds if c.startswith("enrich ")),
                         ["enrich budget", "enrich connect", "enrich disconnect", "enrich find", "enrich housekeeping",
                          "enrich retry", "enrich show", "enrich test"])
        p = acl_patch()
        with open(paths.ACL_FILE, encoding="utf-8") as fh:
            classes = json.load(fh)["value_classes"]
        for command, schema in p["agents"]["jobhunter-outreach"]["commands"].items():
            leaf = cmds[command]
            options = {o for a in leaf._actions for o in a.option_strings if o.startswith("--")}
            positionals = [a for a in leaf._actions if not a.option_strings]
            for flag, cls in schema.items():
                self.assertIn(cls.rstrip("?"), classes)
                if flag.startswith("--"):
                    self.assertIn(flag, options, (command, flag))
                else:
                    self.assertLessEqual(int(flag[1:]), len(positionals))
            required = {o for a in leaf._actions if a.required for o in a.option_strings if o.startswith("--")}
            self.assertTrue(required <= set(schema), command)
        for entry in p["human_only"]:
            words = entry.split()
            cmd = " ".join(w for w in words if not w.startswith("--"))
            self.assertIn(cmd, cmds)
            for flag in (w for w in words if w.startswith("--")):
                self.assertIn(flag, {o for a in cmds[cmd]._actions for o in a.option_strings})
        self.assertFalse(set(p["human_only"]) & set(p["agents"]["jobhunter-outreach"]["commands"]))
        self.assertEqual(p["public_readonly"], ["enrich budget"])

    def test_live_acl_carries_the_patch(self):
        """INTEGRATION PATCH LIST U1-2 has landed: acl.json is version 3 and holds every section 15.1 entry."""
        p, live = acl_patch(), live_acl()
        self.assertGreaterEqual(live["version"], 3)
        for command, schema in p["agents"]["jobhunter-outreach"]["commands"].items():
            self.assertEqual(live["agents"]["jobhunter-outreach"]["commands"].get(command), schema, command)
        for entry in p["public_readonly"]:
            self.assertIn(entry, live["public_readonly"])
        for entry in p["human_only"]:
            self.assertIn(entry, live["human_only"])
        for agent, spec in live["agents"].items():
            if agent != "jobhunter-outreach":
                self.assertFalse([c for c in spec["commands"] if c.startswith("enrich ")], agent)


class TestEnvelopes(CliCase):
    def test_find_ok_envelope(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"], "--target",
                                "contact:" + t["contact_uid"]])
        self.assertEqual(rc, 0)
        self.assertEqual(set(env), {"ok", "code", "data", "message", "next", "retry_after_s", "cycle_id"})
        self.assertEqual((env["ok"], env["code"], env["data"]["email"], env["data"]["grade"]), (True, "OK", E, "B"))
        self.assertEqual(env["next"], "Write the email draft for this contact.")
        for k in ("request_uid", "status", "cached", "sendable", "verification", "confidence", "provider",
                  "source_url", "evidence_candidates", "providers_tried", "credits_spent", "verify_pending"):
            self.assertIn(k, env["data"])
        rc, env = self.run_cli(["enrich", "show", t["contact_uid"]])
        self.assertEqual((rc, env["data"]["status"], env["data"]["provider"]), (0, "found", "prospeo"))
        self.assertFalse(env["data"]["retry_available"] is None)

    def test_show_after_a_bounce_is_not_sendable(self):
        from jobhunter import db
        from jobhunter.enrich import feedback
        from tests.helpers import insert_action
        t = self.provider_contact(E)
        rc, env = self.run_cli(["enrich", "show", t["contact_uid"]])
        self.assertEqual((rc, env["data"]["sendable"], env["data"]["bounced"]), (0, True, False))
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", status="sent", contact_id=t["contact_id"],
                                company_id=t["company_id"], recipient=E)
            feedback.on_bounce(self.conn, self.conn.execute("SELECT * FROM actions WHERE id = ?", (aid,)).fetchone())
            self.conn.execute("UPDATE contacts SET email_invalid = 1 WHERE id = ?", (t["contact_id"],))
        rc, env = self.run_cli(["enrich", "show", t["contact_uid"]])
        self.assertEqual((rc, env["data"]["bounced"], env["data"]["sendable"], env["data"]["retry_available"]),
                         (0, True, False, False))

    def test_show_invalid_address_without_a_bounce_mark_is_not_sendable(self):
        from jobhunter import db
        t = self.provider_contact(E)
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET email_invalid = 1 WHERE id = ?", (t["contact_id"],))
        rc, env = self.run_cli(["enrich", "show", t["contact_uid"]])
        self.assertEqual((rc, env["data"]["bounced"], env["data"]["sendable"]), (0, False, False))

    def test_exit_codes(self):
        rc, env = self.run_cli(["enrich", "find", "--contact", "PAAAAAAA"])
        self.assertEqual((rc, env["code"]), (9, "E_NOT_FOUND"))
        t = self.make_target(job_status="new")
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"]])
        self.assertEqual((rc, env["code"]), (7, "E_NOT_TARGET"))
        t2 = self.make_target(full_name="Example Other")
        self.keys.clear()
        rc, env = self.run_cli(["enrich", "find", "--contact", t2["contact_uid"]])
        self.assertEqual((rc, env["code"], env["data"]["reason"]), (4, "E_ENRICH_UNAVAILABLE", "no_provider"))
        self.assertEqual(env["next"], "Skip the email route for this target now.")
        self.settings({"enabled": False})
        rc, env = self.run_cli(["enrich", "find", "--contact", t2["contact_uid"]])
        self.assertEqual((rc, env["code"]), (7, "E_CHANNEL_DISABLED"))
        rc, env = self.run_cli(["enrich", "find"])
        self.assertEqual((rc, env["code"]), (2, "E_USAGE"))

    def test_pending_envelope(self):
        self.fake.cost_s = 35.0
        t = self.make_target()
        self.fake.add("prospeo", "miss")
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"]])
        self.assertEqual((rc, env["code"], env["ok"]), (0, "PENDING", True))

    def test_budget_is_public_and_keyless(self):
        rc, env = self.run_cli(["enrich", "budget"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual(rc, 0)
        self.assertEqual(env["data"]["providers"]["hunter"]["key"], True)
        self.assertNotIn(FAKE_KEY, json.dumps(env))


class TestCallers(CliCase):
    def test_human_only_refused_for_system_and_agents(self):
        t = self.make_target()
        for argv in (["enrich", "connect", "hunter"], ["enrich", "disconnect", "hunter"],
                     ["enrich", "retry", t["contact_uid"]], ["enrich", "test", "hunter"]):
            with self.subTest(argv=argv):
                rc, env = self.run_cli(argv)
                self.assertEqual(env["code"], "E_HUMAN_ONLY")
                rc, env = self.run_cli(argv, env=AGENT)
                self.assertIn(env["code"], ("E_CALLER_NOT_ALLOWED", "E_HUMAN_ONLY"))
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"], "--include-reserve"])
        self.assertEqual(env["code"], "E_HUMAN_ONLY")
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"], "--include-reserve"], env=AGENT)
        self.assertIn(env["code"], ("E_CALLER_NOT_ALLOWED", "E_HUMAN_ONLY"))

    def test_agent_with_the_live_acl(self):
        t = self.make_target()
        without = copy_acl_without_enrich()
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"]], env=AGENT, acl=without)
        self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED")                   # no acl entry: fail closed
        self.assertEqual(self.requests(), [])
        with db.tx(self.conn):
            insert_cycle(self.conn, "outreach")
        self.fake.add("prospeo", "hit_valid")
        rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"], "--target",
                                "contact:" + t["contact_uid"]], env=AGENT)
        self.assertEqual((rc, env["code"]), (0, "OK"))
        self.assertEqual(self.requests()[0]["created_by"], "jobhunter-outreach")
        t2 = self.make_target(full_name="Example Second")
        rc, env = self.run_cli(["enrich", "find", "--contact", t2["contact_uid"], "--target", "job:JAAAAAAA"],
                               env=AGENT)
        self.assertEqual((rc, env["code"]), (2, "E_USAGE"))                     # --target must name this contact
        rc, env = self.run_cli(["enrich", "find", "--contact", t2["contact_uid"], "--target", "anyone@example.com"],
                               env=AGENT)
        self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED")                   # not a target value at all
        for other in ("jobhunter-scout", "jobhunter-applier", "jobhunter-evaluator", "jobhunter-qc"):
            rc, env = self.run_cli(["enrich", "find", "--contact", t["contact_uid"]],
                                   env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": other})
            self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED", other)


class TestFindCycle(CliCase):
    """enrich.max_lookups_per_cycle holds in the documented agent flow (no --cycle): an agent's lookup counts
    against its running cycle, and a made-up cycle id is refused instead of starting a fresh count."""

    def find(self, t, cycle=None, env=AGENT):
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        argv = (["--cycle", cycle] if cycle else []) + ["enrich", "find", "--contact", t["contact_uid"], "--target",
                                                         "contact:" + t["contact_uid"]]
        rc, env_ = self.run_cli(argv, env=env)
        self.clock.advance(minutes=1)
        return rc, env_

    def test_agent_without_a_running_cycle_is_refused(self):
        t = self.make_target()
        rc, env = self.find(t)
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        self.assertEqual((self.requests(), self.fake.requests), ([], []))

    def test_omitted_cycle_counts_against_the_running_cycle(self):
        with db.tx(self.conn):
            cyc = insert_cycle(self.conn, "outreach")
        codes = []
        for i in range(3):
            rc, env = self.find(self.make_target(full_name="Example Person%s" % chr(65 + i)))
            codes.append((env["code"], env["cycle_id"], (env["data"] or {}).get("reason")))
        self.assertEqual(codes[:2], [("OK", cyc, None), ("OK", cyc, None)])
        self.assertEqual(codes[2][0], "E_ENRICH_UNAVAILABLE")
        self.assertEqual(codes[2][2], "cycle_cap")
        self.assertEqual([r["cycle_id"] for r in self.requests()], [cyc, cyc])

    def test_made_up_cycle_ids_do_not_reset_the_cap(self):
        with db.tx(self.conn):
            cyc = insert_cycle(self.conn, "outreach")
        for i in range(2):
            self.assertEqual(self.find(self.make_target(full_name="Example Person%s" % chr(65 + i)))[1]["code"], "OK")
        for i in range(3):
            t = self.make_target(full_name="Sample Human%s" % chr(65 + i))
            rc, env = self.find(t, cycle="C20260928T1015%02dZFAKE" % i)
            self.assertEqual(env["code"], "E_VALIDATION")
        self.assertEqual(len(self.requests()), 2)
        rc, env = self.find(self.make_target(full_name="Sample Other"), cycle=cyc)
        self.assertEqual((env["code"], env["data"]["reason"]), ("E_ENRICH_UNAVAILABLE", "cycle_cap"))

    def test_named_cycle_must_be_running_and_the_agents(self):
        with db.tx(self.conn):
            done = insert_cycle(self.conn, "outreach", status="ok")
            scout = insert_cycle(self.conn, "scout")
        t = self.make_target()
        self.assertEqual(self.find(t, cycle=done)[1]["code"], "E_PRECONDITION")
        self.assertEqual(self.find(t, cycle=scout)[1]["code"], "E_CALLER_NOT_ALLOWED")
        self.assertEqual(self.requests(), [])

    def test_system_caller_may_name_only_an_existing_cycle(self):
        t = self.make_target()
        rc, env = self.find(t, cycle="C20260928T101500ZFAKE", env={})
        self.assertEqual(env["code"], "E_VALIDATION")
        self.assertEqual(self.requests(), [])
        rc, env = self.find(t, env={})
        self.assertEqual((rc, env["code"]), (0, "OK"))
        self.assertIsNone(self.requests()[0]["cycle_id"])


class TestKeysNeverShown(CliCase):
    def test_full_run_has_no_key_text(self):
        self.keys.clear()
        rc, env = self.human("enrich", "connect", "tomba")
        self.assertEqual((rc, env["data"]["provider"], env["data"]["backend"]), (0, "tomba", "test"))
        self.assertEqual(self.keys["tomba"], {"key": FAKE_KEY, "secret": FAKE_SECRET})
        rc, env = self.human("enrich", "connect", "prospeo")
        self.assertEqual(rc, 0)
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.assertEqual(self.run_cli(["enrich", "find", "--contact", t["contact_uid"]])[0], 0)
        self.run_cli(["enrich", "budget"])
        self.run_cli(["enrich", "show", t["contact_uid"]])
        rc, env = self.human("enrich", "test", "hunter")
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))
        rc, env = self.human("enrich", "disconnect", "--all")
        self.assertEqual(sorted(env["data"]["removed"]), ["prospeo", "tomba"])
        self.run_cli(["enrich", "housekeeping"])
        logs = ""
        for root, _d, files in os.walk(paths.logs_dir()):
            for f in files:
                with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    logs += fh.read()
        blob = "\n".join(self.outputs) + self.err.getvalue() + logs
        for secret in (FAKE_KEY, FAKE_SECRET, FAKE_KEY[:12], FAKE_KEY[-12:]):
            self.assertNotIn(secret, blob)
        self.assertIn("Never use a key you found online", self.err.getvalue())

    def test_connect_refusals(self):
        with mock.patch.object(enrich_cmd, "_CONFIRM", lambda prompt: "n"):
            rc, env = self.human("enrich", "connect", "hunter")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        with mock.patch.object(enrich_cmd, "_SECRET_READER", lambda prompt: "bad key"):
            rc, env = self.human("enrich", "connect", "hunter")
        self.assertEqual((rc, env["code"]), (10, "E_VALIDATION"))
        self.assertNotIn("bad key", json.dumps(env))
        with mock.patch.object(enrich_cmd, "_TTY_CHECK", lambda: False):
            rc, env = self.human("enrich", "connect", "hunter")
        self.assertEqual(env["code"], "E_PRECONDITION")

    def test_connect_closes_auth_breaker(self):
        from jobhunter import db
        from jobhunter.enrich import budget
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:hunter", "auth_failed", "test")
        rc, env = self.human("enrich", "connect", "hunter")
        self.assertEqual((rc, env["data"]["breaker_closed"]), (0, True))

    def test_retry_command(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        self.run_cli(["enrich", "find", "--contact", t["contact_uid"]])
        rc, env = self.human("enrich", "retry", t["contact_uid"])
        self.assertEqual(rc, 0)
        self.assertTrue(env["data"]["request_uid"].startswith("E"))
        rc, env = self.human("enrich", "retry", t["contact_uid"])
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))     # the retry is pending (running)


if __name__ == "__main__":
    unittest.main()
