"""E2E (INT): a cold email through the mailer, the reply, the Sheet rows and the owner notification, with the real
modules of U1, U3, U4, U5, U6 and U9.

Outreach cycle (design 1.3.5): `contact add`, `email verify` (MX answer injected at emailcheck.lookup_mx, no DNS),
`research add`, `draft create`, QC review, owner approval with the PIN; then `mail run` (1.3.7) sends it through
the U9 fake SMTP server and confirms it through the gate. The reply arrives on the U9 fake IMAP server, `mail run`
fetches it, the replies cycle (1.3.6) classifies it with `reply pending` / `reply record`, and `notify flush
--deliver` hands the owner message to a fake `openclaw` that accepts only the options the real `openclaw message
send` has. The nightly `audit run` reads a slow IMAP server without holding the database write lock.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import db, emailcheck, gate, mail, sheets_rows
from jobhunter import qc as qcpkg
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u3 import FakeReviewer, install_reviewer_hashes
from tests.fakes.u9 import APP_PW, OWNER, inbound
from tests.fakes.u9.imap_server import FakeImapServer
from tests.fakes.u9.smtp_server import FakeSmtpServer
from tests.fixtures.e2e.support import OWNER_CHAT, World, install_reviewer
from tests.helpers import insert_company, insert_contact

OU = "jobhunter-outreach"
JORDAN = "jordan.blake@kestrel.example"
SNIP = "pincode-level models beat our city-level RTO model last quarter"
BODY = ("Hi Jordan,\n\nYour post on 18 September said pincode-level models beat the city-level RTO model at Kestrel. "
        "We saw the same pattern at Tidemark Logistics.\n\nI built the COD return-risk model at Tidemark, and RTO fell "
        "from 18% to 13% in two quarters.\n\nWould a 15 minute call next week be useful? If someone else owns this, "
        "a name is plenty.\n\nThanks,")
POST_URL = "https://www.linkedin.com/posts/example-person-activity-1"


class MailWorld(World):
    """World with the U9 fake SMTP and IMAP servers as the mail transport and stored test credentials."""

    def __init__(self):
        super().__init__()
        self.smtp = FakeSmtpServer(OWNER, APP_PW).start()
        self.imap = FakeImapServer(OWNER, APP_PW).start()
        self.imap_wrap = None
        mail.set_test_transport(smtp=lambda c: self.smtp.sender(c.account, c.password), imap=self._imap_client)

    def _imap_client(self, c):
        client = self.imap.client(c.account, c.password)
        return self.imap_wrap(client) if self.imap_wrap else client

    def stop(self) -> None:
        mail.clear_test_transport()
        self.smtp.stop()
        self.imap.stop()
        super().stop()

    def connect_mail(self, **config_over) -> None:
        """The optional app-password route (gmail.route app_password): the shipped default is web_ui, where the
        agent sends in the browser (tests/test_e2e_web_email.py); these tests drive the code-owned mailer."""
        self.write_config(**dict({"owner.gmail_address": OWNER, "gmail.route": "app_password"}, **config_over))
        mail.store_credentials(OWNER, APP_PW, "file")
        with db.tx(self.conn):
            db.meta_set(self.conn, "mail_connected_at", self.clock.now(), "human")


class SlowImap:
    """Wraps an ImapClient: every call is slow and, while it runs, checks that another connection can take the
    write lock (a mail read inside a write transaction would block every other writer for the IMAP time)."""

    def __init__(self, client, world, delay_s: float = 0.05):
        self._client = client
        self._world = world
        self._delay = delay_s
        self.probes = []

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            time.sleep(self._delay)
            self.probes.append((name, self._world.write_lock_free()))
            return attr(*a, **k)
        return call


class E2EMailBase(unittest.TestCase):
    def setUp(self):
        self.w = MailWorld()
        self.addCleanup(self.w.stop)
        prev = qcpkg.SPAWN
        qcpkg.SPAWN = [].append
        self.addCleanup(setattr, qcpkg, "SPAWN", prev)
        install_reviewer(self, FakeReviewer("pass"))


class TestColdEmailThroughMailer(E2EMailBase):
    def outreach_cycle(self) -> str:
        """Returns the approved cold email draft uid."""
        w = self.w
        cyc = w.preflight("outreach", OU)
        contact = {"full_name": "Jordan Blake", "first_name": "Jordan", "title": "Head of Analytics",
                   "company": "Kestrel Commerce", "company_domain": "kestrel.example", "role_type": "hiring_manager",
                   "locale": "US", "email": JORDAN, "email_grade": "A", "email_evidence_url": "https://kestrel.example/team",
                   "linkedin_url": None, "linkedin_member_url": None, "source_url": "https://kestrel.example/team"}
        c = w.ok(["contact", "add", "--file", w.wfile("outreach", "%s/contact.json" % cyc, contact)], OU, cycle=cyc)
        self.assertFalse(c["do_not_contact"])
        ev = w.wfile("outreach", "%s/email-evidence.txt" % cyc, "Team page: https://kestrel.example/team")
        with mock.patch.object(emailcheck, "lookup_mx", return_value=["mx1.kestrel.example"]) as mx:
            v = w.ok(["email", "verify", "--address", JORDAN, "--grade", "A", "--evidence-file", ev], OU, cycle=cyc)
        mx.assert_called_once_with("kestrel.example")
        self.assertEqual((v["mx_ok"], v["allowed"], v["provider"]), (True, True, "other"))
        self.assertEqual(w.one("SELECT email_mx_ok, email_grade FROM contacts WHERE contact_uid = ?",
                               c["contact_uid"])[:], (1, "A"))
        research = {"subject": {"kind": "person", "contact_uid": c["contact_uid"]},
                    "facts": [{"text": "Post: pincode-level models beat the city-level RTO model last quarter.",
                               "snippet": SNIP, "source_type": "linkedin_post", "source_url": POST_URL,
                               "published_at": "2026-09-18", "retrieved_at": "2026-09-28"}]}
        r = w.ok(["research", "add", "--file", w.wfile("outreach", "%s/research.json" % cyc, research)], OU, cycle=cyc)
        fact_uid = r["facts"][0]["fact_uid"]
        draft = {"kind": "cold_email", "channel": "email_cold", "contact_uid": c["contact_uid"],
                 "subject": "Pincode-level RTO models", "body": BODY,
                 "hook": {"anchor": "pincode-level models", "source_type": "linkedin_post", "source_url": POST_URL,
                          "snippet": SNIP, "published_at": "2026-09-18", "retrieved_at": "2026-09-28",
                          "fact_id": fact_uid},
                 "claims": [{"text": "RTO fell from 18% to 13% in two quarters", "fact_id": "P1"}], "links": []}
        d = w.ok(["draft", "create", "--file", w.wfile("outreach", "%s/draft.json" % cyc, draft)], OU, cycle=cyc)
        verdict = w.qc_pass(d["draft_uid"], OU, cyc)
        self.assertEqual(verdict["draft_status"], "awaiting_approval", verdict)
        w.end_cycle(cyc, OU)
        approved = w.approve_all()
        self.assertEqual([a["draft_uid"] for a in approved], [d["draft_uid"]])
        self.assertEqual(w.one("SELECT status, send_route, recipient FROM drafts WHERE draft_uid = ?",
                               d["draft_uid"])[:], ("approved", "mailer", JORDAN))
        return d["draft_uid"]

    def test_cold_email_send_reply_sheet_and_notify(self):
        w = self.w
        w.onboard()
        w.connect_mail(**{"owner.notify.channel": "whatsapp", "owner.notify.to": OWNER_CHAT})
        install_reviewer_hashes(w.conn)
        oc_log = w.install_fake_openclaw()
        w.ok(["config", "apply"])
        draft_uid = self.outreach_cycle()

        # the mailer sends it: precheck by IMAP, reserve, SMTP, confirm
        confirms = []
        real_confirm = gate.confirm

        def spy_confirm(*a, **k):
            out = real_confirm(*a, **k)
            confirms.append(out)
            return out
        with mock.patch.object(gate, "confirm", spy_confirm):
            m = w.ok(["mail", "run"])
        self.assertEqual(len(m["sent"]), 1, m)
        token = m["sent"][0]
        self.assertEqual(len(w.smtp.messages), 1)
        self.assertEqual(len(confirms), 1)
        env = confirms[0]
        self.assertEqual(env["status"], "sent")
        self.assertEqual(env["token"], token)
        self.assertEqual(env["thread_key"], "em:" + token)
        self.assertTrue(env["followup_due_at"] and env["followup_due_at"] > w.clock.now(), env)
        act = w.one("SELECT kind, status, recipient, thread_key, message_id FROM actions WHERE token = ?", token)
        self.assertEqual(act[:4], ("cold_email", "sent", JORDAN, "em:" + token))
        th = w.one("SELECT state, followup_due_at, first_message_id FROM threads WHERE thread_key = ?", "em:" + token)
        self.assertEqual((th["state"], th["followup_due_at"], th["first_message_id"]),
                         ("open", env["followup_due_at"], act["message_id"]))
        self.assertEqual(w.one("SELECT status FROM drafts WHERE draft_uid = ?", draft_uid)[0], "sent")

        outreach_rows = sheets_rows.build_rows(w.conn, "outreach", None)
        self.assertEqual([k for k, _v in outreach_rows], [token])
        row = outreach_rows[0][1]
        self.assertEqual((row["channel"], row["company"], row["subject"]),
                         ("Email", "Kestrel Commerce", "Pincode-level RTO models"))
        self.assertIn("pincode-level models", row["why_them"])
        followups = sheets_rows.build_rows(w.conn, "followups", None)
        self.assertEqual([k for k, _v in followups], ["em:" + token])
        self.assertEqual(followups[0][1]["follow_up_due"], env["followup_due_at"][:10])

        # Jordan replies; the mailer fetches it on its next run
        raw = inbound("Jordan Blake <%s>" % JORDAN, OWNER, "Re: Pincode-level RTO models",
                      "Thanks Sam, happy to talk. How about Thursday?", date="2026-09-29T11:00:00Z",
                      in_reply_to=act["message_id"])
        uid = int(w.imap.add_message(raw))
        w.imap.match(JORDAN, [uid])
        w.imap.match("kestrel.example", [uid])
        w.clock.set("2026-09-29T12:00:00Z")
        write_heartbeat()
        m2 = w.ok(["mail", "run"])
        self.assertEqual((m2["sent"], m2["fetched"], m2["packets_written"]), ([], 1, 1), m2)

        # the replies cycle classifies it
        cyc = w.preflight("replies", OU)
        pending = w.ok(["reply", "pending"], OU, cycle=cyc)["packets"]
        self.assertEqual([p["thread_key"] for p in pending], ["em:" + token])
        with open(pending[0]["packet_path"], "r", encoding="utf-8") as fh:
            packet = json.load(fh)
        self.assertIn("happy to talk", packet["text"])
        rec = {"inbound_id": pending[0]["inbound_id"], "thread_key": "em:" + token, "class": "positive",
               "summary": "Happy to talk on Thursday.", "received_at": packet["received_at"], "msg_ref": None}
        rr = w.ok(["reply", "record", "--file", w.wfile("outreach", "%s/reply.json" % cyc, rec)], OU, cycle=cyc)
        self.assertIn("thread_replied", rr["effects"])
        self.assertIn("review_task", rr["effects"])
        w.end_cycle(cyc, OU)
        th = w.one("SELECT state, reply_class, needs_human FROM threads WHERE thread_key = ?", "em:" + token)
        self.assertEqual(th[:], ("replied", "positive", 1))
        self.assertEqual(w.all("SELECT kind FROM human_tasks WHERE done_at IS NULL AND kind = 'review_reply'"),
                         [("review_reply",)])
        self.assertEqual(w.one("SELECT contact_state FROM companies WHERE display_name = 'Kestrel Commerce'")[0],
                         "active_thread")
        followups = sheets_rows.build_rows(w.conn, "followups", None)
        self.assertEqual(followups[0][1]["reply_type"], "Positive reply")
        self.assertEqual(followups[0][1]["what_they_said"], "Happy to talk on Thursday.")

        # the owner hears about it through `openclaw message send` (the fake refuses options the real one lacks)
        n = w.ok(["notify", "flush", "--deliver"])
        self.assertTrue(n["sent"], n)
        self.assertGreaterEqual(n["delivered"], 1)
        with open(oc_log, "r", encoding="utf-8") as fh:
            calls = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["profile"], "jhtest")
        opts = calls[0]["options"]
        self.assertEqual((opts["--channel"], opts["--target"], opts.get("--json")), ("whatsapp", OWNER_CHAT, True))
        self.assertIn("Jordan Blake at Kestrel Commerce replied", opts["--message"])
        undelivered = w.all("SELECT dedupe_key FROM notifications WHERE priority = 'high' AND delivered_at IS NULL")
        self.assertEqual(undelivered, [])
        rc, again = w.run(["notify", "flush", "--deliver"])                               # nothing twice
        self.assertEqual((rc, again["code"], again["data"]["delivered"]), (0, "NOTHING_TO_DO", 0), again)
        with open(oc_log, "r", encoding="utf-8") as fh:
            self.assertEqual(len([line for line in fh if line.strip()]), 1)

    def test_fake_openclaw_refuses_options_the_real_cli_lacks(self):
        log = self.w.install_fake_openclaw()
        fake = os.path.join(os.path.dirname(log), "openclaw")
        good = ["message", "send", "--channel", "whatsapp", "--target", "+10000000000", "--message=-starts with a dash",
                "--json"]
        for argv, ok in ((good, True), (["--profile", "jhtest"] + good, True),
                         (["message", "send", "--to", "+10000000000", "-m", "x"], False),
                         (["message", "send", "--channel", "whatsapp", "--target", "x", "--text", "x"], False),
                         (["message", "send", "--channel", "whatsapp", "--target", "x", "--message-file", "f"], False),
                         (["message", "send", "--channel", "whatsapp", "--message", "x"], False),
                         (["message", "send", "--channel", "fax", "--target", "x", "-m", "x"], False),
                         (["message", "post", "--target", "x", "-m", "x"], False)):
            p = subprocess.run([sys.executable, fake] + argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=self.w.home.dir)
            self.assertEqual(p.returncode == 0, ok, (argv, p.stderr))
        with open(log, "r", encoding="utf-8") as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([c["options"]["--message"] for c in logged], ["-starts with a dash"] * 2)
        self.assertEqual([c["profile"] for c in logged], [None, "jhtest"])


class TestAuditWithSlowImap(E2EMailBase):
    def test_audit_run_reads_imap_without_the_write_lock(self):
        w = self.w
        w.connect_mail()
        co = insert_company(w.conn, name="Heron Freight", domain="heron.example")
        insert_contact(w.conn, company_id=co, full_name="Morgan Hale", email="morgan.hale@heron.example")
        w.conn.commit()
        # a message in Sent that the ledger does not know: the audit must find it and stop everything
        m = inbound("Sam Lee <%s>" % OWNER, "morgan.hale@heron.example", "Quick question", "Hi Morgan, a note.",
                    date="2026-09-29T07:00:00Z")
        uid = int(w.imap.add_message(m))
        w.imap.match("in:sent", [uid])
        wrappers = []

        def wrap(client):
            s = SlowImap(client, w)
            wrappers.append(s)
            return s
        w.imap_wrap = wrap
        res = w.ok(["audit", "run"])
        probes = [p for s in wrappers for p in s.probes]
        self.assertTrue(any(name == "search" for name, _free in probes), probes)
        self.assertTrue(any(name == "fetch_headers" for name, _free in probes), probes)
        self.assertEqual([name for name, free in probes if not free], [], "the write lock was held during IMAP")
        self.assertEqual([x["kind"] for x in res["mismatches"]], ["unledgered_email"], res)
        self.assertIn("morgan.hale@heron.example", res["mismatches"][0]["detail"])
        self.assertTrue(any(q.startswith("in:sent after:") for q in w.imap.queries), w.imap.queries)
        self.assertEqual(w.one("SELECT state, reason_code FROM breakers WHERE scope = 'global'")[:], ("open", "audit_mismatch"))
        self.assertEqual(w.all("SELECT kind FROM human_tasks WHERE done_at IS NULL"), [("review_audit_mismatch",)])
        self.assertIsNotNone(w.one("SELECT value FROM meta WHERE key = 'audit_last_at'"))


if __name__ == "__main__":
    unittest.main()
