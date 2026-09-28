"""SMTP stage handling against a fake server on 127.0.0.1 (U9, design 2.3.1 step 4)."""
from __future__ import annotations

import io
import json
import os
import stat
import unittest

import tests  # noqa: F401
from jobhunter import canon, cli, db, mail, paths
from jobhunter.errors import Denied
from jobhunter.mail import mime
from jobhunter.mail.smtp import SmtpSender, reply_line, single_recipient
from tests.fakes.u9 import APP_PW, OWNER, MailTestCase
from tests.fakes.u9.smtp_server import ACCOUNT, DATA_READY, PASSWORD, FakeSmtpServer


def _msg(token="TABCDEFGHJKL", to="alex.rivera@kestrel.example"):
    text = canon.canonical_send_text("cold_email", "Hello from Sam", "Hi Alex,\n\n.A line with a leading dot.\n\nThanks,",
                                     None, "Sam Lee")
    return mime.build_message({"subject": None, "body": None}, {"kind": "cold_email", "token": token, "recipient": to},
                              {"address": ACCOUNT, "name": "Sam Lee"}, None, send_text=text,
                              date="2026-09-29T09:00:00Z")


class SmtpTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeSmtpServer().start()
        self.addCleanup(lambda: self.srv.stop())
        self.sender = self.srv.sender(timeout=1.0)

    def test_sent_and_bytes_on_the_wire(self):
        calls = []
        msg = _msg()
        res = self.sender.send(msg, before_data=lambda: calls.append(self.srv.data_commands))
        self.assertTrue(res["ok"])
        self.assertEqual(res["outcome"], "sent")
        self.assertEqual(res["reply_code"], 250)
        self.assertEqual(calls, [0], "before_data runs before the DATA command")
        self.assertEqual(self.srv.envelope_to, ["<alex.rivera@kestrel.example>"])
        self.assertEqual(self.srv.envelope_from, ["<%s>" % ACCOUNT])
        self.assertEqual(len(self.srv.messages), 1)
        self.assertEqual(self.srv.messages[0].rstrip(b"\r\n"), mime.wire_bytes(msg).rstrip(b"\r\n"))
        self.assertIn("AUTH PLAIN <redacted>", self.srv.transcript)
        self.assertEqual(self.srv.transcript[0].lower(), "ehlo [127.0.0.1]", "no machine name in the EHLO")
        self.assertNotIn(PASSWORD, "\n".join(self.srv.transcript))

    def test_reject_at_rcpt_is_before_data(self):
        self.srv.script["rcpt"] = (550, "5.1.1 The email account that you tried to reach does not exist")
        called = []
        res = self.sender.send(_msg(), before_data=lambda: called.append(1))
        self.assertEqual(res["outcome"], "rejected_before_data")
        self.assertEqual(res["stage_reached"], "rcpt")
        self.assertEqual(res["reply_code"], 550)
        self.assertFalse(res["data_started"])
        self.assertEqual(called, [], "never armed")
        self.assertEqual(self.srv.data_commands, 0)
        self.assertIn("RSET", [c.upper() for c in self.srv.transcript])

    def test_reject_at_mail_from(self):
        self.srv.script["mail"] = (421, "4.7.0 Try again later, closing connection")
        res = self.sender.send(_msg())
        self.assertEqual(res["outcome"], "rejected_before_data")
        self.assertEqual(res["stage_reached"], "mail_from")
        self.assertEqual(res["reply_code"], 421)

    def test_auth_failure(self):
        res = self.srv.sender(password="zzzzzzzzzzzzzzzz").send(_msg())
        self.assertEqual(res["outcome"], "rejected_before_data")
        self.assertEqual(res["stage_reached"], "auth")
        self.assertEqual(res["reply_code"], 535)
        self.assertTrue(res["auth_failed"])
        self.assertEqual(self.srv.envelope_from, [])

    def test_5xx_at_data_end(self):
        self.srv.script["data_end"] = (552, "5.7.0 Our system detected an illegal attachment")
        res = self.sender.send(_msg(), before_data=lambda: None)
        self.assertEqual(res["outcome"], "rejected_at_data")
        self.assertTrue(res["data_started"])
        self.assertEqual(res["reply_code"], 552)
        self.assertEqual(self.srv.messages, [])

    def test_data_command_default_reply_is_rfc_text(self):
        import smtplib
        self.assertEqual(DATA_READY, (354, "End data with <CR><LF>.<CR><LF>"))
        s = smtplib.SMTP("127.0.0.1", self.srv.port, local_hostname="[127.0.0.1]", timeout=2)
        try:
            s.ehlo()
            s.mail("<%s>" % ACCOUNT)
            s.rcpt("<alex.rivera@kestrel.example>")
            s.putcmd("data")
            self.assertEqual(s.getreply(), (354, b"End data with <CR><LF>.<CR><LF>"))
            s.send(b"Subject: t\r\n\r\nbody\r\n.\r\n")
            self.assertEqual(s.getreply()[0], 250)
        finally:
            s.close()
        self.assertEqual(self.srv.data_commands, 1)
        self.assertEqual(len(self.srv.messages), 1)

    def test_5xx_to_data_command(self):
        self.srv.script["data_cmd"] = (554, "5.5.1 Error: no valid recipients")
        res = self.sender.send(_msg())
        self.assertEqual(res["outcome"], "rejected_at_data")
        self.assertEqual(res["reply_code"], 554)
        self.assertEqual(self.srv.received, [])

    def test_disconnect_after_data_is_unknown(self):
        self.srv.script["data_end"] = "drop"
        res = self.sender.send(_msg())
        self.assertEqual(res["outcome"], "unknown")
        self.assertTrue(res["data_started"])
        self.assertEqual(len(self.srv.received), 1, "the server got the bytes but never answered")

    def test_timeout_after_data_is_unknown(self):
        self.srv.script["data_end"] = "hang"
        res = self.sender.send(_msg())
        self.assertEqual(res["outcome"], "unknown")

    def test_before_data_failure_aborts(self):
        def boom():
            raise RuntimeError("database is busy")
        res = self.sender.send(_msg(), before_data=boom)
        self.assertEqual(res["outcome"], "aborted_before_data")
        self.assertEqual(self.srv.data_commands, 0)
        self.assertEqual(self.srv.received, [])

    def test_connection_refused(self):
        port = self.srv.port
        self.srv.stop()
        res = SmtpSender(ACCOUNT, PASSWORD, host="127.0.0.1", port=port, use_ssl=False, timeout=0.5).send(_msg())
        self.assertEqual(res["outcome"], "rejected_before_data")
        self.assertEqual(res["stage_reached"], "connect")
        self.srv = FakeSmtpServer().start()   # for the cleanup

    def test_check_login(self):
        self.assertTrue(self.sender.check_login()["ok"])
        bad = self.srv.sender(password="zzzzzzzzzzzzzzzz").check_login()
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["auth_failed"])
        self.assertEqual(self.srv.envelope_from, [])

    def test_one_recipient_only(self):
        msg = _msg()
        msg["Cc"] = "someone@example.com"
        with self.assertRaises(ValueError):
            single_recipient(msg)
        res = self.sender.send(msg)
        self.assertEqual(res["outcome"], "aborted_before_data")
        self.assertEqual(self.srv.connections, 0)

    def test_repr_hides_password(self):
        self.assertNotIn(PASSWORD, repr(self.sender))
        self.assertEqual(reply_line(250, b"2.0.0  OK\r\n"), "250 2.0.0 OK")


class ConnectTests(MailTestCase):
    """mail connect, credential stores and mail test (the Keychain is a fake runner; the real one is never used)."""
    connect = False

    def secrets(self):
        with open(os.path.join(paths.private_dir(), "secrets.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_connect_file_store(self):
        with open(os.path.join(paths.private_dir(), "secrets.json"), "w", encoding="utf-8") as fh:
            json.dump({"sheet_secret": "0" * 64}, fh)
        with db.tx(self.conn):
            db.open_human_task(self.conn, "connect_mail", "Connect Gmail")
        res = mail.connect(self.conn, OWNER, "abcd efgh ijkl mnop", store="file")
        self.assertEqual((res["store"], res["smtp_ok"], res["imap_ok"]), ("file", True, True))
        s = self.secrets()
        self.assertEqual(s["mail_app_password"], APP_PW)
        self.assertEqual(s["mail_account"], OWNER)
        self.assertEqual(s["sheet_secret"], "0" * 64, "other secrets are kept")
        mode = stat.S_IMODE(os.stat(os.path.join(paths.private_dir(), "secrets.json")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertIsNotNone(db.meta_get(self.conn, "mail_connected_at"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM human_tasks WHERE done_at IS NULL").fetchone()[0], 0)
        self.assertEqual(mail.credentials().password, APP_PW)
        self.assertNotIn(APP_PW, repr(mail.credentials()) + str(mail.credentials()))
        self.assertEqual(self.smtp.envelope_from, [], "connect sends nothing")
        self.assertNotIn("UID", self.imap.commands, "connect reads no message")

    def test_connect_with_a_bad_password_stores_nothing(self):
        with self.assertRaises(Denied) as cm:
            mail.connect(self.conn, OWNER, "zzzz zzzz zzzz zzzz", store="file")
        self.assertEqual(cm.exception.code, "E_MAIL_TRANSPORT")
        self.assertFalse(os.path.exists(os.path.join(paths.private_dir(), "secrets.json")))
        self.assertIsNone(db.meta_get(self.conn, "mail_connected_at"))
        self.assertNotIn("zzzz", json.dumps(cm.exception.data))

    def test_app_password_shape(self):
        for bad in ("", "short", "abcd efgh ijkl mno1", "abcdefghijklmnopq"):
            with self.assertRaises(Denied):
                mail.normalize_app_password(bad)
        self.assertEqual(mail.normalize_app_password(" ABCD efgh IJKL mnop "), APP_PW)

    def test_keychain_store_never_puts_the_password_in_argv(self):
        calls = []
        items = {}

        def runner(args, stdin):
            calls.append((list(args), stdin))
            if args == ["-i"]:
                words = stdin.decode().split()
                items[(words[words.index("-a") + 1], words[words.index("-s") + 1])] = \
                    bytes.fromhex(words[words.index("-X") + 1]).decode()
                return 0, b""
            if args[0] == "find-generic-password":
                key = (args[args.index("-a") + 1], args[args.index("-s") + 1])
                return (0, (items[key] + "\n").encode()) if key in items else (44, b"")
            return 1, b""
        mail._security_runner = runner
        self.addCleanup(setattr, mail, "_security_runner", None)
        self.assertEqual(mail.store_credentials(OWNER, APP_PW, "keychain"), "keychain")
        for args, _stdin in calls:
            self.assertNotIn(APP_PW, " ".join(args))
        self.assertNotIn(APP_PW.encode(), calls[0][1], "hex-encoded on stdin")
        self.assertNotIn("mail_app_password", self.secrets())
        self.assertEqual(self.secrets()["mail_store"], "keychain")
        self.assertEqual(mail.credentials().password, APP_PW)
        self.assertTrue(mail.keychain_service().startswith("openclaw-job-hunter."))

    def test_keychain_failure_falls_back_to_the_file(self):
        mail._security_runner = lambda args, stdin: (1, b"")
        self.addCleanup(setattr, mail, "_security_runner", None)
        self.assertEqual(mail.store_credentials(OWNER, APP_PW, "keychain"), "file")
        self.assertEqual(self.secrets()["mail_app_password"], APP_PW)

    def test_mail_test_command(self):
        out = io.StringIO()
        rc = cli.main(["mail", "test"], env={}, stdin=io.StringIO(""), stdout=out)
        self.assertEqual(rc, 11, out.getvalue())
        mail.store_credentials(OWNER, APP_PW, "file")
        out = io.StringIO()
        rc = cli.main(["mail", "test"], env={}, stdin=io.StringIO(""), stdout=out)
        env = json.loads(out.getvalue())
        self.assertEqual(rc, 0, env)
        self.assertEqual((env["data"]["smtp_ok"], env["data"]["imap_ok"], env["data"]["account"]), (True, True, OWNER))
        self.assertNotIn(APP_PW, out.getvalue())
        self.smtp.password = "yyyyyyyyyyyyyyyy"
        out = io.StringIO()
        self.assertEqual(cli.main(["mail", "test"], env={}, stdin=io.StringIO(""), stdout=out), 12)

    def test_connect_is_human_only(self):
        out = io.StringIO()
        rc = cli.main(["mail", "connect"], env={}, stdin=io.StringIO(""), stdout=out)
        self.assertEqual(json.loads(out.getvalue())["code"], "E_HUMAN_ONLY")
        self.assertEqual(rc, 11)


if __name__ == "__main__":
    unittest.main()
