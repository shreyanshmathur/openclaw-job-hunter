"""Provider keys: the person's own free-tier keys, never anyone else's (U10, ENRICH-SPEC section 7).

Where keys live (and nowhere else: never environment variables, argv, config.json, secrets.json, the
database or any file an agent can write):
- `keychain` (macOS): login keychain generic passwords, service
  `openclaw-job-hunter.enrich.<provider>.<field>`, account `<install_id>`. Read with
  `/usr/bin/security find-generic-password ... -w` (the key arrives on captured stdout); written with
  `/usr/bin/security -i` and the add command on stdin, so the key never appears in any argv.
- `file` (Linux, WSL, or enrich.key_store = file): private/enrich_keys.json, mode 0600, owned by the user,
  read with O_NOFOLLOW and refused when it is not a regular file, not ours, or readable by others.

`Secret` wraps a value: repr and str are `<redacted>`, it cannot be pickled, and only the transport calls
reveal(). Nothing here prints, logs or returns a key, a key prefix, suffix or length.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

from .. import paths
from ..errors import Denied

SECURITY = "/usr/bin/security"
SERVICE_PREFIX = "openclaw-job-hunter.enrich."
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{16,200}$")
NOT_FOUND_RC = 44
FILE_NAME = "enrich_keys.json"
KEY_FIELDS = {"prospeo": ("api_key",), "hunter": ("api_key",), "tomba": ("key", "secret"),
              "getprospect": ("api_key",), "anymailfinder": ("api_key",), "findymail": ("api_key",),
              "apollo": ("api_key",), "zerobounce": ("api_key",)}
OWN_KEY_NOTICE = ("Paste a key from your own account. Never use a key you found online: it belongs to "
                  "someone else.")


class Secret:
    """A key value that never shows itself."""
    __slots__ = ("_v",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("secret must be text")
        object.__setattr__(self, "_v", value)

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec) -> str:
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("Secret objects cannot be pickled")

    def __setattr__(self, name, value):
        raise AttributeError("Secret is read-only")

    def __eq__(self, other):
        return isinstance(other, Secret) and other._v == self._v

    def __hash__(self):
        return hash("Secret")


class KeystoreError(Exception):
    """kind: unavailable (keychain locked or security failed), refused (key file mode or owner)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- test and runner injection
_test_store: dict | None = None
_runner = None
_backend_override: str | None = None


def use_test_backend(store: dict | None) -> None:
    """Tests only: an in-memory backend {provider: {field: value}}; None switches it off."""
    global _test_store
    _test_store = store


def set_runner(fn) -> None:
    """Tests only: replace subprocess for the keychain backend. fn(argv, input_text) -> (rc, stdout, stderr)."""
    global _runner
    _runner = fn


def set_backend(name: str | None) -> None:
    """Tests only: force 'keychain' or 'file' (None: decide from config and platform)."""
    global _backend_override
    _backend_override = name


def _run(argv: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    if _runner is not None:
        return _runner(list(argv), input_text)
    try:
        res = subprocess.run(argv, input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10, check=False,
                             env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        return 1, "", "security could not run"
    return res.returncode, res.stdout or "", res.stderr or ""


# ---------------------------------------------------------------- backend choice
def _configured_store() -> str:
    try:
        from . import settings
        return settings.load().get("key_store", "auto")
    except Exception:
        return "auto"


def backend() -> str:
    """'keychain' | 'file' (auto = Keychain on macOS when /usr/bin/security exists, else file)."""
    if _backend_override:
        return _backend_override
    want = _configured_store()
    if want in ("keychain", "file"):
        return want
    if sys.platform == "darwin" and os.path.exists(SECURITY):
        return "keychain"
    return "file"


_active_backend = backend


def fields_for(provider: str) -> tuple:
    if provider not in KEY_FIELDS:
        raise Denied("E_VALIDATION", "unknown provider %r" % provider)
    return KEY_FIELDS[provider]


def service(provider: str, field: str) -> str:
    return "%s%s.%s" % (SERVICE_PREFIX, provider, field)


def label(provider: str) -> str:
    return "openclaw-job-hunter email finder key (%s)" % provider


def _install_id() -> str:
    return str(paths.home().get("install_id") or "")


# ---------------------------------------------------------------- keychain backend
def _kc_get(provider: str) -> dict | None:
    out = {}
    for field in fields_for(provider):
        rc, stdout, _err = _run([SECURITY, "find-generic-password", "-s", service(provider, field), "-a",
                                 _install_id(), "-w"])
        if rc == NOT_FOUND_RC:
            return None
        if rc != 0:
            raise KeystoreError("unavailable", "the keychain could not be read (locked or unavailable)")
        value = stdout.strip("\r\n")
        if not value:
            return None
        out[field] = Secret(value)
    return out


def _kc_store(provider: str, values: dict) -> None:
    for field in fields_for(provider):
        cmd = 'add-generic-password -U -s %s -a %s -l "%s" -w %s\n' % (
            service(provider, field), _install_id(), label(provider), values[field])
        rc, _out, _err = _run([SECURITY, "-i"], cmd)
        if rc != 0:
            raise KeystoreError("unavailable", "the keychain refused the item; use --store file or the "
                                               "interactive prompt")


def _kc_delete(provider: str) -> bool:
    removed = False
    for field in fields_for(provider):
        rc, _out, _err = _run([SECURITY, "delete-generic-password", "-s", service(provider, field), "-a",
                               _install_id()])
        if rc == 0:
            removed = True
        elif rc != NOT_FOUND_RC:
            raise KeystoreError("unavailable", "the keychain item could not be removed")
    return removed


def prompt_argv(provider: str, field: str) -> list[str]:
    """Fallback for `enrich connect` when `security -i` is refused: `-w` last makes security prompt on the
    terminal, so the person types the key straight into security (the key is never in argv)."""
    return [SECURITY, "add-generic-password", "-U", "-s", service(provider, field), "-a", _install_id(), "-l",
            label(provider), "-w"]


# ---------------------------------------------------------------- file backend
def key_file() -> str:
    return os.path.join(paths.private_dir(), FILE_NAME)


def _file_read() -> dict:
    path = key_file()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError:
        raise KeystoreError("refused", "private/enrich_keys.json is not a regular file (symlinks are refused)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise KeystoreError("refused", "private/enrich_keys.json is not a regular file")
        if st.st_uid != os.getuid():
            raise KeystoreError("refused", "private/enrich_keys.json is owned by another user")
        if st.st_mode & 0o077:
            raise KeystoreError("refused", "private/enrich_keys.json must have mode 600 (chmod 600 it)")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = None
            raw = fh.read(1_000_000)
    finally:
        if fd is not None:
            os.close(fd)
    try:
        doc = json.loads(raw)
    except ValueError:
        raise KeystoreError("refused", "private/enrich_keys.json is not valid JSON")
    keys = doc.get("keys") if isinstance(doc, dict) else None
    return keys if isinstance(keys, dict) else {}


def _file_write(keys: dict) -> None:
    path = key_file()
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "keys": keys}, fh, sort_keys=True)
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
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _file_get(provider: str) -> dict | None:
    entry = _file_read().get(provider)
    if not isinstance(entry, dict):
        return None
    out = {}
    for field in fields_for(provider):
        v = entry.get(field)
        if not isinstance(v, str) or not v:
            return None
        out[field] = Secret(v)
    return out


# ---------------------------------------------------------------- public API
def validate(provider: str, values: dict) -> dict:
    """Format check at connect time: every field present and matching KEY_RE. Denied(E_VALIDATION)."""
    out = {}
    for field in fields_for(provider):
        v = values.get(field)
        if not isinstance(v, str) or not KEY_RE.match(v.strip()):
            raise Denied("E_VALIDATION", "the %s %s does not look like an API key (16 to 200 letters, digits, "
                                         "dot, dash or underscore); nothing was stored" % (provider, field))
        out[field] = v.strip()
    return out


def get(provider: str) -> dict | None:
    """{'api_key': Secret} (Tomba: {'key', 'secret'}) or None. KeystoreError when the store cannot be read."""
    fields_for(provider)
    if _test_store is not None:
        entry = _test_store.get(provider)
        if not entry:
            return None
        return {f: Secret(entry[f]) for f in fields_for(provider) if f in entry} or None
    if backend() == "keychain":
        return _kc_get(provider)
    return _file_get(provider)


def present(provider: str) -> bool:
    try:
        return get(provider) is not None
    except KeystoreError:
        return False


def _kc_present(provider: str) -> bool:
    """Presence without reading the secret (no -w: security prints attributes only, discarded)."""
    for field in fields_for(provider):
        rc, _out, _err = _run([SECURITY, "find-generic-password", "-s", service(provider, field), "-a",
                               _install_id()])
        if rc == NOT_FOUND_RC:
            return False
        if rc != 0:
            raise KeystoreError("unavailable", "the keychain could not be read (locked or unavailable)")
    return True


def status(provider: str) -> dict:
    """{key: bool, backend, error} for `enrich budget`: never any key text, and the keychain secret is
    not even read."""
    try:
        if _test_store is not None:
            return {"key": get(provider) is not None, "backend": "test", "error": None}
        be = backend()
        ok = _kc_present(provider) if be == "keychain" else _file_get(provider) is not None
        return {"key": ok, "backend": be, "error": None}
    except KeystoreError as exc:
        return {"key": False, "backend": backend(), "error": exc.kind}


def store_prompt(provider: str, runner=None) -> None:
    """Keychain fallback when `security -i` is refused: security itself prompts on the terminal for each
    field (`-w` last), so the person types the key straight into security. Needs a TTY."""
    import subprocess as _sp
    for field in fields_for(provider):
        argv = prompt_argv(provider, field)
        if runner is not None:
            rc = runner(argv)
        else:
            try:
                with open("/dev/tty", "r") as tty:
                    rc = _sp.run(argv, stdin=tty, check=False, timeout=300,
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C"}).returncode
            except (OSError, _sp.SubprocessError):
                rc = 1
        if rc != 0:
            raise KeystoreError("unavailable", "the keychain refused the item")


def store(provider: str, values: dict, backend: str | None = None) -> str:
    """Validate and store one provider's key fields; returns the backend used ('keychain' or 'file')."""
    clean = validate(provider, values)
    if _test_store is not None:
        _test_store[provider] = dict(clean)
        return "test"
    be = backend or _active_backend()
    if be == "keychain":
        _kc_store(provider, clean)
        return "keychain"
    keys = _file_read()     # a refused file (mode, owner, symlink) is never overwritten silently
    keys[provider] = clean
    _file_write(keys)
    return "file"


def delete(provider: str) -> bool:
    """Remove one provider's key from the active backend (and from the key file when present)."""
    fields_for(provider)
    if _test_store is not None:
        return _test_store.pop(provider, None) is not None
    removed = False
    if backend() == "keychain":
        removed = _kc_delete(provider)
    try:
        keys = _file_read()
    except KeystoreError:
        keys = {}
    if provider in keys:
        del keys[provider]
        _file_write(keys)
        removed = True
    return removed


def file_mode_ok() -> tuple[bool, str]:
    """(ok, detail) for selftest: the key file, when present, is a regular 0600 file owned by us."""
    try:
        _file_read()
    except KeystoreError as exc:
        return False, str(exc)
    return True, "absent" if not os.path.exists(key_file()) else "mode 600"
