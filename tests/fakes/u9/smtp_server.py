"""A scripted fake SMTP server on 127.0.0.1 (socketserver), for the U9 tests only. No TLS, no relaying:
it records what the client sent and answers from a script.

    srv = FakeSmtpServer().start()
    srv.script["rcpt"] = (550, "5.1.1 The email account that you tried to reach does not exist")
    srv.script["data_end"] = "drop"      # close the connection after the message, without a reply
    ...
    srv.stop()

Script keys: ehlo, auth, mail, rcpt, data_cmd, data_end. Values: (code, text), "drop" (close without a
reply) or "hang" (wait `hang_s` seconds, then close). on_data_cmd(srv) runs when the DATA command arrives.
"""
from __future__ import annotations

import base64
import socketserver
import threading
import time

ACCOUNT = "sam.lee.sender@example.com"
PASSWORD = "abcdefghijklmnop"
# Default reply to the DATA command (the RFC 5321 example text).
DATA_READY = (354, "End data with <CR><LF>.<CR><LF>")


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10

    def _reply(self, code: int, text: str) -> None:
        self.wfile.write(("%d %s\r\n" % (code, text)).encode("ascii"))
        self.wfile.flush()

    def _scripted(self, key: str, default: tuple) -> bool:
        """Send the scripted or default reply. False means the connection must close now."""
        srv = self.server.fake
        act = srv.script.get(key, default)
        if act == "drop":
            return False
        if act == "hang":
            time.sleep(srv.hang_s)
            return False
        self._reply(*act)
        return True

    def handle(self):
        srv = self.server.fake
        srv.connections += 1
        self._reply(220, "fake.example ESMTP ready")
        while True:
            try:
                line = self.rfile.readline(4096)
            except OSError:
                return
            if not line:
                return
            cmd = line.decode("utf-8", "replace").rstrip("\r\n")
            up = cmd.upper()
            srv.transcript.append("AUTH PLAIN <redacted>" if up.startswith("AUTH") else cmd)
            if up.startswith("EHLO") or up.startswith("HELO"):
                act = srv.script.get("ehlo")
                if act:
                    if act in ("drop", "hang"):
                        return
                    self._reply(*act)
                    continue
                self.wfile.write(b"250-fake.example at your service\r\n250-SIZE 35882577\r\n250-8BITMIME\r\n"
                                 b"250-AUTH PLAIN\r\n250 ENHANCEDSTATUSCODES\r\n")
                self.wfile.flush()
            elif up.startswith("AUTH PLAIN"):
                parts = cmd.split(" ", 2)
                ok = False
                if len(parts) == 3:
                    try:
                        _z, user, pw = base64.b64decode(parts[2]).decode("utf-8").split("\0")
                        ok = user == srv.account and pw == srv.password
                    except (ValueError, UnicodeDecodeError):
                        ok = False
                if "auth" in srv.script:
                    if not self._scripted("auth", (235, "2.7.0 Accepted")):
                        return
                elif ok:
                    self._reply(235, "2.7.0 Accepted")
                else:
                    self._reply(535, "5.7.8 Username and Password not accepted")
            elif up.startswith("MAIL FROM"):
                srv.envelope_from.append(cmd[10:].strip())
                if not self._scripted("mail", (250, "2.1.0 OK")):
                    return
            elif up.startswith("RCPT TO"):
                srv.envelope_to.append(cmd[8:].strip())
                if not self._scripted("rcpt", (250, "2.1.5 OK")):
                    return
            elif up == "DATA":
                srv.data_commands += 1
                if srv.on_data_cmd is not None:
                    srv.on_data_cmd(srv)
                if not self._scripted("data_cmd", DATA_READY):
                    return
                if srv.script.get("data_cmd", (354,))[0] != 354:
                    continue
                buf = []
                while True:
                    l2 = self.rfile.readline(65536)
                    if not l2:
                        return
                    if l2 == b".\r\n":
                        break
                    buf.append(l2[1:] if l2.startswith(b"..") else l2)
                data = b"".join(buf)
                srv.received.append(data)
                act = srv.script.get("data_end", (250, "2.0.0 OK 1727600000 fake-id - gsmtp"))
                if act not in ("drop", "hang") and act[0] == 250:
                    srv.messages.append(data)
                if not self._scripted("data_end", (250, "2.0.0 OK 1727600000 fake-id - gsmtp")):
                    return
            elif up == "RSET":
                self._reply(250, "2.1.5 Flushed")
            elif up == "NOOP":
                self._reply(250, "2.0.0 OK")
            elif up == "QUIT":
                self._reply(221, "2.0.0 closing connection")
                return
            else:
                self._reply(502, "5.5.1 Unrecognized command")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeSmtpServer:
    def __init__(self, account: str = ACCOUNT, password: str = PASSWORD):
        self.account = account
        self.password = password
        self.script: dict = {}
        self.hang_s = 1.5
        self.transcript: list[str] = []
        self.envelope_from: list[str] = []
        self.envelope_to: list[str] = []
        self.received: list[bytes] = []     # every message body received after DATA
        self.messages: list[bytes] = []     # bodies answered with 250
        self.data_commands = 0
        self.connections = 0
        self.on_data_cmd = None
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "FakeSmtpServer":
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def sender(self, account: str | None = None, password: str | None = None, timeout: float = 1.0):
        from jobhunter.mail.smtp import SmtpSender
        return SmtpSender(account or self.account, password or self.password, host="127.0.0.1", port=self.port,
                          use_ssl=False, timeout=timeout)
