"""Digest (design 1.3.8): delta since the last digest, nothing new means nothing sent, delivery is checked."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import cli, db, digest, notify, paths
from jobhunter import status as S
from jobhunter.commands import report
from tests.fakes.u5 import FakeDesktop, FakeSender, config, seed
from tests.helpers import HomeTestCase, insert_job

CFG = config(owner__notify__quiet_hours=["23:00", "23:30"])


class TestDigest(HomeTestCase):
    def test_empty_database_has_nothing_to_say(self):
        self.assertIsNone(digest.build_digest(self.conn, None, CFG))
        out = digest.run(self.conn, since_last=True, deliver=True, sender=FakeSender(), config=CFG)
        self.assertTrue(out["nothing_new"])
        self.assertIsNone(db.meta_get(self.conn, "last_digest_at"))

    def test_digest_text(self):
        seed(self.conn, self.clock)
        text = digest.build_digest(self.conn, None, CFG)
        self.assertIn("Job Hunter digest", text)
        self.assertIn("Jobs found: 5 (3 passed your filters, 1 good fits)", text)
        self.assertIn("Applications sent: 1", text)
        self.assertIn("Outreach: 1 emails", text)
        self.assertIn("Replies: 1 (1 positive)", text)
        self.assertIn("Alex R. at Kestrel Commerce: Positive reply. Asks for a call next week.", text)
        self.assertIn("Tidewater Labs, Data Analyst (fit 62)", text)
        self.assertIn("Waiting for you: 1 approvals, 1 questions or tasks", text)
        self.assertIn("Stopped: LinkedIn", text)
        self.assertTrue(text.isascii())
        self.assertNotIn("\x20-\x20", text)

    def test_since_last_only_reports_the_delta_and_delivery_is_checked(self):
        seed(self.conn, self.clock)
        sender = FakeSender()
        out = digest.run(self.conn, since_last=True, deliver=True, sender=sender, desktop=FakeDesktop(), config=CFG)
        self.assertTrue(out["sent"])
        self.assertIn("Job Hunter digest", sender.sent[0][2])
        self.assertEqual(db.meta_get(self.conn, "last_digest_at"), self.clock.now())
        self.clock.advance(hours=10)
        self.assertTrue(digest.run(self.conn, since_last=True, deliver=True, sender=sender, config=CFG)["nothing_new"])
        insert_job(self.conn, None, "Analyst", "eval_queued", uid="JAAAAAB2")
        text = digest.build_digest(self.conn, db.meta_get(self.conn, "last_digest_at"), CFG)
        self.assertIn("Jobs found: 1", text)
        self.assertNotIn("Applications sent", text)

    def test_undelivered_digest_stays_queued(self):
        seed(self.conn, self.clock)
        out = digest.run(self.conn, since_last=True, deliver=True, sender=FakeSender(ok=False), desktop=FakeDesktop(),
                         config=CFG)
        self.assertFalse(out["sent"])
        row = self.conn.execute("SELECT attempts, delivered_at FROM notifications WHERE kind = 'digest'").fetchone()
        self.assertEqual((row[0], row[1]), (1, None))

    def test_preview_does_not_move_the_marker(self):
        seed(self.conn, self.clock)
        out = digest.run(self.conn, since_last=True, deliver=False, config=CFG)
        self.assertIn("Job Hunter digest", out["text"])
        self.assertIsNone(db.meta_get(self.conn, "last_digest_at"))

    def test_digest_shows_browser_sites_and_the_email_finder(self):
        from tests.fakes.u5 import consent as fake_consent
        from tests.fakes.u5 import enrich as fake_enrich
        seed(self.conn, self.clock)
        api = fake_consent.FakeConsentAPI(fake_consent.sample_rows())
        name = fake_consent.install(api)
        self.addCleanup(fake_consent.uninstall)
        budget = mock.Mock(return_value=fake_enrich.sample_summary())
        with mock.patch.object(S, "CONSENT_APIS", ((name, ("summary",)),)), \
                mock.patch.object(S, "_import_attr", return_value=budget):
            text = digest.build_digest(self.conn, None, config(enrich__enabled=True))
        self.assertEqual(api.writes, [])
        self.assertIn('Browser sites allowed: Gmail (Chrome profile "Profile 1"); needs you: Naukri', text)
        self.assertIn("Email finder: on; 0 addresses found in 31 days; 142.5 of 202 free credits left (31 days); "
                      "stopped: Tomba", text)
        self.assertTrue(text.isascii())

    def test_digest_leaves_out_an_unused_email_finder(self):
        seed(self.conn, self.clock)
        if os.path.lexists(paths.consent_file()):       # a new install: no site allowed yet
            os.remove(paths.consent_file())
        budget = mock.Mock(side_effect=AssertionError("not called while off and unused"))
        with mock.patch.object(S, "CONSENT_APIS", ()), mock.patch.object(S, "_import_attr", return_value=budget):
            text = digest.build_digest(self.conn, None, CFG)
        self.assertNotIn("Email finder", text)
        self.assertIn("Browser sites allowed: none yet (./jobhunter browser consent)", text)
        with mock.patch.object(S, "browser_consent", return_value=None), \
                mock.patch.object(S, "_import_attr", return_value=None):
            text = digest.build_digest(self.conn, None, CFG)
        self.assertNotIn("Browser sites", text)
        self.assertNotIn("Email finder", text)

    def test_cli(self):
        seed(self.conn, self.clock)
        sender = FakeSender()
        out = io.StringIO()
        with mock.patch.object(S, "load_config", return_value=CFG), \
                mock.patch.object(notify, "_default_sender", sender), \
                mock.patch.object(notify, "desktop_notify", FakeDesktop()):
            rc = cli.main(["digest", "--since-last", "--deliver", "--quiet"], env={}, stdin=io.StringIO(""),
                          stdout=out, modules=[report])
            self.assertEqual((rc, out.getvalue().strip()), (0, "NO_REPLY"))
            out = io.StringIO()
            rc = cli.main(["digest", "--since-last"], env={}, stdin=io.StringIO(""), stdout=out, modules=[report])
        self.assertEqual(json.loads(out.getvalue())["code"], "NOTHING_TO_DO")
        self.assertEqual(len(sender.sent), 1)


if __name__ == "__main__":
    unittest.main()
