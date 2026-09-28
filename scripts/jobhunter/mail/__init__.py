"""Gmail transport (design 2.3.1, 2.3.2, 1.3.7): the email route, credentials, transports and the connection test.

Routes (`gmail.route`):
- `web_ui` (the default): the outreach and applier agents use Gmail in the `jobhunter` browser profile, whose
  Google session was imported from the person's own Chrome with their consent. Prechecks, the guarded send with
  read-back, the Sent-folder confirmation, replies and bounces, and the daily Sent audit all run in the browser
  lane (skill jobhunter-gmail-web). Nothing here needs IMAP, SMTP or an app password on this route: every code
  path answers with a `browser_lane(...)` result instead of failing. When the person also connected an app
  password (optional), the mailer uses it read-only to double-check unknown web sends and the Sent audit.
- `app_password` (optional): the model writes and QC approves; code sends the approved bytes over SMTP
  (smtp.gmail.com:465, SSL) and reads over IMAP (imap.gmail.com:993, SSL), both with a Google app password.

Credentials
- account: the Gmail address (`owner.gmail_address`), recorded at `mail connect` as `mail_account` in
  private/secrets.json (mode 600, shared with the Sheet secrets; other keys are kept).
- password: the 16-letter Google app password, stored either in the macOS Keychain through the `security`
  command line tool (service `openclaw-job-hunter.<install_id>`, account = the Gmail address; the password is
  handed to `security` on stdin, never as a command-line argument) or in private/secrets.json under
  `mail_app_password`. `mail_store` in secrets.json says which one.
- The password is never printed, logged, put in an exception message, passed on a command line or in the
  environment, and no agent can read private/ (guard rule R3).

Transports: `smtp_sender()` and `imap_client()` build the real SSL clients; tests replace them with
`set_test_transport()` (plain TCP to a local fake server). Nothing in this package opens a connection at
import time.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile

from .. import paths
from ..errors import Denied

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
TIMEOUT_S = 30.0
MESSAGE_ID_DOMAIN = "jobhunter.invalid"
MAILER_AGENT = "system:mailer"
SECURITY_BIN = "/usr/bin/security"
SECRETS_FILE = "secrets.json"
PLACEHOLDER_ADDRESSES = frozenset({"you@example.com", ""})
STORES = ("keychain", "file")
ROUTE_WEB = "web_ui"
ROUTE_CODE = "app_password"
DEFAULT_ROUTE = ROUTE_WEB
ROUTES = (ROUTE_WEB, ROUTE_CODE)
BROWSER_LANE = "browser_lane"
WEB_SKILL = "jobhunter-gmail-web"
_BROWSER_WORK = {
    "send": "approved emails are sent by the outreach and applier agents in Gmail in the browser (reserve, type, "
            "read back, arm, one click, Sent-folder confirmation)",
    "precheck": "the Sent, Outbox and Scheduled searches run in the browser (gate precheck-plan, then gate precheck "
                "--file)",
    "fetch": "replies and delivery failures are read in the browser by the replies lane (reply pending, reply record)",
    "audit": "the Sent folder is read in the browser by the replies lane once a day (mail audit --file)",
    "history": "past sends are read from the Sent folder in the browser by the replies lane (mail audit --file with "
               "purpose history)",
    "test": "Gmail is used through the browser session; no app password is needed (./jobhunter doctor checks the "
            "browser login)",
}

_APP_PW_RE = re.compile(r"^[a-z]{16}$")
_ADDR_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_SAFE_KC_RE = re.compile(r"^[A-Za-z0-9@._+-]{1,200}$")

# test hooks (never set by production code)
_test_transport: dict = {"smtp": None, "imap": None}
_security_runner = None


class MailError(Denied):
    """E_MAIL_TRANSPORT with the transport stage and the server reply (never the password)."""

    def __init__(self, message: str, stage: str = "", reply: str = "", auth_failed: bool = False,
                 reply_code: int | None = None):
        super().__init__("E_MAIL_TRANSPORT", message, data={"stage": stage, "reply": (reply or "")[:300],
                                                            "reply_code": reply_code})
        self.stage = stage
        self.reply = (reply or "")[:300]
        self.reply_code = reply_code
        self.auth_failed = auth_failed


class Credentials:
    """Account and app password. repr() and str() never show the password."""

    __slots__ = ("account", "_password")

    def __init__(self, account: str, password: str):
        self.account = account
        self._password = password

    @property
    def password(self) -> str:
        return self._password

    def __repr__(self) -> str:
        return "Credentials(account=%r, password=<hidden>)" % self.account

    __str__ = __repr__


# ---------------------------------------------------------------- validation
def normalize_app_password(raw: str | None) -> str:
    """Google shows app passwords as four groups of four letters; spaces are dropped, case is folded."""
    pw = "".join((raw or "").split()).lower()
    if not _APP_PW_RE.match(pw):
        raise Denied("E_VALIDATION", "that is not a Google app password (16 letters, spaces optional)")
    return pw


def valid_address(addr: str | None) -> bool:
    return bool(addr) and bool(_ADDR_RE.match(addr)) and len(addr) <= 254


def owner_address(cfg: dict) -> str:
    """owner.gmail_address from the effective config; the placeholder is refused."""
    addr = ((cfg.get("owner") or {}).get("gmail_address") or "").strip().lower()
    if addr in PLACEHOLDER_ADDRESSES or not valid_address(addr):
        raise Denied("E_CONFIG_INVALID", "set owner.gmail_address in private/config.json to the Gmail address "
                     "you send from")
    return addr


def owner_name(cfg: dict) -> str:
    owner = cfg.get("owner") or {}
    sig = owner.get("signature") or {}
    full = (sig.get("full_name") or "").strip()
    if not full:
        full = " ".join(x for x in ((owner.get("first_name") or "").strip(), (owner.get("last_name") or "").strip())
                        if x)
    return full


def route(cfg: dict) -> str:
    """`app_password` only when configured exactly so; anything else is the browser route, as in gate.email_route."""
    r = (cfg.get("gmail") or {}).get("route") or DEFAULT_ROUTE
    return ROUTE_CODE if r == ROUTE_CODE else ROUTE_WEB


def is_web_route(cfg: dict) -> bool:
    return route(cfg) == ROUTE_WEB


def browser_lane(what: str) -> dict:
    """The answer of a code path that has nothing to do on the web_ui route: who does the work instead."""
    return {"route": ROUTE_WEB, "handled_by": BROWSER_LANE, "skill": WEB_SKILL, "what": what,
            "message": "gmail.route is web_ui, so the browser lane handles this: %s"
                       % _BROWSER_WORK.get(what, "see skill %s" % WEB_SKILL)}


# ---------------------------------------------------------------- secrets.json (shared with the Sheet secrets)
def _secrets_path() -> str:
    return os.path.join(paths.private_dir(), SECRETS_FILE)


def load_secrets() -> dict:
    try:
        with open(_secrets_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_secrets(data: dict) -> None:
    """Atomic write with mode 600. Callers hold db.file_lock() and merge with load_secrets()."""
    d = paths.private_dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, _secrets_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.chmod(_secrets_path(), 0o600)


def _update_secrets(update: dict, remove: tuple = ()) -> None:
    from .. import db
    with db.file_lock():
        data = load_secrets()
        for k in remove:
            data.pop(k, None)
        data.update(update)
        _write_secrets(data)


# ---------------------------------------------------------------- macOS Keychain (security CLI)
def keychain_available() -> bool:
    return _security_runner is not None or (sys.platform == "darwin" and os.path.exists(SECURITY_BIN))


def keychain_service() -> str:
    iid = str(paths.home().get("install_id") or "default")
    return "openclaw-job-hunter.%s" % re.sub(r"[^A-Za-z0-9]", "", iid)


def _run_security(args: list[str], stdin: bytes | None = None) -> tuple[int, bytes]:
    """Run the security tool. The password never appears in args (see keychain_store)."""
    if _security_runner is not None:
        return _security_runner(args, stdin)
    try:
        p = subprocess.run([SECURITY_BIN] + args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return 1, b""
    return p.returncode, p.stdout or b""


def keychain_store(account: str, password: str) -> None:
    """Add or update the generic password item. The command goes to `security -i` on stdin with the
    password hex-encoded (-X), so it never shows in the process list."""
    svc = keychain_service()
    if not (_SAFE_KC_RE.match(account) and _SAFE_KC_RE.match(svc)):
        raise Denied("E_VALIDATION", "account or service name has characters the Keychain command cannot take")
    line = "add-generic-password -U -a %s -s %s -X %s\n" % (account, svc, password.encode("utf-8").hex())
    rc, _out = _run_security(["-i"], stdin=line.encode("ascii"))
    if rc != 0:
        raise Denied("E_PRECONDITION", "the Keychain did not accept the app password (security exit %d)" % rc)


def keychain_read(account: str) -> str | None:
    rc, out = _run_security(["find-generic-password", "-a", account, "-s", keychain_service(), "-w"])
    if rc != 0:
        return None
    pw = out.decode("utf-8", "replace").strip()
    return pw or None


def keychain_delete(account: str) -> bool:
    rc, _out = _run_security(["delete-generic-password", "-a", account, "-s", keychain_service()])
    return rc == 0


# ---------------------------------------------------------------- credentials
def default_store() -> str:
    return "keychain" if keychain_available() else "file"


def store_credentials(account: str, password: str, store: str | None = None) -> str:
    """Store the app password (keychain or file) and record the account. Returns the store used. A
    keychain write that cannot be read back falls back to the file store."""
    account = account.strip().lower()
    if not valid_address(account):
        raise Denied("E_VALIDATION", "not an email address")
    password = normalize_app_password(password)
    store = store or default_store()
    if store not in STORES:
        raise Denied("E_VALIDATION", "store must be keychain or file")
    if store == "keychain":
        if not keychain_available():
            raise Denied("E_PRECONDITION", "the macOS Keychain tool is not available here; use --store file")
        try:
            keychain_store(account, password)
            ok = keychain_read(account) == password
        except Denied:
            ok = False
        if ok:
            _update_secrets({"mail_account": account, "mail_store": "keychain"}, remove=("mail_app_password",))
            return "keychain"
        store = "file"
    _update_secrets({"mail_account": account, "mail_store": "file", "mail_app_password": password})
    return "file"


def stored_account() -> str | None:
    acct = load_secrets().get("mail_account")
    return acct if isinstance(acct, str) and acct else None


def credentials() -> Credentials:
    """The stored account and app password; Denied(E_PRECONDITION) when mail is not connected."""
    s = load_secrets()
    account = s.get("mail_account")
    if not isinstance(account, str) or not account:
        raise Denied("E_PRECONDITION", "email is not connected; run ./jobhunter mail connect")
    store = s.get("mail_store") or ("file" if s.get("mail_app_password") else "keychain")
    pw = None
    if store == "keychain":
        pw = keychain_read(account)
    else:
        v = s.get("mail_app_password")
        pw = v if isinstance(v, str) and v else None
    if not pw:
        raise Denied("E_PRECONDITION", "the app password is missing from the %s; run ./jobhunter mail connect"
                     % ("Keychain" if store == "keychain" else "secrets file"))
    return Credentials(account, pw)


def is_connected(conn) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'mail_connected_at'").fetchone()
    return bool(row and row[0]) and stored_account() is not None


# ---------------------------------------------------------------- transports
def set_test_transport(smtp=None, imap=None) -> None:
    """Tests only: callables (Credentials) -> SmtpSender / ImapClient pointing at local fakes."""
    _test_transport["smtp"] = smtp
    _test_transport["imap"] = imap


def clear_test_transport() -> None:
    _test_transport["smtp"] = None
    _test_transport["imap"] = None


def smtp_sender(creds: Credentials | None = None):
    from .smtp import SmtpSender
    creds = creds or credentials()
    if _test_transport["smtp"] is not None:
        return _test_transport["smtp"](creds)
    return SmtpSender(creds.account, creds.password)


def imap_client(creds: Credentials | None = None):
    from .imap import ImapClient
    creds = creds or credentials()
    if _test_transport["imap"] is not None:
        return _test_transport["imap"](creds)
    return ImapClient(creds.account, creds.password)


def test_connection(creds: Credentials | None = None) -> dict:
    """SMTP login and IMAP login (nothing is sent, no message is read). {smtp_ok, imap_ok, account, ...}."""
    try:
        creds = creds or credentials()
    except Denied as d:
        return {"smtp_ok": False, "imap_ok": False, "account": stored_account(), "error": d.message}
    out = {"account": creds.account, "smtp_ok": False, "imap_ok": False}
    sender = smtp_sender(creds)
    r = sender.check_login()
    out["smtp_ok"] = bool(r.get("ok"))
    if not r.get("ok"):
        out["smtp_error"] = {"stage": r.get("stage_reached"), "reply_code": r.get("reply_code"),
                             "reply": r.get("reply_text")}
    imap = imap_client(creds)
    try:
        imap.open()
        out["imap_ok"] = True
        out["imap_gmail"] = bool(getattr(imap, "gmail", False))
    except MailError as e:
        out["imap_error"] = {"stage": e.stage, "reply": e.reply}
    finally:
        imap.close()
    return out


def connect(conn, account: str, password: str, store: str | None = None) -> dict:
    """`mail connect`: log in to SMTP and IMAP with the new app password, then store it and set
    meta.mail_connected_at. Nothing is stored when a login fails."""
    from .. import db
    from ..canon import now
    account = (account or "").strip().lower()
    if not valid_address(account):
        raise Denied("E_VALIDATION", "not an email address")
    creds = Credentials(account, normalize_app_password(password))
    res = test_connection(creds)
    if not (res["smtp_ok"] and res["imap_ok"]):
        raise Denied("E_MAIL_TRANSPORT", "Gmail refused the login; nothing was stored. Check the address, that "
                     "2-Step Verification is on, and paste a new app password",
                     data={k: v for k, v in res.items() if k != "account"})
    used = store_credentials(account, creds.password, store)
    ts = now()
    with db.tx(conn):
        db.meta_set(conn, "mail_connected_at", ts, "human")
        conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'mail connected' WHERE kind = 'connect_mail' "
                     "AND done_at IS NULL", (ts,))
        db.log_event(conn, "mail_connected", account=account, store=used)
    return {"account": account, "store": used, "smtp_ok": True, "imap_ok": True, "connected_at": ts}
