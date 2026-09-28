"""MIME exactness (U9, design 2.3.1 step 3, 13.2): the bytes that go out carry exactly the approved text."""
from __future__ import annotations

import hashlib
import unittest

import tests  # noqa: F401
from jobhunter import canon
from jobhunter.errors import Denied
from jobhunter.mail import mime

OWNER = {"address": "sam.lee.sender@example.com", "name": "Sam Lee", "signature": "Sam Lee\nhttps://example.com/sam"}
TOKEN = "TABCDEFGHJKL"
PDF = b"%PDF-1.4\n% fictional resume bytes\n" + bytes(range(256)) + b"\n%%EOF\n"


def _action(kind="cold_email", token=TOKEN, recipient="alex.rivera@kestrel.example"):
    return {"kind": kind, "token": token, "recipient": recipient}


def _text(kind, subject, body, attachment=None):
    att = {"filename": attachment["filename"], "sha256": attachment["sha256"]} if attachment else None
    return canon.canonical_send_text(kind, subject, body, None, OWNER["signature"], att)


class MimeTests(unittest.TestCase):
    def test_cold_email_round_trip_is_exact(self):
        body = "Hi Alex,\n\nI read your note on pincode-level RTO models.\n.This line starts with a dot.\n\nThanks,"
        st = _text("cold_email", "Pincode-level RTO models", body)
        msg = mime.build_message({}, _action(), OWNER, None, send_text=st, date="2026-09-29T09:00:00Z")
        raw = mime.wire_bytes(msg)
        raw.decode("ascii")   # 7bit on the wire
        self.assertIn(b"\r\n", raw)
        self.assertEqual(mime.canonical_from_message(msg, "cold_email"), st)
        parsed = mime.parse_wire(raw)
        # SMTP carries CRLF line ends; the text is otherwise byte for byte the approved body
        self.assertEqual(parsed.get_content(), (st.split("\n\n", 1)[1] + "\n").replace("\n", "\r\n"))
        self.assertEqual(parsed["Message-ID"], "<%s@jobhunter.invalid>" % TOKEN)
        self.assertEqual(parsed["To"], "alex.rivera@kestrel.example")
        self.assertEqual(parsed["From"], "Sam Lee <sam.lee.sender@example.com>")
        self.assertEqual(parsed["Date"], "Tue, 29 Sep 2026 09:00:00 +0000")
        self.assertIsNone(parsed["In-Reply-To"])
        self.assertEqual(parsed.get_content_type(), "text/plain")
        self.assertEqual(parsed.get_content_charset(), "utf-8")

    def test_signature_is_part_of_the_body(self):
        st = _text("cold_email", "Hello", "Hi Alex,\n\nShort.")
        msg = mime.build_message({}, _action(), OWNER, None, send_text=st)
        self.assertTrue(mime.parse_wire(mime.wire_bytes(msg)).get_content().replace("\r\n", "\n").rstrip("\n").endswith(
            "Short.\n\nSam Lee\nhttps://example.com/sam"))

    def test_non_ascii_and_long_subject(self):
        subject = "Caf\u00e9 analytics at Kestrel Commerce and a deliberately long subject line that must fold"
        body = "Hi Ren\u00e9e,\n\nNa\u00efve question about your \u20ac pricing work." + "x" * 1200
        st = _text("cold_email", subject, body)
        msg = mime.build_message({}, _action(), OWNER, None, send_text=st)
        raw = mime.wire_bytes(msg)
        raw.decode("ascii")   # headers encoded, body quoted-printable
        self.assertIn(b"quoted-printable", raw)
        self.assertEqual(mime.canonical_from_message(msg, "cold_email"), st)
        self.assertEqual(str(mime.parse_wire(raw)["Subject"]), subject)

    def test_followup_threading_headers(self):
        st = _text("followup_email", "Re: Pincode-level RTO models", "Hi Alex,\n\nA short follow-up.")
        msg = mime.build_message({}, _action("followup_email", "TQQQQQQQQQQQ"), OWNER, None, send_text=st,
                                 in_reply_to="<TABCDEFGHJKL@jobhunter.invalid>")
        p = mime.parse_wire(mime.wire_bytes(msg))
        self.assertEqual(p["In-Reply-To"], "<TABCDEFGHJKL@jobhunter.invalid>")
        self.assertEqual(p["References"], "<TABCDEFGHJKL@jobhunter.invalid>")
        self.assertEqual(p["Subject"], "Re: Pincode-level RTO models")
        self.assertEqual(p["Message-ID"], "<TQQQQQQQQQQQ@jobhunter.invalid>")
        cold = mime.build_message({}, _action(), OWNER, None, send_text=_text("cold_email", "S", "B"),
                                  in_reply_to="<x@y>")
        self.assertIsNone(cold["In-Reply-To"], "only follow-ups carry threading headers")

    def test_application_email_attachment_hash(self):
        att = {"filename": "Sam_Lee_Resume.pdf", "data": PDF, "sha256": hashlib.sha256(PDF).hexdigest()}
        st = _text("application_email", "Data Analyst application", "Hello,\n\nPlease find my resume attached.", att)
        msg = mime.build_message({}, _action("application_email"), OWNER, att, send_text=st)
        self.assertEqual(canon.sha256_text(mime.canonical_from_message(msg, "application_email")),
                         canon.sha256_text(st))
        p = mime.parse_wire(mime.wire_bytes(msg))
        parts = mime.parts_of(p)
        self.assertEqual(parts["attachment"]["filename"], "Sam_Lee_Resume.pdf")
        self.assertEqual(parts["attachment"]["sha256"], att["sha256"])
        pdf_part = [x for x in p.walk() if x.get_filename()][0]
        self.assertEqual(pdf_part.get_content_type(), "application/pdf")
        self.assertEqual(pdf_part.get_content(), PDF)

    def test_tampered_attachment_is_refused(self):
        att = {"filename": "Sam_Lee_Resume.pdf", "data": PDF + b"x", "sha256": hashlib.sha256(PDF).hexdigest()}
        st = _text("application_email", "S", "B", att)
        with self.assertRaises(Denied) as cm:
            mime.build_message({}, _action("application_email"), OWNER, att, send_text=st)
        self.assertEqual(cm.exception.code, "E_QC_HASH_MISMATCH")

    def test_changed_pdf_changes_the_canonical_text(self):
        att = {"filename": "Sam_Lee_Resume.pdf", "data": PDF, "sha256": hashlib.sha256(PDF).hexdigest()}
        st = _text("application_email", "S", "B", att)
        other = PDF.replace(b"fictional", b"fictitious")
        att2 = {"filename": "Sam_Lee_Resume.pdf", "data": other, "sha256": hashlib.sha256(other).hexdigest()}
        msg = mime.build_message({}, _action("application_email"), OWNER, att2, send_text=st)
        self.assertNotEqual(mime.canonical_from_message(msg, "application_email"), st)

    def test_application_email_needs_attachment(self):
        with self.assertRaises(Denied):
            mime.build_message({}, _action("application_email"), OWNER, None, send_text=_text("cold_email", "S", "B"))

    def test_build_without_send_text_uses_owner_signature(self):
        draft = {"subject": "Hello", "body": "Hi Alex,\n\nShort note."}
        msg = mime.build_message(draft, _action(), OWNER, None)
        self.assertEqual(mime.canonical_from_message(msg, "cold_email"), _text("cold_email", "Hello", draft["body"]))

    def test_split_and_recipient_checks(self):
        with self.assertRaises(Denied):
            mime.split_send_text("no subject line", False)
        with self.assertRaises(Denied):
            mime.split_send_text("Subject: x\n\nbody", True)
        self.assertEqual(mime.split_send_text("Subject: x\n\nbody\n\nAttachment: a.pdf 00", True),
                         ("x", "body", "Attachment: a.pdf 00"))
        with self.assertRaises(Denied):
            mime.build_message({}, _action(recipient="a@example.com, b@example.com"), OWNER, None,
                               send_text=_text("cold_email", "S", "B"))
        with self.assertRaises(Denied):
            mime.build_message({}, _action(kind="li_message"), OWNER, None, send_text="x")
        self.assertEqual(mime.bare_id("<TABC@jobhunter.invalid>"), "TABC@jobhunter.invalid")


if __name__ == "__main__":
    unittest.main()
