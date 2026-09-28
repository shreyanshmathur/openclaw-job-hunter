"""U10 cache (section 9): the same person is never looked up twice; two contact rows for one person share
the answer; merge repoints; the key table holds only salted hashes; one retry per person, none after a
bounce; forget clears provider data."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import db, hooks
from jobhunter.enrich import cache
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard

E = "example.person@kestrel.example"


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class TestCache(EnrichTestCase):
    def test_same_person_twice_one_call(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        first = self.run_find(t["contact_id"])
        self.assertFalse(first["data"]["cached"])
        second = self.run_find(t["contact_id"])
        self.assertTrue(second["data"]["cached"])
        self.assertEqual(second["data"]["request_uid"], first["data"]["request_uid"])
        self.assertEqual(len(self.fake.requests), 2)

    def test_two_contact_rows_for_one_person(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        self.run_find(t["contact_id"])
        t2 = self.make_target(company_id=t["company_id"])          # a second row, same name and company
        out = self.run_find(t2["contact_id"])
        self.assertTrue(out["data"]["cached"])
        self.assertEqual(len(self.fake.requests), 2)

    def test_hashes_only(self):
        t = self.make_target(li_slug="example-person")
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        rows = self.conn.execute("SELECT key_hash, kind FROM enrich_request_keys").fetchall()
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["email_norm", "li", "lookup", "pname"])
        dump = " ".join(" ".join(str(v) for v in tuple(r)) for r in
                        self.conn.execute("SELECT * FROM enrich_request_keys").fetchall() +
                        self.conn.execute("SELECT * FROM enrich_requests").fetchall())
        for bad in ("example", "Example", "person", "kestrel"):
            self.assertNotIn(bad, dump)
        for r in rows:
            self.assertRegex(r["key_hash"], "^[0-9a-f]{64}$")

    def test_found_address_blocks_another_lookup_of_it(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        h, _ = cache.email_key(self.conn, E)
        self.assertIsNotNone(cache.find(self.conn, [h]))

    def test_merge_repoints(self):
        t = self.make_target()
        other = self.make_target(full_name="Example Twin")
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        self.run_find(t["contact_id"])
        with db.tx(self.conn):
            cache.on_merge(self.conn, t["contact_id"], other["contact_id"])
        self.assertEqual(self.requests()[0]["contact_id"], other["contact_id"])
        self.assertEqual(cache.state(self.conn, other["contact_id"]), "not_found")
        self.assertIsNone(cache.state(self.conn, t["contact_id"]))

    def test_one_retry_only(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        self.run_find(t["contact_id"])
        with db.tx(self.conn):
            got = cache.grant_retry(self.conn, t["contact_id"], "human")
        self.assertTrue(got["request_uid"].startswith("E"))
        self.clock.advance(minutes=1)           # past each provider's min_interval_s
        self.fake.add("prospeo", "hit_valid")
        out = self.run_find(t["contact_id"])
        self.assertEqual((out["data"]["grade"], out["data"]["request_uid"]), ("B", got["request_uid"]))
        self.assertDenied("E_ALREADY_DONE", lambda: cache.grant_retry(self.conn, t["contact_id"], "human"))
        self.assertEqual(len(self.fake.requests), 3)

    def test_no_retry_after_bounce(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET bounced_at = ?", (self.clock.now(),))
        d = self.assertDenied("E_PRECONDITION", lambda: cache.grant_retry(self.conn, t["contact_id"], "human"))
        self.assertEqual(d.data["reason"], "bounced")

    def test_retry_needs_a_finished_lookup(self):
        t = self.make_target()
        self.assertDenied("E_NOT_FOUND", lambda: cache.grant_retry(self.conn, t["contact_id"], "human"))

    def test_forget(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        with db.tx(self.conn):
            cache.forget(self.conn, t["contact_id"])
        req = self.requests()[0]
        self.assertEqual((req["contact_id"], req["status"]), (None, "forgotten"))
        call = self.calls()[0]
        self.assertEqual((call["email"], call["source_url"], call["source_urls_json"]), (None, None, "[]"))
        self.assertIsNotNone(call["purged_at"])
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_source"], c["email_enrich_call_id"]), (None, None, None))
        self.assertGreater(self.conn.execute("SELECT count(*) FROM enrich_request_keys").fetchone()[0], 0)
        out = self.run_find(t["contact_id"])          # still never looked up again
        self.assertTrue(out["data"]["cached"])
        self.assertEqual(len(self.fake.requests), 1)


class TestRealHooks(EnrichTestCase):
    """U1 hooks.on_contact_merge and hooks.on_forget reach cache.on_merge and cache.forget."""

    def test_merge_hook(self):
        t = self.make_target()
        other = self.make_target(full_name="Example Twin")
        self.fake.add("prospeo", "miss").add("hunter", "miss")
        self.run_find(t["contact_id"])
        with db.tx(self.conn):
            hooks.on_contact_merge(self.conn, t["contact_id"], other["contact_id"])
        self.assertEqual(self.requests()[0]["contact_id"], other["contact_id"])

    def test_people_merge_moves_requests_and_the_provider_address(self):
        """U1 people.merge calls the hook, and the survivor takes the stronger address as one group with its
        email_source and email_enrich_call_id; the send-time rules then hold for the survivor."""
        from jobhunter import canon, people
        from jobhunter.enrich import gatecheck
        t = self.provider_contact(E)
        other = self.make_target(full_name="Example Twin", company_id=t["company_id"])
        with db.tx(self.conn):
            people.merge(self.conn, t["contact_id"], other["contact_id"], by="human", fuzzy=False)
        self.assertEqual(self.requests()[0]["contact_id"], other["contact_id"])
        c = self.contact(other["contact_id"])
        self.assertEqual((c["email"], c["email_source"], c["email_enrich_call_id"]), (E, "provider", t["call_id"]))
        gatecheck.check(self.conn, kind="cold_email", contact_id=other["contact_id"], recipient=E,
                        reserved_at=canon.now())
        self.assertEqual(cache.state(self.conn, other["contact_id"]), "found")

    def test_forget_command_path(self):
        """U1 `forget` calls hooks.on_forget before it clears the contact."""
        import io
        import json
        from jobhunter import cli
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        out = io.StringIO()
        from jobhunter import auth
        auth.create_guard_key()
        auth.set_pin(None, "482915")
        rc = cli.main(["--pin-stdin", "forget", "--contact", t["contact_uid"]], env={},
                      stdin=io.StringIO("482915\n"), stdout=out)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["code"], "OK")
        self.assertEqual(self.requests()[0]["status"], "forgotten")
        self.assertIsNotNone(self.calls()[0]["purged_at"])
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_source"], c["email_enrich_call_id"]), (None, None, None))

    def test_forget_hook(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        with db.tx(self.conn):
            hooks.on_forget(self.conn, t["contact_id"])
        self.assertEqual(self.requests()[0]["status"], "forgotten")
        self.assertIsNone(self.contact(t["contact_id"])["email_source"])


if __name__ == "__main__":
    unittest.main()
