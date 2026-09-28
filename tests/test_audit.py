"""Nightly audit (design 4.5): an unledgered send trips global (audit_mismatch) and opens a review task;
the LinkedIn gauge is information only; a missing mail module is reported, not fatal."""
from __future__ import annotations

import tests  # noqa: F401
from jobhunter import audit, canon, db
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase, insert_action, insert_company


class TestAudit(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()

    def run_audit(self, sent=()):
        with db.tx(self.conn):
            return audit.audit_run(self.conn, 2, sent_since=lambda conn, days: list(sent))

    def test_clean_audit(self):
        res = self.run_audit()
        self.assertEqual(res["mismatches"], [])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM breakers WHERE scope = 'global'").fetchone())
        self.assertIsNotNone(db.meta_get(self.conn, "audit_last_at"))

    def test_unledgered_email_trips_global(self):
        res = self.run_audit([{"date": canon.now(), "to": "someone@example.org", "subject": "Hello"}])
        self.assertEqual(res["mismatches"][0]["kind"], "unledgered_email")
        b = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'global'").fetchone()
        self.assertEqual(tuple(b), ("open", "audit_mismatch"))
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE kind = 'review_audit_mismatch'").fetchone())

    def test_confirmation_without_application(self):
        k = insert_company(self.conn, name="Kestrel Commerce")
        self.conn.execute("INSERT INTO inbound_messages (msg_ref, channel, company_id, from_domain, received_at, "
                          "code_class, status, created_at, updated_at) VALUES ('gm:1', 'email', ?, 'kestrel.example', ?, "
                          "'application_confirmation', 'classified', ?, ?)", (k, canon.now(), canon.now(), canon.now()))
        res = self.run_audit()
        self.assertEqual(res["mismatches"][0]["kind"], "unledgered_application")
        with db.tx(self.conn):
            self.conn.execute("UPDATE breakers SET state = 'closed'")
        insert_action(self.conn, kind="application", platform="greenhouse", company_id=k, contact_id=None)
        self.assertEqual(self.run_audit()["mismatches"], [])

    def test_linkedin_gauge_is_info_only(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO counters (ts, platform, metric, n, kind) VALUES (?, 'linkedin', "
                              "'li_invites_sent_7d', 5, 'gauge')", (canon.now(),))
        res = self.run_audit()
        self.assertEqual((len(res["mismatches"]), res["info"][0]["kind"]), (0, "li_invites_outside_ledger"))

    def test_run_fetches_mail_before_the_write_transaction(self):
        seen = []

        def sent_since(conn, days):
            seen.append(conn.in_transaction)
            return [{"date": canon.now(), "to": "someone@example.org", "subject": "Hello"}]
        res = audit.run(self.conn, 2, sent_since=sent_since)
        self.assertEqual(seen, [False])
        self.assertEqual(res["mismatches"][0]["kind"], "unledgered_email")
        self.assertEqual(self.conn.execute("SELECT reason_code FROM breakers WHERE scope = 'global'").fetchone()[0],
                         "audit_mismatch")
        # fetch_mail refuses to run inside a transaction
        with db.tx(self.conn):
            self.assertDenied("E_INTERNAL", audit.fetch_mail, self.conn, 2, sent_since)

    def test_mail_errors_are_reported_not_raised(self):
        def broken(conn, days):
            raise OSError("imap timeout")
        res = audit.run(self.conn, 2, sent_since=broken)
        self.assertEqual(res["errors"][0]["source"], "mail")
        self.assertIn("imap timeout", res["errors"][0]["error"])
        self.assertEqual(res["mismatches"], [])

    def test_audit_command_reads_mail_outside_the_transaction(self):
        import types
        from unittest import mock
        from jobhunter.commands import maint
        seen = []

        def fake(conn, days):
            seen.append((conn.in_transaction, days))
            return [], None
        ctx = types.SimpleNamespace(connect=lambda write=True: self.conn)
        with mock.patch.object(audit, "_mail_sent_since", fake):
            res = maint.cmd_audit(types.SimpleNamespace(days=None), ctx)
        self.assertEqual(seen, [(False, 2)])
        self.assertEqual(res["mismatches"], [])

    def test_missing_mail_module_is_reported(self):
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"jobhunter.mail": None, "jobhunter.mail.audit": None}):
            with db.tx(self.conn):
                res = audit.audit_run(self.conn, 2)
        self.assertEqual(res["errors"][0]["source"], "mail")


if __name__ == "__main__":
    import unittest
    unittest.main()
