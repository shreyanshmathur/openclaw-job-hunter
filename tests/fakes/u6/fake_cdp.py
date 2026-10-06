"""An in-process fake Chrome DevTools Protocol server for the code-owned browser steps (U6 tests and INT).

FakeCdp serves on 127.0.0.1:<ephemeral>: GET /json/list, GET /json/version, PUT /json/new?<url>,
GET /json/close/<id>, and a websocket per tab at /devtools/page/<id>. Each tab shows one page of a recorded fixture
(tests/fixtures/browser/ats_account_pages.json): a tiny DOM model of fields (type, label, name, automation id,
required, maxlength, checked), buttons with an action, iframes, page text, URL and title. It applies
Input.dispatchMouseEvent (focus a field, tick a box, press a button), Input.insertText (into the focused field),
Input.dispatchKeyEvent (select all and delete, Enter, the Gmail archive key) and Page.navigate, and answers
Runtime.evaluate only for the fixed pagefill scripts and the read-only Gmail drivers (by their marker comments);
any other script is an exception, so code can only run what it ships.

Button actions (a per-fixture state machine): goto, create_account (required boxes ticked, the two passwords
equal, the site's password rule, an existing account goes to the fixture's exists page), signin (email and
password must match the account the fake holds), verify_code (the typed code must equal FakeCdp.expected_code;
a wrong one shows an error next to the field), submit (goto plus a counted submission). Page.navigate to
FakeCdp.expected_link verifies the account and lands on the link's page (its URL holds no token).

Gmail on the web: a tab on mail.google.com serves a FakeInbox (tests/fakes/u9/fake_inbox.py) through the
read_gmail_list and read_gmail_message drivers and pagefill.GMAIL_LINKS; gmail_security=True makes Gmail show
"Verify it's you".

The log (FakeCdp.log) records every method with the length of inserted text and the host of a navigation, never a
value or a full URL. Values live only in the fake's memory (as a site would hold them).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import socketserver
import struct
import threading
from urllib.parse import unquote, urlsplit

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fixtures",
                       "browser", "ats_account_pages.json")
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# a 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def load_pages() -> dict:
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["pages"]


class Tab:
    def __init__(self, tid: str, page: dict, name: str):
        self.id = tid
        self.set(page, name)

    def set(self, page: dict, name: str) -> None:
        self.name = name
        self.page = copy.deepcopy(page)
        self.url = self.page.get("url", "about:blank")
        self.title = self.page.get("title", "")
        self.values = {i: "" for i, _f in enumerate(self.page.get("fields") or [])}
        self.checked = {i: bool(f.get("checked")) for i, f in enumerate(self.page.get("fields") or [])}
        self.focused = -1
        self.error = None
        self.selected_all = False


class FakeCdp:
    def __init__(self, pages: dict | None = None):
        self.pages = pages or load_pages()
        self.tabs: dict = {}
        self.log: list = []
        self.accounts: dict = {}          # email -> password (the site's own memory)
        self.expected_code: str | None = None
        self.expected_link: str | None = None
        self.link_page = "wd_link_verified"
        self.verified = False
        self.submissions = 0
        self.inbox = None
        self.gmail_security = False
        self.archived: list = []
        self.fragment = False
        self.ping_first = False
        self.foreign_ws = False
        self._n = 0
        self._server = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ server
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "FakeCdp":
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 30

            def handle(self):
                fake._handle(self)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ------------------------------------------------------------ tabs
    def add_tab(self, page: str, tid: str | None = None) -> str:
        with self._lock:
            self._n += 1
            tid = tid or "TAB%04dFAKE%04d" % (self._n, self._n)
            self.tabs[tid] = Tab(tid, self.pages[page], page)
            return tid

    def set_page(self, tid: str, page: str) -> None:
        with self._lock:
            self.tabs[tid].set(self.pages[page], page)

    def page_of(self, tid: str) -> str | None:
        t = self.tabs.get(tid)
        return t.name if t else None

    def close(self, tid: str) -> None:
        with self._lock:
            self.tabs.pop(tid, None)

    def methods(self) -> list:
        return [e["method"] for e in self.log]

    # ------------------------------------------------------------ HTTP and websocket
    def _handle(self, h) -> None:
        line = h.rfile.readline(65536).decode("latin-1").strip()
        headers = {}
        while True:
            hl = h.rfile.readline(65536).decode("latin-1")
            if hl in ("\r\n", "\n", ""):
                break
            k, _s, v = hl.partition(":")
            headers[k.strip().lower()] = v.strip()
        parts = line.split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]
        if headers.get("upgrade", "").lower() == "websocket" and path.startswith("/devtools/page/"):
            tid = path.rsplit("/", 1)[1]
            if tid not in self.tabs:
                h.wfile.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            key = headers.get("sec-websocket-key", "")
            acc = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
            h.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode("ascii"))
            h.wfile.flush()
            self._ws_loop(h, tid)
            return
        body, status = self._http(method, path)
        data = json.dumps(body).encode("utf-8")
        h.wfile.write(("HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                       % (status, len(data))).encode("ascii") + data)

    def _target(self, t: Tab) -> dict:
        ws = "ws://127.0.0.1:%d/devtools/page/%s" % (self.port, t.id)
        if self.foreign_ws:
            ws = "ws://192.0.2.10:%d/devtools/page/%s" % (self.port, t.id)
        return {"id": t.id, "type": "page", "url": t.url, "title": t.title, "webSocketDebuggerUrl": ws}

    def _http(self, method: str, path: str):
        with self._lock:
            if path == "/json/list" or path == "/json":
                self.log.append({"method": "http:list"})
                return [self._target(t) for t in self.tabs.values()], "200 OK"
            if path == "/json/version":
                return {"Browser": "FakeChrome/1.0"}, "200 OK"
            if path.startswith("/json/new"):
                url = unquote(path.split("?", 1)[1]) if "?" in path else "about:blank"
                tid = self.add_tab("blank")
                self._goto_url(self.tabs[tid], url)
                self.log.append({"method": "http:new", "host": urlsplit(url).hostname})
                return self._target(self.tabs[tid]), "200 OK"
            if path.startswith("/json/close/"):
                tid = path.rsplit("/", 1)[1]
                ok = tid in self.tabs
                self.tabs.pop(tid, None)
                self.log.append({"method": "http:close", "tab": tid})
                return ("Target is closing" if ok else "no target"), ("200 OK" if ok else "404 Not Found")
        return {"error": "unknown"}, "404 Not Found"

    def _recv(self, h):
        b = h.rfile.read(2)
        if len(b) < 2:
            return None, None
        op = b[0] & 0x0F
        n = b[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", h.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", h.rfile.read(8))[0]
        mask = h.rfile.read(4) if b[1] & 0x80 else None
        if b[1] & 0x80 == 0:
            raise ValueError("client frames must be masked")
        data = h.rfile.read(n)
        data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
        return op, data

    @staticmethod
    def _frame(op: int, data: bytes, fin: bool = True) -> bytes:
        n = len(data)
        head = bytes([(0x80 if fin else 0) | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack("!H", n)
        else:
            head += bytes([127]) + struct.pack("!Q", n)
        return head + data

    def _send(self, h, text: str) -> None:
        data = text.encode("utf-8")
        if self.ping_first:
            h.wfile.write(self._frame(0x9, b"hi"))
        if self.fragment and len(data) > 8:
            mid = len(data) // 2
            h.wfile.write(self._frame(0x1, data[:mid], fin=False) + self._frame(0x0, data[mid:], fin=True))
        else:
            h.wfile.write(self._frame(0x1, data))
        h.wfile.flush()

    def _ws_loop(self, h, tid: str) -> None:
        while True:
            try:
                op, data = self._recv(h)
            except (OSError, ValueError):
                return
            if op is None or op == 0x8:
                try:
                    h.wfile.write(self._frame(0x8, b""))
                except OSError:
                    pass
                return
            if op == 0xA:
                self.log.append({"method": "ws:pong"})
                continue
            if op != 0x1:
                continue
            msg = json.loads(data.decode("utf-8"))
            with self._lock:
                try:
                    result = self._call(tid, msg.get("method"), msg.get("params") or {})
                    out = {"id": msg.get("id"), "result": result}
                except _CdpErr as e:
                    out = {"id": msg.get("id"), "error": {"code": e.code, "message": "fake"}}
            try:
                self._send(h, json.dumps(out))
            except OSError:
                return

    # ------------------------------------------------------------ the DOM model
    def _call(self, tid: str, method: str, p: dict) -> dict:
        t = self.tabs.get(tid)
        if t is None:
            raise _CdpErr(-32000)
        if method == "Runtime.evaluate":
            expr = p.get("expression") or ""
            name, value = self._evaluate(t, expr)
            self.log.append({"method": method, "script": name})
            if name is None:
                return {"result": {"type": "object"}, "exceptionDetails": {"text": "not a known script"}}
            return {"result": {"type": "object", "value": value}}
        if method == "Input.dispatchMouseEvent":
            if p.get("type") == "mouseReleased":
                self.log.append({"method": method})
                self._click(t, float(p.get("x", -1)), float(p.get("y", -1)))
            return {}
        if method == "Input.insertText":
            text = p.get("text") or ""
            self.log.append({"method": method, "len": len(text)})
            self._insert(t, text)
            return {}
        if method == "Input.dispatchKeyEvent":
            self.log.append({"method": method, "key": p.get("key") if p.get("key") in ("Enter", "Backspace", "a", "e")
                             else "?"})
            if p.get("type") == "keyDown":
                self._key(t, p)
            return {}
        if method == "Page.navigate":
            url = p.get("url") or ""
            self.log.append({"method": method, "host": urlsplit(url).hostname})
            self._goto_url(t, url)
            return {"frameId": t.id}
        if method == "Page.captureScreenshot":
            self.log.append({"method": method})
            return {"data": base64.b64encode(PNG).decode("ascii")}
        raise _CdpErr(-32601)

    def _text(self, t: Tab) -> str:
        if t.url.startswith("https://mail.google.com/"):
            if self.gmail_security:
                return "Verify it's you. Google needs to verify it's you."
            return "Gmail Inbox Compose Starred Sent Drafts"
        text = t.page.get("text", "")
        if t.error:
            text += "\n" + t.error
        return text

    def _evaluate(self, t: Tab, expr: str):
        if "jobhunter pagefill READ_FIELDS" in expr:
            return "READ_FIELDS", self._read_fields(t)
        if "jobhunter pagefill CAPTCHA_STATE" in expr:
            c = t.page.get("captcha") or {}
            return "CAPTCHA_STATE", {"url": t.url, "frames": int(c.get("frames", 0)),
                                     "containers": int(c.get("containers", 0)),
                                     "text_hint": bool(re.search(r"captcha|i.m not a robot", self._text(t), re.I)),
                                     "visible": bool(c.get("frames") or c.get("containers"))}
        if "jobhunter pagefill PAGE_TEXT" in expr:
            return "PAGE_TEXT", {"url": t.url, "title": t.title, "text": self._text(t)}
        if "jobhunter pagefill GMAIL_LINKS" in expr:
            m = self._gmail_message(t)
            return "GMAIL_LINKS", list(m.get("links") or []) if m else []
        if "jobhunter driver read_gmail_list" in expr:
            return "read_gmail_list", self._gmail_list(t)
        if "jobhunter driver read_gmail_message" in expr:
            return "read_gmail_message", self._gmail_conversation(t)
        return None, None

    def _read_fields(self, t: Tab) -> dict:
        fields = []
        for i, f in enumerate(t.page.get("fields") or []):
            typ = f.get("type", "text")
            fields.append({"i": i, "tag": "textarea" if typ == "textarea" else "input", "type": typ,
                           "name": f.get("name", ""), "id": f.get("id", ""), "aid": f.get("aid", ""),
                           "autocomplete": f.get("autocomplete", ""), "label": f.get("label", ""),
                           "placeholder": f.get("placeholder", ""), "required": bool(f.get("required")),
                           "checked": t.checked[i] if typ in ("checkbox", "radio") else None, "disabled": False,
                           "visible": not f.get("hidden"), "maxlength": f.get("maxlength"),
                           "value_length": None if typ in ("checkbox", "radio") else len(t.values[i]),
                           "masked": typ == "password" and not f.get("unmasked"),
                           "x": 100, "y": 100 + 40 * i, "w": 200, "h": 30})
        buttons = []
        for i, b in enumerate(t.page.get("buttons") or []):
            buttons.append({"i": i, "name": b.get("name", ""), "aid": b.get("aid", ""), "type": "button",
                            "disabled": False, "visible": True, "x": 500, "y": 1000 + 40 * i, "w": 120, "h": 30})
        return {"url": t.url, "title": t.title, "fields": fields, "buttons": buttons, "active": t.focused}

    def _click(self, t: Tab, x: float, y: float) -> None:
        if abs(x - 100) < 100:
            i = int(round((y - 100) / 40.0))
            fields = t.page.get("fields") or []
            if 0 <= i < len(fields):
                if fields[i].get("type") in ("checkbox", "radio"):
                    t.checked[i] = not t.checked[i]
                t.focused = i
                t.selected_all = False
            return
        if abs(x - 500) < 60:
            i = int(round((y - 1000) / 40.0))
            buttons = t.page.get("buttons") or []
            if 0 <= i < len(buttons):
                self._act(t, buttons[i].get("action") or {})

    def _insert(self, t: Tab, text: str) -> None:
        i = t.focused
        fields = t.page.get("fields") or []
        if not (0 <= i < len(fields)):
            return
        if t.selected_all:
            t.values[i] = ""
            t.selected_all = False
        v = t.values[i] + text
        ml = fields[i].get("maxlength")
        t.values[i] = v[:ml] if ml else v

    def _key(self, t: Tab, p: dict) -> None:
        if "selectAll" in (p.get("commands") or []):
            t.selected_all = True
        elif p.get("key") == "Backspace" and 0 <= t.focused < len(t.page.get("fields") or []):
            if t.selected_all:
                t.values[t.focused] = ""
            else:
                t.values[t.focused] = t.values[t.focused][:-1]
            t.selected_all = False
        elif p.get("key") == "Enter":
            act = t.page.get("enter_action")
            if act:
                self._act(t, act)
        elif p.get("key") == "e" and t.url.startswith("https://mail.google.com/"):
            self.archived.append(t.url.rsplit("/", 1)[-1])

    def _field(self, t: Tab, pred) -> int | None:
        for i, f in enumerate(t.page.get("fields") or []):
            if pred(f):
                return i
        return None

    def _act(self, t: Tab, act: dict) -> None:
        t.error = None
        if "goto" in act and len(act) == 1:
            self.set_page(t.id, act["goto"])
            return
        if "create_account" in act:
            spec = act["create_account"]
            fields = t.page.get("fields") or []
            for i, f in enumerate(fields):
                if f.get("type") == "checkbox" and f.get("required") and not t.checked[i]:
                    t.error = "Please accept the required terms."
                    return
            pw = [t.values[i] for i, f in enumerate(fields) if f.get("type") == "password"]
            email_i = self._field(t, lambda f: f.get("type") == "email" or f.get("aid") == "email")
            email = t.values.get(email_i, "") if email_i is not None else ""
            if not email or not pw or any(x != pw[0] for x in pw):
                t.error = "Passwords must match."
                return
            rule = spec.get("password_rule")
            if rule == "alnum_dash_only" and not re.fullmatch(r"[A-Za-z0-9-]+", pw[0]):
                t.error = "Password must contain only letters, numbers and a dash."
                return
            if email in self.accounts and spec.get("exists"):
                self.set_page(t.id, spec["exists"])
                return
            self.accounts[email] = pw[0]
            self.set_page(t.id, spec["next"])
            return
        if "signin" in act:
            spec = act["signin"]
            fields = t.page.get("fields") or []
            pw_i = self._field(t, lambda f: f.get("type") == "password")
            email_i = self._field(t, lambda f: f.get("type") == "email" or f.get("aid") == "email")
            email = t.values.get(email_i, "")
            if self.accounts.get(email) != t.values.get(pw_i) or not email:
                t.error = "Incorrect email or password."
                return
            self.set_page(t.id, spec["next"])
            return
        if "verify_code" in act:
            spec = act["verify_code"]
            typed = "".join(t.values[i] for i, f in enumerate(t.page.get("fields") or [])
                            if f.get("type") in ("text", "tel", "number") and f.get("code"))
            if self.expected_code and typed == self.expected_code:
                self.verified = True
                self.set_page(t.id, spec["next"])
            else:
                t.error = "That code is incorrect. Try again."
            return
        if "submit" in act:
            self.submissions += 1
            self.set_page(t.id, act["submit"])
            return

    def _goto_url(self, t: Tab, url: str) -> None:
        if self.expected_link and url == self.expected_link:
            self.verified = True
            self.set_page(t.id, self.link_page)
            return
        for name, page in self.pages.items():
            if page.get("url") == url:
                self.set_page(t.id, name)
                return
        t.set({"url": url, "title": "", "text": "", "fields": [], "buttons": []}, "url")

    # ------------------------------------------------------------ Gmail on the web
    def _gmail_query(self, t: Tab) -> str | None:
        frag = urlsplit(t.url).fragment
        if frag.startswith("search/"):
            return unquote(frag[len("search/"):].split("/", 1)[0])
        return None

    def _gmail_list(self, t: Tab) -> dict:
        if self.inbox is None or self.gmail_security:
            return {"platform": "gmail", "view": "list", "loaded": False, "count": None, "rows": []}
        q = self._gmail_query(t)
        rows = []
        for m in self.inbox.search(q or ""):
            rows.append({"thread_id": m["thread_id"], "participants": [{"name": "", "email": m["from"]}],
                         "to": [], "subject": m["subject"], "snippet": "", "date": m["received_at"][:17] + "00Z",
                         "unread": True, "url": "https://mail.google.com/mail/u/0/#all/" + m["thread_id"]})
        return {"platform": "gmail", "view": "list", "folder": "search", "query": q, "loaded": True,
                "count": len(rows), "empty": not rows, "complete": True, "rows": rows}

    def _gmail_message(self, t: Tab):
        if self.inbox is None:
            return None
        frag = urlsplit(t.url).fragment
        tid = frag.rsplit("/", 1)[-1] if frag.startswith("all/") else None
        return self.inbox.by_thread(tid) if tid else None

    def _gmail_conversation(self, t: Tab) -> dict:
        m = self._gmail_message(t)
        if m is None or self.gmail_security:
            return {"platform": "gmail", "view": "conversation", "loaded": False, "messages": []}
        self.inbox.read.append(m["thread_id"])
        return {"platform": "gmail", "view": "conversation", "loaded": True, "subject": m["subject"],
                "thread_id": m["thread_id"], "message_count": 1,
                "messages": [{"msg_ref": "gm:" + m["thread_id"], "from": m["from"], "from_name": "",
                              "from_owner": False, "recipients": list(m["to"]), "to": list(m["to"]), "cc": [],
                              "bcc": [], "date": m["received_at"][:17] + "00Z", "expanded": True,
                              "body": m.get("text") or "", "attachments": [], "is_bounce": False,
                              "bounce_addresses": [], "readback_text": None}]}


class _CdpErr(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code
