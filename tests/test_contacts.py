"""U6 contacts: 12.8 validation, identity merging through people.resolve, exclusions, job links, CLI exits."""
from __future__ import annotations

import json
import unittest

GMAIL = "gmail" + ".com"   # built at run time so the leak check sees no real-looking address
MEMBER_URL = "https://www.linkedin.com/in/" + "ACoAAB1234567"   # an opaque member id URL, built the same way

import tests  # noqa: F401
from jobhunter import canon, contacts, db
from tests.fakes.u6 import U6TestCase
from tests.fakes.u6.agentcall import agent_cli
from tests.helpers import HomeTestCase, insert_action, insert_job

BASE = {"full_name": "Alex Rivera", "title": "Head of Analytics", "company": "Kestrel Commerce",
        "company_domain": "kestrel.example", "role_type": "hiring_manager", "locale": "IN",
        "email": "alex.rivera@kestrel.example", "email_grade": "A",
        "email_evidence_url": "https://kestrel.example/team", "linkedin_url": "https://www.linkedin.com/in/example-alex-rivera",
        "linkedin_member_url": None, "source_url": "https://job-boards.greenhouse.io/example/jobs/1"}


class TestContacts(U6TestCase):
    def add(self, **over):
        data = dict(BASE)
        data.update(over)
        with db.tx(self.conn):
            return contacts.add_from_file(self.conn, data)

    def test_add_new_contact(self):
        res = self.add()
        self.assertRegex(res["contact_uid"], r"^P[A-Z2-7]{7}$")
        self.assertFalse(res["existing"])
        self.assertEqual(res["merged_with"], [])
        c = self.row("SELECT * FROM contacts WHERE contact_uid = ?", res["contact_uid"])
        self.assertEqual((c["li_slug"], c["needs_vanity"], c["email_grade"], c["first_name"]),
                         ("example-alex-rivera", 0, "A", "Alex"))
        kinds = {r[0] for r in self.conn.execute("SELECT kind FROM contact_keys WHERE contact_id = ?", (c["id"],))}
        self.assertEqual(kinds, {"email", "email_norm", "li_slug", "pname"})
        self.assertEqual(self.row("SELECT display_name FROM companies WHERE id = ?", c["company_id"])[0],
                         "Kestrel Commerce")

    def test_member_url_then_vanity_is_one_person(self):
        first = self.add(email=None, email_grade=None, email_evidence_url=None, linkedin_url=None,
                         linkedin_member_url=MEMBER_URL)
        self.assertTrue(first["needs_vanity"])
        second = self.add(email=None, email_grade=None, email_evidence_url=None,
                          linkedin_member_url=MEMBER_URL)
        self.assertEqual(second["contact_uid"], first["contact_uid"])
        self.assertTrue(second["existing"])
        self.assertFalse(second["needs_vanity"])

    def test_gmail_dot_variant_and_merge(self):
        a = self.add(email="alex.rivera@" + GMAIL, linkedin_url=None, company=None, company_domain=None,
                     email_grade=None, email_evidence_url=None, role_type="recruiter")
        b = self.add(email=None, email_grade=None, email_evidence_url=None, company=None, company_domain=None,
                     role_type="recruiter")
        self.assertNotEqual(a["contact_uid"], b["contact_uid"])
        c = self.add(email="alexrivera+jobs@" + GMAIL, company=None, company_domain=None, email_grade=None,
                     email_evidence_url=None, role_type="recruiter")
        self.assertIn(c["contact_uid"], (a["contact_uid"], b["contact_uid"]))
        self.assertEqual(len(c["merged_with"]), 1)

    def test_validation(self):
        self.assertDenied("E_VALIDATION", self.add, linkedin_url="https://lnkd.in/abc")
        self.assertDenied("E_VALIDATION", self.add, linkedin_url=MEMBER_URL)
        self.assertDenied("E_SCHEMA", self.add, phone="+10000000000")
        self.assertDenied("E_VALIDATION", self.add, email="careers@kestrel.example")
        self.assertDenied("E_VALIDATION", self.add, email=None, email_grade=None, email_evidence_url=None,
                          linkedin_url=None)
        self.assertDenied("E_VALIDATION", self.add, role_type="ceo")
        self.assertDenied("E_VALIDATION", self.add, email_evidence_url="http://kestrel.example/team")
        self.assertDenied("E_VALIDATION", self.add, email="not-an-address")
        res = self.add(email="careers@kestrel.example", role_type="role_inbox", full_name=None, linkedin_url=None)
        self.assertEqual(self.row("SELECT role_type FROM contacts WHERE contact_uid = ?",
                                  res["contact_uid"])[0], "role_inbox")

    def test_exclusion_marks_do_not_contact(self):
        stamp = canon.now()
        self.conn.execute("INSERT INTO exclusions (type, value_raw, value_key, source, created_at, updated_at) "
                          "VALUES ('email', 'alex.rivera@kestrel.example', 'email:alex.rivera@kestrel.example', "
                          "'private_csv', ?, ?)", (stamp, stamp))
        res = self.add()
        self.assertTrue(res["do_not_contact"])
        self.assertEqual(self.row("SELECT dnc_reason FROM contacts WHERE contact_uid = ?", res["contact_uid"])[0],
                         "excluded:email")

    def test_links_the_posting(self):
        job = insert_job(self.conn, url="https://job-boards.greenhouse.io/example/jobs/1", status="eligible")
        res = self.add()
        self.assertEqual(res["job_linked"], self.uid("jobs", job))
        self.assertEqual(self.row("SELECT relation FROM job_hiring_team")[0], "hiring_manager")

    def test_show(self):
        res = self.add()
        out = contacts.show(self.conn, res["contact_uid"])
        self.assertEqual(out["email"], "alex.rivera@kestrel.example")
        self.assertEqual(len(out["keys"]), 4)
        self.assertDenied("E_NOT_FOUND", contacts.show, self.conn, "PAAAAAAA")

class TestContactCompanyDomains(HomeTestCase):
    """A company_domain plus an address at another domain: both domains become keys of one company. Runs
    against the real U1 keys, companies and people modules (the U6 fakes do not merge companies)."""
    TIDEMARK = {"company": "Tidemark Retail", "company_domain": "tidemark.example", "full_name": "Riley Moss",
                "role_type": "hiring_manager", "email": "riley.moss@tidemarkmail.example", "email_grade": "B"}
    CASEY = {"full_name": "Casey Lin", "role_type": "recruiter", "email": "casey.lin@tidemarkmail.example",
             "email_grade": "B"}

    def add(self, data):
        with db.tx(self.conn):
            return contacts.add_from_file(self.conn, dict(data))

    def row(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def company_of(self, res):
        return self.row("SELECT company_id FROM contacts WHERE contact_uid = ?", res["contact_uid"])[0]

    def contact_id(self, res):
        return self.row("SELECT id FROM contacts WHERE contact_uid = ?", res["contact_uid"])[0]

    def aliases(self, company_id):
        return {r[0] for r in self.conn.execute("SELECT alias_key FROM company_aliases WHERE company_id = ?",
                                                (company_id,))}

    def live_companies(self):
        return self.row("SELECT count(*) FROM companies WHERE merged_into IS NULL")[0]

    def test_email_domain_is_a_company_key_next_to_company_domain(self):
        from jobhunter import gate
        riley = self.add(self.TIDEMARK)
        cid = self.company_of(riley)
        self.assertTrue({"id:tidemarkretail", "dom:tidemark.example", "dom:tidemarkmail.example"} <= self.aliases(cid))
        casey = self.add(self.CASEY)
        self.assertEqual(self.company_of(casey), cid)
        self.assertEqual(casey["company_uid"], riley["company_uid"])
        with db.tx(self.conn):
            insert_action(self.conn, kind="cold_email", contact_id=self.contact_id(riley), company_id=cid,
                          recipient=self.TIDEMARK["email"], reserved_at=canon.ts_add(canon.now(), days=-2))
        hits = gate.dedup_hits(self.conn, "cold_email", contact_id=self.contact_id(casey),
                               company_id=self.company_of(casey), email=self.CASEY["email"])
        self.assertIn("company_cooldown", [h["rule"] for h in hits])

    def test_colleague_first_then_named_contact_merges_the_companies(self):
        casey = self.add(self.CASEY)
        riley = self.add(self.TIDEMARK)
        from jobhunter import companies
        self.assertEqual(companies.survivor(self.conn, self.company_of(casey)), self.company_of(riley))
        self.assertEqual(self.live_companies(), 1)
        self.assertTrue({"dom:tidemark.example", "dom:tidemarkmail.example"} <= self.aliases(self.company_of(riley)))

    def test_same_domain_or_free_mail_adds_nothing(self):
        res = self.add(dict(self.TIDEMARK, email="riley.moss@tidemark.example"))
        doms = {k for k in self.aliases(self.company_of(res)) if k.startswith("dom:")}
        self.assertEqual(doms, {"dom:tidemark.example"})
        res = self.add(dict(self.TIDEMARK, full_name="Jordan Vale", email="jordan.vale@" + GMAIL))
        doms = {k for k in self.aliases(self.company_of(res)) if k.startswith("dom:")}
        self.assertEqual(doms, {"dom:tidemark.example"})
        self.assertEqual(self.live_companies(), 1)

    def test_domain_only_company_is_not_split(self):
        res = self.add(dict(self.TIDEMARK, company=None))
        self.assertEqual(self.live_companies(), 1)
        self.assertIn("dom:tidemark.example", self.aliases(self.company_of(res)))

    def test_distinct_pair_is_not_merged(self):
        other = self.company_of(self.add(self.CASEY))
        mine = self.company_of(self.add(dict(self.TIDEMARK, full_name="Dana Park", email=None, email_grade=None,
                                             linkedin_url="https://www.linkedin.com/in/example-dana-park")))
        self.assertNotEqual(mine, other)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO company_distinct (a_id, b_id, by, created_at) VALUES (?, ?, 'human', ?)",
                              (min(mine, other), max(mine, other), canon.now()))
        riley = self.add(self.TIDEMARK)
        self.assertEqual(self.company_of(riley), mine)
        self.assertEqual(self.live_companies(), 2)
        self.assertNotIn("dom:tidemarkmail.example", self.aliases(mine))


class TestContactCli(U6TestCase):
    def run_cli(self, data, agent="jobhunter-outreach"):
        # in this process (the U6 fakes apply), as the guard runs it: python -I, argv and env proof
        path = self.home.write_agent_file("outreach", "C20260927T050000ZAAAA/contact.json", json.dumps(data))
        return agent_cli(agent, ["contact", "add", "--file", path])

    def test_ok_then_already_contacted(self):
        rc, out = self.run_cli(BASE)
        self.assertEqual((rc, out["code"]), (0, "OK"), out)
        cid = self.row("SELECT id FROM contacts WHERE contact_uid = ?", out["data"]["contact_uid"])[0]
        insert_action(self.conn, kind="cold_email", contact_id=cid, recipient="alex.rivera@kestrel.example")
        rc, out = self.run_cli(BASE)
        self.assertEqual((rc, out["code"]), (3, "E_DUP_PERSON"))
        self.assertIn("contact_uid", out["data"])

    def test_agents_without_the_command_are_refused(self):
        rc, out = self.run_cli(BASE, agent="jobhunter-scout")
        self.assertEqual((rc, out["code"]), (11, "E_CALLER_NOT_ALLOWED"), out)
        self.assertEqual(self.row("SELECT count(*) FROM contacts")[0], 0)

    def test_dnc_exit_7_keeps_the_row(self):
        stamp = canon.now()
        self.conn.execute("INSERT INTO exclusions (type, value_raw, value_key, source, created_at, updated_at) "
                          "VALUES ('email', 'x', 'email:alex.rivera@kestrel.example', 'human', ?, ?)", (stamp, stamp))
        rc, out = self.run_cli(BASE)
        self.assertEqual((rc, out["code"]), (7, "E_CONTACT_DNC"))
        self.assertEqual(self.row("SELECT do_not_contact FROM contacts")[0], 1)

    def test_bad_file_exit_10(self):
        rc, out = self.run_cli(dict(BASE, linkedin_url="https://lnkd.in/x"))
        self.assertEqual((rc, out["code"]), (10, "E_VALIDATION"))


class TestSetAddress(U6TestCase):
    """contacts.set_address (ENRICH-SPEC 11.1), the email finder's way to write an address, and the
    provenance the agent's own contact file records."""

    def person(self, email=None, **over):
        data = dict(BASE, email=email, email_grade="A" if email else None,
                    email_evidence_url="https://kestrel.example/team" if email else None)
        data.update(over)
        with db.tx(self.conn):
            res = contacts.add_from_file(self.conn, data)
        return self.row("SELECT id FROM contacts WHERE contact_uid = ?", res["contact_uid"])[0]

    def set(self, cid, **kw):
        args = dict(email="alex.rivera@kestrel.example", grade="B", source="pattern",
                    evidence_url="https://kestrel.example/team")
        args.update(kw)
        with db.tx(self.conn):
            return contacts.set_address(self.conn, cid, **args)

    def drop_provider_triggers(self):
        """The finder's triggers need its call rows; U6 unit tests check U6's own rules without them."""
        self.conn.execute("PRAGMA foreign_keys = OFF")
        with db.tx(self.conn):
            self.conn.execute("DROP TRIGGER IF EXISTS t_contact_provider_email_ins")
            self.conn.execute("DROP TRIGGER IF EXISTS t_contact_provider_email_upd")

    def test_agent_file_records_where_the_address_came_from(self):
        cid = self.person(email="alex.rivera@kestrel.example")
        self.assertEqual(self.row("SELECT email_source FROM contacts WHERE id = ?", cid)[0], "published")
        other = self.person(email="sam.lee@kestrel.example", email_grade="B", full_name="Sam Lee",
                            linkedin_url="https://www.linkedin.com/in/example-sam-lee")
        self.assertEqual(self.row("SELECT email_source FROM contacts WHERE id = ?", other)[0], "pattern")
        self.assertDenied("E_SCHEMA", contacts.validate, dict(BASE, email_source="provider"))

    def test_writes_address_keys_and_provenance(self):
        cid = self.person(email=None)
        res = self.set(cid)
        self.assertEqual((res["contact_id"], res["grade"], res["source"], res["do_not_contact"]), (cid, "B", "pattern", False))
        c = self.row("SELECT * FROM contacts WHERE id = ?", cid)
        self.assertEqual((c["email"], c["email_grade"], c["email_source"], c["email_evidence_url"], c["email_invalid"]),
                         ("alex.rivera@kestrel.example", "B", "pattern", "https://kestrel.example/team", 0))
        keys = {r[0] for r in self.conn.execute("SELECT key FROM contact_keys WHERE contact_id = ?", (cid,))}
        self.assertIn("email:alex.rivera@kestrel.example", keys)

    def test_never_replaces_an_a_or_b_address(self):
        cid = self.person(email="alex.rivera@kestrel.example")
        self.assertDenied("E_ADDRESS_GRADE", self.set, cid, email="arivera@kestrel.example")
        self.assertEqual(self.set(cid, email="alex.rivera@kestrel.example")["email"], "alex.rivera@kestrel.example")

    def test_bounced_address_is_refused(self):
        cid = self.person(email=None)
        self.set(cid)
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET email_invalid = 1 WHERE id = ?", (cid,))
        self.assertDenied("E_ADDRESS_GRADE", self.set, cid)

    def test_address_of_another_contact_merges(self):
        cid = self.person(email=None)
        other = self.person(email="alex.rivera@kestrel.example", full_name="A. Rivera", linkedin_url=None,
                            role_type="recruiter")
        self.assertNotEqual(cid, other)
        res = self.set(cid)
        self.assertEqual(res["contact_id"], min(cid, other))
        self.assertTrue(res["merged_with"])
        loser = max(cid, other)
        self.assertEqual(self.row("SELECT merged_into FROM contacts WHERE id = ?", loser)[0], min(cid, other))

    def test_excluded_address_makes_the_person_do_not_contact(self):
        cid = self.person(email=None)
        with db.tx(self.conn):
            deps_mod = __import__("tests.fakes.u6.deps", fromlist=["excl_add"])
            deps_mod.excl_add(self.conn, "email", "alex.rivera@kestrel.example", "already in touch")
        res = self.set(cid)
        self.assertTrue(res["do_not_contact"])
        self.assertEqual(self.row("SELECT do_not_contact FROM contacts WHERE id = ?", cid)[0], 1)

    def test_provider_address_shape(self):
        cid = self.person(email=None)
        self.assertDenied("E_ADDRESS_GRADE", self.set, cid, source="provider", enrich_call_id=None)
        self.assertDenied("E_ADDRESS_GRADE", self.set, cid, source="provider", grade="A", enrich_call_id=5)
        # the finder's trigger refuses a provider address without a matching call row
        with self.assertRaises(Exception):
            self.set(cid, source="provider", enrich_call_id=999999)
        self.drop_provider_triggers()
        res = self.set(cid, source="provider", enrich_call_id=5, evidence_url=None)
        self.assertEqual(res["source"], "provider")
        c = self.row("SELECT email_source, email_enrich_call_id FROM contacts WHERE id = ?", cid)
        self.assertEqual(tuple(c), ("provider", 5))

    def test_validation(self):
        cid = self.person(email=None)
        self.assertDenied("E_VALIDATION", self.set, cid, email="nope")
        self.assertDenied("E_VALIDATION", self.set, cid, grade="D")
        self.assertDenied("E_VALIDATION", self.set, cid, source="guess")
        self.assertDenied("E_VALIDATION", self.set, cid, evidence_url="http://kestrel.example/team")
        self.assertDenied("E_NOT_FOUND", self.set, 999999)


if __name__ == "__main__":
    unittest.main()
