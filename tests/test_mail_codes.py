"""U9: reading email codes and sign-in links (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.4) on both routes from one fake
mailbox: IMAP (search, INTERNALDATE window, the \\Seen and archive flags, a refused login trips gmail) and Gmail on
the web through the fake CDP server (search page, conversation, links, the tab closed after, a Gmail security page
trips gmail)."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import canon, cdp, db, mail
from jobhunter.errors import Denied
from jobhunter.mail import codes, webcodes
from tests.fakes.u6.fake_cdp import FakeCdp
from tests.fakes.u9.fake_inbox import FakeInbox
from tests.fakes.u9.imap_server import FakeImapServer
from tests.helpers import HomeTestCase

OWNER = "sam.lee@example.com"
PW = "abcdefghijklmnop"
QUERY = "from:(myworkday.com OR workday.com) newer_than:1d"


class TestImapCodes(HomeTestCase):
    start_ts = "2026-10-06T09:00:00Z"

    def setUp(self):
        super().setUp()
        self.srv = FakeImapServer(OWNER, PW).start()
        self.addCleanup(self.srv.stop)
        mail.set_test_transport(imap=lambda c: self.srv.client(c.account, c.password))
        self.addCleanup(mail.clear_test_transport)
        mail.store_credentials(OWNER, PW, "file")
        self.inbox = FakeInbox().attach_imap(self.srv)

    def test_search_and_window(self):
        r = canon.now()
        self.inbox.add(sender="no-reply@myworkday.com", to=OWNER, subject="old", text="Code 111111",
                       received_at=canon.ts_add(r, seconds=-60))
        self.inbox.add(sender="no-reply@myworkday.com", to=OWNER, subject="new", text="Code 483920",
                       received_at=canon.ts_add(r, seconds=30))
        msgs = codes.search(self.conn, QUERY, r, canon.ts_add(r, minutes=10))
        self.assertEqual([m["subject"] for m in msgs], ["new"])
        self.assertIn("483920", msgs[0]["text"])
        self.assertEqual(msgs[0]["received_at"], canon.ts_add(r, seconds=30))
        self.assertEqual(self.srv.queries, [QUERY])
        self.assertEqual(self.srv.stores, [])          # reading never writes

    def test_after_use_flags(self):
        m = self.inbox.add(sender="no-reply@myworkday.com", to=OWNER, subject="x", text="Code 483920",
                           received_at=canon.now())
        codes.after_use(m["uid"], "mark_read")
        self.assertEqual(self.srv.stores, [(m["uid"], "+FLAGS", "(\\Seen)")])
        codes.after_use(m["uid"], "archive")
        self.assertEqual(self.srv.stores[1:], [(m["uid"], "+FLAGS", "(\\Seen)"), (m["uid"], "-X-GM-LABELS",
                                                                                 "(\\Inbox)")])
        codes.after_use(m["uid"], "leave")
        self.assertEqual(len(self.srv.stores), 3)

    def test_refused_login_trips_gmail(self):
        self.srv.login_fail_text = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
        with self.assertRaises(Denied):
            with db.tx(self.conn):
                codes.search(self.conn, QUERY, canon.now(), canon.now())
        row = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'gmail'").fetchone()
        self.assertEqual(tuple(row), ("open", "gmail_auth_failed"))


class TestWebCodes(HomeTestCase):
    start_ts = "2026-10-06T09:00:00Z"

    def setUp(self):
        super().setUp()
        self.f = FakeCdp().start()
        self.addCleanup(self.f.stop)
        cdp.set_test_port(self.f.port)
        self.addCleanup(cdp.set_test_port, None)
        self.sleep = cdp.SLEEP
        cdp.SLEEP = lambda s: None
        self.addCleanup(setattr, cdp, "SLEEP", self.sleep)
        self.inbox = FakeInbox()
        self.f.inbox = self.inbox

    def test_search_reads_in_process_and_closes_the_tab(self):
        link = "https://kestrel.wd5.myworkdayjobs.com/verify?t=FIXTURETOKEN123"
        self.inbox.add(sender="no-reply@myworkday.com", to=OWNER, subject="Verify", text="Confirm your email",
                       links=[{"href": link, "text": "Verify Email"}], received_at=canon.ts_add(canon.now(), seconds=40))
        self.inbox.add(sender="news@kestrel.example", to=OWNER, subject="Newsletter", text="x",
                       received_at=canon.now())
        msgs = webcodes.search(self.conn, QUERY, canon.now())
        self.assertEqual([(m["from"], m["links"][0]["href"]) for m in msgs], [("no-reply@myworkday.com", link)])
        self.assertEqual(msgs[0]["received_at"], canon.now()[:17] + "00Z")
        self.assertEqual(self.f.tabs, {})                        # the Gmail tab was closed
        scripts = [e.get("script") for e in self.f.log if e["method"] == "Runtime.evaluate"]
        self.assertIn("read_gmail_list", scripts)
        self.assertIn("read_gmail_message", scripts)
        self.assertIn("GMAIL_LINKS", scripts)

    def test_gmail_security_page_trips_gmail(self):
        self.inbox.add(sender="no-reply@myworkday.com", to=OWNER, subject="Code", text="483920",
                       received_at=canon.now())
        self.f.gmail_security = True
        with self.assertRaises(Denied) as cm:
            webcodes.search(self.conn, QUERY, canon.now())
        self.assertEqual(cm.exception.code, "E_STOP_DETECTED")
        row = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'gmail'").fetchone()
        self.assertEqual(tuple(row), ("open", "gmail_security"))
        self.assertEqual(self.inbox.read, [])

    def test_archive_presses_e_on_the_conversation(self):
        m = self.inbox.add(sender="no-reply@myworkday.com", to=OWNER, subject="Code", text="483920",
                           received_at=canon.now())
        webcodes.after_use("https://mail.google.com/mail/u/0/#all/" + m["thread_id"], "archive")
        self.assertEqual(self.f.archived, [m["thread_id"]])
        webcodes.after_use("https://evil.example/#all/x", "archive")
        self.assertEqual(len(self.f.archived), 1)


if __name__ == "__main__":
    unittest.main()
