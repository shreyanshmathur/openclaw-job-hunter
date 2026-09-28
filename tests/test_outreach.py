"""U6 outreach queue: target order, route choice, every drop rule, post-accept targets, skips, history import."""
from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, cli, db, outreach, threads
from tests.fakes.u6 import U6TestCase, set_config
from tests.fakes.u6 import enrich as enrich_fakes
from tests.helpers import insert_action, insert_company, insert_contact, insert_draft, insert_job


class OutreachBase(U6TestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.job = insert_job(self.conn, company_id=self.co, status="eligible")
        self.juid = self.uid("jobs", self.job)

    def person(self, name="Alex Rivera", relation="hiring_manager", email=None, li=True, job=None, company=None):
        slug = name.lower().replace(" ", "-")
        cid = insert_contact(self.conn, company_id=company or self.co, full_name=name, email=email,
                             linkedin_url="https://www.linkedin.com/in/example-%s" % slug if li else None)
        self.conn.execute("UPDATE contacts SET li_slug = ? WHERE id = ?", (slug if li else None, cid))
        self.conn.execute("INSERT INTO job_hiring_team (job_id, contact_id, relation) VALUES (?, ?, ?)",
                          (job or self.job, cid, relation))
        return cid

    def targets(self, limit=10):
        return outreach.work_list(self.conn, limit)


class TestTargets(OutreachBase):
    def test_route_choice(self):
        cid = self.person()
        t = self.targets()
        self.assertEqual([(x["kind"], x["route"], x["contact_uid"]) for x in t],
                         [("person", "email", self.uid("contacts", cid))])
        self.enable_linkedin()
        self.assertEqual(self.targets()[0]["route"], "linkedin")
        self.conn.execute("UPDATE contacts SET email = 'alex.rivera@kestrel.example', email_grade = 'A'")
        self.assertEqual(self.targets()[0]["route"], "email")
        self.conn.execute("UPDATE contacts SET email_grade = 'C'")
        self.assertEqual(self.targets()[0]["route"], "linkedin")

    def test_order(self):
        rec = self.person("Sam Lee", relation="recruiter")
        hm = self.person("Alex Rivera")
        self.conn.execute("UPDATE jobs SET apply_email = 'talent@kestrel.example', apply_route = 'ats_form'")
        t = self.targets()
        self.assertEqual([x["kind"] for x in t], ["person", "job_email", "person"])
        self.assertEqual(t[0]["contact_uid"], self.uid("contacts", hm))
        self.assertEqual(t[2]["contact_uid"], self.uid("contacts", rec))
        self.assertEqual(t[1]["target_key"], "job:" + self.juid)

    def test_drop_rules(self):
        cid = self.person()
        self.assertEqual(len(self.targets()), 1)
        checks = [
            ("UPDATE contacts SET do_not_contact = 1", "UPDATE contacts SET do_not_contact = 0"),
            ("UPDATE companies SET contact_state = 'active_thread'", "UPDATE companies SET contact_state = 'none'"),
            ("UPDATE companies SET contact_state = 'do_not_contact'", "UPDATE companies SET contact_state = 'none'"),
            ("UPDATE jobs SET status = 'closed'", "UPDATE jobs SET status = 'eligible'"),
        ]
        for bad, good in checks:
            with self.subTest(rule=bad):
                self.conn.execute(bad)
                self.assertEqual(self.targets(), [])
                self.conn.execute(good)
                self.assertEqual(len(self.targets()), 1)
        with db.tx(self.conn):
            outreach.skip(self.conn, "contact:" + self.uid("contacts", cid), "no_hook")
        self.assertEqual(self.targets(), [])
        self.conn.execute("DELETE FROM target_skips")
        d = insert_draft(self.conn, kind="cold_email", status="awaiting_approval", contact_id=cid, company_id=self.co)
        self.assertEqual(self.targets(), [])
        self.conn.execute("DELETE FROM drafts WHERE id = ?", (d,))
        insert_action(self.conn, kind="li_invite", contact_id=cid, company_id=self.co, status="unknown")
        self.assertEqual(self.targets(), [], "an unconfirmed first touch still blocks the person")

    def test_per_job_limit_and_company_cooldown(self):
        a = self.person("Alex Rivera", email="alex.rivera@kestrel.example")
        b = self.person("Sam Lee", email="sam.lee@kestrel.example")
        self.assertEqual(len(self.targets()), 2)
        insert_action(self.conn, kind="cold_email", contact_id=a, company_id=self.co,
                      recipient="alex.rivera@kestrel.example")
        self.assertEqual(self.targets(), [], "one contact per job, and the company is in its email cooldown")
        set_config("outreach.per_job_max_contacts", 2)
        self.assertEqual(self.targets(), [], "company email cooldown and no LinkedIn route")
        self.enable_linkedin()
        t = self.targets()
        self.assertEqual([(x["contact_uid"], x["route"]) for x in t], [(self.uid("contacts", b), "linkedin")])

    def test_post_accept_target(self):
        self.enable_linkedin()
        cid = self.person()
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="li_invite", contact_id=cid, company_id=self.co, recipient="example-alex-rivera")
            key = threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))["thread_key"]
            threads.mark_invite_accepted(self.conn, key, canon.now())
        self.assertEqual(self.targets(), [])
        self.clock.advance(days=3, minutes=1)
        t = self.targets()
        self.assertEqual([(x["kind"], x["thread_key"], x["route"]) for x in t],
                         [("post_accept_message", key, "linkedin")])
        self.assertEqual(outreach.work_count(self.conn), 1)

    def test_work_count_includes_followups(self):
        cid = self.person(email="alex.rivera@kestrel.example")
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", contact_id=cid, company_id=self.co,
                                recipient="alex.rivera@kestrel.example")
            threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))
        self.assertEqual(outreach.work_count(self.conn), 0)
        self.clock.advance(days=12)
        self.assertEqual(outreach.work_count(self.conn), 1)


class TestSkip(OutreachBase):
    def test_skip_rules(self):
        key = "job:" + self.juid
        with db.tx(self.conn):
            outreach.skip(self.conn, key, "not_relevant")
            outreach.skip(self.conn, key, "no_hook")
        row = self.row("SELECT * FROM target_skips WHERE target_key = ?", key)
        self.assertEqual((row["drops"], row["reason"]), (2, "no_hook"))
        self.assertEqual(row["until"], canon.ts_add(canon.now(), days=30))
        self.assertDenied("E_VALIDATION", outreach.skip, self.conn, "person:PAAAAAAA", "no_hook")
        self.assertDenied("E_VALIDATION", outreach.skip, self.conn, key, "bored")
        self.assertDenied("E_NOT_FOUND", outreach.skip, self.conn, "contact:PAAAAAAA", "no_hook")

    def test_cli(self):
        out = io.StringIO()
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-outreach"}
        rc = cli.main(["outreach", "skip", "job:" + self.juid, "--reason", "no_address"], env=env,
                      stdin=io.StringIO(""), stdout=out)
        self.assertEqual(rc, 0, out.getvalue())
        out = io.StringIO()
        rc = cli.main(["outreach", "next"], env=env, stdin=io.StringIO(""), stdout=out)
        self.assertEqual((rc, json.loads(out.getvalue())["code"]), (0, "NOTHING_TO_DO"))


class TestAddressHint(OutreachBase):
    """Research step 5 hint per email target, from the email finder's state (faked) and pattern evidence."""

    def hint(self):
        return {t["contact_uid"]: t.get("address") for t in self.targets() if t["kind"] == "person"}

    def test_hints(self):
        with_addr = self.person("Alex Rivera", email="alex.rivera@kestrel.example", li=False)
        no_addr = self.person("Sam Lee", li=False, relation="recruiter",
                              job=insert_job(self.conn, company_id=self.co, status="eligible"))
        h = self.hint()
        self.assertEqual((h[self.uid("contacts", with_addr)], h[self.uid("contacts", no_addr)]),
                         ("have_address", "enrich_possible"))
        enrich_fakes.STATES[no_addr] = "not_found"
        self.assertEqual(self.hint()[self.uid("contacts", no_addr)], "enrich_done_no_address")
        enrich_fakes.STATES[no_addr] = "running"
        self.assertEqual(self.hint()[self.uid("contacts", no_addr)], "enrich_possible")
        from jobhunter import emailcheck
        with mock.patch.object(emailcheck, "pattern_evidence", return_value={"pattern": "{first}.{last}@kestrel.example",
                                                                             "evidence_urls": [], "addresses": 2}):
            self.assertEqual(self.hint()[self.uid("contacts", no_addr)], "pattern_available")
        self.conn.execute("UPDATE companies SET domain = NULL WHERE id = ?", (self.co,))
        self.assertEqual(self.hint()[self.uid("contacts", no_addr)], "no_domain")

    def test_no_hint_without_the_finder(self):
        self.person("Sam Lee", li=False)
        with mock.patch.dict(sys.modules, {"jobhunter.enrich.cache": None}):
            self.assertNotIn("address", self.targets()[0])


class TestImport(OutreachBase):
    def run_import(self, items):
        with db.tx(self.conn):
            return outreach.import_actions(self.conn, {"source": "gmail_web_scan", "items": items})

    def test_import(self):
        item = {"kind": "cold_email", "platform": "gmail", "recipient": "alex.rivera@kestrel.example",
                "company_name": "Kestrel Commerce", "sent_at": "2026-09-01T10:00:00Z", "evidence": "Sent folder entry"}
        res = self.run_import([item, dict(item, recipient="alexr@kestrel.example", company_name="Kestrel Commerce",
                                          sent_at="2026-09-02T10:00:00Z")])
        # Imported history is a fact: the company rules (cooldown, caps) exempt route 'import' (schema.sql), so
        # the second person at the same company is recorded too and stays blocked after the cooldown ends.
        self.assertEqual((res["imported"], res["already_covered"], res["refused"]), (2, 0, []))
        a = self.row("SELECT * FROM actions WHERE token = ?", res["tokens"][0])
        self.assertEqual((a["status"], a["route"], a["first_touch"], a["sent_at"]),
                         ("imported", "import", 1, "2026-09-01T10:00:00Z"))
        b = self.row("SELECT * FROM actions WHERE token = ?", res["tokens"][1])
        self.assertEqual((b["recipient"], b["company_id"]), ("alexr@kestrel.example", a["company_id"]))
        again = self.run_import([item])
        self.assertEqual((again["imported"], again["already_covered"]), (0, 1))
        self.assertEqual(again["refused"][0]["code"], "E_DUP_PERSON")
        li = self.run_import([{"kind": "li_invite", "platform": "linkedin",
                               "recipient": "https://www.linkedin.com/in/example-sam-lee", "sent_at": "2026-09-03T10:00:00Z"}])
        self.assertEqual(li["imported"], 1)
        self.assertDenied("E_VALIDATION", self.run_import, [dict(item, kind="application")])
        self.assertDenied("E_VALIDATION", self.run_import, [dict(item, sent_at="2030-01-01T00:00:00Z")])
        self.assertDenied("E_SCHEMA", self.run_import, [dict(item, note="x")])


if __name__ == "__main__":
    unittest.main()
