"""A scripted fake Gmail IMAP server on 127.0.0.1 (socketserver), for the U9 tests only.

It speaks just enough IMAP4rev1 for imaplib and jobhunter.mail.imap: CAPABILITY, LOGIN, LIST, EXAMINE/SELECT,
UID SEARCH (X-GM-RAW as a quoted string or a UTF-8 literal, X-GM-THRID, other criteria), UID FETCH (UID,
X-GM-MSGID, X-GM-THRID, INTERNALDATE, BODY.PEEK[HEADER], BODY.PEEK[]<0.N>), NOOP, CLOSE, LOGOUT.

Searches are scripted: srv.on("in:sent to:alex.rivera@kestrel.example", [3]) makes that exact X-GM-RAW query
return UID 3; srv.match("thank you for applying", [5]) answers every other query that contains the text; any
other query returns no UIDs. Every query received is kept in srv.queries.
"""
from __future__ import annotations

import re
import socketserver
import threading

ACCOUNT = "sam.lee.sender@example.com"
PASSWORD = "abcdefghijklmnop"
# Continuation request for a synchronizing literal ({N}); imaplib only looks at the leading "+".
CONTINUATION = b"+ Ready for literal data\r\n"


def tokenize(s: str) -> list[tuple[str, str]]:
    toks = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == " ":
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < len(s) and s[j] != '"':
                if s[j] == "\\" and j + 1 < len(s):
                    j += 1
                buf.append(s[j])
                j += 1
            toks.append(("q", "".join(buf)))
            i = j + 1
        elif c == "(":
            depth = 0
            j = i
            while j < len(s):
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            toks.append(("p", s[i:j + 1]))
            i = j + 1
        else:
            j = i
            while j < len(s) and s[j] != " ":
                j += 1
            toks.append(("a", s[i:j]))
            i = j
    return toks


def _uid_set(spec: str, have: list[int]) -> list[int]:
    out = set()
    top = max(have) if have else 0
    for part in spec.split(","):
        if ":" in part:
            a, b = part.split(":", 1)
            lo = int(a) if a != "*" else top
            hi = int(b) if b != "*" else top
            out |= {u for u in have if min(lo, hi) <= u <= max(lo, hi)}
        elif part.isdigit():
            out.add(int(part))
    return sorted(u for u in out if u in have)


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def _w(self, s) -> None:
        self.wfile.write(s if isinstance(s, bytes) else s.encode("utf-8"))

    def _read_command(self):
        toks = []
        while True:
            line = self.rfile.readline(65536)
            if not line:
                return None
            m = re.search(rb"\{(\d+)(\+?)\}\r\n$", line)
            if m:
                toks += tokenize(line[:m.start()].decode("utf-8", "replace"))
                if not m.group(2):
                    self._w(CONTINUATION)
                    self.wfile.flush()
                data = self.rfile.read(int(m.group(1)))
                toks.append(("q", data.decode("utf-8", "replace")))
                continue
            toks += tokenize(line.rstrip(b"\r\n").decode("utf-8", "replace"))
            return toks

    def handle(self):
        srv = self.server.fake
        caps = "IMAP4rev1 UIDPLUS" + (" X-GM-EXT-1" if srv.gmail else "")
        self._w("* OK [CAPABILITY %s] fake Gmail IMAP ready\r\n" % caps)
        self.wfile.flush()
        while True:
            try:
                toks = self._read_command()
            except OSError:
                return
            if not toks:
                return
            tag = toks[0][1]
            verb = toks[1][1].upper() if len(toks) > 1 else ""
            srv.commands.append(verb if verb != "LOGIN" else "LOGIN <redacted>")
            if srv.drop_on and verb == srv.drop_on:
                return
            if verb == "CAPABILITY":
                self._w("* CAPABILITY %s\r\n%s OK CAPABILITY completed\r\n" % (caps, tag))
            elif verb == "LOGIN":
                user, pw = toks[2][1], toks[3][1]
                if srv.login_fail_text or user != srv.account or pw != srv.password:
                    self._w("%s NO %s\r\n" % (tag, srv.login_fail_text or "[AUTHENTICATIONFAILED] Invalid credentials "
                                              "(Failure)"))
                else:
                    self._w("%s OK [CAPABILITY %s] %s authenticated (Success)\r\n" % (tag, caps, user))
            elif verb == "LIST":
                for flags, name in srv.folders:
                    self._w('* LIST (%s) "/" "%s"\r\n' % (flags, name))
                self._w("%s OK Success\r\n" % tag)
            elif verb in ("EXAMINE", "SELECT"):
                name = toks[2][1]
                if name in [n for _f, n in srv.folders]:
                    srv.selected.append(name)
                    self._w("* FLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen)\r\n* %d EXISTS\r\n* 0 RECENT\r\n"
                            "%s OK [%s] %s (Success)\r\n" % (len(srv.messages), tag,
                                                              "READ-ONLY" if verb == "EXAMINE" else "READ-WRITE", verb))
                else:
                    self._w("%s NO [NONEXISTENT] Unknown Mailbox: %s (Failure)\r\n" % (tag, name))
            elif verb == "UID" and len(toks) > 2 and toks[2][1].upper() == "SEARCH":
                args = toks[3:]
                vals = [v for _k, v in args]
                if "X-GM-RAW" in [v.upper() for v in vals]:
                    i = [v.upper() for v in vals].index("X-GM-RAW")
                    key = vals[i + 1] if i + 1 < len(vals) else ""
                    if "CHARSET" in [v.upper() for v in vals[:i]]:
                        srv.literal_queries.append(key)
                else:
                    key = " ".join(vals)
                srv.queries.append(key)
                if key in srv.search_fail:
                    self._w("%s NO %s\r\n" % (tag, srv.search_fail[key]))
                    continue
                uids = srv.searches.get(key)
                if uids is None:
                    uids = sorted({u for sub, us in srv.rules if sub in key for u in us})
                self._w("* SEARCH%s\r\n%s OK SEARCH completed (Success)\r\n"
                        % ("".join(" %d" % u for u in uids), tag))
            elif verb == "UID" and len(toks) > 2 and toks[2][1].upper() == "FETCH":
                have = sorted(srv.messages)
                uids = _uid_set(toks[3][1], have)
                items = toks[4][1].upper() if len(toks) > 4 else ""
                srv.fetches.append((toks[3][1], items))
                for uid in uids:
                    self._fetch_one(srv, uid, have.index(uid) + 1, items)
                self._w("%s OK Success\r\n" % tag)
            elif verb == "UID" and len(toks) > 2 and toks[2][1].upper() == "STORE":
                srv.stores.append((toks[3][1], toks[4][1], toks[5][1] if len(toks) > 5 else ""))
                self._w("%s OK Success\r\n" % tag)
            elif verb == "LOGOUT":
                self._w("* BYE LOGOUT Requested\r\n%s OK 73 good day (Success)\r\n" % tag)
                self.wfile.flush()
                return
            elif verb in ("NOOP", "CLOSE"):
                self._w("%s OK Success\r\n" % tag)
            else:
                self._w("%s BAD Unknown command\r\n" % tag)
            self.wfile.flush()

    def _fetch_one(self, srv, uid: int, seq: int, items: str) -> None:
        m = srv.messages[uid]
        parts = []
        if "UID" in items:
            parts.append("UID %d" % uid)
        if "X-GM-MSGID" in items and srv.gmail:
            parts.append("X-GM-MSGID %d" % m["msgid"])
        if "X-GM-THRID" in items and srv.gmail:
            parts.append("X-GM-THRID %d" % m["thrid"])
        if "INTERNALDATE" in items:
            parts.append('INTERNALDATE "%s"' % m["internaldate"])
        label = data = None
        if "BODY.PEEK[HEADER]" in items:
            raw = m["raw"]
            cut = raw.find(b"\r\n\r\n")
            data = raw[:cut + 4] if cut >= 0 else raw
            label = "BODY[HEADER]"
        else:
            pm = re.search(r"BODY\.PEEK\[\](?:<0\.(\d+)>)?", items)
            if pm:
                data = m["raw"][:int(pm.group(1))] if pm.group(1) else m["raw"]
                label = "BODY[]<0>" if pm.group(1) else "BODY[]"
        head = "* %d FETCH (%s" % (seq, " ".join(parts))
        if data is not None:
            self._w((head + " %s {%d}\r\n" % (label, len(data))).encode("utf-8") + data + b")\r\n")
        else:
            self._w(head + ")\r\n")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeImapServer:
    def __init__(self, account: str = ACCOUNT, password: str = PASSWORD, gmail: bool = True):
        self.account = account
        self.password = password
        self.gmail = gmail
        self.messages: dict[int, dict] = {}
        self.searches: dict[str, list[int]] = {}
        self.search_fail: dict[str, str] = {}
        self.rules: list[tuple[str, list[int]]] = []
        self.queries: list[str] = []
        self.literal_queries: list[str] = []
        self.commands: list[str] = []
        self.fetches: list[tuple] = []
        self.selected: list[str] = []
        self.stores: list[tuple] = []          # (uid set, item, value) of every UID STORE
        self.folders = [("\\HasNoChildren", "INBOX"), ("\\HasChildren \\Noselect", "[Gmail]"),
                        ("\\All \\HasNoChildren", "[Gmail]/All Mail"), ("\\HasNoChildren \\Sent", "[Gmail]/Sent Mail")]
        self.login_fail_text = None
        self.drop_on = None
        self._server = None
        self._next_uid = 1

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "FakeImapServer":
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def add_message(self, raw: bytes | str, uid: int | None = None, msgid: int | None = None,
                    thrid: int | None = None, internaldate: str = "28-Sep-2026 06:02:00 +0000") -> str:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        uid = uid or self._next_uid
        self._next_uid = max(self._next_uid, uid + 1)
        self.messages[uid] = {"raw": raw, "msgid": msgid or (1000 + uid), "thrid": thrid or (5000 + uid),
                              "internaldate": internaldate}
        return str(uid)

    def on(self, query: str, uids) -> None:
        self.searches[query] = [int(u) for u in uids]

    def match(self, substring: str, uids) -> None:
        """Every query containing substring (and not scripted exactly with on()) returns these UIDs."""
        self.rules.append((substring, [int(u) for u in uids]))

    def client(self, account: str | None = None, password: str | None = None, timeout: float = 5.0):
        from jobhunter.mail.imap import ImapClient
        return ImapClient(account or self.account, password or self.password, host="127.0.0.1", port=self.port,
                          use_ssl=False, timeout=timeout)
