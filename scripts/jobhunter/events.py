"""Event log, notification queue and human tasks (design 1.3.8, 1.5, 12.10).

- log_event: buffered on the connection while a db.tx() is open and appended to
  logs/events-YYYY-MM.jsonl only after COMMIT (a rolled-back change leaves no event line). Outside a
  transaction the line is written at once. Writing the log never fails a command.
- enqueue_notification: one row per dedupe_key (a repeat is ignored); delivered by `notify flush` (U5).
- open_human_task: returns the task uid ('H' + 7); an identical open task (same kind, question and
  refs) is reused instead of duplicated.
All three must run inside the caller's transaction when they write; none of them commits.
"""
from __future__ import annotations

import json
import os

from . import paths
from .canon import new_uid, now
from .errors import Denied

NOTIFY_PRIORITIES = ("high", "normal", "low")
NOTIFY_KINDS = ("approval", "question", "alert", "positive_reply", "info", "digest")
HUMAN_TASK_KINDS = ("apply_manually", "answer_question", "review_reply", "resolve_unknown", "confirm_not_sent",
                    "reset_breaker", "confirm_profile", "relax_gate", "relogin", "confirm_company_merge",
                    "confirm_agency", "review_audit_mismatch", "connect_mail", "suggest_auto")
HUMAN_TASK_REFS = ("job_id", "draft_id", "thread_id", "action_id", "company_id")


def _events_path(ts: str) -> str:
    return os.path.join(paths.logs_dir(), "events-%s.jsonl" % ts[:7])


def _append(records: list[dict]) -> None:
    if not records:
        return
    try:
        os.makedirs(paths.logs_dir(), exist_ok=True)
        by_file: dict[str, list[str]] = {}
        for rec in records:
            line = json.dumps(rec, sort_keys=True, default=str, ensure_ascii=True)
            by_file.setdefault(_events_path(rec.get("ts") or now()), []).append(line)
        for path, lines in by_file.items():
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    except OSError:
        pass  # the database row is the record; the jsonl file is a convenience copy


def log_event(conn, kind: str, **data) -> None:
    """Record one state change. Appended to logs/events-YYYY-MM.jsonl after the transaction commits."""
    if not kind or not isinstance(kind, str):
        raise Denied("E_INTERNAL", "event kind must be a non-empty string")
    rec = {"ts": now(), "kind": kind}
    for k, v in data.items():
        if k not in rec:
            rec[k] = v
    pending = getattr(conn, "jh_pending_events", None)
    if pending is not None and conn.in_transaction:
        pending.append(rec)
    else:
        _append([rec])


def flush_pending(conn) -> None:
    """Called by db.tx() after COMMIT."""
    pending = getattr(conn, "jh_pending_events", None)
    if not pending:
        return
    records = list(pending)
    del pending[:]
    _append(records)


def enqueue_notification(conn, dedupe_key: str, priority: str, kind: str, text: str) -> None:
    """Queue one notification; a row with the same dedupe_key already queued is left as it is."""
    if priority not in NOTIFY_PRIORITIES:
        raise Denied("E_VALIDATION", "priority must be one of %s" % ", ".join(NOTIFY_PRIORITIES))
    if kind not in NOTIFY_KINDS:
        raise Denied("E_VALIDATION", "notification kind must be one of %s" % ", ".join(NOTIFY_KINDS))
    if not dedupe_key or not isinstance(dedupe_key, str):
        raise Denied("E_VALIDATION", "dedupe_key is required")
    if not isinstance(text, str) or not text.strip():
        raise Denied("E_VALIDATION", "notification text is required")
    conn.execute(
        "INSERT INTO notifications (dedupe_key, priority, kind, text, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (dedupe_key) DO NOTHING",
        (dedupe_key, priority, kind, text, now()))


def open_human_task(conn, kind: str, question: str, **refs) -> str:
    """Open a human task and return its uid. refs: job_id, draft_id, thread_id, action_id, company_id
    (integers) and detail (text). An open task with the same kind, question and refs is reused."""
    if kind not in HUMAN_TASK_KINDS:
        raise Denied("E_VALIDATION", "unknown human task kind %r" % kind)
    detail = refs.pop("detail", None)
    unknown = [k for k in refs if k not in HUMAN_TASK_REFS]
    if unknown:
        raise Denied("E_INTERNAL", "unknown human task refs: %s" % ", ".join(sorted(unknown)))
    cols = [k for k in HUMAN_TASK_REFS]
    vals = [refs.get(k) for k in cols]
    where = " AND ".join("%s IS ?" % c for c in cols)
    row = conn.execute(
        "SELECT task_uid FROM human_tasks WHERE done_at IS NULL AND kind = ? AND question IS ? AND " + where,
        [kind, question] + vals).fetchone()
    if row:
        return row[0]
    uid = new_uid("H")
    conn.execute(
        "INSERT INTO human_tasks (task_uid, kind, %s, question, detail, created_at) VALUES (?, ?, %s, ?, ?, ?)"
        % (", ".join(cols), ", ".join("?" for _ in cols)),
        [uid, kind] + vals + [question, detail, now()])
    log_event(conn, "human_task_opened", task_uid=uid, task_kind=kind, **{k: v for k, v in refs.items() if v})
    return uid
