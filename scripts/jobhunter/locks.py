"""Database leases (design 1.1.2, 4.3): the `browser` lease (one browser cycle at a time), pacing targets
(`pace:<agent>:<platform>:<kind>`, `pace:<agent>:dwell`) and other named locks. Rows live in `locks`;
an expired row is free. Every function runs inside the caller's transaction.
"""
from __future__ import annotations

from .canon import now, ts_add
from .errors import Denied


def get(conn, name: str):
    return conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()


def is_held(conn, name: str) -> bool:
    row = get(conn, name)
    return row is not None and row["expires_at"] > now()


def acquire(conn, name: str, holder: str, ttl_s: int) -> bool:
    """Take the lock when it is free, expired, or already ours (then the expiry is extended)."""
    if not name or not holder or ttl_s <= 0:
        raise Denied("E_INTERNAL", "lock name, holder and a positive ttl are required")
    ts = now()
    row = get(conn, name)
    if row is not None and row["expires_at"] > ts and row["holder"] != holder:
        return False
    conn.execute("INSERT INTO locks (name, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, acquired_at = CASE WHEN locks.holder = "
                 "excluded.holder AND locks.expires_at > ? THEN locks.acquired_at ELSE excluded.acquired_at END, "
                 "expires_at = excluded.expires_at", (name, holder, ts, ts_add(ts, seconds=ttl_s), ts))
    return True


def renew(conn, name: str, holder: str, ttl_s: int) -> bool:
    """Extend a lock we hold and that has not expired."""
    ts = now()
    cur = conn.execute("UPDATE locks SET expires_at = ? WHERE name = ? AND holder = ? AND expires_at > ?",
                       (ts_add(ts, seconds=ttl_s), name, holder, ts))
    return cur.rowcount == 1


def release(conn, name: str, holder: str) -> None:
    conn.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))


def release_holder(conn, holder: str) -> int:
    return conn.execute("DELETE FROM locks WHERE holder = ?", (holder,)).rowcount


def prune(conn) -> int:
    return conn.execute("DELETE FROM locks WHERE expires_at <= ?", (now(),)).rowcount
