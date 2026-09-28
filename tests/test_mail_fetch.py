"""Inbound fetch over scripted IMAP (U9, design 1.3.6, 1.3.7, 4.5): code pre-rules, packets, bounces,
confirmations and security emails, with U6's real replies module applying the consequences."""
from __future__ import annotations

import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import db
from jobhunter.mail import fetch
from tests.fakes.u9 import OWNER, MailTestCase, fixture, inbound, write_config
from tests.helpers import insert_action, insert_company, insert_contact, insert_job, insert_thread

ALEX = "alex.rivera@kestrel.example"
GOOGLE_SENDER = "no-reply" + "@" + fetch.GOOGLE_SEC_DOMAIN     # a service address, assembled so the leak check
LINKEDIN_SENDER = "security-noreply" + "@" + "linkedin.com"   # (which allows only example domains) stays quiet


class FetchRules(unittest.TestCase):
    def test_return_date(self):
        self.assertEqual(fetch.return_date("I will be back on October 12, 2026.", "2026-09-29T09:00:00Z"), "2026-10-12")
        self.assertEqual(fetch.return_date("Out until 5 Oct.", "2026-09-29T09:00:00Z"), "2026-10-05")
        self.assertEqual(fetch.return_date("Back on 2026-10-20 at the latest", "2026-09-29T09:00:00Z"), "2026-10-20")
        self.assertEqual(fetch.return_date("Returning on January 4th", "2026-12-20T09:00:00Z"), "2027-01-04")
        self.assertIsNone(fetch.return_date("I am away for a while.", "2026-09-29T09:00:00Z"))

    def test_classify(self):
        auto = {"subject": "Automatic reply: Hello", "auto_submitted": "auto-replied"}
        self.assertEqual(fetch.classify_reply(auto, "I am out of the office until Oct 5.")[0], "out_of_office")
        self.assertEqual(fetch.classify_reply({"subject": "Out of Office"}, "")[0], "out_of_office")
        self.assertEqual(fetch.classify_reply({"subject": "We have received your message"}, "")[0], "auto_ack")
        self.assertEqual(fetch.classify_reply({"subject": "Re: Hello", "auto_submitted": "auto-generated"},
                                              "This is an automated message.")[0], "auto_ack")
        self.assertIsNone(fetch.classify_reply({"subject": "Re: Hello"}, "Sure, let us talk Thursday.")[0])
        self.assertIsNone(fetch.classify_reply({"subject": "Re: Hello"}, "Unfortunately we are not hiring.")[0])
        self.assertEqual(fetch.classify_reply({"subject": "Thank you for applying"}, "", application=True)[0],
                         "application_confirmation")
        self.assertFalse(fetch.is_confirmation("Your application", "Thank you for applying. Unfortunately no."))

    def test_bounce_info(self):
        hard = fixture("bounce_hard.eml").decode().replace("{TOKEN}", "TABCDEFGHJKL")
        info = fetch.bounce_info(hard)
        self.assertEqual(info, {"hard": True, "token": "TABCDEFGHJKL", "recipient": ALEX})
        soft = fetch.bounce_info(fixture("bounce_delay.eml").decode().replace("{TOKEN}", "TABCDEFGHJKL"))
        self.assertFalse(soft["hard"])


class FetchTests(MailTestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        self.first = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                                   recipient=ALEX, reserved_at="2026-09-29T09:00:00Z")
        self.token = self.conn.execute("SELECT token FROM actions WHERE id = ?", (self.first,)).fetchone()[0]
        self.mid = "<%s@jobhunter.invalid>" % self.token
        self.conn.execute("UPDATE actions SET message_id = ? WHERE id = ?", (self.mid, self.first))
        self.tid = insert_thread(self.conn, self.first, contact_id=self.pc, company_id=self.co)
        self.conn.execute("UPDATE threads SET first_message_id = ?, subject = 'Pincode-level RTO models', "
                          "followup_due_at = '2026-10-06T09:00:00Z', last_outbound_at = '2026-09-29T09:00:00Z' "
                          "WHERE id = ?", (self.mid, self.tid))
        self.clock.set("2026-09-29T12:00:00Z")
        self.client = self.imap.client()
        self.client.open()
        self.addCleanup(self.client.close)

    def add(self, name, uid, **kw):
        raw = fixture(name).decode().replace("{TOKEN}", self.token).replace("{GOOGLE}", GOOGLE_SENDER) \
            .replace("{LINKEDIN}", LINKEDIN_SENDER)
        return self.imap.add_message(raw, uid=uid, **kw)

    def run_fetch(self):
        return fetch.fetch(self.conn, self.client, owner_addr=OWNER)

    def thread(self):
        return self.conn.execute("SELECT * FROM threads WHERE id = ?", (self.tid,)).fetchone()

    def inbound_rows(self):
        return self.conn.execute("SELECT * FROM inbound_messages ORDER BY id").fetchall()

    def test_human_reply_becomes_a_packet_once(self):
        self.add("human_reply.eml", 11, msgid=0xabc)
        self.imap.match("from:%s" % ALEX, [11])
        st = self.run_fetch()
        self.assertEqual((st["fetched"], st["packets_written"], st["replies_classified"]), (1, 1, 0))
        rows = self.inbound_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["msg_ref"], "gm:abc")
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["thread_id"], self.tid)
        with open(rows[0]["packet_path"], "r", encoding="utf-8") as fh:
            packet = json.load(fh)
        self.assertEqual(packet["thread_key"], "em:" + self.token)
        self.assertEqual(packet["from_domain"], "kestrel.example")
        self.assertIn("notice period", packet["text"])
        self.assertNotIn("I read your note", packet["text"], "quoted history is stripped")
        self.assertTrue(os.path.dirname(rows[0]["packet_path"]).endswith(os.path.join("outreach", "inbox")))
        q = [x for x in self.imap.queries if ALEX in x][0]
        self.assertTrue(q.startswith("after:2026/09/28 -in:sent {"), q)
        self.assertIn("from:kestrel.example", q)
        st2 = self.run_fetch()
        self.assertEqual(st2["fetched"], 0)
        self.assertIsNotNone(db.meta_get(self.conn, "mail_fetch_last_at"))

    def test_out_of_office_moves_the_follow_up(self):
        self.add("out_of_office.eml", 12)
        self.imap.match("from:%s" % ALEX, [12])
        st = self.run_fetch()
        self.assertEqual((st["replies_classified"], st["packets_written"]), (1, 0))
        r = self.inbound_rows()[0]
        self.assertEqual((r["code_class"], r["status"]), ("out_of_office", "classified"))
        t = self.thread()
        self.assertEqual(t["reply_class"], "out_of_office")
        self.assertGreater(t["followup_due_at"], "2026-10-12")
        self.assertEqual(t["state"], "open")

    def test_auto_ack_from_the_company_domain(self):
        self.add("auto_ack.eml", 13)
        self.imap.match("from:kestrel.example", [13])
        st = self.run_fetch()
        self.assertEqual(st["replies_classified"], 1)
        self.assertEqual(self.inbound_rows()[0]["code_class"], "auto_ack")
        self.assertEqual(self.thread()["state"], "open")

    def test_hard_bounce_marks_the_thread(self):
        self.add("bounce_hard.eml", 14)
        self.imap.match("from:mailer-daemon", [14])
        st = self.run_fetch()
        self.assertEqual(st["bounces"], 1)
        self.assertEqual(self.thread()["state"], "bounced")
        c = self.conn.execute("SELECT email_invalid FROM contacts WHERE id = ?", (self.pc,)).fetchone()
        self.assertEqual(c[0], 1)
        self.assertEqual(self.inbound_rows()[0]["code_class"], "bounce")

    def test_delay_notice_is_ignored(self):
        self.add("bounce_delay.eml", 15)
        self.imap.match("from:mailer-daemon", [15])
        st = self.run_fetch()
        self.assertEqual((st["bounces"], st["ignored"]), (0, 1))
        self.assertEqual(self.thread()["state"], "open")
        self.assertEqual(self.inbound_rows()[0]["status"], "ignored")

    def test_confirmation_for_an_application(self):
        job = insert_job(self.conn, company_id=self.co, status="applied")
        act = insert_action(self.conn, kind="application", status="unknown", company_id=self.co, job_id=job,
                            platform="greenhouse", reserved_at="2026-09-29T09:10:00Z")
        self.conn.execute("UPDATE actions SET armed_at = '2026-09-29T09:10:00Z' WHERE id = ?", (act,))
        self.add("confirmation_ats.eml", 16)
        self.add("rejection_ats.eml", 17)
        self.imap.match('"thank you for applying"', [16, 17])
        st = self.run_fetch()
        self.assertEqual(st["confirmations_seen"], 1)
        rows = {r["msg_ref"]: r for r in self.inbound_rows()}
        conf = [r for r in rows.values() if r["code_class"] == "application_confirmation"]
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]["company_id"], self.co)
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE id = ?", (act,)).fetchone()[0], "sent",
                         "an unknown application with a confirmation email is reconciled to sent")
        self.assertEqual(len([r for r in rows.values() if r["status"] == "ignored"]), 1)

    def test_google_security_email_trips_gmail(self):
        self.add("google_security.eml", 18)
        self.add("google_signin_notice.eml", 19)
        self.imap.match("accounts.google.com", [18, 19])
        st = self.run_fetch()
        self.assertEqual(st["tripped"], ["gmail"])
        b = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'gmail'").fetchone()
        self.assertEqual(tuple(b), ("open", "gmail_security"))
        self.conn.execute("UPDATE breakers SET state = 'closed' WHERE scope = 'gmail'")
        self.assertEqual(self.run_fetch()["tripped"], [], "a handled security email never trips twice")

    def test_linkedin_security_only_when_enabled(self):
        self.add("linkedin_security.eml", 20)
        self.imap.match("linkedin.com", [20])
        self.assertEqual(self.run_fetch()["tripped"], [])
        self.assertFalse(any("linkedin.com" in q for q in self.imap.queries))
        with db.tx(self.conn):
            db.meta_set(self.conn, "channel_linkedin_enabled", "1", "human")
        self.assertEqual(self.run_fetch()["tripped"], ["linkedin"])

    def test_own_mail_and_old_mail_are_skipped(self):
        self.imap.add_message(inbound(OWNER, ALEX, "Pincode-level RTO models", "our copy", msg_id=self.mid), uid=21)
        self.imap.add_message(inbound("news@kestrel.example", OWNER, "Kestrel newsletter", "news",
                                      date="2026-09-20T08:00:00Z"), uid=22)
        self.imap.match("from:kestrel.example", [21, 22])
        st = self.run_fetch()
        self.assertEqual(st["fetched"], 1)
        self.assertEqual(st["packets_written"], 0)
        self.assertEqual(self.inbound_rows()[0]["status"], "ignored")

    def test_company_newsletter_is_not_a_reply(self):
        self.imap.add_message(inbound("news@kestrel.example", OWNER, "Kestrel product update", "Our news.",
                                      headers={"List-Unsubscribe": "<https://kestrel.example/unsubscribe>"}), uid=23)
        self.imap.add_message(inbound("jordan.kim@kestrel.example", OWNER, "Your note to Alex", "Alex asked me to "
                                      "reply: we are hiring for this team."), uid=24)
        self.imap.match("from:kestrel.example", [23, 24])
        st = self.run_fetch()
        self.assertEqual((st["packets_written"], st["ignored"]), (1, 1))
        pend = self.conn.execute("SELECT count(*) FROM inbound_messages WHERE status = 'pending'").fetchone()[0]
        self.assertEqual(pend, 1, "a colleague at the company is a reply; the newsletter is not")

    def test_nothing_to_watch(self):
        self.conn.execute("UPDATE threads SET state = 'closed' WHERE id = ?", (self.tid,))
        self.conn.execute("UPDATE actions SET reserved_at = '2026-08-01T09:00:00Z' WHERE id = ?", (self.first,))
        st = self.run_fetch()
        self.assertEqual(st["fetched"], 0)
        self.assertEqual(len(self.imap.queries), 1, "only the Google security search runs")


class WebRouteFetchTests(MailTestCase):
    """gmail.route = web_ui: replies and delivery failures are read in the browser; fetch reads nothing."""
    connect = False

    def test_fetch_is_handled_by_the_browser_lane(self):
        write_config(self.home, route="web_ui")

        class NoImap:
            account = OWNER

            def __getattr__(self, name):
                raise AssertionError("IMAP used on the web_ui route: %s" % name)

        st = fetch.fetch(self.conn, NoImap())
        self.assertEqual((st["handled_by"], st["route"], st["what"]), ("browser_lane", "web_ui", "fetch"))
        self.assertEqual((st["fetched"], st["packets_written"], st["errors"]), (0, 0, []))
        self.assertIn("replies lane", st["message"])
        self.assertIsNone(db.meta_get(self.conn, "mail_fetch_last_at"), "no fetch was stamped")


if __name__ == "__main__":
    unittest.main()
