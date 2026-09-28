"""U3: approval codes, approve, skip, expiry, human edits with their own budget, packages (design 5.4, 5.5)."""
from __future__ import annotations

import json
import unittest

import tests  # noqa: F401
from jobhunter import approvals, canon, db, drafts
from jobhunter.errors import Denied
from jobhunter.qc import worker
from tests.fakes.u3 import GOOD_BODY, QCTestCase


class ApprovalCase(QCTestCase):
    def awaiting(self, **over) -> tuple:
        out = self.create(**over)
        self.run_review(out["draft_uid"])
        row = self.row(out["draft_uid"])
        self.assertEqual(row["status"], "awaiting_approval")
        code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL",
                                 (row["id"],)).fetchone()[0]
        return out["draft_uid"], code

    def approve(self, code, by="human:chat"):
        with db.tx(self.conn):
            return approvals.approve(self.conn, code, by)


class TestApprove(ApprovalCase):
    def test_approve_by_code(self):
        uid, code = self.awaiting()
        out = self.approve(code)
        self.assertEqual(out["status"], "approved")
        self.assertIn("Approved %s" % code, out["message"])
        self.assertIn("Alex R.", out["message"])
        self.assertIn("Kestrel Commerce", out["message"])
        row = self.row(uid)
        self.assertEqual((row["status"], row["approved_by"]), ("approved", "human:chat"))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM approval_codes WHERE code = ? AND closed_at IS NULL",
                                            (code,)).fetchone())
        again = self.approve(code, "human:sheet")          # sheet edits can be applied twice
        self.assertTrue(again["already"])

    def test_only_humans_approve(self):
        uid, code = self.awaiting()
        for by in ("auto", "jobhunter-outreach", "system"):
            with self.subTest(by=by), self.assertRaises(Denied) as cm, db.tx(self.conn):
                approvals.approve(self.conn, code, by)
            self.assertEqual(cm.exception.code, "E_CALLER_NOT_ALLOWED")

    def test_unknown_and_expired(self):
        with self.assertRaises(Denied) as cm:
            self.approve("ACDE")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")
        uid, code = self.awaiting()
        self.clock.advance(hours=73)
        with self.assertRaises(Denied) as cm:
            self.approve(code)
        self.assertEqual(cm.exception.code, "E_DRAFT_EXPIRED")
        with db.tx(self.conn):
            self.assertEqual(drafts.expire(self.conn), 1)
        self.assertEqual(self.row(uid)["status"], "expired")
        with self.assertRaises(Denied) as cm:
            self.approve(code)
        self.assertEqual(cm.exception.code, "E_DRAFT_EXPIRED")

    def test_codes_never_reused(self):
        seen = set()
        with db.tx(self.conn):
            for i in range(60):
                did = self.conn.execute(
                    "INSERT INTO drafts (draft_uid, kind, channel, send_route, payload_json, text_sha256, status, "
                    "created_at, updated_at) VALUES (?, 'li_message', 'li_message', 'browser', '{}', 'x', "
                    "'awaiting_approval', ?, ?)", (canon.new_uid("D"), canon.now(), canon.now())).lastrowid
                code = approvals.issue_code(self.conn, did)
                self.assertEqual(approvals.issue_code(self.conn, did), code)      # one open code per draft
                self.assertNotIn(code, seen)
                seen.add(code)

    def test_changed_signature_blocks_approval(self):
        uid, code = self.awaiting()
        self.write_config({"owner": {"signature": {"full_name": "Someone Else", "links": []}}})
        with self.assertRaises(Denied) as cm:
            self.approve(code)
        self.assertEqual(cm.exception.code, "E_QC_HASH_MISMATCH")

    def test_pending_list(self):
        uid, code = self.awaiting()
        with db.tx(self.conn):
            items = approvals.pending(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["code"], items[0]["draft_uid"]), (code, uid))
        self.assertEqual(items[0]["preview"], "Pincode-level RTO models")
        self.assertEqual(items[0]["qc_score"], 4.7)


class TestSkip(ApprovalCase):
    def test_skip(self):
        uid, code = self.awaiting()
        with db.tx(self.conn):
            out = approvals.skip(self.conn, code, "not now", "human:cli")
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.row(uid)["status"], "skipped_by_human")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM target_skips WHERE target_key = ?",
                                               ("contact:" + self.contact_uid,)).fetchone())
        with self.assertRaises(Denied) as cm, db.tx(self.conn):
            approvals.approve(self.conn, code, "human:chat")
        self.assertEqual(cm.exception.code, "E_QC_NOT_APPROVED")


class TestHumanEdit(ApprovalCase):
    def edit(self, code, text):
        with db.tx(self.conn):
            return approvals.human_edit(self.conn, code, text, "human:chat")

    def test_lint_failure_keeps_old_text_and_explains(self):
        uid, code = self.awaiting()
        before = self.row(uid)
        new = GOOD_BODY.replace("We saw the same pattern", "We saw \u2014 the same pattern")
        out = self.edit(code, new)
        self.assertFalse(out["passed"])
        self.assertEqual(out["edits_left"], 2)
        self.assertTrue(any("dash" in m and "line 3" in m for m in out["messages"]), out["messages"])
        row = self.row(uid)
        self.assertEqual((row["status"], row["text_sha256"], row["body"]),
                         ("awaiting_approval", before["text_sha256"], before["body"]))
        self.assertEqual((row["human_edits"], row["attempt"]), (1, 1))
        qr = self.conn.execute("SELECT stage, human_edit_no, passed FROM qc_results WHERE stage = 'human_edit_lint'"
                               ).fetchone()
        self.assertEqual(tuple(qr), ("human_edit_lint", 1, 0))
        self.approve(code)                                          # the old text can still be approved

    def test_clean_edit_goes_to_review_then_back_to_the_person(self):
        self.set_auto()                                           # an edited text never auto-approves
        with db.tx(self.conn):
            db.meta_set(self.conn, "approval_mode", "human", "human")
        uid, code = self.awaiting()
        with db.tx(self.conn):
            db.meta_set(self.conn, "approval_mode", "auto", "human")
        new = GOOD_BODY.replace("We saw the same pattern at Tidemark Logistics.",
                                "Tidemark Logistics had the same result.")
        out = self.edit(code, new)
        self.assertTrue(out["passed"], out)
        self.assertEqual(self.row(uid)["status"], "review_pending")
        worker.run_job(out["qjob_uid"])
        row = self.row(uid)
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertIn("Tidemark Logistics had the same result.", row["body"])
        self.assertEqual(row["attempt"], 1)                        # writer budget untouched
        new_code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL",
                                     (row["id"],)).fetchone()[0]
        self.assertNotEqual(new_code, code)
        with self.assertRaises(Denied) as cm:
            self.approve(code)                                     # the old code is closed ...
        self.assertEqual(cm.exception.code, "E_QC_NOT_APPROVED")
        self.assertIn(new_code, cm.exception.message)
        self.assertEqual(self.approve(new_code)["status"], "approved")

    def test_soft_review_failure_may_be_approved(self):
        uid, code = self.awaiting()
        self.reviewer.mode = "soft_fail"
        out = self.edit(code, GOOD_BODY.replace("a name is plenty", "a name would help"))
        worker.run_job(out["qjob_uid"])
        row = self.row(uid)
        self.assertEqual((row["status"], row["status_reason"]), ("awaiting_approval", "human_edit_review_soft_fail"))
        note = self.conn.execute("SELECT text FROM notifications ORDER BY id DESC").fetchone()[0]
        self.assertIn("You may still approve", note)
        new_code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL",
                                     (row["id"],)).fetchone()[0]
        self.assertEqual(self.approve(new_code)["status"], "approved")

    def test_hard_review_failure_cannot_be_approved(self):
        uid, code = self.awaiting()
        self.reviewer.mode = "untruthful"
        out = self.edit(code, GOOD_BODY.replace("a name is plenty", "a name would help"))
        worker.run_job(out["qjob_uid"])
        row = self.row(uid)
        self.assertEqual((row["status"], row["status_reason"]), ("review_failed", "human_edit_review_failed"))
        new_code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL",
                                     (row["id"],)).fetchone()[0]
        with self.assertRaises(Denied) as cm:
            self.approve(new_code)
        self.assertEqual(cm.exception.code, "E_QC_NOT_APPROVED")
        with self.assertRaises(Denied) as cm, db.tx(self.conn):
            drafts.revise_draft(self.conn, uid, self.draft_file())   # the model never rewrites the person's words
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        self.reviewer.mode = "pass"
        out = self.edit(new_code, GOOD_BODY)                         # the person may edit again
        worker.run_job(out["qjob_uid"])
        self.assertEqual(self.row(uid)["status"], "awaiting_approval")

    def test_budget(self):
        uid, code = self.awaiting()
        bad = GOOD_BODY.replace("We saw", "We \u2014 saw")
        for _ in range(3):
            self.edit(code, bad)
        with self.assertRaises(Denied) as cm:
            self.edit(code, GOOD_BODY)
        self.assertEqual(cm.exception.code, "E_QC_BUDGET_EXHAUSTED")

    def test_subject_line_edit(self):
        uid, code = self.awaiting()
        out = self.edit(code, "Subject: Pincode RTO results\n\n" + GOOD_BODY)
        self.assertTrue(out["passed"], out)
        self.assertEqual(self.row(uid)["subject"], "Pincode RTO results")


class TestPackage(ApprovalCase):
    def setUp(self):
        super().setUp()
        with db.tx(self.conn):
            rid = self.conn.execute(
                "INSERT INTO drafts (draft_uid, kind, channel, send_route, job_id, payload_json, text_sha256, status, "
                "approved_by, approved_at, created_at, updated_at) VALUES (?, 'resume', 'resume', 'none', ?, '{}', 'x', "
                "'qc_passed', NULL, NULL, ?, ?)", (canon.new_uid("D"), self.job_id, canon.now(), canon.now())).lastrowid
            self.resume_draft_id = rid
            self.variant_uid = "V" + canon.new_uid("A", 7)[1:]
            self.conn.execute(
                "INSERT INTO resume_variants (variant_uid, job_id, mode, base_sha256, pdf_path, txt_path, pdf_sha256, "
                "draft_id, created_at) VALUES (?, ?, 'light', 'b', '/x.pdf', '/x.txt', ?, ?, ?)",
                (self.variant_uid, self.job_id, "ab" * 32, rid, canon.now()))
        with open(__import__("os").path.join(__import__("jobhunter").paths.private_dir(), "answers.json"), "w") as fh:
            json.dump({"version": 1, "answers": [{"key": "notice_period_days", "patterns": ["notice"], "value": "30",
                                                  "source": "user_confirmed", "sensitive": False}]}, fh)

    def package(self):
        from jobhunter.auth import Caller
        f = {"kind": "application_package", "payload": {
            "job_uid": self.job_uid, "resume_variant_uid": self.variant_uid, "cover_note_draft_uid": None,
            "fields": [{"label": "Notice period (days)", "type": "text", "value": "30",
                        "answer_key": "notice_period_days"}]}}
        with db.tx(self.conn):
            return drafts.create_draft(self.conn, f, None, Caller("agent", "jobhunter-applier"))

    def test_package_flow_and_job_status(self):
        out = self.package()
        self.assertTrue(out["lint"]["pass"], out["lint"])
        self.run_review(out["draft_uid"])
        row = self.row(out["draft_uid"])
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0],
                         "awaiting_approval")
        text = json.loads(drafts.send_text(self.conn, row["id"]))
        self.assertTrue(text["resume"].endswith(".pdf"))
        self.assertEqual(text["fields"], [{"label": "Notice period (days)", "value": "30"}])
        code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ?", (row["id"],)).fetchone()[0]
        self.approve(code, "human:cli")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0],
                         "apply_queued")
        self.assertEqual(self.conn.execute("SELECT status, approved_by FROM drafts WHERE id = ?",
                                           (self.resume_draft_id,)).fetchone()[:], ("approved", "human:cli"))

    def test_skip_closes_job(self):
        out = self.package()
        self.run_review(out["draft_uid"])
        code = self.conn.execute("SELECT a.code FROM approval_codes a JOIN drafts d ON d.id = a.draft_id "
                                 "WHERE d.draft_uid = ?", (out["draft_uid"],)).fetchone()[0]
        with db.tx(self.conn):
            approvals.skip(self.conn, code, "", "human:sheet")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0],
                         "closed")

    def test_unapproved_variant_refused(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE drafts SET status = 'superseded' WHERE id = ?", (self.resume_draft_id,))
        with self.assertRaises(Denied) as cm:
            self.package()
        self.assertEqual(cm.exception.code, "E_QC_NOT_APPROVED")


if __name__ == "__main__":
    unittest.main()
