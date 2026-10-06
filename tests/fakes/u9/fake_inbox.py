"""One fake mailbox for both email routes (U9 tests and INT): the same messages are served over IMAP (the scripted
FakeImapServer, route app_password) and as Gmail web pages through the fake CDP server (route web_ui).

    inbox = FakeInbox().attach_imap(imap_server)          # optional
    fake_cdp.inbox = inbox                                  # web route
    inbox.add(sender="no-reply@myworkday.com", to="sam.lee@example.com", subject="Verify your email",
              text="Your code is 483920", received_at=clock.now())

Fictional senders, recipients and codes only.
"""
from __future__ import annotations

import datetime as _dt
import re
from email.message import EmailMessage


def _imap_date(ts: str) -> str:
    d = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    return d.strftime("%d-%b-%Y %H:%M:%S +0000")


class FakeInbox:
    def __init__(self):
        self.messages: list = []
        self.read: list = []
        self.imap = None
        self._n = 0

    def attach_imap(self, srv) -> "FakeInbox":
        self.imap = srv
        for m in self.messages:
            self._to_imap(m)
        return self

    def add(self, *, sender: str, to, subject: str, received_at: str, text: str = "", html: str | None = None,
            links: list | None = None) -> dict:
        self._n += 1
        tid = "18f%013dab" % self._n
        m = {"thread_id": tid, "from": sender.lower(), "to": [to] if isinstance(to, str) else list(to),
             "subject": subject, "text": text, "html": html, "links": list(links or []), "received_at": received_at,
             "message_id": "<fake-%d@mail.example.com>" % self._n}
        self.messages.append(m)
        if self.imap is not None:
            self._to_imap(m)
        return m

    def _to_imap(self, m: dict) -> None:
        msg = EmailMessage()
        msg["From"] = m["from"]
        msg["To"] = ", ".join(m["to"])
        msg["Subject"] = m["subject"]
        msg["Message-ID"] = m["message_id"]
        msg["Date"] = _dt.datetime.strptime(m["received_at"], "%Y-%m-%dT%H:%M:%SZ").strftime(
            "%a, %d %b %Y %H:%M:%S +0000")
        html = m.get("html")
        if m.get("links") and not html:
            html = "<p>%s</p>" % m["text"].replace("\n", "<br>") + "".join(
                '<p><a href="%s">%s</a></p>' % (x["href"], x.get("text") or "Verify") for x in m["links"])
        msg.set_content(m["text"] or " ")
        if html:
            msg.add_alternative(html, subtype="html")
        uid = self.imap.add_message(msg.as_bytes(), internaldate=_imap_date(m["received_at"]))
        m["uid"] = uid
        self.imap.match("from:(", [int(uid)])

    def search(self, query: str) -> list:
        """Messages whose sender domain is named in a 'from:(a OR b)' query, newest first."""
        mm = re.search(r"from:\(([^)]*)\)", query or "")
        doms = [d.strip().lower() for d in mm.group(1).split(" OR ")] if mm else []
        out = []
        for m in self.messages:
            dom = m["from"].rsplit("@", 1)[-1]
            if any(dom == d or dom.endswith("." + d) for d in doms):
                out.append(m)
        return sorted(out, key=lambda m: m["received_at"], reverse=True)

    def by_thread(self, tid: str):
        for m in self.messages:
            if m["thread_id"] == tid:
                return m
        return None
