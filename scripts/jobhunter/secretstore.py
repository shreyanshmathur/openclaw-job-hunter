"""ATS account passwords (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.9) [U1].

Where a site password lives, and nowhere else (never argv, the environment, a log, the database, the Sheet, a
chat message or anything a model can read):
- `keychain` (macOS): a login keychain generic password, service `openclaw-job-hunter.<install_id>.ats`, account
  `<host>|<email>`. Read with `/usr/bin/security find-generic-password -s <svc> -a <acct> -w` (the value arrives on
  captured stdout); written with `/usr/bin/security -i` and the add command on stdin, so the value never appears
  in any argv; removed with `delete-generic-password`.
- `file` (Linux, WSL, or accounts.key_store = "file"): private/ats_accounts.json, mode 0600, owned by the user,
  opened with O_NOFOLLOW, owner and mode checked on every read, written atomically through a temp file in
  private/.

`Secret` wraps a value: repr, str and format are `<redacted>`, it cannot be pickled, and only the code that types
it into the page (cdp.Session.insert_text) reveals it. Errors carry a kind and a fixed message, never the value.
Test hooks: use_test_backend(dict) and set_runner(fn), like the email finder's key store (a separate module, so
no unit imports another unit's private helpers).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

from . import paths
from .errors import Denied

SECURITY = "/usr/bin/security"
NOT_FOUND_RC = 44
LOCKED_RCS = (51, 36, 25308)          # user interaction not allowed, keychain locked
FILE_NAME = "ats_accounts.json"
STORES = ("keychain", "file")
_ACCT_RE = re.compile(r"^[a-z0-9.-]{1,100}\|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")
_SAFE_VALUE_RE = re.compile(r'^[A-Za-z0-9!#%+\-=?@^_*]{8,128}$')


class Secret:
    """A secret value that never shows itself (a password, an email code, a sign-in link)."""
    __slots__ = ("_v",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("secret must be text")
        object.__setattr__(self, "_v", value)

    def reveal(self) -> str:
        return self._v

    def __len__(self) -> int:
        return len(self._v)

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec) -> str:
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("Secret objects cannot be pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("Secret objects cannot be pickled")

    def __setattr__(self, name, value):
        raise AttributeError("Secret is read-only")

    def __eq__(self, other):
        return isinstance(other, Secret) and other._v == self._v

    def __hash__(self):
        return hash("Secret")


class StoreError(Exception):
    """kind: unavailable (keychain locked or security failed), refused (file mode, owner or link), missing."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- test hooks
_test_store: dict | None = None
_runner = None
_backend_override: str | None = None


def use_test_backend(store: dict | None) -> None:
    """Tests only: an in-memory backend {service|account: value}; None switches it off."""
    global _test_store
    _test_store = store


def set_runner(fn) -> None:
    """Tests only: replace subprocess for the keychain backend. fn(argv, input_text) -> (rc, stdout, stderr)."""
    global _runner
    _runner = fn


def set_backend(name: str | None) -> None:
    """Tests only: force 'keychain' or 'file' (None: decide from the config and the platform)."""
    global _backend_override
    _backend_override = name


def _run(argv: list, input_text: str | None = None) -> tuple:
    if _runner is not None:
        return _runner(list(argv), input_text)
    try:
        res = subprocess.run(argv, input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10, check=False,
                             env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        return 1, "", "security could not run"
    return res.returncode, res.stdout or "", res.stderr or ""


# ---------------------------------------------------------------- names
def _install_id() -> str:
    return str(paths.home().get("install_id") or "")


def service() -> str:
    return "openclaw-job-hunter.%s.ats" % _install_id()


def account(host: str, email: str) -> str:
    a = "%s|%s" % (str(host or "").lower(), str(email or "").strip().lower())
    if not _ACCT_RE.match(a):
        raise Denied("E_VALIDATION", "bad account name for the password store")
    return a


def ref(host: str, email: str) -> str:
    """'<service>|<account>' names only (ats_accounts.secret_ref), never the value."""
    return "%s|%s" % (service(), account(host, email))


def backend(cfg: dict | None = None) -> str:
    """'keychain' | 'file' (auto: the Keychain on macOS when /usr/bin/security exists, else the file)."""
    if _backend_override:
        return _backend_override
    want = "auto"
    try:
        if cfg is None:
            from . import config
            cfg = config.load()
        want = ((cfg or {}).get("accounts") or {}).get("key_store") or "auto"
    except Exception:
        want = "auto"
    if want in STORES:
        return want
    if sys.platform == "darwin" and os.path.exists(SECURITY):
        return "keychain"
    return "file"


# ---------------------------------------------------------------- keychain
def _kc_get(acct: str) -> Secret | None:
    rc, out, _err = _run([SECURITY, "find-generic-password", "-s", service(), "-a", acct, "-w"])
    if rc == NOT_FOUND_RC:
        return None
    if rc != 0:
        raise StoreError("unavailable", "the keychain could not be read (locked or unavailable)")
    value = out.strip("\r\n")
    return Secret(value) if value else None


def _kc_put(acct: str, value: Secret) -> None:
    v = value.reveal()
    if not _SAFE_VALUE_RE.match(v):
        raise StoreError("refused", "the password has characters the keychain command cannot carry")
    cmd = 'add-generic-password -U -s "%s" -a "%s" -l "openclaw-job-hunter site account" -w "%s"\n' % (
        service(), acct, v)
    rc, _out, _err = _run([SECURITY, "-i"], cmd)
    if rc != 0:
        raise StoreError("unavailable", "the keychain refused the item (locked or unavailable)")


def _kc_delete(acct: str) -> bool:
    rc, _out, _err = _run([SECURITY, "delete-generic-password", "-s", service(), "-a", acct])
    if rc == 0:
        return True
    if rc == NOT_FOUND_RC:
        return False
    raise StoreError("unavailable", "the keychain item could not be removed")


def _kc_present(acct: str) -> bool:
    rc, _out, _err = _run([SECURITY, "find-generic-password", "-s", service(), "-a", acct])
    return rc == 0


# ---------------------------------------------------------------- file
def file_path() -> str:
    return os.path.join(paths.private_dir(), FILE_NAME)


def _file_read() -> dict:
    path = file_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError:
        raise StoreError("refused", "private/ats_accounts.json is not a regular file (links are refused)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StoreError("refused", "private/ats_accounts.json is not a regular file")
        if st.st_uid != os.getuid():
            raise StoreError("refused", "private/ats_accounts.json belongs to another user")
        if st.st_mode & 0o077:
            raise StoreError("refused", "private/ats_accounts.json must have mode 600 (chmod 600 it)")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = None
            raw = fh.read(1_000_000)
    finally:
        if fd is not None:
            os.close(fd)
    try:
        doc = json.loads(raw)
    except ValueError:
        raise StoreError("refused", "private/ats_accounts.json is not valid JSON")
    items = doc.get("items") if isinstance(doc, dict) else None
    return items if isinstance(items, dict) else {}


def _file_write(items: dict) -> None:
    path = file_path()
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.path.islink(path):
        raise StoreError("refused", "private/ats_accounts.json is a link")
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "items": items}, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- public API
def put(host: str, email: str, value: Secret, store: str | None = None) -> str:
    """Store a password; returns the store used. StoreError when the store refuses."""
    if not isinstance(value, Secret):
        raise TypeError("put() takes a Secret")
    acct = account(host, email)
    store = store or backend()
    if _test_store is not None:
        _test_store["%s|%s" % (service(), acct)] = value.reveal()
        return store
    if store == "keychain":
        _kc_put(acct, value)
    else:
        items = _file_read()
        items[acct] = value.reveal()
        _file_write(items)
    return store


def get(host: str, email: str, store: str | None = None) -> Secret | None:
    acct = account(host, email)
    store = store or backend()
    if _test_store is not None:
        v = _test_store.get("%s|%s" % (service(), acct))
        return Secret(v) if isinstance(v, str) and v else None
    if store == "keychain":
        return _kc_get(acct)
    v = _file_read().get(acct)
    return Secret(v) if isinstance(v, str) and v else None


def delete(host: str, email: str, store: str | None = None) -> bool:
    acct = account(host, email)
    store = store or backend()
    if _test_store is not None:
        return _test_store.pop("%s|%s" % (service(), acct), None) is not None
    if store == "keychain":
        return _kc_delete(acct)
    items = _file_read()
    if acct not in items:
        return False
    del items[acct]
    _file_write(items)
    return True


def present(host: str, email: str, store: str | None = None) -> bool:
    """Whether an item exists, without reading the value (keychain: no -w)."""
    acct = account(host, email)
    store = store or backend()
    if _test_store is not None:
        return "%s|%s" % (service(), acct) in _test_store
    if store == "keychain":
        return _kc_present(acct)
    try:
        return acct in _file_read()
    except StoreError:
        return False


def availability(store: str | None = None) -> dict:
    """{store, available} for doctor (never reads a value)."""
    store = store or backend()
    if store == "keychain":
        ok = os.path.exists(SECURITY) or _runner is not None or _test_store is not None
        return {"store": store, "available": bool(ok)}
    try:
        _file_read()
        return {"store": store, "available": True}
    except StoreError as e:
        return {"store": store, "available": False, "why": str(e)}
