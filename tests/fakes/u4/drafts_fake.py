"""Stand-in for U3 `drafts.create_draft` in U4 tests: stores a plain `resume` draft row and records the
call, without running the QC linter. `status` controls the result (drafted or lint_failed)."""
from __future__ import annotations

import json

from jobhunter import canon, jobstate


class FakeDrafts:
    def __init__(self, status: str = "drafted"):
        self.status = status
        self.calls: list[dict] = []

    def create_draft(self, conn, draft: dict, cycle_id, caller) -> dict:
        self.calls.append({"draft": draft, "cycle_id": cycle_id, "caller": caller})
        job_id = None
        if draft.get("job_uid"):
            row = conn.execute("SELECT id FROM jobs WHERE job_uid = ?", (draft["job_uid"],)).fetchone()
            job_id = row[0]
        body = draft["body"]
        text = canon.canonical_send_text("resume", None, body, None, None)
        uid = canon.new_uid("D")
        stamp = canon.now()
        payload = {"payload": draft.get("payload"), "links": draft.get("links"), "claims": draft.get("claims")}
        cur = conn.execute(
            "INSERT INTO drafts (draft_uid, kind, channel, send_route, job_id, body, payload_json, text_sha256, "
            "status, created_at, updated_at) VALUES (?, 'resume', 'resume', 'none', ?, ?, ?, ?, 'drafted', ?, ?)",
            (uid, job_id, body, json.dumps(payload, sort_keys=True), canon.sha256_text(text), stamp, stamp))
        status = "drafted"
        if self.status == "lint_failed":
            jobstate.set_draft_status(conn, cur.lastrowid, "lint_failed", "lint", "system:qc")
            status = "lint_failed"
        return {"draft_uid": uid, "draft_id": cur.lastrowid, "status": status, "attempt": 1,
                "lint": {"pass": status == "drafted", "blocks": [], "warns": []}}


def pass_qc(conn, draft_uid: str) -> None:
    """drafted -> review_pending -> qc_passed through the single writer."""
    row = conn.execute("SELECT id FROM drafts WHERE draft_uid = ?", (draft_uid,)).fetchone()
    jobstate.set_draft_status(conn, row[0], "review_pending", "test", "test")
    jobstate.set_draft_status(conn, row[0], "qc_passed", "test", "test")
