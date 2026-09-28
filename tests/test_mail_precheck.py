"""Precheck by code over IMAP (U9, design 2.3.1 step 1, 12.7) with U1's real precheck plan and record."""
from __future__ import annotations

import json
import os
import re
import unittest

import tests  # noqa: F401
from jobhunter import canon, gate
from jobhunter.errors import Denied
from jobhunter.mail import MailError, precheck
from tests.fakes.u9 import FIXTURES, OWNER, MailTestCase, inbound, write_config
from tests.helpers import insert_action, insert_company, insert_contact, insert_draft, insert_thread

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, "skills-src", "jobhunter-gmail-web", "SKILL.template.md")


def web_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, "web", name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def skill_json_blocks() -> list:
    with open(SKILL, "r", encoding="utf-8") as fh:
        text = fh.read()
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]


class NoImap:
    """An IMAP client that must never be used (the web_ui route reads nothing over IMAP)."""
    account = OWNER

    def __getattr__(self, name):
        raise AssertionError("IMAP used on the web_ui route: %s" % name)

ALEX = "alex.rivera@kestrel.example"


class PrecheckTests(MailTestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email', ?)",
                          ("email:alex@kestrel-mail.example", self.pc, canon.now()))
        self.draft_id = insert_draft(self.conn, kind="cold_email", company_id=self.co, contact_id=self.pc)
        self.client = self.imap.client()
        self.client.open()
        self.addCleanup(self.client.close)

    def draft(self, did=None):
        return self.conn.execute("SELECT * FROM drafts WHERE id = ?", (did or self.draft_id,)).fetchone()

    def plan(self, kind="cold_email", **kw):
        return gate.precheck_plan(self.conn, kind, route="mailer", **kw)

    def row(self, pid):
        return self.conn.execute("SELECT * FROM prechecks WHERE id = ?", (pid,)).fetchone()

    def test_clear_runs_every_plan_query(self):
        plan = self.plan(contact_id=self.pc)
        pid = precheck.run_precheck(self.conn, self.client, self.draft(), owner=OWNER)
        r = self.row(pid)
        self.assertEqual(r["result"], "clear")
        self.assertEqual(r["source"], "code_imap")
        self.assertEqual(r["platform"], "gmail")
        self.assertEqual(r["contact_id"], self.pc)
        checks = json.loads(r["checks_json"])
        self.assertEqual(sorted(c["name"] for c in checks), sorted(c["name"] for c in plan["checks"]))
        self.assertTrue(all(c["value"] == 0 for c in checks))
        want = [c["query"] for c in plan["checks"] if c.get("query")]
        self.assertEqual(self.imap.queries, want)
        self.assertIn("in:sent to:%s" % ALEX, self.imap.queries)
        self.assertTrue(any("alex@kestrel-mail.example" in q for q in self.imap.queries))

    def _hit(self, name):
        plan = self.plan(contact_id=self.pc)
        q = [c["query"] for c in plan["checks"] if c["name"] == name][0]
        self.imap.add_message(inbound(OWNER, ALEX, "Earlier note", "sent by hand"), uid=3)
        self.imap.on(q, [3])
        pid = precheck.run_precheck(self.conn, self.client, self.draft(), owner=OWNER)
        return self.row(pid)

    def test_sent_before_to_the_address(self):
        r = self._hit("sent_to_address")
        self.assertEqual(r["result"], "already_done")
        imp = self.conn.execute("SELECT * FROM actions WHERE status = 'imported'").fetchall()
        self.assertEqual(len(imp), 1)
        self.assertEqual(imp[0]["contact_id"], self.pc)

    def test_sent_before_to_another_address(self):
        self.assertEqual(self._hit("sent_to_other_addresses")["result"], "already_done")

    def test_sent_before_to_the_company(self):
        self.assertEqual(self._hit("sent_company_query")["result"], "already_done")

    def test_imap_error_records_nothing(self):
        plan = self.plan(contact_id=self.pc)
        self.imap.search_fail[plan["checks"][0]["query"]] = "[UNAVAILABLE] try later"
        with self.assertRaises(MailError):
            precheck.run_precheck(self.conn, self.client, self.draft(), owner=OWNER)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM prechecks").fetchone()[0], 0)

    def test_dedup_refusal_propagates(self):
        insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                      recipient=ALEX)
        with self.assertRaises(Denied) as cm:
            precheck.run_precheck(self.conn, self.client, self.draft(), owner=OWNER)
        self.assertIn(cm.exception.code, ("E_DUP_PERSON", "E_COMPANY_COOLDOWN"))
        self.assertEqual(self.imap.queries, [], "no IMAP search when the ledger already refuses")


class FollowupPrecheckTests(MailTestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        first = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                              recipient=ALEX, reserved_at="2026-09-20T09:00:00Z")
        tok = self.conn.execute("SELECT token FROM actions WHERE id = ?", (first,)).fetchone()[0]
        self.mid = "<%s@jobhunter.invalid>" % tok
        self.conn.execute("UPDATE actions SET message_id = ? WHERE id = ?", (self.mid, first))
        self.tid = insert_thread(self.conn, first, contact_id=self.pc, company_id=self.co)
        self.conn.execute("UPDATE threads SET first_message_id = ?, subject = 'Hello' WHERE id = ?",
                          (self.mid, self.tid))
        self.key = "em:" + tok
        self.draft_id = insert_draft(self.conn, kind="followup_email", company_id=self.co, contact_id=self.pc,
                                     thread_key=self.key, subject="Re: Hello")
        self.client = self.imap.client()
        self.client.open()
        self.addCleanup(self.client.close)
        self.ours = self.imap.add_message(inbound(OWNER, ALEX, "Hello", "first note", date="2026-09-20T09:00:00Z",
                                                  msg_id=self.mid), uid=1, thrid=900)
        self.imap.on("rfc822msgid:%s" % self.mid.strip("<>"), [1])

    def run_it(self):
        d = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (self.draft_id,)).fetchone()
        pid = precheck.run_precheck(self.conn, self.client, d, owner=OWNER)
        return self.conn.execute("SELECT * FROM prechecks WHERE id = ?", (pid,)).fetchone()

    def test_no_reply_is_clear_and_auto_replies_do_not_count(self):
        self.imap.add_message(inbound(ALEX, OWNER, "Automatic reply: Hello",
                                      "I am out of the office until October 5 with limited access to email.",
                                      in_reply_to=self.mid, headers={"Auto-Submitted": "auto-replied"}),
                              uid=2, thrid=900)
        self.imap.on("X-GM-THRID 900", [1, 2])
        r = self.run_it()
        self.assertEqual(r["result"], "clear")
        vals = {c["name"]: c["value"] for c in json.loads(r["checks_json"])}
        self.assertEqual(vals, {"thread_has_reply": False, "company_inbound_since_first": 0})
        self.assertEqual(r["thread_key"], self.key)

    def test_real_reply_in_thread_is_already_done(self):
        self.imap.add_message(inbound(ALEX, OWNER, "Re: Hello", "Thanks Sam, let us talk on Friday.",
                                      in_reply_to=self.mid), uid=2, thrid=900)
        self.imap.on("X-GM-THRID 900", [1, 2])
        r = self.run_it()
        self.assertEqual(r["result"], "already_done")
        task = self.conn.execute("SELECT kind FROM human_tasks WHERE done_at IS NULL").fetchall()
        self.assertIn("review_reply", [t[0] for t in task])

    def test_company_inbound_counts(self):
        plan = gate.precheck_plan(self.conn, "followup_email", route="mailer", thread_key=self.key)
        q = [c["query"] for c in plan["checks"] if c["name"] == "company_inbound_since_first"][0]
        self.assertTrue(q)
        self.imap.add_message(inbound("jordan.kim@kestrel.example", OWNER, "Your note", "Alex forwarded it to me."),
                              uid=4, thrid=901)
        self.imap.on(q, [4])
        r = self.run_it()
        self.assertEqual(r["result"], "already_done")


class WebRoutePrecheckTests(MailTestCase):
    """gmail.route = web_ui: code never prechecks over IMAP; the agent's browser precheck (recorded fixtures, in
    the format skill jobhunter-gmail-web asks for) goes through U1's real plan and record."""
    connect = False

    def setUp(self):
        super().setUp()
        write_config(self.home, route="web_ui")
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        self.draft_id = insert_draft(self.conn, kind="cold_email", company_id=self.co, contact_id=self.pc)

    def test_code_precheck_refuses_with_a_browser_lane_answer(self):
        d = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (self.draft_id,)).fetchone()
        with self.assertRaises(Denied) as cm:
            precheck.run_precheck(self.conn, NoImap(), d, owner=OWNER)
        self.assertEqual(cm.exception.code, "E_ROUTE_UNAVAILABLE")
        self.assertEqual((cm.exception.data["handled_by"], cm.exception.data["what"]), ("browser_lane", "precheck"))
        self.assertIn("gate precheck", cm.exception.message)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM prechecks").fetchone()[0], 0)
        self.assertEqual(self.imap.commands, [])

    def test_recorded_browser_precheck_clear(self):
        plan = gate.precheck_plan(self.conn, "cold_email", route="browser", contact_id=self.pc)
        ev = web_fixture("precheck_cold_clear.json")
        self.assertEqual([c["name"] for c in ev["checks"]], [c["name"] for c in plan["checks"]])
        self.assertTrue(all(c["query"] for c in plan["checks"] if c["name"] in ("sent_to_address", "outbox_query",
                                                                                "scheduled_query")))
        res = gate.record_precheck(self.conn, "cold_email", "gmail", ev, "agent", contact_id=self.pc)
        self.assertEqual(res["result"], "clear")
        row = self.conn.execute("SELECT source, platform FROM prechecks WHERE id = ?", (res["precheck_id"],)).fetchone()
        self.assertEqual(tuple(row), ("agent", "gmail"))

    def test_recorded_browser_precheck_hit_is_already_done(self):
        res = gate.record_precheck(self.conn, "cold_email", "gmail", web_fixture("precheck_cold_hit.json"), "agent",
                                   contact_id=self.pc)
        self.assertEqual(res["result"], "already_done")
        self.assertTrue(res.get("imported_token"))
        st = self.conn.execute("SELECT status FROM actions WHERE token = ?", (res["imported_token"],)).fetchone()[0]
        self.assertEqual(st, "imported", "the earlier message blocks the person and company from now on")

    def test_confirm_without_the_sent_readback_is_refused(self):
        """As skill section 3 and EMAIL-SETUP say: a web-route email is confirmed only with its Sent read-back,
        and a read-back with a Cc (or any other recipient) never makes it sent."""
        tok = canon.new_token()
        insert_action(self.conn, kind="cold_email", status="armed", company_id=self.co, contact_id=self.pc,
                      recipient=ALEX, draft_id=self.draft_id, route="browser", platform="gmail", token=tok)
        with self.assertRaises(Denied) as cm:
            gate.confirm(self.conn, tok, "Toast: Message sent")
        self.assertEqual(cm.exception.code, "E_EVIDENCE_MISSING")
        st = lambda: self.conn.execute("SELECT status FROM actions WHERE token = ?", (tok,)).fetchone()[0]
        self.assertEqual(st(), "armed")
        with self.assertRaises(Denied) as cm:
            gate.confirm(self.conn, tok, "Toast: Message sent",
                         observed_text="Subject: Hello\nTo: %s\nCc: morgan.lee@harbor-analytics.example\n\nHi" % ALEX)
        self.assertEqual(cm.exception.code, "E_OBSERVED_MISMATCH")
        self.assertEqual(cm.exception.data["mismatch"]["recipient"], "cc_or_bcc")
        self.assertEqual(st(), "unknown", "never sent: reconcile decides")

    def test_recorded_browser_followup_precheck(self):
        first = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                              recipient=ALEX, reserved_at="2026-09-20T09:00:00Z", route="browser")
        self.conn.execute("UPDATE actions SET sent_at = reserved_at WHERE id = ?", (first,))
        insert_thread(self.conn, first, contact_id=self.pc, company_id=self.co)
        key = self.conn.execute("SELECT thread_key FROM threads WHERE first_action_id = ?", (first,)).fetchone()[0]
        plan = gate.precheck_plan(self.conn, "followup_email", route="browser", thread_key=key)
        names = [c["name"] for c in plan["checks"]]
        ev = web_fixture("precheck_followup_reply.json")
        self.assertEqual([c["name"] for c in ev["checks"]], names)
        res = gate.record_precheck(self.conn, "followup_email", "gmail", ev, "agent", thread_key=key)
        self.assertEqual(res["result"], "already_done")
        self.assertIn("review_reply", [r[0] for r in self.conn.execute("SELECT kind FROM human_tasks")])

    def test_skill_examples_are_valid_contracts(self):
        blocks = skill_json_blocks()
        pre = [b for b in blocks if "checks" in b]
        self.assertEqual(sorted(b["kind"] for b in pre), ["cold_email", "followup_email"])
        for b in pre:
            spec = gate.CHECK_SPECS[gate._spec_key(b["kind"])]
            self.assertEqual([c["name"] for c in b["checks"]], [n for n, _t in spec], b["kind"])
            b["observed_at"] = canon.now()
        res = gate.record_precheck(self.conn, "cold_email", "gmail", [b for b in pre if b["kind"] == "cold_email"][0],
                                   "agent", contact_id=self.pc)
        self.assertEqual(res["result"], "clear")
        reads = [b for b in blocks if "purpose" in b]
        self.assertEqual(sorted(b["purpose"] for b in reads), ["audit", "history"])
        from jobhunter.mail import audit
        for b in reads:
            b["observed_at"] = canon.now()
            self.assertTrue(audit.parse_web_read(b)["headers"], b["purpose"])
        from jobhunter import replies
        rec = [b for b in blocks if b.get("class") == "bounce"]
        self.assertEqual(len(rec), 1)
        self.assertLessEqual(set(rec[0]), replies.RECORD_KEYS, "the bounce record uses only reply record keys")
        self.assertIn(rec[0]["class"], replies.CODE_CLASSES)
        self.assertTrue(rec[0]["msg_ref"].startswith("gmweb:"))


class SkillSentReadbackTests(unittest.TestCase):
    """Section 3 of the web-route skill confirms with the Sent read-back (gate confirm --observed-file)."""

    def section3(self) -> str:
        with open(SKILL, "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("## 3. Send once and confirm by Sent read-back")
        return text[start:text.index("## 4.", start)]

    def test_confirm_passes_the_sent_readback(self):
        sec = self.section3()
        confirms = re.findall(r"`(gate confirm [^`]*)`", sec)
        self.assertEqual(len(confirms), 1, confirms)
        self.assertIn("--observed-file <readback-<token>.txt>", confirms[0])
        self.assertLess(sec.index("read_gmail_message.js"), sec.index("readback-<token>.txt"))
        self.assertLess(sec.index("readback-<token>.txt"), sec.index("gate confirm"))
        for need in ("readback_text", "from_owner", "E_OBSERVED_MISMATCH", "Never send again"):
            self.assertIn(need, sec)

    def test_skill_matches_the_int_recording(self):
        path = os.path.join(REPO, "tests", "fixtures", "e2e", "guard_web_email.json")
        if not os.path.exists(path):
            self.skipTest("INT recording not present")
        with open(path, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
        steps = []

        def walk(node):
            if isinstance(node, dict):
                cmd = (node.get("params") or {}).get("command") if isinstance(node.get("params"), dict) else None
                if isinstance(cmd, str) and " gate confirm " in cmd:
                    steps.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(rec)
        real = [s for s in steps if not str(s.get("note", "")).startswith("mutation")]
        self.assertTrue(real)
        for s in real:
            self.assertIn("--observed-file {WS}/work/{CYCLE}/readback-{TOKEN}.txt", s["params"]["command"])

    def section2(self) -> str:
        with open(SKILL, "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("## 2. Reserve, compose, read back, arm")
        return text[start:text.index("## 3.", start)]

    def test_arm_step_reads_back_the_recipient(self):
        sec = self.section2()
        arms = re.findall(r"`(gate arm [^`]*)`", sec)
        self.assertEqual(arms, ["gate arm <token> --observed-file <observed-<token>.txt>"])
        self.assertLess(sec.index("read_compose.js"), sec.index("observed-<token>.txt"))
        self.assertLess(sec.index("observed-<token>.txt"), sec.index("gate arm"))
        for need in ("`To: <address>`", "exactly one To address", "no Cc and no Bcc", "E_OBSERVED_MISMATCH",
                     "mismatch.recipient", "Never remove an address yourself", "--reason precondition_changed"):
            self.assertIn(need, sec)

    def test_skill_observed_example_is_one_to_address(self):
        """The example observed file of section 2 is in the format gate arm and gate confirm parse, and the
        recipient rule holds for it; a Cc, a Bcc, a second To or no To line breaks the rule."""
        blocks = re.findall(r"```text\n(.*?)```", self.section2(), re.S)
        self.assertEqual(len(blocks), 1, blocks)
        example = "\n".join(line[3:] if line.startswith("   ") else line for line in blocks[0].split("\n"))
        rb = gate.email_readback(example)
        self.assertEqual((rb["cc"], rb["bcc"]), (None, None))
        action = {"recipient": rb["to"]}
        self.assertIsNone(gate.recipient_problem(action, rb))
        self.assertIsNone(gate.recipient_problem({"recipient": rb["to"].upper()}, rb))
        head, _sep, body = example.partition("\n\n")
        cases = {"cc_or_bcc": [head + "\nCc: morgan.lee@harbor-analytics.example",
                               head + "\nBcc: morgan.lee@harbor-analytics.example"],
                 "to_not_one_address": [head.replace(rb["to"], rb["to"] + ", morgan.lee@harbor-analytics.example")],
                 "to_other_address": [head.replace(rb["to"], "morgan.lee@harbor-analytics.example")],
                 "to_missing": ["\n".join(x for x in head.split("\n") if not x.startswith("To:"))]}
        for why, heads in cases.items():
            for h in heads:
                self.assertEqual(gate.recipient_problem(action, gate.email_readback(h + "\n\n" + body)), why, h)

    def test_confirm_requires_the_sent_readback(self):
        sec = self.section3()
        for need in ("`To: <address>`", "exactly one To", "no Cc and no Bcc", "Confirm requires the Sent read-back",
                     "E_EVIDENCE_MISSING", "Never confirm without the Sent read-back", "went to other recipients"):
            self.assertIn(need, sec)
        self.assertNotIn("or the search URL", sec, "a message you could not open has no Sent read-back")
        self.assertLess(sec.index("readback-<token>.txt"), sec.index("E_EVIDENCE_MISSING"))

    def test_email_setup_describes_the_recipient_readback(self):
        with open(os.path.join(REPO, "docs", "EMAIL-SETUP.md"), "r", encoding="utf-8") as fh:
            text = fh.read()
        start = text.index("### What the agents do in Gmail")
        steps = text[start:text.index("### When the session expires", start)]
        arm = steps[steps.index("3. **Type and read back.**"):steps.index("4. **One click on Send**")]
        conf = steps[steps.index("5. **Confirm.**"):steps.index("6. **Replies")]
        for sec in (arm, conf):
            flat = " ".join(sec.split())
            self.assertIn("exactly one To address", flat)
            self.assertIn("no Cc or Bcc", flat)
        flat = " ".join(conf.split())
        self.assertIn("only with this Sent read-back; code refuses to confirm without it", flat)
        self.assertIn("differs in text or recipients is recorded as `unknown`", flat)

    def test_gate_confirm_accepts_observed_file(self):
        with open(os.path.join(REPO, "scripts", "jobhunter", "commands", "gate.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        start = src.index('"gate confirm"')
        self.assertIn('"--observed-file"', src[start:src.index('"gate fail"', start)])


if __name__ == "__main__":
    unittest.main()
