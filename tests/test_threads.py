"""U6 threads: on_confirm hook, follow-up timing, LinkedIn sequence, invite acceptance, outcomes, checks."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import canon, db, hooks, threads
from tests.fakes.u6 import U6TestCase, set_config
from tests.helpers import insert_action, insert_company, insert_contact, insert_draft, insert_job, insert_thread


class ThreadsBase(U6TestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.ct = insert_contact(self.conn, company_id=self.co, linkedin_url="https://www.linkedin.com/in/example-alex-rivera")
        self.cuid = self.uid("contacts", self.ct)

    def confirm(self, **kw):
        """Insert a sent action and run the hook inside a transaction, like gate.confirm does."""
        with db.tx(self.conn):
            aid = insert_action(self.conn, **kw)
            row = dict(self.row("SELECT * FROM actions WHERE id = ?", aid))
            res = threads.on_confirm(self.conn, row)
            self.assertTrue(self.conn.in_transaction, "the hook must not end the transaction")
        return aid, res

    def test_u1_hook_forwards_to_threads(self):
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", company_id=self.co, contact_id=self.ct,
                                recipient="alex.rivera@kestrel.example")
            hooks.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))
        self.assertEqual(self.row("SELECT state FROM threads WHERE first_action_id = ?", aid)[0], "open")


class TestBusinessDays(unittest.TestCase):
    def test_weekends_skipped(self):
        sunday = "2026-09-27T05:00:00Z"
        self.assertEqual(threads.add_business_days(sunday, 5), "2026-10-02T05:00:00Z")
        self.assertEqual(threads.add_business_days("2026-10-02T05:00:00Z", 1), "2026-10-05T05:00:00Z")
        self.assertEqual(threads.add_business_days(sunday, 0), sunday)


class TestEmailThreads(ThreadsBase):
    def test_cold_email_opens_thread_with_due_date(self):
        d = insert_draft(self.conn, company_id=self.co, contact_id=self.ct, subject="Returns forecasting")
        aid, _ = self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct, draft_id=d,
                              recipient="alex.rivera@kestrel.example")
        token = self.row("SELECT token FROM actions WHERE id = ?", aid)[0]
        t = self.row("SELECT * FROM threads WHERE first_action_id = ?", aid)
        self.assertEqual(t["thread_key"], "em:" + token)
        self.assertEqual((t["channel"], t["state"], t["subject"]), ("email", "open", "Returns forecasting"))
        due = canon.parse_ts(t["followup_due_at"])
        self.assertLess(due.weekday(), 5)
        self.assertGreaterEqual(t["followup_due_at"], threads.add_business_days(canon.now(), 5))
        self.assertLessEqual(t["followup_due_at"], threads.add_business_days(canon.now(), 7))

    def test_followup_marks_thread_and_clears_due(self):
        aid, res = self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct,
                                recipient="alex.rivera@kestrel.example")
        key = res["thread_key"]
        fid, res2 = self.confirm(kind="followup_email", company_id=self.co, contact_id=self.ct, thread_key=key,
                                 recipient="alex.rivera@kestrel.example")
        t = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual((t["state"], t["followup_action_id"], t["followup_due_at"]), ("followed_up", fid, None))
        self.assertEqual(res2["thread_key"], key)

    def test_application_form_has_no_thread(self):
        job = insert_job(self.conn, company_id=self.co)
        _, res = self.confirm(kind="application", company_id=self.co, job_id=job)
        self.assertIsNone(res)
        self.assertEqual(self.row("SELECT count(*) FROM threads")[0], 0)


class TestLinkedInSequence(ThreadsBase):
    def test_invite_without_note_then_message_then_followup(self):
        _, res = self.confirm(kind="li_invite", company_id=self.co, contact_id=self.ct, recipient="example-alex-rivera")
        key = "li:" + self.cuid
        self.assertEqual(res["thread_key"], key)
        self.assertEqual(self.row("SELECT state FROM threads WHERE thread_key = ?", key)[0], "invite_pending")
        with db.tx(self.conn):
            threads.mark_invite_accepted(self.conn, key, canon.now())
        t = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual(t["state"], "invite_accepted")
        gap = canon.seconds_between(canon.now(), t["followup_due_at"])
        self.assertTrue(86400 <= gap <= 3 * 86400, gap)
        _, res = self.confirm(kind="li_message", company_id=self.co, contact_id=self.ct, li_msg_seq=1,
                              recipient="example-alex-rivera")
        t = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual(t["state"], "open")
        gap = canon.seconds_between(canon.now(), t["followup_due_at"])
        self.assertTrue(7 * 86400 <= gap <= 10 * 86400, gap)
        fid, _ = self.confirm(kind="li_followup", company_id=self.co, contact_id=self.ct, li_msg_seq=2,
                              thread_key=key, recipient="example-alex-rivera")
        t = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual((t["state"], t["followup_action_id"], t["followup_due_at"]), ("followed_up", fid, None))

    def test_invite_with_note_allows_no_followup(self):
        self.confirm(kind="li_invite", company_id=self.co, contact_id=self.ct, li_note=1, li_msg_seq=1,
                     recipient="example-alex-rivera")
        key = "li:" + self.cuid
        with db.tx(self.conn):
            threads.mark_invite_accepted(self.conn, key, canon.now())
        self.confirm(kind="li_message", company_id=self.co, contact_id=self.ct, li_msg_seq=2, recipient="example-alex-rivera")
        t = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual(t["state"], "open")
        self.assertIsNone(t["followup_due_at"], "after the second message-bearing touch nothing is scheduled")

    def test_accept_rules(self):
        self.confirm(kind="li_invite", company_id=self.co, contact_id=self.ct, recipient="example-alex-rivera")
        key = "li:" + self.cuid
        with db.tx(self.conn):
            threads.mark_invite_accepted(self.conn, key, canon.now())
            threads.mark_invite_accepted(self.conn, key, canon.now())   # idempotent
        self.assertDenied("E_NOT_FOUND", threads.mark_invite_accepted, self.conn, "li:PAAAAAAA", canon.now())
        aid, res = self.confirm(kind="cold_email", company_id=self.co, contact_id=insert_contact(
            self.conn, company_id=self.co, email="sam.lee@kestrel.example", full_name="Sam Lee"),
            recipient="sam.lee@kestrel.example")
        self.assertDenied("E_VALIDATION", threads.mark_invite_accepted, self.conn, res["thread_key"], canon.now())

    def test_withdraw(self):
        self.confirm(kind="li_invite", company_id=self.co, contact_id=self.ct, recipient="example-alex-rivera")
        self.confirm(kind="li_withdraw", company_id=self.co, contact_id=self.ct, recipient="example-alex-rivera")
        self.assertEqual(self.row("SELECT state FROM threads")[0], "invite_withdrawn")


class TestFollowupsDue(ThreadsBase):
    def test_due_after_clock_moves(self):
        _, res = self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct,
                              recipient="alex.rivera@kestrel.example")
        self.assertEqual(threads.due_followups(self.conn, 10), [])
        self.clock.advance(days=12)
        items = threads.due_followups(self.conn, 10)
        self.assertEqual([i["thread_key"] for i in items], [res["thread_key"]])
        self.assertEqual(items[0]["kind"], "followup_email")
        self.assertEqual(items[0]["contact_uid"], self.cuid)

    def test_blocked_or_drafted_threads_not_due(self):
        _, res = self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct,
                              recipient="alex.rivera@kestrel.example")
        self.clock.advance(days=12)
        insert_draft(self.conn, kind="followup_email", status="review_pending", thread_key=res["thread_key"],
                     company_id=None, contact_id=None)
        self.assertEqual(threads.due_followups(self.conn, 10), [])
        self.conn.execute("DELETE FROM drafts")
        self.conn.execute("UPDATE companies SET contact_state = 'active_thread'")
        self.assertEqual(threads.due_followups(self.conn, 10), [])
        self.conn.execute("UPDATE companies SET contact_state = 'contacted'")
        set_config("channels.email_outreach.enabled", False)
        self.assertEqual(threads.due_followups(self.conn, 10), [])

    def test_linkedin_followups_need_linkedin_enabled(self):
        self.confirm(kind="li_message", company_id=self.co, contact_id=self.ct, li_msg_seq=1, recipient="example-alex-rivera")
        self.clock.advance(days=11)
        self.assertEqual(threads.due_followups(self.conn, 10), [])
        self.enable_linkedin()
        items = threads.due_followups(self.conn, 10)
        self.assertEqual([i["kind"] for i in items], ["li_followup"])


class TestOutcomes(ThreadsBase):
    def test_thread_outcomes(self):
        _, res = self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct,
                              recipient="alex.rivera@kestrel.example")
        key = res["thread_key"]
        with db.tx(self.conn):
            threads.set_outcome(self.conn, thread_key=key, outcome="interview", by="human:sheet")
        t = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual((t["outcome"], t["followup_due_at"]), ("interview", None))
        self.assertEqual(self.row("SELECT contact_state FROM companies WHERE id = ?", self.co)[0], "active_thread")
        with db.tx(self.conn):
            threads.set_outcome(self.conn, thread_key=key, outcome="rejected", by="human:sheet")
        self.assertEqual(self.row("SELECT state FROM threads WHERE thread_key = ?", key)[0], "closed")
        self.assertDenied("E_VALIDATION", threads.set_outcome, self.conn, thread_key=key, outcome="withdrawn", by="x")
        self.assertDenied("E_USAGE", threads.set_outcome, self.conn, outcome="none", by="x")
        self.assertDenied("E_NOT_FOUND", threads.set_outcome, self.conn, thread_key="em:TAAAAAAAAAAA",
                          outcome="none", by="x")

    def test_job_outcome_and_notes(self):
        job = insert_job(self.conn, company_id=self.co, status="applied")
        aid = insert_action(self.conn, kind="application", company_id=self.co, job_id=job)
        stamp = canon.now()
        self.conn.execute("INSERT INTO applications (action_id, job_id, route, created_at, updated_at) "
                          "VALUES (?, ?, 'ats_form', ?, ?)", (aid, job, stamp, stamp))
        juid = self.uid("jobs", job)
        with db.tx(self.conn):
            threads.set_outcome(self.conn, job_uid=juid, outcome="withdrawn", by="human:sheet", notes="found a better fit")
        app = self.row("SELECT outcome, notes FROM applications WHERE job_id = ?", job)
        self.assertEqual((app[0], app[1]), ("withdrawn", "found a better fit"))
        self.assertDenied("E_VALIDATION", threads.set_outcome, self.conn, job_uid=juid, outcome="referred", by="x")

    def test_set_notes(self):
        job = insert_job(self.conn, company_id=self.co, status="applied")
        aid = insert_action(self.conn, kind="application", company_id=self.co, job_id=job)
        stamp = canon.now()
        self.conn.execute("INSERT INTO applications (action_id, job_id, route, outcome, created_at, updated_at) "
                          "VALUES (?, ?, 'ats_form', 'interview', ?, ?)", (aid, job, stamp, stamp))
        juid = self.uid("jobs", job)
        with db.tx(self.conn):
            res = threads.set_notes(self.conn, job_uid=juid, notes="Second round on Friday", by="human:sheet")
        self.assertEqual(res, {"job_uid": juid, "notes_stored": True, "changed": True})
        app = self.row("SELECT outcome, notes FROM applications WHERE job_id = ?", job)
        self.assertEqual(tuple(app), ("interview", "Second round on Friday"), "the outcome is left as it was")
        with db.tx(self.conn):
            self.assertFalse(threads.set_notes(self.conn, job_uid=juid, notes="Second round on Friday",
                                               by="human:sheet")["changed"])
            threads.set_notes(self.conn, job_uid=juid, notes="x" * (threads.NOTES_MAX + 50), by="human:sheet")
        self.assertEqual(len(self.row("SELECT notes FROM applications WHERE job_id = ?", job)[0]), threads.NOTES_MAX)
        self.assertDenied("E_VALIDATION", threads.set_notes, self.conn, job_uid=juid, notes=None, by="x")
        self.assertDenied("E_NOT_FOUND", threads.set_notes, self.conn, job_uid="JAAAAAAA", notes="n", by="x")
        other = insert_job(self.conn, company_id=self.co, status="eligible")
        self.assertDenied("E_NOT_FOUND", threads.set_notes, self.conn, job_uid=self.uid("jobs", other), notes="n",
                          by="x")


class TestChecks(ThreadsBase):
    def test_linkedin_checks_need_enabled_channel(self):
        self.confirm(kind="li_invite", company_id=self.co, contact_id=self.ct, recipient="example-alex-rivera")
        self.assertEqual(threads.needs_check(self.conn), [])
        self.enable_linkedin()
        items = threads.needs_check(self.conn)
        self.assertEqual([(i["channel"], i["how"]) for i in items], [("linkedin", "linkedin_invitation_manager")])
        self.assertEqual(threads.needs_check_count(self.conn), 1)
        with db.tx(self.conn):
            threads.mark_checked(self.conn, [items[0]["thread_key"]])
        self.assertEqual(threads.needs_check(self.conn), [])
        self.clock.advance(hours=threads.CHECK_EVERY_HOURS + 1)
        self.assertEqual(len(threads.needs_check(self.conn)), 1)

    def test_web_route_email_checks_and_packets(self):
        _, res = self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct,
                              recipient="alex.rivera@kestrel.example")
        self.assertEqual(threads.needs_check(self.conn), [])
        set_config("gmail.route", "web_ui")
        items = threads.needs_check(self.conn)
        self.assertEqual(items[0]["query"], "from:alex.rivera@kestrel.example after:2026/09/27 -in:sent")
        self.assertEqual(items[0]["company_query"], "from:(@kestrel.example) after:2026/09/27 -in:sent")
        # All Mail, not the inbox: a reply the owner archived (or a filter skipped the inbox for) is still found.
        for q in (items[0]["query"], items[0]["company_query"]):
            self.assertNotIn("in:inbox", q)
        set_config("gmail.route", "app_password")
        stamp = canon.now()
        self.conn.execute("INSERT INTO inbound_messages (msg_ref, channel, received_at, packet_path, status, "
                          "created_at, updated_at) VALUES ('gm:1', 'email', ?, '/x/reply-1.json', 'pending', ?, ?)",
                          (stamp, stamp, stamp))
        self.assertEqual(threads.needs_check_count(self.conn), 1)

    def test_list_threads(self):
        self.confirm(kind="cold_email", company_id=self.co, contact_id=self.ct, recipient="alex.rivera@kestrel.example")
        rows = threads.list_threads(self.conn, state="open")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contact_uid"], self.cuid)
        self.assertDenied("E_VALIDATION", threads.list_threads, self.conn, state="bogus")



class TestCommandCallers(unittest.TestCase):
    """The caller letters of every U6 command match the 3.4 command reference (ap and ou are A)."""
    DESIGN_3_4 = {"apply next": "A", "apply release": "A", "outreach next": "A", "outreach skip": "A",
                  "contact add": "A", "contact show": "AHS", "research add": "A", "research list": "AHS",
                  "email verify": "A", "thread list": "AHS", "reply pending": "A", "reply record": "AS",
                  "followup due": "A", "outcome set": "HS", "actions import": "AH"}

    def test_callers_match_the_command_reference(self):
        from jobhunter import cli
        registered = cli.registered_commands(cli.build_parser())
        for command, letters in self.DESIGN_3_4.items():
            with self.subTest(command=command):
                self.assertEqual(registered[command].get_default("_jh_callers"), letters)

if __name__ == "__main__":
    unittest.main()
