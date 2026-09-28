"""U10 eligibility (section 6): only selected outreach targets, never anyone else. Not a target, wrong
role, excluded, do-not-contact, company blocked, cooldown, live first touch, open draft, target skip, no
domain, no name, no-guessing domain, locale skip, day, cycle and backlog caps."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import canon, db, exclusions
from jobhunter.enrich import eligibility
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard
from tests.helpers import insert_action, insert_draft

CYCLE = "C20260927T050000ZAAAA"


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class TestEligibility(EnrichTestCase):
    def check(self, t, target=None):
        return eligibility.check(self.conn, t["contact_id"], target=target, cycle_id=None, s=self.settings())

    def test_ok(self):
        t = self.make_target()
        info = self.check(t, target="contact:" + t["contact_uid"])
        self.assertEqual((info["first"], info["last"], info["domain"]), ("Example", "Person", "kestrel.example"))
        self.assertIsNone(info["li_slug"])

    def test_not_a_target(self):
        for status in ("new", "rejected", "closed", "borderline"):
            with self.subTest(status=status):
                t = self.make_target(job_status=status)
                self.assertDenied("E_NOT_TARGET", self.check, t)
        with db.tx(self.conn):
            from tests.helpers import insert_contact
            loose = insert_contact(self.conn, company_id=t["company_id"], full_name="Example Loner", email=None)
        self.assertDenied("E_NOT_TARGET", eligibility.check, self.conn, loose, target=None, cycle_id=None,
                          s=self.settings())

    def test_wrong_role(self):
        for role in ("employee", "role_inbox", "other"):
            with self.subTest(role=role):
                self.assertDenied("E_NOT_TARGET", self.check, self.make_target(role_type=role))
        self.check(self.make_target(role_type="recruiter"))
        self.check(self.make_target(role_type="founder"))

    def test_target_flag(self):
        t = self.make_target()
        other = self.make_target(full_name="Example Other")
        self.assertDenied("E_USAGE", self.check, t, target="contact:" + other["contact_uid"])
        self.assertDenied("E_USAGE", self.check, t, target="job:JAAAAAAA")

    def test_dnc_and_exclusions(self):
        t = self.make_target()
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET do_not_contact = 1 WHERE id = ?", (t["contact_id"],))
        self.assertDenied("E_CONTACT_DNC", self.check, t)
        t2 = self.make_target()
        ts = canon.now()
        with db.tx(self.conn):   # a matching row whose effect was not pushed to the job (the match itself counts)
            self.conn.execute("INSERT INTO exclusions (type, value_raw, value_key, reason, source, created_at, "
                              "updated_at) VALUES ('domain', 'kestrel.example', ?, 'never', 'human', ?, ?)",
                              (exclusions.value_key("domain", "kestrel.example"), ts, ts))
        self.assertDenied("E_EXCLUDED", self.check, t2)

    def test_company_blocked(self):
        t = self.make_target()
        with db.tx(self.conn):
            self.conn.execute("UPDATE companies SET contact_state = 'active_thread' WHERE id = ?", (t["company_id"],))
        self.assertDenied("E_COMPANY_BLOCKED", self.check, t)

    def test_cooldown_and_live_first_touch(self):
        t = self.make_target()
        with db.tx(self.conn):
            insert_action(self.conn, kind="li_invite", status="sent", contact_id=t["contact_id"],
                          company_id=t["company_id"], recipient="example-person")
        self.assertDenied("E_DUP_PERSON", self.check, t)
        t2 = self.make_target()
        with db.tx(self.conn):
            from tests.helpers import insert_contact
            other = insert_contact(self.conn, company_id=t2["company_id"], full_name="Example Else",
                                   email="example.else@kestrel.example")
            insert_action(self.conn, kind="cold_email", status="sent", contact_id=other, company_id=t2["company_id"],
                          recipient="example.else@kestrel.example")
        self.assertDenied("E_COMPANY_COOLDOWN", self.check, t2)

    def test_open_draft(self):
        t = self.make_target()
        with db.tx(self.conn):
            insert_draft(self.conn, kind="cold_email", status="drafted", company_id=t["company_id"])
        self.assertDenied("E_DUP_DRAFT", self.check, t)

    def test_target_skip(self):
        t = self.make_target()
        ts = canon.now()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO target_skips (target_key, reason, until, created_at, updated_at) VALUES "
                              "(?, 'no_address', ?, ?, ?)", ("contact:" + t["contact_uid"], canon.ts_add(ts, days=30),
                                                            ts, ts))
        self.assertDenied("E_TARGET_SKIPPED", self.check, t)

    def test_inputs(self):
        d = self.assertDenied("E_PRECONDITION", self.check, self.make_target(domain=None))
        self.assertEqual(d.data["reason"], "no_company_domain")
        d = self.assertDenied("E_PRECONDITION", self.check, self.make_target(domain="gmail.com"))
        self.assertEqual(d.data["reason"], "no_company_domain")
        d = self.assertDenied("E_PRECONDITION", self.check, self.make_target(full_name="Example"))
        self.assertEqual(d.data["reason"], "no_name")

    def test_linkedin_identifier_rule(self):
        t = self.make_target(full_name="Example", li_slug="example-person")
        self.assertDenied("E_PRECONDITION", self.check, t)                  # off by default
        self.settings({"enabled": True, "use_linkedin_identifier": True})
        info = self.check(t)
        self.assertEqual((info["li_slug"], info["last"]), ("example-person", None))
        opaque = self.make_target(full_name="Example", li_slug="acoaexampleopaque")
        self.assertDenied("E_PRECONDITION", self.check, opaque)             # never an opaque member id

    def test_honorifics_removed(self):
        info = self.check(self.make_target(full_name="Dr. Example Person"))
        self.assertEqual((info["first"], info["last"]), ("Example", "Person"))
        first, last, full = eligibility.name_parts("Dr. Example Person", None)
        self.assertEqual((first, last, full), ("Example", "Person", "Example Person"))

    def test_no_guess_and_locale(self):
        t = self.make_target()
        with db.tx(self.conn):
            db.meta_set(self.conn, "no_guess:kestrel.example", "1", "system")
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.check, t)
        self.assertEqual(d.data["reason"], "no_guess_domain")
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM meta WHERE key = 'no_guess:kestrel.example'")
        self.settings({"enabled": True, "skip_locales": ["de"]})
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.check, self.make_target(locale="DE"))
        self.assertEqual(d.data["reason"], "locale_skipped")


class TestVolume(EnrichTestCase):
    enrich_block = {"enabled": True, "max_lookups_per_day": 2, "max_lookups_per_cycle": 1, "max_unsent_found": 1}

    def lookup(self, cycle=None):
        ts = canon.now()
        with db.tx(self.conn):
            rid = self.conn.execute("INSERT INTO enrich_requests (request_uid, status, created_by, cycle_id, "
                                    "started_at, created_at, updated_at) VALUES (?, 'not_found', 'system', ?, ?, ?, ?)",
                                    (canon.new_uid("E"), cycle, ts, ts, ts)).lastrowid
            self.conn.execute("INSERT INTO enrich_calls (request_id, provider, op, outcome, started_at, "
                              "credits_charged, created_at, updated_at) VALUES (?, 'hunter', 'find_name_domain', "
                              "'inflight', ?, 1, ?, ?)", (rid, ts, ts, ts))
        self.clock.advance(minutes=1)
        return rid

    def test_cycle_cap(self):
        s = self.settings()
        eligibility.check_volume(self.conn, s, CYCLE)
        self.lookup(CYCLE)
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", eligibility.check_volume, self.conn, s, CYCLE)
        self.assertEqual(d.data["reason"], "cycle_cap")
        eligibility.check_volume(self.conn, s, "C20260927T060000ZBBBB")

    def test_day_cap(self):
        s = self.settings()
        self.lookup()
        self.lookup()
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", eligibility.check_volume, self.conn, s, None)
        self.assertEqual(d.data["reason"], "day_cap")
        self.assertTrue(0 < d.retry_after <= 86400 + 1)
        self.clock.advance(days=1)
        eligibility.check_volume(self.conn, s, None)

    def test_backlog_cap(self):
        s = self.settings()
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        self.assertEqual(eligibility.unsent_found(self.conn), 1)
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", eligibility.check_volume, self.conn, s, None)
        self.assertEqual(d.data["reason"], "unsent_backlog")


if __name__ == "__main__":
    unittest.main()
