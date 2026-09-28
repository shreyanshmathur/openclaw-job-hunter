"""The mailer run end to end (U9, design 1.3.7, 2.3.1, 2.3.4) with U1's real gate and U6's real thread hook,
a fake SMTP server and scripted IMAP. Only U3's presend hook is replaced (it needs a stored QC review)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import breakers, canon, cli, db, drafts, gate, mail, paths
from jobhunter.errors import Denied
from jobhunter.mail import mime, outbox
from tests.fakes.u9 import OWNER, MailTestCase, inbound, write_config
from tests.helpers import insert_action, insert_company, insert_contact, insert_draft, insert_job, insert_thread

ALEX = "alex.rivera@kestrel.example"
PDF = b"%PDF-1.4\n% fictional resume for tests\n%%EOF\n"


def fake_presend(conn, draft_id):
    row = conn.execute("SELECT text_sha256 FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return {"ok": True, "code": "OK", "sha256": row[0], "send_text": drafts.send_text(conn, draft_id), "blocks": []}


class OutboxTests(MailTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch("jobhunter.hooks.presend", fake_presend)
        p.start()
        self.addCleanup(p.stop)
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        self.conn.execute("UPDATE contacts SET email_grade = 'A', email_mx_ok = 1 WHERE id = ?", (self.pc,))

    # ------------------------------------------------------------ helpers
    def approve(self, draft_id):
        """Store the canonical hash of the draft (what QC and the person approved)."""
        text = drafts.send_text(self.conn, draft_id)
        self.conn.execute("UPDATE drafts SET text_sha256 = ? WHERE id = ?", (canon.sha256_text(text), draft_id))
        return text

    def cold(self, **kw):
        did = insert_draft(self.conn, kind="cold_email", company_id=self.co, contact_id=self.pc,
                           subject=kw.pop("subject", "Pincode-level RTO models"),
                           body=kw.pop("body", "Hi Alex,\n\nI read your note on RTO models.\n\nThanks,"), **kw)
        self.conn.execute("UPDATE drafts SET recipient = ? WHERE id = ?", (ALEX, did))
        return did, self.approve(did)

    def run_mailer(self, **kw):
        return outbox.run_once(self.conn, **kw)

    def action(self, token=None):
        if token:
            return self.conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
        return self.conn.execute("SELECT * FROM actions WHERE route = 'mailer' ORDER BY id DESC LIMIT 1").fetchone()

    def draft_status(self, did):
        return self.conn.execute("SELECT status FROM drafts WHERE id = ?", (did,)).fetchone()[0]

    def breaker(self, scope="gmail"):
        r = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = ?", (scope,)).fetchone()
        return tuple(r) if r else None

    # ------------------------------------------------------------ sends
    def test_cold_email_sent_with_the_approved_bytes(self):
        did, text = self.cold()
        seen = {}

        def at_data(_srv):
            c = sqlite3.connect(paths.db_path())
            seen["row"] = c.execute("SELECT status, message_id FROM actions WHERE route = 'mailer'").fetchone()
            c.close()
        self.smtp.on_data_cmd = at_data
        res = self.run_mailer()
        self.assertEqual(len(res["sent"]), 1, res)
        token = res["sent"][0]
        a = self.action(token)
        self.assertEqual(a["status"], "sent")
        self.assertEqual(a["agent_id"], "system:mailer")
        self.assertEqual(a["message_id"], "<%s@jobhunter.invalid>" % token)
        self.assertTrue(a["evidence"].startswith("SMTP 250"))
        self.assertEqual(seen["row"], ("armed", "<%s@jobhunter.invalid>" % token), "armed before DATA")
        self.assertEqual(self.draft_status(did), "sent")
        wire = mime.parse_wire(self.smtp.messages[0])
        self.assertEqual(wire["Message-ID"], a["message_id"])
        self.assertEqual(wire["To"], ALEX)
        self.assertIn(OWNER, wire["From"])
        body = wire.get_content().replace("\r\n", "\n")
        self.assertEqual(canon.canonical_send_text("cold_email", str(wire["Subject"]), body, None, None), text)
        self.assertEqual(canon.sha256_text(text), a["approved_sha256"])
        th = self.conn.execute("SELECT * FROM threads WHERE thread_key = ?", ("em:" + token,)).fetchone()
        self.assertEqual(th["first_message_id"], a["message_id"])
        pc = self.conn.execute("SELECT * FROM prechecks WHERE used_by_action = ?", (a["id"],)).fetchone()
        self.assertEqual(pc["source"], "code_imap")
        self.assertIn("in:sent to:%s" % ALEX, self.imap.queries)
        self.assertEqual(self.smtp.envelope_to, ["<%s>" % ALEX])
        # nothing else goes out in the next run: one send per draft, and the pacing gap holds
        res2 = self.run_mailer()
        self.assertEqual(res2["sent"], [])
        self.assertEqual(len(self.smtp.messages), 1)

    def test_message_id_is_recorded_by_the_gate_when_arming(self):
        """The mailer hands the Message-ID to gate.mark_armed and never writes actions itself (design 2.5)."""
        self.cold()
        calls = []
        real = gate.mark_armed

        def spy(conn, token, message_id=None):
            calls.append((token, message_id))
            return real(conn, token, message_id=message_id)
        with mock.patch.object(gate, "mark_armed", spy):
            res = self.run_mailer()
        self.assertEqual(len(res["sent"]), 1, res)
        token = res["sent"][0]
        self.assertEqual(calls, [(token, "<%s@jobhunter.invalid>" % token)])
        self.assertEqual(self.action(token)["message_id"], "<%s@jobhunter.invalid>" % token)
        with open(outbox.__file__, encoding="utf-8") as fh:
            src = fh.read().upper()
        for stmt in ("UPDATE ACTIONS", "INSERT INTO ACTIONS", "DELETE FROM ACTIONS"):
            self.assertNotIn(stmt, src, "only the gate writes actions")

    def test_a_refused_arm_sends_nothing(self):
        """If the gate refuses to arm (here: a bad Message-ID), DATA is never sent and no message_id is stored."""
        self.cold()
        real = mime.message_id_for
        n = {"calls": 0}

        def mid_for(token):  # the built message keeps a good header; the id handed to the gate is bad
            n["calls"] += 1
            return real(token) if n["calls"] == 1 else "bad\r\nBcc: x@y.example"
        with mock.patch.object(outbox.mime, "message_id_for", mid_for):
            res = self.run_mailer()
        self.assertEqual(n["calls"], 2, "built once, armed once")
        self.assertEqual(res["sent"], [])
        self.assertEqual(self.smtp.messages, [])
        a = self.action()
        self.assertIsNone(a["message_id"])
        self.assertEqual(a["status"], "failed", "never armed, so the slot is freed")

    def test_recipient_refused_frees_the_slot_and_drops_the_draft(self):
        did, _ = self.cold()
        self.smtp.script["rcpt"] = (550, "5.1.1 The email account that you tried to reach does not exist")
        res = self.run_mailer()
        self.assertEqual(res["sent"], [])
        a = self.action()
        self.assertEqual((a["status"], a["fail_reason"]), ("failed", "smtp_rejected_before_data"))
        self.assertIsNone(a["message_id"], "never armed")
        self.assertEqual(self.smtp.data_commands, 0)
        self.assertEqual(self.draft_status(did), "expired")
        self.assertIsNone(self.breaker())
        n = self.conn.execute("SELECT text FROM notifications WHERE dedupe_key LIKE 'mail_dropped:%'").fetchone()
        self.assertIn("refused the recipient", n[0])

    def test_5xx_at_data_is_failed_smtp_rejected(self):
        did, _ = self.cold()
        self.smtp.script["data_end"] = (554, "5.6.0 Message rejected: malformed content")
        self.run_mailer()
        a = self.action()
        self.assertEqual((a["status"], a["fail_reason"]), ("failed", "smtp_rejected"))
        self.assertIsNotNone(a["armed_at"])
        self.assertEqual(self.draft_status(did), "expired")
        self.assertIsNone(self.breaker())

    def test_policy_block_at_data_trips_gmail_and_keeps_the_draft(self):
        did, _ = self.cold()
        self.smtp.script["data_end"] = (552, "5.7.0 This message was blocked because its content presents a "
                                             "potential security issue")
        self.run_mailer()
        self.assertEqual(self.action()["fail_reason"], "smtp_rejected")
        self.assertEqual(self.breaker(), ("open", "gmail_auth_failed"))
        self.assertEqual(self.draft_status(did), "approved", "the person decides after a policy stop")

    def test_rate_deferral_trips_gmail(self):
        did, _ = self.cold()
        self.smtp.script["mail"] = (421, "4.7.0 Try again later, closing connection")
        res = self.run_mailer()
        self.assertEqual(self.breaker(), ("open", "gmail_sending_limit"))
        self.assertEqual(res["tripped"], "gmail")
        self.assertEqual(self.action()["fail_reason"], "smtp_rejected_before_data")
        self.assertEqual(self.draft_status(did), "approved", "a temporary refusal keeps the draft")
        self.assertEqual(self.run_mailer()["blocked"]["scope"], "gmail")

    def test_auth_failure_trips_gmail(self):
        self.cold()
        self.smtp.password = "zzzzzzzzzzzzzzzz"
        self.run_mailer()
        self.assertEqual(self.breaker(), ("open", "gmail_auth_failed"))
        self.assertEqual(self.action()["status"], "failed")

    def test_disconnect_after_data_is_unknown_then_reconciled(self):
        did, _ = self.cold()
        self.smtp.script["data_end"] = "drop"
        res = self.run_mailer()
        a = self.action()
        self.assertEqual(a["status"], "unknown")
        self.assertEqual(res["attempts"][0]["status"], "unknown")
        self.assertEqual(self.draft_status(did), "approved")
        q = "rfc822msgid:%s@jobhunter.invalid" % a["token"]
        self.clock.advance(minutes=5)
        self.run_mailer()
        self.assertNotIn(q, self.imap.queries, "too early for the first check")
        self.clock.advance(minutes=11)
        self.imap.add_message(inbound(OWNER, ALEX, "Pincode-level RTO models", "x", msg_id=a["message_id"]), uid=5)
        self.imap.on(q, [5])
        res = self.run_mailer()
        self.assertEqual(res["reconciled"][0]["result"], "found")
        self.assertEqual(self.action(a["token"])["status"], "sent")
        self.assertEqual(self.draft_status(did), "sent")
        self.assertEqual(len(self.smtp.received), 1, "never sent twice")

    def test_unknown_not_found_twice_frees_the_slot(self):
        self.cold()
        self.smtp.script["data_end"] = "drop"
        self.run_mailer()
        tok = self.action()["token"]
        self.clock.advance(minutes=16)
        self.run_mailer()
        self.assertEqual(self.action(tok)["status"], "unknown")
        self.clock.advance(hours=24, minutes=1)
        self.smtp.script.pop("data_end")
        res = self.run_mailer(max_sends=0)
        self.assertEqual(self.action(tok)["status"], "failed")
        self.assertEqual(self.action(tok)["fail_reason"], "not_found_twice")
        self.assertEqual([r["result"] for r in res["reconciled"]], ["not_found"])

    def test_sent_before_by_hand_is_recorded_not_sent(self):
        did, _ = self.cold()
        self.imap.add_message(inbound(OWNER, ALEX, "Hello", "sent by hand"), uid=3)
        self.imap.on("in:sent to:%s" % ALEX, [3])
        res = self.run_mailer()
        self.assertEqual(res["sent"], [])
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual(self.draft_status(did), "expired")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions WHERE status = 'imported'").fetchone()[0], 1)

    def test_outside_hours_skips_before_any_imap_work(self):
        write_config(self.home, sender_window=["03:00", "04:00"])
        did, _ = self.cold()
        res = self.run_mailer(fetch=False)
        self.assertEqual(res["skipped"][0]["code"], "E_OUTSIDE_HOURS")
        self.assertEqual(self.imap.queries, [])
        self.assertEqual(self.draft_status(did), "approved")

    def test_send_after_in_the_future_waits(self):
        did, _ = self.cold()
        self.conn.execute("UPDATE drafts SET send_after = ? WHERE id = ?", (canon.ts_add(canon.now(), hours=2), did))
        self.assertEqual(self.run_mailer()["sent"], [])
        self.clock.advance(hours=2)
        self.assertEqual(len(self.run_mailer()["sent"]), 1)

    def test_followup_first_and_threaded(self):
        first = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                              recipient=ALEX, reserved_at="2026-09-20T09:00:00Z")
        ftok = self.conn.execute("SELECT token FROM actions WHERE id = ?", (first,)).fetchone()[0]
        fmid = "<%s@jobhunter.invalid>" % ftok
        self.conn.execute("UPDATE actions SET message_id = ? WHERE id = ?", (fmid, first))
        tid = insert_thread(self.conn, first, contact_id=self.pc, company_id=self.co)
        self.conn.execute("UPDATE threads SET first_message_id = ?, subject = 'Pincode-level RTO models' WHERE id = ?",
                          (fmid, tid))
        other = insert_contact(self.conn, company_id=insert_company(self.conn, name="Heron Labs",
                                                                    domain="heron.example"),
                               full_name="Jordan Kim", email="jordan.kim@heron.example")
        self.conn.execute("UPDATE contacts SET email_grade = 'A', email_mx_ok = 1 WHERE id = ?", (other,))
        cold = insert_draft(self.conn, kind="cold_email", contact_id=other,
                            company_id=self.conn.execute("SELECT company_id FROM contacts WHERE id = ?",
                                                         (other,)).fetchone()[0])
        self.conn.execute("UPDATE drafts SET recipient = 'jordan.kim@heron.example' WHERE id = ?", (cold,))
        self.approve(cold)
        fu = insert_draft(self.conn, kind="followup_email", company_id=self.co, contact_id=self.pc,
                          thread_key="em:" + ftok, subject="Re: Pincode-level RTO models",
                          body="Hi Alex,\n\nA short follow-up on my note.")
        self.conn.execute("UPDATE drafts SET recipient = ? WHERE id = ?", (ALEX, fu))
        self.approve(fu)
        res = self.run_mailer()
        self.assertEqual(len(res["sent"]), 1)
        a = self.action(res["sent"][0])
        self.assertEqual(a["kind"], "followup_email")
        wire = mime.parse_wire(self.smtp.messages[0])
        self.assertEqual(wire["In-Reply-To"], fmid)
        self.assertEqual(wire["References"], fmid)
        self.assertEqual(wire["Subject"], "Re: Pincode-level RTO models")
        th = self.conn.execute("SELECT state, followup_action_id FROM threads WHERE id = ?", (tid,)).fetchone()
        self.assertEqual(tuple(th), ("followed_up", a["id"]))
        self.assertEqual(self.draft_status(cold), "approved", "one send per run")

    def _application(self, tamper=False):
        job = insert_job(self.conn, company_id=self.co, status="eligible")
        inbox = insert_contact(self.conn, company_id=self.co, full_name="Kestrel Careers",
                               email="careers@kestrel.example", role_type="role_inbox")
        self.conn.execute("UPDATE contacts SET email_mx_ok = 1 WHERE id = ?", (inbox,))
        pdf_path = os.path.join(self.home.dir, "private", "variant.pdf")
        with open(pdf_path, "wb") as fh:
            fh.write(PDF)
        sha = hashlib.sha256(PDF).hexdigest()
        vid = self.conn.execute(
            "INSERT INTO resume_variants (variant_uid, job_id, mode, base_sha256, pdf_path, txt_path, pdf_sha256, "
            "created_at) VALUES ('VAAAAAAAA', ?, 'light', ?, ?, ?, ?, ?)",
            (job, sha, pdf_path, pdf_path + ".txt", sha, canon.now())).lastrowid
        did = insert_draft(self.conn, kind="application_email", company_id=self.co, contact_id=inbox, job_id=job,
                           subject="Data Analyst application", body="Hello,\n\nPlease find my resume attached.")
        payload = {"attachment": {"variant_uid": "VAAAAAAAA", "filename": "Sam_Lee_Resume.pdf", "sha256": sha}}
        self.conn.execute("UPDATE drafts SET attachment_variant_id = ?, payload_json = ?, recipient = ? WHERE id = ?",
                          (vid, json.dumps(payload), "careers@kestrel.example", did))
        self.approve(did)
        if tamper:
            with open(pdf_path, "ab") as fh:
                fh.write(b"% changed after approval\n")
        return job, did

    def test_application_email_with_the_approved_pdf(self):
        job, did = self._application()
        res = self.run_mailer()
        self.assertEqual(len(res["sent"]), 1, res)
        wire = mime.parse_wire(self.smtp.messages[0])
        att = [p for p in wire.walk() if p.get_filename()][0]
        self.assertEqual(att.get_filename(), "Sam_Lee_Resume.pdf")
        self.assertEqual(att.get_content(), PDF)
        a = self.action(res["sent"][0])
        self.assertEqual(a["attachment_sha256"], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job,)).fetchone()[0], "applied")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM applications WHERE action_id = ?",
                                           (a["id"],)).fetchone()[0], 1)

    def test_changed_pdf_is_never_sent(self):
        job, did = self._application(tamper=True)
        res = self.run_mailer()
        self.assertEqual(res["sent"], [])
        self.assertEqual(self.smtp.connections, 0)
        a = self.action()
        self.assertEqual((a["status"], a["fail_reason"]), ("failed", "not_attempted"))
        self.assertEqual(self.draft_status(did), "expired")

    # ------------------------------------------------------------ stop rules between nightly runs (design 4.5)
    def past_sends(self, n, bounced=0):
        """n earlier cold sends (a week ago), the first `bounced` of them with a hard bounce 3 days ago."""
        for i in range(n):
            aid = insert_action(self.conn, kind="cold_email", platform="gmail", reserved_at="2026-09-22T09:00:00Z")
            if i < bounced:
                self.conn.execute("INSERT INTO threads (thread_key, channel, first_action_id, state, reply_class, "
                                  "reply_at, created_at, updated_at) VALUES (?, 'email', ?, 'bounced', 'bounce', ?, "
                                  "?, ?)", ("em:T%011d" % aid, aid, "2026-09-26T09:00:00Z", canon.now(), canon.now()))

    def complaint(self, address="casey.morgan@heron.example"):
        self.conn.execute("INSERT INTO exclusions (type, value_raw, value_key, source, created_at, updated_at) "
                          "VALUES ('email', ?, ?, 'complaint', ?, ?)",
                          (address, "email_norm:" + address, canon.now(), canon.now()))

    def test_a_bounce_fetched_in_this_run_stops_cold_email_before_the_send(self):
        """The rolling hard-bounce rate reaches 5% with the bounce the run's own fetch records: gmail.cold opens
        before the queued cold email, instead of at the nightly housekeeping run."""
        self.past_sends(19)
        co2 = insert_company(self.conn, name="Heron Logistics", domain="heron.example")
        pc2 = insert_contact(self.conn, company_id=co2, email="jordan.lee@heron.example")
        aid = insert_action(self.conn, kind="cold_email", company_id=co2, contact_id=pc2,
                            recipient="jordan.lee@heron.example", reserved_at="2026-09-28T09:00:00Z")
        token = self.conn.execute("SELECT token FROM actions WHERE id = ?", (aid,)).fetchone()[0]
        self.conn.execute("UPDATE actions SET message_id = ? WHERE id = ?", ("<%s@jobhunter.invalid>" % token, aid))
        insert_thread(self.conn, aid, contact_id=pc2, company_id=co2)
        did, _ = self.cold()
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "mail", "bounce_hard.eml"), encoding="utf-8") as fh:
            self.imap.add_message(fh.read().replace("{TOKEN}", token), uid=14)
        self.imap.match("from:mailer-daemon", [14])
        res = self.run_mailer()
        self.assertEqual(res["health"]["tripped"], ["bounce_rate_stop"], res)
        self.assertEqual(res["tripped"], "gmail.cold")
        self.assertEqual(self.breaker("gmail.cold"), ("open", "bounce_rate_stop"))
        self.assertEqual(res["sent"], [])
        self.assertEqual(self.smtp.messages, [])
        self.assertEqual([(s["draft_uid"], s["code"]) for s in res["skipped"]],
                         [(self.conn.execute("SELECT draft_uid FROM drafts WHERE id = ?", (did,)).fetchone()[0],
                           "E_BREAKER_OPEN")])
        self.assertEqual(self.draft_status(did), "approved", "the draft waits for the person to clear the stop")

    def test_the_first_complaint_cuts_the_cold_cap_at_the_next_run(self):
        """A complaint recorded between runs (U6 applies a classified reply) halves the cold cap before the next
        send; the clamp runs 7 days from the complaint."""
        with db.tx(self.conn):
            self.complaint()
        self.cold()
        res = self.run_mailer(fetch=False)
        self.assertEqual(res["health"]["clamped"], ["gmail.cold"])
        clamp = json.loads(db.meta_get(self.conn, "clamp:gmail.cold"))
        self.assertEqual((clamp["factor"], clamp["reason"]), (0.5, "complaint"))
        self.assertEqual(clamp["until"], canon.ts_add(canon.now(), days=7))
        self.assertIsNone(self.breaker("gmail.cold"), "one complaint cuts the cap; it does not stop cold email")
        self.assertEqual(len(res["sent"]), 1, res)

    def test_the_stop_rules_run_again_only_on_new_evidence(self):
        """Without a new bounce or complaint the mailer does not re-run the rules, so a stop the person reset is
        not reopened from the same data; the next bounce re-checks at once."""
        from jobhunter import breakers
        self.past_sends(20, bounced=1)
        calls = []
        real = breakers.email_health

        def spy(conn, cfg=None):
            calls.append(1)
            return real(conn, cfg)
        with mock.patch.object(breakers, "email_health", spy):
            res = self.run_mailer(fetch=False, max_sends=0)
            self.assertEqual(res["health"]["tripped"], ["bounce_rate_stop"])
            with db.tx(self.conn):
                breakers.reset(self.conn, "gmail.cold", "checked the address list", "human")
            res = self.run_mailer(fetch=False, max_sends=0)
            self.assertNotIn("health", res)
            self.assertEqual(self.breaker("gmail.cold")[0], "closed")
            self.assertEqual(len(calls), 1)
            with db.tx(self.conn):
                self.past_sends(1, bounced=1)
            res = self.run_mailer(fetch=False, max_sends=0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.breaker("gmail.cold")[0], "open")
            self.assertEqual(res["tripped"], "gmail.cold")
            with db.tx(self.conn):
                self.complaint()
            res = self.run_mailer(fetch=False, max_sends=0)
            self.assertEqual(len(calls), 3, "a new complaint also re-checks")
            self.assertEqual(res["health"]["clamped"], ["gmail.cold"])

    # ------------------------------------------------------------ run conditions
    def test_paused_is_exit_5(self):
        self.cold()
        with open(paths.paused_file(), "w") as fh:
            fh.write("paused by test\n")
        with self.assertRaises(Denied) as cm:
            self.run_mailer()
        self.assertEqual(cm.exception.code, "E_PAUSED")
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM locks WHERE name = 'mailer'").fetchone()[0], 0,
                         "the run lock is released")

    def test_web_route_does_nothing(self):
        write_config(self.home, route="web_ui")
        self.cold()
        res = self.run_mailer()
        self.assertEqual(res["blocked"]["reason"], "web_ui_route")
        self.assertEqual((res["handled_by"], res["route"]), ("browser_lane", "web_ui"))
        self.assertIn("browser", res["blocked"]["message"])
        self.assertEqual(self.imap.queries, [])
        self.assertEqual(self.smtp.connections, 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    def web_unknown(self, minutes_ago=60):
        a = insert_action(self.conn, kind="cold_email", status="unknown", company_id=self.co, contact_id=self.pc,
                          recipient=ALEX, reserved_at=canon.ts_add(canon.now(), minutes=-minutes_ago),
                          agent_id="jobhunter-outreach", route="browser")
        return self.conn.execute("SELECT token FROM actions WHERE id = ?", (a,)).fetchone()[0]

    def test_web_route_with_the_optional_app_password_checks_unknown_web_sends(self):
        write_config(self.home, route="web_ui")
        self.cold()
        tok = self.web_unknown()
        res = self.run_mailer()
        self.assertEqual(res["blocked"]["reason"], "web_ui_route")
        self.assertEqual([(r["token"], r["method"], r["result"]) for r in res["reconciled"]],
                         [(tok, "imap_sent_search", "not_found")])
        self.assertEqual(len(self.imap.queries), 1)
        self.assertTrue(self.imap.queries[0].startswith("in:sent to:%s after:" % ALEX), self.imap.queries)
        chk = self.conn.execute("SELECT method, result, by FROM reconcile_checks").fetchall()
        self.assertEqual([tuple(r) for r in chk], [("imap_sent_search", "not_found", outbox.MAILER_AGENT)])
        self.assertEqual(self.smtp.connections, 0, "nothing is ever sent by code on the web_ui route")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions WHERE route = 'mailer'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM locks WHERE name = 'mailer'").fetchone()[0], 0)

    def test_web_route_too_early_or_nothing_unknown_opens_no_imap(self):
        write_config(self.home, route="web_ui")
        self.run_mailer()
        self.web_unknown(minutes_ago=5)
        res = self.run_mailer()
        self.assertEqual(res["reconciled"], [])
        self.assertEqual(self.imap.queries, [])

    def test_web_route_imap_failure_trips_nothing(self):
        write_config(self.home, route="web_ui")
        self.web_unknown()
        mail.store_credentials(OWNER, "zzzzzzzzzzzzzzzz", "file")
        res = self.run_mailer()
        self.assertEqual(res["errors"][0]["code"], "E_MAIL_TRANSPORT")
        self.assertIsNone(self.breaker(), "the optional IMAP check never stops the browser lane")
        n = self.conn.execute("SELECT count(*) FROM notifications WHERE dedupe_key LIKE 'mail_web_imap_failed:%'"
                              ).fetchone()[0]
        self.assertEqual(n, 1)
        self.run_mailer()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM notifications WHERE dedupe_key LIKE "
                                           "'mail_web_imap_failed:%'").fetchone()[0], 1, "once a day")

    def test_web_route_respects_pause_and_gmail_breaker(self):
        write_config(self.home, route="web_ui")
        self.web_unknown()
        with open(paths.paused_file(), "w") as fh:
            fh.write("paused by test\n")
        res = self.run_mailer()
        self.assertEqual((res["blocked"]["reason"], res["imap_skipped"]["reason"]), ("web_ui_route", "paused"))
        os.unlink(paths.paused_file())
        with db.tx(self.conn):
            breakers.trip(self.conn, "gmail", "gmail_security", "test", by="test")
        res = self.run_mailer()
        self.assertEqual(res["imap_skipped"]["scope"], "gmail")
        self.assertEqual(self.imap.queries, [])

    def test_web_route_cli_is_nothing_to_do(self):
        write_config(self.home, route="web_ui")
        self.cold()
        out = io.StringIO()
        rc = cli.main(["mail", "run"], env={}, stdin=io.StringIO(""), stdout=out)
        env = json.loads(out.getvalue())
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))
        self.assertIn("browser lane", env["message"])
        self.assertEqual(env["data"]["handled_by"], "browser_lane")

    def test_lock_held_by_another_run(self):
        self.cold()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO locks (name, holder, acquired_at, expires_at) VALUES ('mailer', 'other', ?, ?)",
                              (canon.now(), canon.ts_add(canon.now(), minutes=3)))
        self.assertEqual(self.run_mailer()["blocked"]["reason"], "another_mailer_run")

    def test_cli_quiet_and_exit_codes(self):
        self.cold()
        out = io.StringIO()
        rc = cli.main(["mail", "run", "--quiet"], env={}, stdin=io.StringIO(""), stdout=out)
        self.assertEqual((rc, out.getvalue().strip()), (0, "NO_REPLY"))
        self.assertEqual(len(self.smtp.messages), 1)
        with open(paths.paused_file(), "w") as fh:
            fh.write("x\n")
        out = io.StringIO()
        rc = cli.main(["mail", "run"], env={}, stdin=io.StringIO(""), stdout=out)
        self.assertEqual(rc, 5)
        self.assertEqual(json.loads(out.getvalue())["code"], "E_PAUSED")

    def test_agents_cannot_run_the_mailer(self):
        out = io.StringIO()
        rc = cli.main(["mail", "run"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-outreach"},
                      stdin=io.StringIO(""), stdout=out)
        self.assertEqual(rc, 11)


class NotConnectedTests(MailTestCase):
    connect = False

    def test_not_connected_opens_a_task(self):
        res = outbox.run_once(self.conn)
        self.assertEqual(res["blocked"]["reason"], "not_connected")
        t = self.conn.execute("SELECT kind FROM human_tasks WHERE done_at IS NULL").fetchall()
        self.assertEqual([r[0] for r in t], ["connect_mail"])
        self.assertEqual(self.imap.commands, [])

    def test_web_route_needs_no_app_password(self):
        """The default route: no credentials anywhere, no connect_mail task, a clear browser-lane answer."""
        write_config(self.home, route="web_ui")
        self.assertEqual(mail.load_secrets(), {})
        insert_action(self.conn, kind="cold_email", status="unknown", recipient=ALEX, route="browser",
                      reserved_at=canon.ts_add(canon.now(), hours=-2), agent_id="jobhunter-outreach")
        res = outbox.run_once(self.conn)
        self.assertEqual((res["blocked"]["reason"], res["handled_by"]), ("web_ui_route", "browser_lane"))
        self.assertEqual(res["errors"], [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM human_tasks").fetchone()[0], 0)
        self.assertEqual((self.imap.commands, self.smtp.connections), ([], 0))
        out = io.StringIO()
        rc = cli.main(["mail", "test"], env={}, stdin=io.StringIO(""), stdout=out)
        env = json.loads(out.getvalue())
        self.assertEqual((rc, env["code"], env["data"]["handled_by"]), (0, "NOTHING_TO_DO", "browser_lane"))
        self.assertIn("no app password is needed", env["message"])

    def test_default_route_is_web_ui(self):
        self.assertEqual(mail.route({}), "web_ui")
        self.assertEqual(mail.route({"gmail": {}}), "web_ui")
        self.assertEqual(mail.route({"gmail": {"route": "app_password"}}), "app_password")
        self.assertEqual(mail.route({"gmail": {"route": "App-Password"}}), "web_ui",
                         "anything but app_password is the browser route, as in gate.email_route")
        self.assertEqual(mail.browser_lane("send")["handled_by"], "browser_lane")


if __name__ == "__main__":
    unittest.main()
