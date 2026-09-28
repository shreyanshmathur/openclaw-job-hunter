"""Canonical time, ids and send text (design 2 intro and 12.10).

- Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ` everywhere.
- Human-facing ids: prefix + base32 (`A-Z2-7`): `J` jobs, `K` companies, `P` contacts, `R` research facts,
  `D` drafts, `Q` QC jobs, `H` human tasks (7 chars), `T` action tokens (11 chars), `C` + compact time + 4
  for cycles, `I` + 8 for the install id.
- `canonical_send_text` is the one function both sides of every text comparison go through (approved
  draft, observed read-back, SMTP body).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import secrets
import unicodedata

B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

EMAIL_KINDS = frozenset({"cold_email", "followup_email", "application_email"})
FORM_KINDS = frozenset({"form_answer", "application_package", "application"})
BODY_KINDS = frozenset({"li_invite_note", "li_invite", "li_message", "li_followup", "inmail", "cover_note",
                        "resume", "referral_ask", "li_withdraw"})

# ---------------------------------------------------------------- clock
_clock_override: _dt.datetime | None = None


def set_test_clock(ts: str | _dt.datetime | None) -> None:
    """Tests only: freeze now() at ts (None restores the real clock). tests.helpers.FakeClock wraps it."""
    global _clock_override
    if ts is None:
        _clock_override = None
    elif isinstance(ts, _dt.datetime):
        _clock_override = ts.astimezone(_dt.timezone.utc).replace(microsecond=0)
    else:
        _clock_override = parse_ts(ts)


def utcnow() -> _dt.datetime:
    """Current UTC time as an aware datetime (second precision), honouring the test clock."""
    if _clock_override is not None:
        return _clock_override
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def now() -> str:
    """Current UTC timestamp, `YYYY-MM-DDTHH:MM:SSZ`."""
    return fmt_ts(utcnow())


def fmt_ts(d: _dt.datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc).strftime(TS_FORMAT)


def parse_ts(s: str) -> _dt.datetime:
    """Parse our timestamp format (also accepts a `+00:00` suffix); ValueError otherwise."""
    if not isinstance(s, str):
        raise ValueError("timestamp must be a string")
    t = s.strip()
    if t.endswith("+00:00"):
        t = t[:-6] + "Z"
    if not TS_RE.match(t):
        raise ValueError("bad timestamp %r" % s)
    return _dt.datetime.strptime(t, TS_FORMAT).replace(tzinfo=_dt.timezone.utc)


def ts_add(ts: str, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> str:
    d = parse_ts(ts) + _dt.timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
    return fmt_ts(d)


def seconds_between(a: str, b: str) -> int:
    """b - a in whole seconds."""
    return int((parse_ts(b) - parse_ts(a)).total_seconds())


# ---------------------------------------------------------------- ids
def _b32(n: int) -> str:
    return "".join(secrets.choice(B32) for _ in range(n))


def new_uid(prefix: str, n: int = 7) -> str:
    """prefix + n random base32 characters, e.g. new_uid('J') -> 'J7Q2KX4M'."""
    if not prefix or not prefix.isalpha() or not prefix.isupper():
        raise ValueError("uid prefix must be upper-case letters")
    if n < 1:
        raise ValueError("n must be positive")
    return prefix + _b32(n)


def new_token() -> str:
    """Action token: 'T' + 11 base32 characters."""
    return "T" + _b32(11)


def new_cycle_id(at: str | None = None) -> str:
    """Cycle id: 'C' + compact UTC time + 4 base32, e.g. C20260927T041500Z7Q2K."""
    d = parse_ts(at) if at else utcnow()
    return "C" + d.strftime("%Y%m%dT%H%M%SZ") + _b32(4)


# ---------------------------------------------------------------- text
_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00a0": " ", "\u202f": " ", "\u2007": " ",
}
_QUOTE_RE = re.compile("[" + "".join(_QUOTES) + "]")
_TRAIL_RE = re.compile(r"[ \t]+$", re.M)
_MULTI_NL_RE = re.compile(r"\n{3,}")


def normalize_text(s: str | None) -> str:
    """The text normalisation of 12.10: NFC; CRLF and CR to LF; curly quotes to ASCII; no-break spaces to
    space; strip trailing spaces on each line; collapse three or more newlines to two; strip ends."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _QUOTE_RE.sub(lambda m: _QUOTES[m.group(0)], s)
    s = _TRAIL_RE.sub("", s)
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


def _one_line(s: str | None) -> str:
    return " ".join(normalize_text(s).split())


def _field_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ", ".join(_one_line(x) for x in v)
    return normalize_text(str(v))


def canonical_send_text(kind: str, subject: str | None, body: str | None, fields: list | None,
                        signature: str | None, attachment: dict | None = None) -> str:
    """Canonical text that is hashed, approved, sent and compared with the observed read-back.

    email kinds: "Subject: <subject>\\n\\n<body>" + "\\n\\n<signature>" when a signature is given
      + "\\n\\nAttachment: <filename> <sha256>" when an attachment is given. The web route observes the
      signature inside the body, which normalises to the same text.
    inmail: "Subject: <subject>\n\n<body>" when a subject is given (an InMail has one, so it is hashed and
      compared like an email subject); body only without a subject.
    LinkedIn and other text kinds: body only.
    form kinds: json.dumps({"fields": [{"label", "value"}] sorted by label, "resume": filename},
      sort_keys=True, separators=(",", ":")); only label and value of each field take part.
    """
    if kind in EMAIL_KINDS:
        out = "Subject: " + _one_line(subject) + "\n\n" + normalize_text(body)
        sig = normalize_text(signature)
        if sig:
            out += "\n\n" + sig
        if attachment:
            out += "\n\nAttachment: " + _one_line(attachment.get("filename")) + " " + \
                   _one_line(attachment.get("sha256"))
        return normalize_text(out)
    if kind in FORM_KINDS:
        items = []
        for f in fields or []:
            if not isinstance(f, dict):
                raise ValueError("form fields must be objects with label and value")
            items.append({"label": _one_line(f.get("label")), "value": _field_value(f.get("value"))})
        items.sort(key=lambda x: (x["label"], x["value"]))
        resume = None
        if attachment:
            resume = _one_line(attachment.get("filename")) or None
        return json.dumps({"fields": items, "resume": resume}, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    if kind == "inmail" and _one_line(subject):
        return normalize_text("Subject: " + _one_line(subject) + "\n\n" + normalize_text(body))
    if kind in BODY_KINDS:
        return normalize_text(body)
    raise ValueError("unknown send kind %r" % kind)


def sha256_text(s: str) -> str:
    """Hex sha256 of the UTF-8 bytes of s."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
