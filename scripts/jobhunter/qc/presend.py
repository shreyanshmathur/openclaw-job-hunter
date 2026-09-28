"""Pre-send check, called by gate.reserve (U1) through hooks.presend inside its BEGIN IMMEDIATE transaction.

presend(conn, draft_id) -> {"ok", "sha256", "send_text", "blocks", "code"}

Hook rules (design 11 rule 5): never commits, never opens a transaction, no network, fast. It re-computes the
canonical send text from the stored row (signature and attachment included) and requires:
- the draft is approved (approved_by set) and not past expires_at             -> else E_QC_NOT_APPROVED / E_DRAFT_EXPIRED
- sha256(send text) equals drafts.text_sha256, and the attachment still matches the stored variant hash
                                                                               -> else E_QC_HASH_MISMATCH
- a review of exactly this text passed (or a soft-failed human edit the person approved) -> else E_QC_NOT_APPROVED
- a re-lint of the stored text has zero blocks (warnings were judged at QC time; the text is unchanged)
                                                                               -> else E_QC_LINT_FAILED, or
                                                                                  E_RESEARCH_STALE when the only
                                                                                  blocks are stale research dates
The result is written as a qc_results row (stage presend) in the caller's transaction. `ok` False never raises:
gate.reserve maps `code` to its denial.
"""
from __future__ import annotations

from .. import drafts
from ..canon import now, sha256_text
from . import review

STALE_RULES = ("H-STALE-RETRIEVED_AT", "H-STALE-PUBLISHED_AT")


def _fail(code: str, blocks: list, sha: str | None = None, text: str | None = None) -> dict:
    return {"ok": False, "code": code, "sha256": sha, "send_text": text, "blocks": blocks}


def presend(conn, draft_id: int) -> dict:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return _fail("E_NOT_FOUND", [["PRESEND-NO-DRAFT", str(draft_id)]])
    if row["status"] != "approved" or not row["approved_by"]:
        return _fail("E_QC_NOT_APPROVED", [["PRESEND-NOT-APPROVED", row["status"]]])
    if row["expires_at"] and row["expires_at"] < now():
        return _fail("E_DRAFT_EXPIRED", [["PRESEND-EXPIRED", row["expires_at"]]])
    cfg = drafts._settings(conn)
    text = drafts.send_text(conn, row["id"], cfg)
    sha = sha256_text(text)
    if sha != row["text_sha256"]:
        _record(conn, row, False, sha, [["PRESEND-HASH-MISMATCH", "send text differs from the approved text"]])
        return _fail("E_QC_HASH_MISMATCH", [["PRESEND-HASH-MISMATCH", "send text differs from the approved text"]],
                     sha, text)
    att = drafts.payload_of(row).get("attachment") or {}
    if att.get("variant_uid"):
        v = conn.execute("SELECT pdf_sha256 FROM resume_variants WHERE variant_uid = ?", (att["variant_uid"],)).fetchone()
        if v is None or v[0] != att.get("sha256"):
            blocks = [["PRESEND-ATTACHMENT", "the resume variant changed after approval"]]
            _record(conn, row, False, sha, blocks)
            return _fail("E_QC_HASH_MISMATCH", blocks, sha, text)
    rv = conn.execute("SELECT * FROM qc_results WHERE draft_id = ? AND stage = 'review' AND text_sha256 = ? "
                      "ORDER BY id DESC LIMIT 1", (row["id"], sha)).fetchone()
    reviewed = rv is not None and ((rv["passed"] and rv["code_verdict"] == "pass" and rv["model_verdict"] == "pass")
                                   or (drafts.payload_of(row).get("edited_by_human")
                                       and str(row["approved_by"]).startswith("human:")
                                       and review.decision_from_row(rv, row["channel"]).get("soft_only")))
    if not reviewed:
        blocks = [["PRESEND-NO-REVIEW", "no passing review for this exact text"]]
        _record(conn, row, False, sha, blocks)
        return _fail("E_QC_NOT_APPROVED", blocks, sha, text)
    res = drafts.run_lint(conn, row, cfg)
    blocks = res["blocks"]
    _record(conn, row, not blocks, sha, blocks, res["warns"])
    if blocks:
        code = "E_RESEARCH_STALE" if all(b[0] in STALE_RULES for b in blocks) else "E_QC_LINT_FAILED"
        return _fail(code, blocks, sha, text)
    return {"ok": True, "code": "OK", "sha256": sha, "send_text": text, "blocks": []}


def _record(conn, row, passed: bool, sha: str, blocks, warns=()) -> None:
    drafts.record_qc(conn, row["id"], row["attempt"], "presend", passed, sha, blocks, warns,
                     human_edit_no=int(row["human_edits"] or 0))
