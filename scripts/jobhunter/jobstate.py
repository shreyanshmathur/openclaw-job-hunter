"""Single writers for jobs.status and drafts.status (design 2.5). Hook-safe: never commits, never opens
a transaction, no network. The graphs below mirror t_job_status_graph and t_draft_status_graph in
schema.sql exactly (tests/test_jobstate.py compares them); the trigger stays the last line.
"""
from __future__ import annotations

from .canon import now
from .errors import Denied
from .events import log_event

JOB_STATUSES = ("new", "prefilter_rejected", "excluded", "duplicate", "eval_queued", "evaluating", "eligible",
                "borderline", "rejected", "apply_queued", "awaiting_approval", "applying", "applied",
                "apply_failed", "needs_human", "closed")

JOB_GRAPH: dict[str, frozenset] = {
    "new": frozenset({"prefilter_rejected", "excluded", "duplicate", "eval_queued"}),
    "eval_queued": frozenset({"evaluating", "excluded", "closed"}),
    "evaluating": frozenset({"eligible", "borderline", "rejected", "needs_human", "eval_queued", "excluded"}),
    "borderline": frozenset({"eligible", "eval_queued", "closed", "excluded"}),
    "rejected": frozenset({"eligible", "eval_queued", "closed", "excluded"}),
    "prefilter_rejected": frozenset({"eligible", "eval_queued", "closed", "excluded"}),
    "eligible": frozenset({"apply_queued", "needs_human", "closed", "excluded", "eval_queued"}),
    "apply_queued": frozenset({"awaiting_approval", "applying", "eligible", "needs_human", "closed", "excluded"}),
    "awaiting_approval": frozenset({"apply_queued", "eligible", "closed", "excluded"}),
    "applying": frozenset({"applied", "apply_failed", "needs_human"}),
    "apply_failed": frozenset({"eligible", "closed", "needs_human"}),
    "needs_human": frozenset({"eligible", "closed", "applied", "excluded"}),
    "excluded": frozenset({"eval_queued"}),
    "closed": frozenset({"eligible"}),
    "duplicate": frozenset(),
    "applied": frozenset(),
}

DRAFT_STATUSES = ("drafted", "lint_failed", "review_pending", "review_failed", "qc_passed", "awaiting_approval",
                  "approved", "skipped_by_human", "dropped_qc", "expired", "sent", "superseded")

DRAFT_GRAPH: dict[str, frozenset] = {
    "drafted": frozenset({"lint_failed", "review_pending", "superseded"}),
    "lint_failed": frozenset({"drafted", "dropped_qc", "superseded"}),
    "review_failed": frozenset({"drafted", "dropped_qc", "superseded"}),
    "review_pending": frozenset({"qc_passed", "review_failed", "superseded"}),
    "qc_passed": frozenset({"approved", "awaiting_approval", "superseded"}),
    "awaiting_approval": frozenset({"approved", "skipped_by_human", "drafted", "expired", "superseded"}),
    "approved": frozenset({"sent", "expired", "superseded"}),
    "skipped_by_human": frozenset(),
    "dropped_qc": frozenset(),
    "expired": frozenset(),
    "sent": frozenset(),
    "superseded": frozenset(),
}

APPROVERS = ("auto", "human:chat", "human:sheet", "human:cli")


def job_transition_allowed(old: str, new: str) -> bool:
    return old == new or new in JOB_GRAPH.get(old, frozenset())


def draft_transition_allowed(old: str, new: str) -> bool:
    return old == new or new in DRAFT_GRAPH.get(old, frozenset())


def set_job_status(conn, job_id: int, new: str, reason: str, by: str) -> None:
    """Move one job to `new` (graph checked here and by the trigger). Same status is a no-op apart
    from the reason. Denied: E_NOT_FOUND, E_VALIDATION (unknown status), E_BAD_TRANSITION."""
    if new not in JOB_STATUSES:
        raise Denied("E_VALIDATION", "unknown job status %r" % new)
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no job with id %r" % job_id)
    old = row[0]
    if not job_transition_allowed(old, new):
        raise Denied("E_BAD_TRANSITION", "job status %s -> %s is not allowed" % (old, new),
                     data={"from": old, "to": new})
    conn.execute("UPDATE jobs SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                 (new, reason, now(), job_id))
    if old != new:
        log_event(conn, "job_status", job_id=job_id, old=old, new=new, reason=reason, by=by)


def set_draft_status(conn, draft_id: int, new: str, reason: str, by: str) -> None:
    """Move one draft to `new` (graph checked here and by the trigger). For new == 'approved' the
    approver is recorded from `by` when the row has none yet (`by` must then be one of auto,
    human:chat, human:sheet, human:cli). Denied: E_NOT_FOUND, E_VALIDATION, E_BAD_TRANSITION."""
    if new not in DRAFT_STATUSES:
        raise Denied("E_VALIDATION", "unknown draft status %r" % new)
    row = conn.execute("SELECT status, approved_by FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no draft with id %r" % draft_id)
    old, approved_by = row[0], row[1]
    if not draft_transition_allowed(old, new):
        raise Denied("E_BAD_TRANSITION", "draft status %s -> %s is not allowed" % (old, new),
                     data={"from": old, "to": new})
    stamp = now()
    if new == "approved" and old != "approved" and approved_by is None:
        if by not in APPROVERS:
            raise Denied("E_QC_NOT_APPROVED", "an approval needs an approver (auto or human:*)", data={"by": by})
        conn.execute("UPDATE drafts SET status = ?, status_reason = ?, approved_by = ?, approved_at = ?, "
                     "updated_at = ? WHERE id = ?", (new, reason, by, stamp, stamp, draft_id))
    else:
        conn.execute("UPDATE drafts SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                     (new, reason, stamp, draft_id))
    if old != new:
        log_event(conn, "draft_status", draft_id=draft_id, old=old, new=new, reason=reason, by=by)
