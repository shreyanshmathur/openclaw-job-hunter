"""MIME construction for the code-owned route (design 2.3.1 step 3, 12.10).

The message is built from the approved canonical send text (drafts.send_text, the text QC and the person
approved), not re-assembled from parts, so the bytes that go out are the bytes that were approved:

    From: owner name <owner.gmail_address>     To: the one recipient of the action
    Subject: the approved subject              Date, MIME-Version
    Message-ID: <TOKEN@jobhunter.invalid>      (set by code; reconciliation searches for it)
    In-Reply-To / References: the thread's first Message-ID (follow-ups only)
    text/plain; charset=utf-8 body = the approved body including the code-appended signature
    application_email: the approved resume PDF as an attachment (sha256 checked against the variant)

`canonical_from_message` rebuilds the canonical text from the wire bytes; the mailer refuses to send when its
sha256 differs from the approved one (the attachment hash is part of that text).
"""
from __future__ import annotations

import hashlib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr

from .. import canon
from ..errors import Denied
from . import MESSAGE_ID_DOMAIN

MAX_7BIT_LINE = 998


def message_id_for(token: str) -> str:
    """RFC 5322 Message-ID of an action: <TOKEN@jobhunter.invalid>."""
    return "<%s@%s>" % (token, MESSAGE_ID_DOMAIN)


def bare_id(mid: str | None) -> str:
    return (mid or "").strip().lstrip("<").rstrip(">")


def split_send_text(send_text: str, has_attachment: bool) -> tuple[str, str, str | None]:
    """Canonical email text -> (subject, body incl. signature, attachment line or None)."""
    if not isinstance(send_text, str) or not send_text.startswith("Subject:"):
        raise Denied("E_VALIDATION", "the approved text is not an email text (no Subject line)")
    head, sep, rest = send_text.partition("\n\n")
    if not sep or "\n" in head:
        raise Denied("E_VALIDATION", "the approved text has no body")
    subject = head[len("Subject:"):].strip()
    att_line = None
    if has_attachment:
        body, sep2, tail = rest.rpartition("\n\nAttachment: ")
        if not sep2 or "\n" in tail:
            raise Denied("E_VALIDATION", "the approved text has no attachment line")
        rest, att_line = body, "Attachment: " + tail
    if not rest.strip():
        raise Denied("E_VALIDATION", "the approved text has an empty body")
    return subject, rest, att_line


def _cte(body: str) -> str:
    try:
        body.encode("ascii")
    except UnicodeEncodeError:
        return "quoted-printable"
    if any(len(line) > MAX_7BIT_LINE for line in body.split("\n")):
        return "quoted-printable"
    return "7bit"


def build_message(draft_row, action_row, owner: dict, attachment: dict | None, *, send_text: str | None = None,
                  in_reply_to: str | None = None, date: str | None = None) -> EmailMessage:
    """Build the outgoing message.

    draft_row: drafts row (kind, subject, body); action_row: actions row (token, recipient, kind).
    owner: {"address": gmail address, "name": display name or "", "signature": text or None}.
    attachment: {"filename", "data" (bytes), "sha256"} or None (application_email needs one).
    send_text: the approved canonical text; when given, subject and body come from it (the mailer always
      passes it). Without it the text is assembled from draft_row and owner["signature"].
    in_reply_to: the thread's first Message-ID (follow-ups).
    """
    kind = action_row["kind"]
    if kind not in canon.EMAIL_KINDS:
        raise Denied("E_VALIDATION", "%s is not an email kind" % kind)
    if kind == "application_email" and not attachment:
        raise Denied("E_VALIDATION", "an application email needs its resume attachment")
    if attachment:
        data = attachment.get("data")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise Denied("E_VALIDATION", "the attachment has no bytes")
        if hashlib.sha256(bytes(data)).hexdigest() != attachment.get("sha256"):
            raise Denied("E_QC_HASH_MISMATCH", "the attachment bytes differ from the approved resume")
    if send_text is None:
        send_text = canon.canonical_send_text(
            kind, draft_row["subject"], draft_row["body"], None, owner.get("signature"),
            {"filename": attachment["filename"], "sha256": attachment["sha256"]} if attachment else None)
    subject, body, _att = split_send_text(send_text, bool(attachment))
    recipient = (action_row["recipient"] or "").strip()
    if not recipient or "@" not in recipient or any(c in recipient for c in " ,;<>\r\n"):
        raise Denied("E_VALIDATION", "the action has no single email recipient")
    address = (owner.get("address") or "").strip()
    if not address or "@" not in address:
        raise Denied("E_CONFIG_INVALID", "no sender address")
    msg = EmailMessage(policy=policy.SMTP)
    name = (owner.get("name") or "").strip()
    msg["From"] = formataddr((name, address)) if name else address
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = format_datetime(canon.parse_ts(date or canon.now()))
    msg["Message-ID"] = message_id_for(action_row["token"])
    if kind == "followup_email" and in_reply_to:
        mid = "<%s>" % bare_id(in_reply_to)
        msg["In-Reply-To"] = mid
        msg["References"] = mid
    msg.set_content(body, subtype="plain", charset="utf-8", cte=_cte(body))
    if attachment:
        msg.add_attachment(bytes(attachment["data"]), maintype="application", subtype="pdf",
                           filename=attachment["filename"])
    return msg


def wire_bytes(msg) -> bytes:
    return msg.as_bytes(policy=policy.SMTP)


def parse_wire(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def parts_of(msg) -> dict:
    """{subject, body, attachment: {filename, sha256, size} | None} of a message (built or parsed)."""
    body = None
    att = None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart():
            continue
        if part.get_filename() and att is None:
            data = part.get_content()
            if isinstance(data, str):
                data = data.encode("utf-8")
            att = {"filename": part.get_filename(), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        elif part.get_content_type() == "text/plain" and body is None:
            body = part.get_content()
    return {"subject": str(msg.get("Subject") or ""), "body": body, "attachment": att}


def canonical_from_message(msg, kind: str) -> str:
    """The canonical send text of what the message really carries, computed from its wire bytes. The MIME
    body always ends with one line break; canonical_send_text strips it."""
    parsed = parse_wire(wire_bytes(msg))
    p = parts_of(parsed)
    if p["body"] is None:
        raise Denied("E_VALIDATION", "the message has no text/plain body")
    att = {"filename": p["attachment"]["filename"], "sha256": p["attachment"]["sha256"]} if p["attachment"] else None
    return canon.canonical_send_text(kind, p["subject"], p["body"], None, None, att)
