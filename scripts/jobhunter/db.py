"""SQLite state store: connection, transactions, migrations, meta (design section 2).

- One fixed database file: <root>/state/jobhunter.sqlite3 (paths.db_path()).
- connect() checks the home binding (private/home.json repo and install_id against meta) and, for
  writers, that every REQUIRED_META key exists (B4 write guard).
- Pragmas: journal_mode=WAL, foreign_keys=ON, busy_timeout=10000, synchronous=NORMAL; recursive
  triggers stay off (the updated_at stamp triggers rely on it).
- Every write runs inside tx(conn) = BEGIN IMMEDIATE ... COMMIT/ROLLBACK. Trigger RAISE and UNIQUE
  errors leave tx() as errors.Denied (errors.map_sqlite_error). Events logged with events.log_event
  inside the block are appended to logs/events-YYYY-MM.jsonl only after COMMIT.
- Migrations: scripts/jobhunter/migrations/NNNN_name.sql, applied in order under flock(state/.lock),
  each in one transaction, recorded in meta.schema_version.

For convenience the canon and events helpers are re-exported here (db.now(), db.log_event(), ...).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import secrets
import sqlite3
from typing import Iterator

from . import events, paths
from .canon import (canonical_send_text, new_cycle_id, new_token, new_uid, now,  # noqa: F401  (re-exports)
                    sha256_text)
from .errors import Denied, map_sqlite_error
from .events import enqueue_notification, log_event, open_human_task  # noqa: F401  (re-exports)

REQUIRED_META: tuple[str, ...] = (
    "schema_version", "install_id", "jitter_seed", "profile_version", "approval_mode", "tier_gmail",
    "tier_linkedin", "channel_linkedin_enabled", "linkedin_tos_ack", "company_email_cooldown_days",
    "company_apps_per_day", "company_apps_per_30d", "company_apps_per_90d", "li_invites_per_company_per_7d",
    "agency_emails_per_day", "agency_emails_per_30d", "agency_apps_per_day", "agency_apps_per_30d", "max_seen_ts",
)
REQUIRED_FOR_QC: tuple[str, ...] = ("reviewer_prompt_sha256", "qc_agents_md_sha256")

# Defaults written by init (the researched defaults of config.example.json, strictest tier). `config apply`
# later rewrites the limit rows from the effective config.
DEFAULT_META: dict[str, str] = {
    "profile_version": "",
    "approval_mode": "human",
    "tier_gmail": "conservative",
    "tier_linkedin": "conservative",
    "channel_linkedin_enabled": "0",
    "linkedin_tos_ack": "0",
    "company_email_cooldown_days": "90",
    "company_apps_per_day": "1",
    "company_apps_per_30d": "2",
    "company_apps_per_90d": "3",
    "li_invites_per_company_per_7d": "2",
    "agency_emails_per_day": "1",
    "agency_emails_per_30d": "3",
    "agency_apps_per_day": "2",
    "agency_apps_per_30d": "6",
}

META_WRITERS = frozenset({"init", "config_apply", "human", "install", "system"})
# meta keys whose value must be a non-negative integer (the triggers CAST them)
INT_META = frozenset({
    "schema_version", "company_email_cooldown_days", "company_apps_per_day", "company_apps_per_30d",
    "company_apps_per_90d", "li_invites_per_company_per_7d", "agency_emails_per_day", "agency_emails_per_30d",
    "agency_apps_per_day", "agency_apps_per_30d",
})
ENUM_META = {
    "approval_mode": ("human", "auto"),
    "tier_gmail": ("conservative", "moderate"),
    "tier_linkedin": ("conservative", "moderate"),
    "channel_linkedin_enabled": ("0", "1"),
    "linkedin_tos_ack": ("0", "1"),
}

_MIGRATION_RE = re.compile(r"^([0-9]{4})_[a-z0-9_]+\.sql$")


class Connection(sqlite3.Connection):
    """sqlite3 connection that carries the events buffered for the open transaction."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.jh_pending_events: list[dict] = []
        self.jh_deferred: list = []
        self.jh_write = True


def _open(path: str, write: bool) -> Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    if write:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA recursive_triggers=OFF")
    conn.jh_write = write
    if not write:
        conn.execute("PRAGMA query_only=1")
    return conn


# ---------------------------------------------------------------- migrations
def migrations() -> list[tuple[int, str]]:
    """[(version, path)] of the migration files, in order."""
    out = []
    for name in sorted(os.listdir(paths.MIGRATIONS_DIR)):
        m = _MIGRATION_RE.match(name)
        if m:
            out.append((int(m.group(1)), os.path.join(paths.MIGRATIONS_DIR, name)))
    versions = [v for v, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise Denied("E_INTERNAL", "migration files are not numbered 0001, 0002, ... without gaps")
    return out


def latest_version() -> int:
    ms = migrations()
    return ms[-1][0] if ms else 0


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() \
        is not None


def schema_version(conn) -> int:
    if not _has_table(conn, "meta"):
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        raise Denied("E_CONFIG_INVALID", "meta.schema_version is not a number; run ./jobhunter doctor")


@contextlib.contextmanager
def file_lock() -> Iterator[None]:
    """flock(state/.lock): serialises migrations and file staging across processes."""
    os.makedirs(paths.state_dir(), exist_ok=True)
    fd = os.open(paths.lock_file(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def migrate(conn) -> list[int]:
    """Apply pending migrations in order; returns the versions applied. Refuses a database that is newer
    than the code (E_CONFIG_INVALID)."""
    if conn.in_transaction:
        raise Denied("E_INTERNAL", "migrate() must not run inside a transaction")
    applied: list[int] = []
    with file_lock():
        current = schema_version(conn)
        latest = latest_version()
        if current > latest:
            raise Denied("E_CONFIG_INVALID", "the database schema (v%d) is newer than this code (v%d)"
                         % (current, latest))
        for version, path in migrations():
            if version <= current:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                script = fh.read()
            stamp = now()
            script = ("BEGIN IMMEDIATE;\n" + script + "\n"
                      "INSERT INTO meta (key, value, updated_at, updated_by) VALUES ('schema_version', '%d', '%s', "
                      "'system') ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                      "updated_at = excluded.updated_at, updated_by = excluded.updated_by;\nCOMMIT;\n"
                      % (version, stamp))
            try:
                conn.executescript(script)
            except sqlite3.DatabaseError as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise Denied("E_INTERNAL", "migration %04d failed: %s" % (version, exc))
            applied.append(version)
    return applied


# ---------------------------------------------------------------- connect
def missing_meta(conn, keys: tuple[str, ...] = REQUIRED_META) -> list[str]:
    if not _has_table(conn, "meta"):
        return list(keys)
    have = {r[0] for r in conn.execute("SELECT key FROM meta")}
    return [k for k in keys if k not in have]


def _check_install_id(conn, h: dict) -> None:
    if not _has_table(conn, "meta"):
        raise Denied("E_CONFIG_INVALID", "the database has no schema; run ./jobhunter init")
    got = meta_get(conn, "install_id")
    if got is None:
        raise Denied("E_CONFIG_INVALID", "meta.install_id is missing; run ./jobhunter doctor")
    if got != h.get("install_id"):
        raise Denied("E_HOME_MISMATCH", "the database belongs to another install",
                     data={"db_install_id": got, "home_install_id": h.get("install_id")})


def connect(write: bool = True) -> sqlite3.Connection:
    """Open the install's database. Checks the home binding (E_HOME_MISMATCH) and, when write is true,
    applies pending migrations and requires every REQUIRED_META key (E_CONFIG_INVALID)."""
    h = paths.check_home_binding()
    path = paths.db_path()
    if not os.path.exists(path):
        raise Denied("E_CONFIG_INVALID", "the database does not exist; run ./jobhunter init", data={"db_path": path})
    try:
        conn = _open(path, write)
    except sqlite3.DatabaseError as exc:
        raise map_sqlite_error(exc)
    try:
        _check_install_id(conn, h)
        if write:
            if schema_version(conn) != latest_version():
                migrate(conn)   # applies pending files; refuses a schema newer than the code
            missing = missing_meta(conn)
            if missing:
                raise Denied("E_CONFIG_INVALID", "required meta rows are missing; run ./jobhunter doctor",
                             data={"missing": missing})
        elif schema_version(conn) > latest_version():
            raise Denied("E_CONFIG_INVALID", "the database schema is newer than this code")
    except BaseException:
        conn.close()
        raise
    return conn


def init_db(extra_meta: dict | None = None) -> sqlite3.Connection:
    """Create the database if absent, apply migrations and write missing default meta rows (by 'init').
    Idempotent: existing meta rows are never changed. The install id comes from private/home.json; a
    database with another install id is refused (E_HOME_MISMATCH). Used by `init` and tests."""
    h = paths.check_home_binding()
    os.makedirs(paths.state_dir(), exist_ok=True)
    try:
        conn = _open(paths.db_path(), True)
    except sqlite3.DatabaseError as exc:
        raise map_sqlite_error(exc)
    try:
        migrate(conn)
        existing = meta_get(conn, "install_id")
        if existing is not None and existing != h["install_id"]:
            raise Denied("E_HOME_MISMATCH", "the database belongs to another install",
                         data={"db_install_id": existing, "home_install_id": h["install_id"]})
        wanted = dict(DEFAULT_META)
        wanted["install_id"] = h["install_id"]
        wanted["jitter_seed"] = secrets.token_hex(16)
        wanted["max_seen_ts"] = now()
        wanted.update(extra_meta or {})
        with tx(conn):
            for key, value in wanted.items():
                if meta_get(conn, key) is None:
                    meta_set(conn, key, value, "init")
    except BaseException:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------- transactions
@contextlib.contextmanager
def tx(conn) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception. Nested use is a bug (E_INTERNAL): helpers
    and hooks run inside the caller's transaction and never open one. sqlite errors leave as Denied."""
    if conn.in_transaction:
        raise Denied("E_INTERNAL", "nested transaction: helpers must run inside the caller's tx()")
    pending = getattr(conn, "jh_pending_events", None)
    if pending is not None:
        del pending[:]
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.DatabaseError as exc:
        raise map_sqlite_error(exc)
    try:
        yield conn
        if not conn.in_transaction:
            raise Denied("E_INTERNAL", "the transaction was ended inside tx() (a helper committed or rolled back)")
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
        if pending is not None:
            del pending[:]
        _run_deferred(conn)
        if isinstance(exc, sqlite3.DatabaseError):
            raise map_sqlite_error(exc) from exc
        raise
    events.flush_pending(conn)
    _run_deferred(conn)


def defer_write(conn, fn) -> None:
    """Run fn(conn) in its own transaction after the current one ends, whether it commits or rolls back.
    For records that must survive a refusal raised in the same call (a breaker trip before
    E_IDENTITY_MISMATCH, a failed token before E_OBSERVED_MISMATCH, a human task before
    E_COMPANY_AMBIGUOUS). Outside a transaction fn runs at once in a new one."""
    if not conn.in_transaction:
        with tx(conn):
            fn(conn)
        return
    lst = getattr(conn, "jh_deferred", None)
    if lst is None:
        raise Denied("E_INTERNAL", "defer_write needs a jobhunter db connection")
    lst.append(fn)


def _run_deferred(conn) -> None:
    lst = getattr(conn, "jh_deferred", None)
    while lst:
        fn = lst.pop(0)
        try:
            with tx(conn):
                fn(conn)
        except Exception as exc:   # a deferred record must never mask the original result
            events.log_event(conn, "deferred_write_failed", error="%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------- meta
def meta_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def meta_all(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta ORDER BY key")}


def _validate_meta(key: str, value: str) -> None:
    if not isinstance(key, str) or not key or len(key) > 200:
        raise Denied("E_VALIDATION", "bad meta key")
    if not isinstance(value, str):
        raise Denied("E_VALIDATION", "meta values are strings", data={"key": key})
    if key in INT_META and not re.match(r"^[0-9]{1,9}$", value):
        raise Denied("E_VALIDATION", "meta %s must be a non-negative integer" % key, data={"value": value})
    if key in ENUM_META and value not in ENUM_META[key]:
        raise Denied("E_VALIDATION", "meta %s must be one of %s" % (key, ", ".join(ENUM_META[key])),
                     data={"value": value})


def meta_set(conn, key: str, value: str, by: str) -> None:
    """Upsert one meta row. `by` is one of init, config_apply, human, install, system. Authority rules
    (who may loosen what) are enforced by the callers (config, auth), not here."""
    if by not in META_WRITERS:
        raise Denied("E_VALIDATION", "meta writer must be one of %s" % ", ".join(sorted(META_WRITERS)))
    _validate_meta(key, value)
    conn.execute(
        "INSERT INTO meta (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (key, value, now(), by))


def meta_delete(conn, key: str) -> None:
    """Remove an optional meta row (for example a 'raise:<path>' override). REQUIRED_META rows cannot be
    deleted this way."""
    if key in REQUIRED_META:
        raise Denied("E_VALIDATION", "required meta rows cannot be deleted", data={"key": key})
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
