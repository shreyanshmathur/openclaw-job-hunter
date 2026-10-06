"""Email codes and sign-in links over IMAP, route app_password (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.4) [U9].

search(): EXAMINE All Mail (the client's read-only select), UID SEARCH X-GM-RAW "from:(<domains>) newer_than:1d",
keep messages whose INTERNALDATE lies between the request time and the window end, fetch at most 5 of the newest
(headers, text/plain and text/html, 64 KB each) and hand them to jobhunter.otp, which applies every rule. Nothing
here decides what a code is, and nothing is written anywhere: the messages stay in this process.

after_use(): the only IMAP write of the package: UID STORE +FLAGS (\\Seen) for mark_read, and also
-X-GM-LABELS (\\Inbox) for archive, on the one used message (otp.after_use).

An IMAP login refused for the app password trips the gmail breaker (gmail_auth_failed), as the mailer does.
"""
from __future__ import annotations

from . import MailError, imap_client
from ..errors import Denied

MAX_MESSAGES = 5
MAX_BYTES = 65536


def _parts(msg) -> tuple:
    """(text/plain, text/html) of a parsed message, each at most MAX_BYTES characters."""
    plain, html = "", ""
    if msg is None:
        return plain, html
    try:
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                text = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode("utf-8", "replace")
            if ctype == "text/plain" and not plain:
                plain = str(text)[:MAX_BYTES]
            elif ctype == "text/html" and not html:
                html = str(text)[:MAX_BYTES]
    except Exception:
        pass
    return plain, html


def _auth_trip(conn, e) -> None:
    if conn is None or not getattr(e, "auth_failed", False):
        return
    from .. import breakers, db

    def _trip(c):
        breakers.trip(c, "gmail", "gmail_auth_failed", "IMAP login refused while reading an email code", by="code")
    db.defer_write(conn, _trip)


def search(conn, query: str, requested_at: str, window_ends_at: str) -> list:
    """[{id, uid, from, to, subject, received_at, text, html}] newest first (at most 5)."""
    try:
        client = imap_client()
    except Denied:
        raise Denied("E_ROUTE_UNAVAILABLE", "the mailbox is not connected (./jobhunter mail connect)",
                     data={"reason": "mail_not_connected"})
    out = []
    try:
        with client as c:
            uids = c.search(query)
            heads = c.fetch_headers(uids[-50:]) if uids else []
            keep = [h for h in heads if h.get("internaldate") and requested_at <= h["internaldate"] <= window_ends_at]
            keep.sort(key=lambda h: h["internaldate"], reverse=True)
            for h in keep[:MAX_MESSAGES]:
                plain, html = _parts(c.fetch_message(h["uid"], MAX_BYTES))
                out.append({"id": h.get("msg_ref") or h.get("message_id") or "uid:%s" % h["uid"], "uid": h["uid"],
                            "from": h.get("from_addr") or h.get("from"), "to": list(h.get("to") or []) +
                            list(h.get("cc") or []), "subject": h.get("subject") or "",
                            "received_at": h["internaldate"], "text": plain, "html": html})
    except MailError as e:
        _auth_trip(conn, e)
        raise
    return out


def after_use(uid, mode: str) -> None:
    """mark_read or archive the used message (best effort; the caller ignores a failure)."""
    if mode not in ("mark_read", "archive") or not str(uid or "").isdigit():
        return
    with imap_client() as c:
        c.mark_used(str(uid), mode)
