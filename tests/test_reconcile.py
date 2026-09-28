"""reconcile: the only ways a blocking action becomes free (design 2.3.4): too early, two negative checks
24 h apart on the mailer route, web-route rounds, found -> sent, applications and LinkedIn never
auto-freed, the human rule."""
from __future__ import annotations

import tests  # noqa: F401
from jobhunter import canon, db, reconcile
from tests.fakes.u1 import TUESDAY_NOON, World, patch_hooks, write_config, write_heartbeat
from tests.helpers import HomeTestCase, insert_action


class ReconcileCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        write_heartbeat()
        self.p = patch_hooks()
        self.p.start()
        self.w = World(self.conn)

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def unknown(self, kind="cold_email", route="mailer", platform="gmail", **kw):
        aid = insert_action(self.conn, kind=kind, status="unknown", route=route, platform=platform,
                            contact_id=kw.get("contact_id", self.w.person), company_id=self.w.company,
                            job_id=kw.get("job_id"), agent_id=kw.get("agent_id"))
        self.conn.execute("UPDATE actions SET armed_at = reserved_at, message_id = ? WHERE id = ?",
                          ("<%d@jobhunter.invalid>" % aid, aid))
        return self.conn.execute("SELECT token FROM actions WHERE id = ?", (aid,)).fetchone()[0]

    def check(self, token, method, result, agent_id=None):
        with db.tx(self.conn):
            return reconcile.record_check(self.conn, token, method, result, "detail", "test", agent_id=agent_id)

    def status(self, token):
        return self.conn.execute("SELECT status, fail_reason FROM actions WHERE token = ?", (token,)).fetchone()


class TestMailerRoute(ReconcileCase):
    def test_too_early_then_two_checks_24h_apart(self):
        tok = self.unknown()
        d = self.assertDenied("E_TOO_EARLY", self.check, tok, "imap_message_id", "not_found")
        self.assertGreater(d.retry_after, 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM reconcile_checks").fetchone()[0], 0)
        self.clock.advance(minutes=16)
        r = self.check(tok, "imap_message_id", "not_found")
        self.assertEqual(r["status"], "unknown")
        self.clock.advance(hours=23)
        self.assertDenied("E_TOO_EARLY", self.check, tok, "imap_message_id", "not_found")
        self.clock.advance(hours=1, minutes=1)
        r = self.check(tok, "imap_message_id", "not_found")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(tuple(self.status(tok)), ("failed", "not_found_twice"))

    def test_found_means_sent(self):
        tok = self.unknown()
        r = self.check(tok, "imap_message_id", "found")
        self.assertEqual(r["status"], "sent")
        self.assertEqual(self.status(tok)[0], "sent")

    def test_unknowable_is_recorded_and_frees_nothing(self):
        tok = self.unknown()
        self.clock.advance(hours=30)
        r = self.check(tok, "imap_message_id", "unknowable")
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(self.conn.execute("SELECT result FROM reconcile_checks").fetchone()[0], "error")

    def test_human_cannot_free_mailer_email(self):
        tok = self.unknown()
        self.clock.advance(days=3)
        with db.tx(self.conn):
            self.assertDenied("E_PRECONDITION", reconcile.confirm_not_sent, self.conn, tok, "human")


class TestWebRoute(ReconcileCase):
    def test_two_rounds_needed(self):
        tok = self.unknown(route="browser", agent_id="jobhunter-outreach")
        self.clock.advance(minutes=20)
        for m in reconcile.WEB_METHODS:
            self.check(tok, m, "not_found", agent_id="jobhunter-outreach")
        self.clock.advance(hours=12)
        self.assertDenied("E_TOO_EARLY", self.check, tok, "web_sent_search", "not_found")
        self.clock.advance(hours=13)
        r1 = self.check(tok, "web_sent_search", "not_found")
        r2 = self.check(tok, "web_outbox_search", "not_found")
        self.assertEqual((r1["status"], r2["status"]), ("unknown", "unknown"))
        r3 = self.check(tok, "web_scheduled_search", "not_found")
        self.assertEqual(r3["status"], "failed")

    def test_imap_search_required_when_connected(self):
        with db.tx(self.conn):
            db.meta_set(self.conn, "mail_connected_at", canon.now(), "system")
        tok = self.unknown(route="browser")
        self.clock.advance(minutes=20)
        for m in reconcile.WEB_METHODS:
            self.check(tok, m, "not_found")
        self.clock.advance(hours=25)
        for m in reconcile.WEB_METHODS:
            r = self.check(tok, m, "not_found")
        self.assertEqual(r["status"], "unknown")
        r = self.check(tok, "imap_sent_search", "not_found")
        self.assertEqual(r["status"], "failed")

    def test_work_list_by_route_and_agent(self):
        tok = self.unknown(route="browser", agent_id="jobhunter-outreach")
        with db.tx(self.conn):
            tasks = reconcile.work_list(self.conn, "browser", "jobhunter-outreach")
            other = reconcile.work_list(self.conn, "browser", "jobhunter-applier")
        self.assertEqual(sorted(t["method"] for t in tasks), sorted(reconcile.WEB_METHODS))
        self.assertTrue(all(t["token"] == tok for t in tasks))
        self.assertEqual(other, [])


class TestNeverAutoFreed(ReconcileCase):
    def test_application_stays_unknown_until_the_human(self):
        tok = self.unknown(kind="application", route="browser", platform="greenhouse", job_id=self.w.job,
                           contact_id=None)
        self.clock.advance(days=3)
        for _ in range(3):
            r = self.check(tok, "ats_page", "not_found")
            self.clock.advance(days=1)
        self.assertEqual(r["status"], "unknown")
        with db.tx(self.conn):
            res = reconcile.confirm_not_sent(self.conn, tok, "human:cli")
        self.assertEqual(res["status"], "failed")
        self.assertEqual(tuple(self.status(tok)), ("failed", "human_confirmed_not_sent"))

    def test_human_rule_waits_and_respects_confirmation_email(self):
        tok = self.unknown(kind="application", route="browser", platform="greenhouse", job_id=self.w.job,
                           contact_id=None)
        with db.tx(self.conn):
            self.assertDenied("E_TOO_EARLY", reconcile.confirm_not_sent, self.conn, tok, "human")
        self.conn.execute("INSERT INTO inbound_messages (msg_ref, channel, company_id, received_at, code_class, status, "
                          "created_at, updated_at) VALUES ('gm:1', 'email', ?, ?, 'application_confirmation', "
                          "'classified', ?, ?)", (self.w.company, canon.ts_add(canon.now(), hours=2), canon.now(),
                                                  canon.now()))
        self.clock.advance(days=2)
        with db.tx(self.conn):
            self.assertDenied("E_PRECONDITION", reconcile.confirm_not_sent, self.conn, tok, "human")

    def test_linkedin_found_on_sent_page(self):
        tok = self.unknown(kind="li_invite", route="browser", platform="linkedin")
        self.clock.advance(days=5)
        r = self.check(tok, "li_sent_invites", "not_found")
        self.assertEqual(r["status"], "unknown")
        r = self.check(tok, "li_sent_invites", "found")
        self.assertEqual(r["status"], "sent")


if __name__ == "__main__":
    import unittest
    unittest.main()
