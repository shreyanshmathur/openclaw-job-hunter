"""Fake of U1's per-site browser consent API (change request "use the existing Chrome logins, with consent":
private/consent.json), used by the U5 tests until the real API lands and to prove that status, the digest
and the Sheet only read it. Every value is a fictional placeholder.

`FakeConsentAPI` is passed as `api=` or installed as a module through `install()` (for status.CONSENT_APIS);
any call to a writer (grant, revoke, forget, record, require_consent) is recorded in `writes` and fails the
test. `write_consent_file()` writes private/consent.json in the shape the consent step records, for the
direct-read fallback."""
from __future__ import annotations

import sys
import types

SITES = ("gmail", "linkedin", "naukri", "indeed", "glassdoor", "foundit", "instahyre", "wellfound")
MODULE_NAME = "tests.fakes.u5.consent_installed"


class ReadOnlyViolation(AssertionError):
    pass


class FakeConsentAPI:
    """summary(conn=None) -> {"chrome_profile", "sites": [{site, state, chrome_profile, granted_at, revoked_at,
    method}]}: the interface U5 asks U1 for."""

    SITES = SITES

    def __init__(self, rows=None, chrome_profile: str | None = "Profile 1", fail: bool = False):
        self.rows = [dict(r) for r in (rows or [])]
        self.chrome_profile = chrome_profile
        self.fail = fail
        self.calls: list[str] = []
        self.writes: list[tuple] = []

    def summary(self, conn=None) -> dict:
        self.calls.append("summary")
        if self.fail:
            raise OSError("consent file unreadable")
        return {"chrome_profile": self.chrome_profile, "sites": [dict(r) for r in self.rows]}

    def _write(self, name, *args, **kwargs):
        self.writes.append((name, args, kwargs))
        raise ReadOnlyViolation("U5 must never write consent (%s)" % name)

    def grant(self, *args, **kwargs):
        return self._write("grant", *args, **kwargs)

    def revoke(self, *args, **kwargs):
        return self._write("revoke", *args, **kwargs)

    def forget(self, *args, **kwargs):
        return self._write("forget", *args, **kwargs)

    def record(self, *args, **kwargs):
        return self._write("record", *args, **kwargs)


class LoadOnlyAPI:
    """An API that only has load() -> {site: row} (another shape U1 may choose)."""

    def __init__(self, mapping: dict):
        self.mapping = mapping

    def load(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.mapping.items()}


def sample_rows() -> list[dict]:
    """Gmail allowed, LinkedIn taken back, Naukri needing a new login; everything else never asked."""
    return [
        {"site": "gmail", "state": "granted", "chrome_profile": "Profile 1", "granted_at": "2026-09-27T09:00:00Z",
         "method": "import"},
        {"site": "linkedin", "state": "revoked", "chrome_profile": "Profile 1", "granted_at": "2026-09-27T09:00:00Z",
         "revoked_at": "2026-09-28T08:00:00Z", "method": "import"},
        {"site": "naukri.com", "state": "granted", "chrome_profile": "Profile 1", "granted_at": "2026-09-27T09:00:00Z",
         "login_check": "expired", "method": "import"},
    ]


class IdentityLikeAPI:
    """Consent functions living next to other identity helpers (the shape U1 may choose): a read-only
    consent_status(conn) and a require_consent(conn, platform) gate that status must never call."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.writes: list[tuple] = []

    def require_consent(self, conn, platform):
        self.writes.append(("require_consent", platform))
        raise ReadOnlyViolation("status must not call the gate check")

    def identity_check(self, conn, platform, observed, cfg=None, cycle_id=None):
        self.writes.append(("identity_check", platform))
        raise ReadOnlyViolation("status must not run identity checks")

    def consent_status(self, conn) -> list:
        return [dict(r) for r in self.rows]


def file_doc() -> dict:
    """private/consent.json as the consent step records it: Gmail allowed from a Chrome profile, LinkedIn
    declined, Naukri revoked, Instahyre logged in by hand, YC allowed (a site outside the fixed list)."""
    base = {"domains": [], "by": "owner", "revoked_at": None, "declined_at": None}
    return {"version": 1, "updated_at": "2026-09-28T02:00:00Z", "sites": {
        "gmail": dict(base, site="gmail", status="granted", method="chrome_import", chrome_profile="Profile 1",
                      chrome_profile_name="Personal", granted_at="2026-09-27T09:00:00Z"),
        "linkedin": dict(base, site="linkedin", status="declined", method=None, chrome_profile=None,
                         chrome_profile_name=None, granted_at=None, declined_at="2026-09-27T09:00:00Z"),
        "naukri": dict(base, site="naukri", status="revoked", method="chrome_import", chrome_profile="Profile 1",
                       chrome_profile_name="Personal", granted_at="2026-09-27T09:00:00Z",
                       revoked_at="2026-09-28T01:30:00Z"),
        "instahyre": dict(base, site="instahyre", status="granted", method="manual_login", chrome_profile=None,
                          chrome_profile_name=None, granted_at="2026-09-27T10:00:00Z"),
        "yc": dict(base, site="yc", status="granted", method="chrome_import", chrome_profile="Profile 2",
                   chrome_profile_name="Side projects", granted_at="2026-09-27T11:00:00Z"),
    }}


def write_consent_file(path: str, doc=None, mode: int = 0o600, raw: str | None = None) -> str:
    """Write `doc` (default file_doc()) or the raw text to `path` with `mode`."""
    import json
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw if raw is not None else json.dumps(doc if doc is not None else file_doc(), indent=2))
    os.chmod(path, mode)
    return path


def install(api: FakeConsentAPI) -> str:
    """Register `api` as an importable module and return its name (for status.CONSENT_MODULES)."""
    mod = types.ModuleType(MODULE_NAME)
    mod.SITES = api.SITES
    mod.summary = api.summary
    mod.grant = api.grant
    mod.revoke = api.revoke
    mod.forget = api.forget
    sys.modules[MODULE_NAME] = mod
    return MODULE_NAME


def uninstall() -> None:
    sys.modules.pop(MODULE_NAME, None)
