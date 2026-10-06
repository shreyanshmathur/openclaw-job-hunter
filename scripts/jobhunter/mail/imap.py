"""Read-only IMAP client for Gmail (design 2.3.1 step 1, 1.3.7, 2.3.4).

- Opens imap.gmail.com:993 over SSL, logs in with the app password, finds the All Mail folder by its
  special-use flag (\\All, so a localized folder name works) and EXAMINEs it (read-only: nothing is marked
  read, moved or deleted; message bodies are fetched with BODY.PEEK).
- Searches use Gmail's X-GM-RAW extension, so every query is a normal Gmail search string
  ("in:sent to:alex.rivera@kestrel.example"). ASCII queries go as an IMAP quoted string; other queries go
  as a UTF-8 literal with CHARSET UTF-8.
- Every failure is a MailError (E_MAIL_TRANSPORT); an authentication failure sets auth_failed so the caller
  can trip the gmail breaker (4.5).
"""
from __future__ import annotations

import datetime as _dt
import imaplib
import re
import socket
import ssl
from email import policy
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from . import IMAP_HOST, IMAP_PORT, TIMEOUT_S, MailError
from ..canon import fmt_ts

ALL_MAIL_FALLBACK = "[Gmail]/All Mail"
HEADER_ITEMS = "(UID X-GM-MSGID X-GM-THRID INTERNALDATE BODY.PEEK[HEADER])"
HEADER_ITEMS_PLAIN = "(UID INTERNALDATE BODY.PEEK[HEADER])"
FETCH_CHUNK = 100
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')
_META_RES = {
    "uid": re.compile(rb"\bUID (\d+)"),
    "gm_msgid": re.compile(rb"\bX-GM-MSGID (\d+)"),
    "gm_thrid": re.compile(rb"\bX-GM-THRID (\d+)"),
    "internaldate": re.compile(rb'\bINTERNALDATE "([^"]+)"'),
}
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_WS_RE = re.compile(r"[ \t]+")


def quote(s: str) -> str:
    """IMAP quoted string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(b: bytes) -> str:
    s = b.decode("utf-8", "replace").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


def _err_text(exc) -> str:
    return " ".join(str(exc).split())[:300]


def internaldate_ts(s: str | None) -> str | None:
    if not s:
        return None
    try:
        d = _dt.datetime.strptime(s.strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None
    return fmt_ts(d)


def header_ts(value: str | None) -> str | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return fmt_ts(d)


def _hget(h, name: str) -> str:
    try:
        v = h.get(name)
    except Exception:  # a malformed header must not stop the fetch
        try:
            v = h.get_all(name, [None])[0]
        except Exception:
            v = None
    return " ".join(str(v).split()) if v is not None else ""


def _addrs(h, name: str) -> list[str]:
    try:
        vals = [str(v) for v in (h.get_all(name) or [])]
    except Exception:
        vals = []
    return [a.strip().lower() for _n, a in getaddresses(vals) if a and "@" in a]


def _msg_ids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def parse_headers(raw: bytes, meta: dict | None = None) -> dict:
    """Header dict of one message (also used by the tests and by fetch for full messages)."""
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw or b"")
    meta = meta or {}
    froms = _addrs(h, "From")
    ctype = _hget(h, "Content-Type").lower()
    out = {
        "uid": meta.get("uid"), "gm_msgid": meta.get("gm_msgid"), "gm_thrid": meta.get("gm_thrid"),
        "message_id": (_msg_ids(_hget(h, "Message-ID")) or [None])[0],
        "from_addr": froms[0] if froms else None,
        "from": _hget(h, "From"),
        "to": _addrs(h, "To"), "cc": _addrs(h, "Cc"),
        "subject": _CTRL_RE.sub("", _hget(h, "Subject")),
        "date": header_ts(_hget(h, "Date")) or internaldate_ts(meta.get("internaldate")),
        "internaldate": internaldate_ts(meta.get("internaldate")),
        "in_reply_to": _msg_ids(_hget(h, "In-Reply-To")),
        "references": _msg_ids(_hget(h, "References")),
        "auto_submitted": _hget(h, "Auto-Submitted").lower(),
        "precedence": _hget(h, "Precedence").lower(),
        "x_autoreply": bool(_hget(h, "X-Autoreply") or _hget(h, "X-Autorespond") or _hget(h, "X-Auto-Response-Suppress")),
        "content_type": ctype,
        "list_id": _hget(h, "List-Id"),
        "list_unsubscribe": _hget(h, "List-Unsubscribe"),
        "return_path": _hget(h, "Return-Path"),
    }
    out["msg_ref"] = ("gm:%x" % int(out["gm_msgid"])) if out.get("gm_msgid") else \
        ("mid:" + out["message_id"] if out["message_id"] else ("uid:%s" % out["uid"] if out["uid"] else None))
    return out


def text_of(msg, max_chars: int = 2000) -> str:
    """Readable text of a message: the first text/plain part, else text/html without tags."""
    plain = html = None
    try:
        parts = list(msg.walk()) if msg.is_multipart() else [msg]
    except Exception:
        parts = [msg]
    for part in parts:
        try:
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = part.get_content()
            elif ctype == "text/html" and html is None:
                html = part.get_content()
        except Exception:
            continue
    text = plain
    if text is None and html is not None:
        text = _TAG_RE.sub(" ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html))
        text = re.sub(r"(?i)&nbsp;", " ", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = _CTRL_RE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]


def _literal_items(dat) -> list[tuple[bytes, bytes | None]]:
    """imaplib FETCH data -> [(meta text, literal bytes)], one per message. Meta text joins the part before
    the literal with any plain bytes that follow it (Gmail may put UID after the literal)."""
    out: list[list] = []
    for item in dat or []:
        if isinstance(item, tuple):
            out.append([item[0] or b"", item[1]])
        elif isinstance(item, bytes):
            if out and item.strip() not in (b"", b")") and not re.match(rb"^\d+ \(", item):
                out[-1][0] = out[-1][0] + b" " + item
            elif re.match(rb"^\d+ \(", item):
                out.append([item, None])
            elif out:
                out[-1][0] = out[-1][0] + b" " + item
    return [(m, lit) for m, lit in out]


def _meta(meta: bytes) -> dict:
    out = {}
    for k, rx in _META_RES.items():
        m = rx.search(meta)
        if m:
            out[k] = m.group(1).decode("ascii", "replace")
    return out


class ImapClient:
    """Minimal read-only Gmail IMAP client. use_ssl=False is for tests against a local fake server only."""

    def __init__(self, account: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT,
                 use_ssl: bool = True, timeout: float = TIMEOUT_S, context: ssl.SSLContext | None = None):
        self.account = account
        self._password = password
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.context = context
        self._m = None
        self.gmail = False
        self.all_mail = None
        self._selected = None
        self.queries: list[str] = []      # every search sent, for evidence and tests

    def __repr__(self) -> str:
        return "ImapClient(account=%r, host=%r, port=%r)" % (self.account, self.host, self.port)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ session
    def open(self) -> "ImapClient":
        if self._m is not None:
            return self
        try:
            if self.use_ssl:
                m = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=self.context or ssl.create_default_context(),
                                      timeout=self.timeout)
            else:
                m = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        except (OSError, imaplib.IMAP4.error, socket.timeout) as e:
            raise MailError("cannot reach the IMAP server: %s" % type(e).__name__, stage="connect",
                            reply=_err_text(e))
        m.debug = 0
        try:
            m.login(self.account, self._password)
        except imaplib.IMAP4.error as e:
            self._drop(m)
            text = _err_text(e)
            raise MailError("IMAP login refused", stage="auth", reply=text,
                            auth_failed=bool(re.search(r"authenticationfailed|invalid credentials|web login required|"
                                                       r"application-specific password", text, re.I)))
        except (OSError, socket.timeout, imaplib.IMAP4.abort) as e:
            self._drop(m)
            raise MailError("IMAP connection lost during login", stage="auth", reply=_err_text(e))
        self._m = m
        try:
            typ, dat = m.capability()
            caps = (dat[0] or b"").upper().split() if typ == "OK" and dat else []
            self.gmail = b"X-GM-EXT-1" in caps
            self.all_mail = self._find_all_mail()
            self._select(self.all_mail)
        except MailError:
            self.close()
            raise
        except (OSError, socket.timeout, imaplib.IMAP4.error) as e:
            self.close()
            raise MailError("IMAP setup failed", stage="select", reply=_err_text(e))
        return self

    @staticmethod
    def _drop(m) -> None:
        try:
            m.shutdown()
        except Exception:
            pass

    def close(self) -> None:
        m, self._m = self._m, None
        self._selected = None
        if m is None:
            return
        try:
            m.logout()
        except Exception:
            self._drop(m)

    def _need(self):
        if self._m is None:
            self.open()
        return self._m

    def _find_all_mail(self) -> str:
        m = self._m
        typ, dat = m.list('""', '"*"')
        if typ != "OK":
            return ALL_MAIL_FALLBACK
        names = []
        for item in dat or []:
            if isinstance(item, tuple):   # a literal folder name
                head, lit = item
                mm = re.match(rb"^\(([^)]*)\)", head or b"")
                names.append(((mm.group(1) if mm else b""), lit.decode("utf-8", "replace")))
                continue
            if not item:
                continue
            mm = _LIST_RE.match(item)
            if mm:
                names.append((mm.group("flags"), _unquote(mm.group("name"))))
        for flags, name in names:
            if b"\\ALL" in flags.upper().split():
                return name
        for _flags, name in names:
            if name == ALL_MAIL_FALLBACK:
                return name
        raise MailError("no All Mail folder over IMAP; turn on 'Show in IMAP' for All Mail in Gmail settings",
                        stage="select")

    def _select(self, folder: str) -> None:
        if self._selected == folder:
            return
        typ, dat = self._need().select(quote(folder), readonly=True)
        if typ != "OK":
            raise MailError("cannot open the folder %s" % folder, stage="select", reply=_err_text(dat))
        self._selected = folder

    def select_rw(self, folder: str | None = None) -> None:
        """SELECT (read-write) for the one write this package makes: the flag or label change of a used email code
        (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.4 step 4, otp.after_use). Every other path stays on EXAMINE."""
        folder = folder or self.all_mail
        typ, dat = self._need().select(quote(folder), readonly=False)
        if typ != "OK":
            raise MailError("cannot open the folder %s" % folder, stage="select", reply=_err_text(dat))
        self._selected = None            # the next read selects read-only again

    def mark_used(self, uid: str, mode: str) -> None:
        """mark_read: UID STORE +FLAGS (\\Seen); archive: also UID STORE -X-GM-LABELS (\\Inbox)."""
        if not str(uid).isdigit() or mode not in ("mark_read", "archive"):
            return
        self.select_rw()
        m = self._need()
        self._call(m.uid, "STORE", str(uid), "+FLAGS", "(\\Seen)")
        if mode == "archive" and self.gmail:
            self._call(m.uid, "STORE", str(uid), "-X-GM-LABELS", "(\\Inbox)")
        self._select(self.all_mail)

    def _call(self, fn, *args):
        try:
            typ, dat = fn(*args)
        except imaplib.IMAP4.abort as e:
            self.close()
            raise MailError("IMAP connection lost", stage="command", reply=_err_text(e))
        except (OSError, socket.timeout) as e:
            self.close()
            raise MailError("IMAP connection lost", stage="command", reply=_err_text(e))
        except imaplib.IMAP4.error as e:
            raise MailError("IMAP command refused", stage="command", reply=_err_text(e))
        if typ != "OK":
            raise MailError("IMAP command refused", stage="command", reply=_err_text(dat))
        return dat

    # ------------------------------------------------------------ searches
    def search(self, gm_raw: str, folder: str | None = None) -> list[str]:
        """UIDs (as strings, ascending) matching a Gmail search string."""
        if not gm_raw or not isinstance(gm_raw, str):
            return []
        m = self._need()
        self._select(folder or self.all_mail)
        self.queries.append(gm_raw)
        if not self.gmail:
            raise MailError("the IMAP server does not speak Gmail search (X-GM-EXT-1)", stage="search")
        if all(32 <= ord(c) < 127 for c in gm_raw):
            dat = self._call(m.uid, "SEARCH", "X-GM-RAW", quote(gm_raw))
        else:
            m.literal = gm_raw.encode("utf-8")
            dat = self._call(m.uid, "SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        return self._uids(dat)

    def search_criteria(self, *criteria: str, folder: str | None = None) -> list[str]:
        """Plain IMAP UID SEARCH with ready-made criteria tokens (for X-GM-THRID and header searches)."""
        m = self._need()
        self._select(folder or self.all_mail)
        self.queries.append(" ".join(criteria))
        return self._uids(self._call(m.uid, "SEARCH", *criteria))

    @staticmethod
    def _uids(dat) -> list[str]:
        out = []
        for chunk in dat or []:
            if isinstance(chunk, bytes):
                out += [x.decode("ascii") for x in chunk.split() if x.isdigit()]
        return sorted(set(out), key=int)

    def count(self, gm_raw: str | None, folder: str | None = None) -> int:
        return len(self.search(gm_raw, folder)) if gm_raw else 0

    def thread_uids(self, uid: str) -> list[str]:
        """UIDs of every message in the Gmail conversation of `uid` (X-GM-THRID)."""
        heads = self.fetch_headers([uid])
        if not heads or not heads[0].get("gm_thrid"):
            return [str(uid)]
        return self.search_criteria("X-GM-THRID", heads[0]["gm_thrid"])

    # ------------------------------------------------------------ fetches
    def fetch_headers(self, uids) -> list[dict]:
        """Parsed headers (parse_headers) of the given UIDs, in UID order."""
        uids = [str(u) for u in uids if str(u).isdigit()]
        if not uids:
            return []
        m = self._need()
        self._select(self._selected or self.all_mail)
        items = HEADER_ITEMS if self.gmail else HEADER_ITEMS_PLAIN
        out = []
        for i in range(0, len(uids), FETCH_CHUNK):
            chunk = uids[i:i + FETCH_CHUNK]
            dat = self._call(m.uid, "FETCH", ",".join(chunk), items)
            for meta, lit in _literal_items(dat):
                md = _meta(meta)
                if not md.get("uid") or lit is None:
                    continue
                out.append(parse_headers(lit, md))
        out.sort(key=lambda h: int(h["uid"]))
        return out

    def fetch_message(self, uid: str, max_bytes: int = 65536):
        """The first max_bytes of the message, parsed (email.message.EmailMessage) or None."""
        m = self._need()
        self._select(self._selected or self.all_mail)
        dat = self._call(m.uid, "FETCH", str(uid), "(UID BODY.PEEK[]<0.%d>)" % int(max_bytes))
        for meta, lit in _literal_items(dat):
            if lit is None:
                continue
            try:
                return BytesParser(policy=policy.default).parsebytes(lit)
            except Exception:
                return None
        return None

    def fetch_text(self, uid: str, max_chars: int = 2000) -> str:
        msg = self.fetch_message(uid)
        return text_of(msg, max_chars) if msg is not None else ""
