"""Messages to the person: queue, delivery with a checked result, desktop fallback (design 1.5, M8).

- Rows live in `notifications` (queued with events.enqueue_notification inside the writer's transaction).
- `flush(deliver=True)` builds one chat message of at most `max_items` items, highest priority first, and
  sends it with `openclaw message send` (ocrun.message_send, U1). Rows are marked delivered only when the
  send reports success; otherwise `attempts` grows and `next_attempt_at` backs off 5, 10, 20, 40 minutes,
  then hourly. Inside `owner.notify.quiet_hours` only high items go out.
- High items are also shown at once as a desktop notification (macOS osascript, Linux notify-send) and stay
  on the Sheet's red banner, in `status` and in `inbox` until delivered.
- `owner.notify.channel = "none"` means no chat: rows are marked suppressed after the desktop step, so they
  do not count as undelivered. An empty `owner.notify.to`, or the example number the shipped config carries
  (install.OWNER_PLACEHOLDERS), counts as "no number set" and is handled the same way, with a reason that
  says so instead of a failed send to the example number.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from . import canon, db, paths
from . import sheets_labels as L
from . import status as S
from .errors import Denied
from .install import OWNER_PLACEHOLDERS

PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
BACKOFF_MIN = (5, 10, 20, 40)
MAX_MESSAGE_CHARS = 12000
HEADER = "Job Hunter"


def backoff_minutes(attempts: int) -> int:
    """Minutes to wait after the given number of failed attempts (1 -> 5, 2 -> 10, 3 -> 20, 4 -> 40, then 60)."""
    if attempts <= 0:
        return 0
    return BACKOFF_MIN[attempts - 1] if attempts <= len(BACKOFF_MIN) else 60


def _hm(s: str) -> int | None:
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def in_quiet_hours(config: dict, at: _dt.datetime | None = None) -> bool:
    q = S.cfg(config, "owner.notify.quiet_hours", None)
    if not isinstance(q, (list, tuple)) or len(q) != 2:
        return False
    start, end = _hm(q[0]), _hm(q[1])
    if start is None or end is None or start == end:
        return False
    loc = (at or canon.utcnow()).astimezone(S.tzinfo(config))
    cur = loc.hour * 60 + loc.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def enqueue(conn, kind: str, priority: str, text: str, dedupe: str) -> dict:
    """`notify enqueue`: one row per dedupe key (a repeat is ignored)."""
    with db.tx(conn):
        before = conn.execute("SELECT id FROM notifications WHERE dedupe_key = ?", (dedupe,)).fetchone()
        db.enqueue_notification(conn, dedupe, priority, kind, text)
        row = conn.execute("SELECT id FROM notifications WHERE dedupe_key = ?", (dedupe,)).fetchone()
    return {"id": row[0], "queued": before is None}


def _due(conn, only_high: bool, limit: int | None = None) -> list:
    now = canon.now()
    sql = ("SELECT * FROM notifications WHERE delivered_at IS NULL AND suppressed = 0 "
           "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)")
    if only_high:
        sql += " AND priority = 'high'"
    rows = conn.execute(sql, (now,)).fetchall()
    rows.sort(key=lambda r: (PRIORITY_ORDER.get(r["priority"], 3), r["created_at"], r["id"]))
    return rows if limit is None else rows[:limit]


# ---------------------------------------------------------------- stale items
# An approval or a question can be settled before the next flush (approved in chat, profile confirmed).
# Such rows are not sent: `flush --deliver` marks them suppressed with the reason in last_error.
def _one_row(conn, sql: str, args=()):
    try:
        return conn.execute(sql, args).fetchone()
    except sqlite3.Error:   # a table from another unit may be missing in a partial install
        return None


def _approval_stale(conn, key: str) -> str | None:
    parts = key.split(":")
    code = parts[1] if len(parts) > 1 else ""
    if not code:
        return None
    row = _one_row(conn, "SELECT a.closed_at, d.status, d.text_sha256 FROM approval_codes a "
                         "JOIN drafts d ON d.id = a.draft_id WHERE a.code = ?", (code,))
    if row is None:   # unknown code (queued by hand, or pruned): keep the message
        return None
    if row[0]:
        return "approval %s is closed" % code
    if row[1] != "awaiting_approval":
        return "approval %s is no longer waiting" % code
    sha8 = parts[2] if len(parts) > 2 else ""
    if sha8 and row[2] and not str(row[2]).startswith(sha8):
        return "approval %s text changed" % code
    return None


def _task_stale(conn, kind: str, like: str | None = None) -> bool:
    """True when every task of `kind` (whose question contains `like`, if given) is done; False when one is
    still open or none exists (then the message is kept)."""
    where, args = "kind = ?", [kind]
    if like:
        where += " AND instr(coalesce(question, ''), ?) > 0"
        args.append(like)
    row = _one_row(conn, "SELECT sum(done_at IS NULL), sum(done_at IS NOT NULL) FROM human_tasks WHERE " + where,
                   args)
    if row is None:
        return False
    open_n, done_n = int(row[0] or 0), int(row[1] or 0)
    return open_n == 0 and done_n > 0


def stale_reason(conn, r) -> str | None:
    """Why a queued approval or question no longer needs to be sent, or None."""
    key = str(r["dedupe_key"] or "")
    if r["kind"] == "approval" or key.startswith("approval:"):
        return _approval_stale(conn, key) if key.startswith("approval:") else None
    if r["kind"] != "question":
        return None
    if key.startswith("profile:questions:"):
        return "the profile questions are answered" if _task_stale(conn, "confirm_profile") else None
    if key.startswith("feasibility:"):
        code = key.split(":")[1]
        if code and _task_stale(conn, "relax_gate", "the %s check" % code):
            return "the filter question is answered"
    return None


def _drop_stale(conn, rows: list, mark: bool) -> tuple[list, int]:
    keep, stale = [], []
    for r in rows:
        why = stale_reason(conn, r)
        if why:
            stale.append((why, r["id"]))
        else:
            keep.append(r)
    if stale and mark:
        with db.tx(conn):
            conn.executemany("UPDATE notifications SET suppressed = 1, last_error = ? WHERE id = ? "
                             "AND delivered_at IS NULL", [(L.clip("no longer needed: " + w, 300), i)
                                                           for w, i in stale])
    return keep, len(stale)


def compose(rows: list) -> tuple[str, int]:
    """One chat message: a header line, then the items separated by blank lines. Returns (text, n) where n
    is how many of `rows` (in order) fit under MAX_MESSAGE_CHARS; at least one always fits (clipped)."""
    parts = []
    total = 0
    for r in rows:
        text = str(r["text"]).strip()
        if parts and total + len(text) > MAX_MESSAGE_CHARS:
            break
        parts.append(text[:MAX_MESSAGE_CHARS])
        total += len(text) + 2
    if len(parts) == 1:
        return "%s: %s" % (HEADER, parts[0]), 1
    return "%s: %d updates\n\n%s" % (HEADER, len(parts), "\n\n".join(parts)), len(parts)


def desktop_notify(title: str, text: str) -> bool:
    """Show a desktop notification; False when no notifier exists. Text goes through argv, never a script."""
    title = L.clip(" ".join(str(title).split()), 80)
    text = L.clip(" ".join(str(text).split()), 220)
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            argv = ["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                    title, text]
        elif shutil.which("notify-send"):
            argv = ["notify-send", "--", title, text]
        else:
            return False
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             timeout=10, check=False)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _default_sender(channel: str, target: str, message_file: str) -> dict:
    try:
        mod = importlib.import_module("jobhunter.ocrun")
        fn = getattr(mod, "message_send")
        res = fn(channel, target, message_file)
    except (ImportError, AttributeError, NotImplementedError) as exc:
        return {"ok": False, "error": "openclaw message send is not available: %s" % exc}
    except Denied as d:
        return {"ok": False, "error": "%s: %s" % (d.code, d.message)}
    if not isinstance(res, dict):
        return {"ok": False, "error": "unexpected result from message_send"}
    return {"ok": res.get("ok") is True, "error": res.get("error")}


def _write_message(text: str) -> str:
    d = os.path.join(paths.state_dir(), "tmp")
    os.makedirs(d, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="notify-", suffix=".txt", dir=d)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def chat_target(config: dict) -> tuple[str, str, str | None]:
    """(channel, to, why_not). why_not is None when a chat message can be sent, else a sentence for the person:
    the channel is `none`, or `owner.notify.to` is empty or still the example number (same rule as
    install.notify_target)."""
    channel = str(S.cfg(config, "owner.notify.channel", "none") or "none").strip() or "none"
    target = str(S.cfg(config, "owner.notify.to", "") or "").strip()
    if channel == "none":
        return channel, target, "owner.notify.channel is none"
    if not target:
        return channel, target, "owner.notify.to is empty: put your own %s number or id in private/config.json" % channel
    if target in OWNER_PLACEHOLDERS:
        return channel, target, ("owner.notify.to is still the example number %s: your chat number was never set. "
                                 "Put your own %s number or id in private/config.json" % (target, channel))
    return channel, target, None


def send_text(text: str, config: dict | None = None, sender=None) -> dict:
    """Send one message to the owner's chat now (used by `notify test` and flush)."""
    config = S.load_config() if config is None else config
    channel, target, why_not = chat_target(config)
    if why_not:
        return {"ok": False, "error": why_not, "channel": channel, "not_configured": True}
    path = _write_message(text)
    try:
        res = (sender or _default_sender)(channel, target, path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    res = dict(res or {})
    res["ok"] = res.get("ok") is True
    res["channel"] = channel
    return res


def flush(conn, deliver: bool, max_items: int = 8, *, sender=None, desktop=None, config: dict | None = None) -> dict:
    """Deliver queued notifications (manages its own transactions). Without deliver: return the text only."""
    config = S.load_config() if config is None else config
    max_items = max(1, min(int(max_items or 8), 20))
    quiet = in_quiet_hours(config)
    rows, n_stale = _drop_stale(conn, _due(conn, quiet), mark=deliver)
    rows = rows[:max_items]
    pending_high = S.undelivered_high_count(conn)
    if not rows:
        return {"delivered": 0, "failed": 0, "pending_high": pending_high, "quiet_hours": quiet, "sent": False,
                "stale": n_stale}
    text, n_fit = compose(rows)
    if not deliver:
        return {"delivered": 0, "failed": 0, "pending_high": pending_high, "quiet_hours": quiet, "sent": False,
                "count": n_fit, "text": text, "stale": n_stale}

    shown = 0
    if S.cfg(config, "owner.notify.desktop", True):
        show = desktop or desktop_notify
        for r in rows:
            if r["priority"] == "high" and not r["desktop_shown_at"]:
                if show(HEADER, r["text"]):
                    with db.tx(conn):
                        conn.execute("UPDATE notifications SET desktop_shown_at = ? WHERE id = ?", (canon.now(), r["id"]))
                    shown += 1

    channel, _target, why_not = chat_target(config)
    ids = [r["id"] for r in rows]
    if why_not:
        reason = "no chat channel" if channel == "none" else "chat number not set"
        with db.tx(conn):
            conn.executemany("UPDATE notifications SET suppressed = 1, last_error = ? WHERE id = ?",
                             [(reason, i) for i in ids])
        return {"delivered": 0, "failed": 0, "suppressed": len(ids), "desktop_shown": shown,
                "pending_high": S.undelivered_high_count(conn), "quiet_hours": quiet, "sent": False,
                "stale": n_stale, "not_sent_reason": why_not}

    included = rows[:n_fit]   # only the items that fit in the composed message count as sent
    res = send_text(text, config, sender)
    now = canon.now()
    with db.tx(conn):
        if res["ok"]:
            conn.executemany("UPDATE notifications SET delivered_at = ?, delivered_via = ?, attempts = attempts + 1, "
                             "last_error = NULL WHERE id = ?", [(now, channel, r["id"]) for r in included])
        else:
            err = L.clip(str(res.get("error") or "not delivered"), 300)
            for r in included:
                n = int(r["attempts"] or 0) + 1
                conn.execute("UPDATE notifications SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                             (n, err, canon.ts_add(now, minutes=backoff_minutes(n)), r["id"]))
        db.log_event(conn, "notify_flush", ok=res["ok"], items=len(included), channel=channel)
    return {"delivered": len(included) if res["ok"] else 0, "failed": 0 if res["ok"] else len(included),
            "desktop_shown": shown, "pending_high": S.undelivered_high_count(conn), "quiet_hours": quiet,
            "sent": res["ok"], "error": None if res["ok"] else res.get("error"), "stale": n_stale}
