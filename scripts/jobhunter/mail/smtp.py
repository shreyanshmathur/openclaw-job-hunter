"""SMTP sender with explicit stages (design 2.3.1 step 4).

Stages: connect, ehlo, auth, mail_from, rcpt, data. Before DATA nothing of the message has been transmitted,
so a refusal there frees the slot (`smtp_rejected_before_data`). The caller's `before_data` callback commits
the token as `armed` (and stores the Message-ID) right before the DATA command; if it fails, the session is
reset and closed and nothing is sent. After DATA started:

    250 to the end of data              -> outcome "sent"
    a 4xx or 5xx reply to DATA or to the end of data -> outcome "rejected_at_data" (the server refused it)
    timeout or connection loss           -> outcome "unknown" (it may or may not have been accepted)

`send()` never raises for transport problems; it returns
{ok, outcome, stage_reached, reply_code, reply_text, data_started, error}.
"""
from __future__ import annotations

import smtplib
import socket
import ssl
from email import policy
from email.utils import getaddresses

from . import SMTP_HOST, SMTP_PORT, TIMEOUT_S

STAGES = ("connect", "ehlo", "auth", "mail_from", "rcpt", "data", "done")
EHLO_NAME = "[127.0.0.1]"   # never the machine name: it would end up in the Received header of every email
OUTCOMES = ("sent", "rejected_before_data", "aborted_before_data", "rejected_at_data", "unknown")


def _text(v) -> str:
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return " ".join(str(v or "").split())[:300]


def reply_line(code, text) -> str:
    """'<code> <text>' as used for evidence and for the smtp.json signatures."""
    return ("%s %s" % (code if code is not None else "", _text(text))).strip()


def message_bytes(msg) -> bytes:
    """The exact bytes that go over the wire (CRLF line ends)."""
    return msg.as_bytes(policy=policy.SMTP)


def single_recipient(msg) -> str:
    tos = [a for _n, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", [])) if a]
    if len(tos) != 1:
        raise ValueError("a mailer message has exactly one recipient (got %d)" % len(tos))
    return tos[0]


class SmtpSender:
    """One SMTP session per send. use_ssl=False is for tests against a local fake server only."""

    def __init__(self, account: str, password: str, host: str = SMTP_HOST, port: int = SMTP_PORT,
                 use_ssl: bool = True, timeout: float = TIMEOUT_S, context: ssl.SSLContext | None = None):
        self.account = account
        self._password = password
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.context = context

    def __repr__(self) -> str:
        return "SmtpSender(account=%r, host=%r, port=%r)" % (self.account, self.host, self.port)

    # ------------------------------------------------------------ session
    def _open(self) -> smtplib.SMTP:
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.host, self.port, local_hostname=EHLO_NAME, timeout=self.timeout,
                                    context=self.context or ssl.create_default_context())
        return smtplib.SMTP(self.host, self.port, local_hostname=EHLO_NAME, timeout=self.timeout)

    @staticmethod
    def _close(s) -> None:
        if s is None:
            return
        try:
            s.quit()
        except Exception:
            try:
                s.close()
            except Exception:
                pass

    def _start(self, res: dict):
        """connect, EHLO, AUTH. Returns the session or None (res filled with the refusal)."""
        s = None
        res["stage_reached"] = "connect"
        try:
            s = self._open()
        except smtplib.SMTPConnectError as e:
            res.update(reply_code=e.smtp_code, reply_text=_text(e.smtp_error), error="connect refused")
            return None
        except (OSError, smtplib.SMTPException) as e:
            res.update(reply_text=_text(e), error="%s during connect" % type(e).__name__)
            return None
        try:
            res["stage_reached"] = "ehlo"
            code, resp = s.ehlo()
            if not 200 <= code < 300:
                res.update(reply_code=code, reply_text=_text(resp), error="EHLO refused")
                self._close(s)
                return None
            res["stage_reached"] = "auth"
            try:
                code, resp = s.login(self.account, self._password)
            except smtplib.SMTPAuthenticationError as e:
                res.update(reply_code=e.smtp_code, reply_text=_text(e.smtp_error), error="authentication failed",
                           auth_failed=True)
                self._close(s)
                return None
            except smtplib.SMTPNotSupportedError:
                res.update(reply_text="AUTH not offered", error="authentication not offered")
                self._close(s)
                return None
            return s
        except (OSError, smtplib.SMTPException) as e:
            res.update(reply_code=getattr(e, "smtp_code", None), reply_text=_text(getattr(e, "smtp_error", e)),
                       error="%s at %s" % (type(e).__name__, res["stage_reached"]))
            self._close(s)
            return None

    # ------------------------------------------------------------ public
    def check_login(self) -> dict:
        """Connect, EHLO, AUTH, QUIT. Nothing is sent."""
        res = {"ok": False, "stage_reached": "connect", "reply_code": None, "reply_text": "", "error": None,
               "auth_failed": False}
        s = self._start(res)
        if s is None:
            return res
        res.update(ok=True, stage_reached="done", error=None)
        self._close(s)
        return res

    def send(self, msg, before_data=None) -> dict:
        """Send one message with exactly one recipient. before_data() runs after RCPT TO was accepted and
        before the DATA command; an exception there aborts the session before anything is transmitted."""
        res = {"ok": False, "outcome": "rejected_before_data", "stage_reached": "connect", "reply_code": None,
               "reply_text": "", "data_started": False, "error": None, "auth_failed": False}
        try:
            rcpt = single_recipient(msg)
            payload = message_bytes(msg)
        except Exception as e:  # a bad message never reaches the network
            res.update(error="message not sendable: %s" % e, outcome="aborted_before_data")
            return res
        s = self._start(res)
        if s is None:
            return res
        try:
            res["stage_reached"] = "mail_from"
            code, resp = s.mail(self.account)
            if code != 250:
                res.update(reply_code=code, reply_text=_text(resp), error="MAIL FROM refused")
                self._reset_close(s)
                return res
            res["stage_reached"] = "rcpt"
            code, resp = s.rcpt(rcpt)
            if code not in (250, 251):
                res.update(reply_code=code, reply_text=_text(resp), error="RCPT TO refused")
                self._reset_close(s)
                return res
        except (OSError, smtplib.SMTPException) as e:
            res.update(reply_code=getattr(e, "smtp_code", None), reply_text=_text(getattr(e, "smtp_error", e)),
                       error="%s at %s" % (type(e).__name__, res["stage_reached"]))
            self._reset_close(s)
            return res
        if before_data is not None:
            try:
                before_data()
            except Exception as e:
                res.update(outcome="aborted_before_data", error="not armed: %s: %s" % (type(e).__name__, e))
                self._reset_close(s)
                return res
        res["stage_reached"] = "data"
        res["data_started"] = True
        try:
            code, resp = s.data(payload)
        except smtplib.SMTPDataError as e:          # the DATA command itself was refused
            res.update(outcome="rejected_at_data", reply_code=e.smtp_code, reply_text=_text(e.smtp_error),
                       error="DATA refused")
            self._close(s)
            return res
        except (smtplib.SMTPServerDisconnected, socket.timeout, OSError) as e:
            res.update(outcome="unknown", error="%s after DATA started" % type(e).__name__, reply_text=_text(e))
            self._close_quietly(s)
            return res
        except smtplib.SMTPException as e:
            res.update(outcome="unknown", error="%s after DATA started" % type(e).__name__, reply_text=_text(e))
            self._close_quietly(s)
            return res
        res.update(reply_code=code, reply_text=_text(resp))
        if code == 250:
            res.update(ok=True, outcome="sent", stage_reached="done")
        elif 400 <= code < 600:
            res.update(outcome="rejected_at_data", error="message refused after DATA")
        else:
            res.update(outcome="unknown", error="unexpected reply %s after DATA" % code)
        self._close(s)
        return res

    def _reset_close(self, s) -> None:
        try:
            s.rset()
        except Exception:
            pass
        self._close(s)

    @staticmethod
    def _close_quietly(s) -> None:
        try:
            s.close()
        except Exception:
            pass
