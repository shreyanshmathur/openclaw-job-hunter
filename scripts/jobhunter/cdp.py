"""Browser control by code over the Chrome DevTools Protocol (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.2) [U6].

Code-owned compound steps (jh.py account create, account signin, code submit, code open-link, the CAPTCHA
screenshot and the read-only CAPTCHA check) drive the agent's own browser profile directly, so a password, an
email code or a sign-in link never passes through a model's tool call, its parameters or its result.

- Endpoint: private/home.json `browser_cdp: {"port": <int>}` (written by the installer from the jobhunter profile's
  cdpPort). Only 127.0.0.1:<port> is ever contacted. GET /json/list gives the targets; a target's
  webSocketDebuggerUrl must be exactly ws://127.0.0.1:<port>/devtools/page/<targetId> or it is refused.
- A stdlib websocket client: HTTP/1.1 upgrade, client masking, text frames, continuation frames, ping/pong,
  close. 10 s per call, 45 s per command budget (Session.deadline).
- Session API: evaluate(script) (only the fixed scripts of pagefill.py and the read-only drivers), click_at(x, y)
  (one mouse press and release: the only way code clicks), insert_text(text or Secret) (Input.insertText),
  press(key), clear_focused(), navigate(url), screenshot(path) (PNG, mode 600), page_text(), close().
- Secrets are passed as secretstore.Secret objects and revealed only into the frame payload; frames are never
  logged; a CdpError carries the CDP method name and error code only, never a parameter or page text.
- Test hooks: set_test_port(port) points at a local fake server (tests/fakes/u6/fake_cdp.py); SLEEP replaces
  time.sleep in the wait loops.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
from urllib.parse import quote

from . import paths
from .errors import Denied

HOST = "127.0.0.1"
CALL_TIMEOUT_S = 10.0
COMMAND_BUDGET_S = 45.0
MAX_MESSAGE = 32 * 1024 * 1024
TARGET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_test_port: int | None = None
SLEEP = time.sleep


class CdpError(Exception):
    """A CDP call failed. Carries the method name and an error code only (never params, values or page text)."""

    def __init__(self, method: str, code="error"):
        super().__init__("cdp %s failed (%s)" % (method, code))
        self.method = method
        self.code = code


def set_test_port(port: int | None) -> None:
    """Tests only: use this loopback port instead of private/home.json browser_cdp."""
    global _test_port
    _test_port = port


def port() -> int:
    """The jobhunter profile's loopback CDP port; Denied(E_ROUTE_UNAVAILABLE) when none is recorded."""
    if _test_port is not None:
        return int(_test_port)
    try:
        h = paths.home()
    except Denied:
        h = {}
    bc = h.get("browser_cdp") if isinstance(h, dict) else None
    p = bc.get("port") if isinstance(bc, dict) else None
    if isinstance(p, bool) or not isinstance(p, int) or not (1024 <= p <= 65535):
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser's control port is not known; run ./jobhunter doctor",
                     data={"reason": "cdp_unknown"})
    return p


def available() -> bool:
    try:
        list_targets()
        return True
    except Denied:
        return False


# ---------------------------------------------------------------- HTTP endpoints
def _http(method: str, path: str, timeout: float = CALL_TIMEOUT_S) -> bytes:
    p = port()
    try:
        s = socket.create_connection((HOST, p), timeout=timeout)
    except OSError:
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser does not answer on its control port",
                     data={"reason": "cdp_unreachable"})
    try:
        s.settimeout(timeout)
        req = "%s %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\nContent-Length: 0\r\n\r\n" % (method, path, HOST, p)
        s.sendall(req.encode("ascii"))
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
            if sum(len(c) for c in chunks) > 4 * 1024 * 1024:
                break
    except OSError:
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser's control port did not answer",
                     data={"reason": "cdp_unreachable"})
    finally:
        s.close()
    raw = b"".join(chunks)
    head, _sep, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or not status[1].isdigit() or int(status[1]) >= 400:
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser's control port refused %s" % path.split("?")[0],
                     data={"reason": "cdp_refused"})
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return body


def _dechunk(body: bytes) -> bytes:
    out = []
    while body:
        line, _sep, rest = body.partition(b"\r\n")
        try:
            n = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out.append(rest[:n])
        body = rest[n + 2:]
    return b"".join(out)


def _ws_url_ok(t: dict, p: int) -> bool:
    tid = t.get("id")
    return (isinstance(tid, str) and bool(TARGET_RE.match(tid)) and
            t.get("webSocketDebuggerUrl") == "ws://%s:%d/devtools/page/%s" % (HOST, p, tid))


def list_targets() -> list:
    """Page targets of the jobhunter profile: [{id, url, title}] (targets with a foreign websocket URL dropped)."""
    p = port()
    try:
        doc = json.loads(_http("GET", "/json/list").decode("utf-8", "replace"))
    except ValueError:
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser's tab list is not readable", data={"reason": "cdp_bad"})
    out = []
    for t in doc if isinstance(doc, list) else []:
        if isinstance(t, dict) and t.get("type", "page") == "page" and _ws_url_ok(t, p):
            out.append({"id": t["id"], "url": str(t.get("url") or ""), "title": str(t.get("title") or "")})
    return out


def find_target(tab_id: str) -> dict | None:
    if not TARGET_RE.match(str(tab_id or "")):
        return None
    for t in list_targets():
        if t["id"] == tab_id:
            return t
    return None


def new_tab(url: str) -> str:
    """Open a new tab of the jobhunter profile on url; returns its target id."""
    body = _http("PUT", "/json/new?" + quote(url, safe=":/?#[]@!$&'()*+,;=%"))
    try:
        t = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser did not open a tab", data={"reason": "cdp_bad"})
    if not isinstance(t, dict) or not _ws_url_ok(t, port()):
        raise Denied("E_ROUTE_UNAVAILABLE", "the agent browser opened a tab with a foreign control URL",
                     data={"reason": "cdp_foreign"})
    return t["id"]


def close_tab(tab_id: str) -> bool:
    if not TARGET_RE.match(str(tab_id or "")):
        return False
    try:
        _http("GET", "/json/close/" + tab_id)
        return True
    except Denied:
        return False


def connect(tab_id: str, budget_s: float = COMMAND_BUDGET_S) -> "Session":
    """A session on one tab. Denied(E_ROUTE_UNAVAILABLE) when the port does not answer or the tab is gone."""
    t = find_target(tab_id)
    if t is None:
        raise Denied("E_ROUTE_UNAVAILABLE", "the tab %s is not open in the agent browser" % tab_id,
                     data={"reason": "tab_not_found"})
    return Session(tab_id, port(), budget_s)


# ---------------------------------------------------------------- websocket
class _WebSocket:
    def __init__(self, p: int, path: str, timeout: float):
        self.sock = socket.create_connection((HOST, p), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, HOST, p, key))
        self.sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            b = self.sock.recv(1)
            if not b:
                raise CdpError("handshake", "closed")
            head += b
            if len(head) > 16384:
                raise CdpError("handshake", "too_long")
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise CdpError("handshake", "status")
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        got = ""
        for line in lines[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip()
        if got != want:
            raise CdpError("handshake", "accept")
        self._buf = b""

    def settimeout(self, t: float) -> None:
        self.sock.settimeout(max(0.05, t))

    def send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            b = self.sock.recv(max(65536, n - len(self._buf)))
            if not b:
                raise CdpError("recv", "closed")
            self._buf += b
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_message(self) -> str:
        parts = []
        total = 0
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            if n > MAX_MESSAGE:
                raise CdpError("recv", "too_large")
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                      # ping
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode == 0x8:                      # close
                try:
                    self.send_frame(0x8, payload[:2])
                except OSError:
                    pass
                raise CdpError("recv", "closed")
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                total += n
                if total > MAX_MESSAGE:
                    raise CdpError("recv", "too_large")
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")
                continue
            raise CdpError("recv", "opcode")

    def close(self) -> None:
        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- session
class Session:
    """One websocket session on one tab of the jobhunter profile."""

    def __init__(self, tab_id: str, p: int, budget_s: float = COMMAND_BUDGET_S):
        self.tab_id = tab_id
        self.port = p
        self.deadline = time.monotonic() + budget_s
        self._id = 0
        try:
            self.ws = _WebSocket(p, "/devtools/page/%s" % tab_id, CALL_TIMEOUT_S)
        except (OSError, CdpError):
            raise Denied("E_ROUTE_UNAVAILABLE", "cannot open the control session of tab %s" % tab_id,
                         data={"reason": "cdp_session"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self) -> str:
        return "Session(tab=%s)" % self.tab_id

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def call(self, method: str, params: dict | None = None) -> dict:
        left = self.remaining()
        if left <= 0:
            raise CdpError(method, "budget")
        self._id += 1
        mid = self._id
        msg = json.dumps({"id": mid, "method": method, "params": params or {}}, ensure_ascii=True)
        try:
            self.ws.settimeout(min(CALL_TIMEOUT_S, left))
            self.ws.send_text(msg)
            del msg
            end = time.monotonic() + min(CALL_TIMEOUT_S, left)
            while True:
                if time.monotonic() > end:
                    raise CdpError(method, "timeout")
                raw = self.ws.recv_message()
                try:
                    doc = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(doc, dict) or doc.get("id") != mid:
                    continue                       # an event or another reply
                if "error" in doc:
                    err = doc.get("error") or {}
                    raise CdpError(method, err.get("code", "error") if isinstance(err, dict) else "error")
                return doc.get("result") or {}
        except socket.timeout:
            raise CdpError(method, "timeout")
        except OSError:
            raise CdpError(method, "io")

    # ------------------------------------------------------------ page
    def evaluate(self, script: str):
        """Run one fixed read-only script (a zero-argument arrow function) and return its JSON value."""
        res = self.call("Runtime.evaluate", {"expression": "(%s)()" % script.strip(), "returnByValue": True,
                                             "awaitPromise": True})
        if res.get("exceptionDetails"):
            raise CdpError("Runtime.evaluate", "exception")
        return (res.get("result") or {}).get("value")

    def click_at(self, x: float, y: float) -> None:
        """One left mouse press and release at page coordinates (the step's one button, a field to focus it,
        the standard terms box)."""
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": kind, "x": float(x), "y": float(y)}
            if kind != "mouseMoved":
                p.update(button="left", clickCount=1)
            self.call("Input.dispatchMouseEvent", p)

    def insert_text(self, value) -> None:
        """Type text into the focused element. A Secret is revealed only into this frame's payload."""
        text = value.reveal() if hasattr(value, "reveal") else str(value)
        try:
            self.call("Input.insertText", {"text": text})
        finally:
            text = None

    def press(self, key: str) -> None:
        """One key press: Enter (a verify field on a page without a button) or a single character."""
        if key == "Enter":
            base = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"}
        elif len(key) == 1 and key.isalnum():
            base = {"key": key, "code": "Key" + key.upper(), "text": key}
        else:
            raise CdpError("Input.dispatchKeyEvent", "key")
        self.call("Input.dispatchKeyEvent", dict(base, type="keyDown"))
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": base["key"], "code": base["code"]})

    def clear_focused(self) -> None:
        """Empty the focused field (select all, then delete) without reading it."""
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA",
                                             "commands": ["selectAll"]})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA"})
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace",
                                             "windowsVirtualKeyCode": 8})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace"})

    def navigate(self, url) -> None:
        u = url.reveal() if hasattr(url, "reveal") else str(url)
        try:
            self.call("Page.navigate", {"url": u})
        finally:
            u = None

    def screenshot(self, path: str) -> str:
        res = self.call("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(res.get("data") or "")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(path, 0o600)
        return path

    def page_text(self) -> dict:
        """{url, title, text} of the page (the first 200,000 characters of document.body.innerText)."""
        from . import pagefill
        v = self.evaluate(pagefill.PAGE_TEXT)
        if not isinstance(v, dict):
            return {"url": "", "title": "", "text": ""}
        return {"url": str(v.get("url") or ""), "title": str(v.get("title") or ""),
                "text": str(v.get("text") or "")[:200000]}

    def close(self) -> None:
        ws, self.ws = getattr(self, "ws", None), None
        if ws is not None:
            ws.close()


def wait(pred, timeout_s: float = 20.0, step_s: float = 0.5):
    """Call pred() until it returns a true value or timeout_s passes (SLEEP between tries); returns the value."""
    end = time.monotonic() + timeout_s
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() >= end:
            return v
        SLEEP(step_s)
