"""U9 test support: fake SMTP and IMAP servers on 127.0.0.1, a test case that wires them in as the mail
transport, and small builders for inbound messages. Nothing here reaches a real mail server or the Keychain."""
from __future__ import annotations

import json
import os
from email.message import EmailMessage
from email.utils import format_datetime

import tests  # noqa: F401
from jobhunter import canon, db, mail
from tests.helpers import HomeTestCase

from .imap_server import FakeImapServer
from .smtp_server import FakeSmtpServer

OWNER = "sam.lee.sender@example.com"
APP_PW = "abcdefghijklmnop"
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fixtures",
                        "mail")
TUESDAY = "2026-09-29T09:00:00Z"


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def write_config(home, **gmail) -> dict:
    """private/config.json with the owner fields, wide-open send windows (the test home runs in UTC) and the
    app_password route; pass route="web_ui" for the browser route."""
    path = os.path.join(home.dir, "private", "config.json")
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "config.example.json"), "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["timezone"] = "UTC"
    cfg["owner"].update({"first_name": "Sam", "last_name": "Lee", "gmail_address": OWNER,
                         "signature": {"full_name": "Sam Lee", "phone": "", "links": ["https://example.com/sam"]}})
    g = cfg["gmail"]
    # the code-owned route (app_password) unless a test asks for the default browser route (web_ui)
    g.update({"route": "app_password", "active_days": [1, 2, 3, 4, 5, 6, 7], "sender_window": ["00:00", "23:59"],
              "recipient_window": ["00:00", "23:59"]})
    g.update(gmail)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return cfg


def inbound(frm: str, to: str, subject: str, body: str, date: str = "2026-09-29T10:00:00Z",
            msg_id: str | None = None, in_reply_to: str | None = None, headers: dict | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = format_datetime(canon.parse_ts(date))
    m["Message-ID"] = msg_id or "<%s@mail.example.com>" % canon.new_uid("M", 10)
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return bytes(m)


class MailTestCase(HomeTestCase):
    """HomeTestCase + fake SMTP and IMAP servers + stored test credentials (file store) + config."""
    start_ts = TUESDAY
    connect = True

    def setUp(self):
        super().setUp()
        self.smtp = FakeSmtpServer(OWNER, APP_PW).start()
        self.imap = FakeImapServer(OWNER, APP_PW).start()
        self.addCleanup(self.smtp.stop)
        self.addCleanup(self.imap.stop)
        mail.set_test_transport(smtp=lambda c: self.smtp.sender(c.account, c.password),
                                imap=lambda c: self.imap.client(c.account, c.password))
        self.addCleanup(mail.clear_test_transport)
        self.cfg = write_config(self.home)
        if self.connect:
            mail.store_credentials(OWNER, APP_PW, "file")
            with db.tx(self.conn):
                db.meta_set(self.conn, "mail_connected_at", canon.now(), "human")
