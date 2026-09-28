"""IMAP client against scripted responses from a fake Gmail IMAP server on 127.0.0.1 (U9)."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter.mail import MailError
from jobhunter.mail.imap import parse_headers, quote, text_of
from tests.fakes.u9 import inbound
from tests.fakes.u9.imap_server import CONTINUATION, PASSWORD, FakeImapServer


class ImapTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeImapServer().start()
        self.addCleanup(self.srv.stop)

    def _open(self, **kw):
        c = self.srv.client(**kw)
        c.open()
        self.addCleanup(c.close)
        return c

    def test_open_examines_all_mail_read_only(self):
        c = self._open()
        self.assertTrue(c.gmail)
        self.assertEqual(c.all_mail, "[Gmail]/All Mail")
        self.assertIn("EXAMINE", self.srv.commands)
        self.assertNotIn("SELECT", self.srv.commands)
        self.assertIn("LOGIN <redacted>", self.srv.commands)

    def test_localized_all_mail_found_by_flag(self):
        self.srv.folders = [("\\HasNoChildren", "INBOX"), ("\\All \\HasNoChildren", "[Gmail]/Tous les messages")]
        c = self._open()
        self.assertEqual(c.all_mail, "[Gmail]/Tous les messages")
        self.assertEqual(self.srv.selected, ["[Gmail]/Tous les messages"])

    def test_auth_failure_flags_breaker(self):
        c = self.srv.client(password="zzzzzzzzzzzzzzzz")
        with self.assertRaises(MailError) as cm:
            c.open()
        self.assertTrue(cm.exception.auth_failed)
        self.assertEqual(cm.exception.code, "E_MAIL_TRANSPORT")
        self.assertIn("AUTHENTICATIONFAILED", cm.exception.reply)
        self.assertNotIn(PASSWORD, repr(c) + str(cm.exception) + repr(cm.exception.data))

    def test_search_quotes_gmail_queries(self):
        q = 'in:sent ("kestrel commerce" OR kestrelcommerce OR kestrel.example)'
        self.srv.on(q, [4, 2])
        c = self._open()
        self.assertEqual(c.search(q), ["2", "4"])
        self.assertEqual(c.count(q), 2)
        self.assertEqual(c.count("in:sent to:nobody@example.com"), 0)
        self.assertEqual(c.count(None), 0)
        self.assertEqual(self.srv.queries[0], q)
        self.assertEqual(quote('a "b" \\c'), '"a \\"b\\" \\\\c"')

    def test_non_ascii_query_goes_as_literal(self):
        q = 'in:sent ("caf\u00e9 m\u00fcller" OR cafemuller)'
        self.srv.on(q, [9])
        c = self._open()
        self.assertEqual(c.search(q), ["9"])
        self.assertEqual(self.srv.literal_queries, [q])

    def test_literal_continuation_is_a_neutral_placeholder(self):
        self.assertEqual(CONTINUATION, b"+ Ready for literal data\r\n")
        q = 'in:sent "r\u00e9sum\u00e9"'
        self.srv.on(q, [2])
        c = self._open()
        self.assertEqual(c.search(q), ["2"])
        self.assertEqual(c._m.continuation_response, b"Ready for literal data")

    def test_search_refused_is_mail_error(self):
        self.srv.search_fail["in:sent to:x@example.com"] = "[CANNOT] bad query"
        c = self._open()
        with self.assertRaises(MailError):
            c.search("in:sent to:x@example.com")

    def test_not_gmail_refuses_searches(self):
        self.srv.gmail = False
        c = self._open()
        self.assertFalse(c.gmail)
        with self.assertRaises(MailError):
            c.search("in:sent")

    def test_fetch_headers_and_text(self):
        raw = inbound("Alex Rivera <Alex.Rivera@Kestrel.example>", "sam.lee.sender@example.com",
                      "Re: Caf\u00e9 question", "Hi Sam,\n\nHappy to talk next week.\n\n> quoted history",
                      date="2026-09-29T10:15:00Z", msg_id="<reply1@kestrel.example>",
                      in_reply_to="<TABCDEFGHJKL@jobhunter.invalid>")
        uid = self.srv.add_message(raw, uid=7, msgid=0x18c2f, thrid=77)
        c = self._open()
        heads = c.fetch_headers([uid])
        self.assertEqual(len(heads), 1)
        h = heads[0]
        self.assertEqual(h["uid"], "7")
        self.assertEqual(h["msg_ref"], "gm:18c2f")
        self.assertEqual(h["gm_thrid"], "77")
        self.assertEqual(h["from_addr"], "alex.rivera@kestrel.example")
        self.assertEqual(h["subject"], "Re: Caf\u00e9 question")
        self.assertEqual(h["date"], "2026-09-29T10:15:00Z")
        self.assertEqual(h["in_reply_to"], ["<TABCDEFGHJKL@jobhunter.invalid>"])
        self.assertEqual(h["message_id"], "<reply1@kestrel.example>")
        self.assertIn("BODY.PEEK[HEADER]", self.srv.fetches[0][1])
        text = c.fetch_text(uid)
        self.assertIn("Happy to talk next week.", text)
        self.assertIn("BODY.PEEK[]<0.65536>", self.srv.fetches[-1][1])

    def test_fetch_many_in_order(self):
        for i in range(3):
            self.srv.add_message(inbound("a%d@kestrel.example" % i, "sam.lee.sender@example.com", "S%d" % i, "b"))
        c = self._open()
        heads = c.fetch_headers(["3", "1", "2", "99"])
        self.assertEqual([h["uid"] for h in heads], ["1", "2", "3"])

    def test_thread_uids(self):
        uid = self.srv.add_message(inbound("sam.lee.sender@example.com", "alex.rivera@kestrel.example", "Hello", "x"),
                                   thrid=4242)
        self.srv.on("X-GM-THRID 4242", [1, 5])
        c = self._open()
        self.assertEqual(c.thread_uids(uid), ["1", "5"])

    def test_connection_drop_is_mail_error(self):
        c = self._open()
        self.srv.drop_on = "UID"
        with self.assertRaises(MailError):
            c.search("in:sent")

    def test_parse_helpers(self):
        h = parse_headers(b"From: Mailer <MAILER-DAEMON@mail.example.com>\r\nSubject: Delivery Status Notification "
                          b"(Failure)\r\nAuto-Submitted: auto-replied\r\nDate: bad date\r\n\r\n",
                          {"uid": "3", "internaldate": "28-Sep-2026 06:02:00 +0000"})
        self.assertEqual(h["from_addr"], "mailer-daemon@mail.example.com")
        self.assertEqual(h["auto_submitted"], "auto-replied")
        self.assertEqual(h["date"], "2026-09-28T06:02:00Z")
        self.assertEqual(h["msg_ref"], "uid:3")
        from email import message_from_string, policy
        html = message_from_string("Content-Type: text/html; charset=utf-8\n\n<p>Hello&nbsp;<b>there</b></p>"
                                   "<script>x()</script>", policy=policy.default)
        self.assertEqual(text_of(html), "Hello there")


if __name__ == "__main__":
    unittest.main()
