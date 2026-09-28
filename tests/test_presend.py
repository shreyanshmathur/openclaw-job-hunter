"""U3: the pre-send hook gate.reserve calls inside its transaction (design 2.3, 5.1, 11 rule 5)."""
from __future__ import annotations

import time
import unittest

import tests  # noqa: F401
from jobhunter import approvals, db, hooks
from jobhunter.qc import presend
from tests.fakes.u3 import QCTestCase
from tests.helpers import insert_action


class PresendCase(QCTestCase):
    def approved(self, **over) -> tuple:
        out = self.create(**over)
        self.run_review(out["draft_uid"])
        row = self.row(out["draft_uid"])
        code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL",
                                 (row["id"],)).fetchone()[0]
        with db.tx(self.conn):
            approvals.approve(self.conn, code, "human:cli")
        return self.row(out["draft_uid"])

    def check(self, draft_id: int) -> dict:
        with db.tx(self.conn):
            return presend.presend(self.conn, draft_id)


class TestPresend(PresendCase):
    def test_ok(self):
        row = self.approved()
        res = self.check(row["id"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["sha256"], row["text_sha256"])
        self.assertTrue(res["send_text"].startswith("Subject: Pincode-level RTO models\n\nHi Alex,"))
        qr = self.conn.execute("SELECT passed FROM qc_results WHERE stage = 'presend'").fetchone()
        self.assertEqual(qr[0], 1)

    def test_through_the_u1_hook(self):
        row = self.approved()
        with db.tx(self.conn):
            res = hooks.presend(self.conn, row["id"])
        self.assertTrue(res["ok"])

    def test_hook_rules(self):
        row = self.approved()
        with db.tx(self.conn):
            t0 = time.monotonic()
            presend.presend(self.conn, row["id"])
            elapsed = time.monotonic() - t0
            self.assertTrue(self.conn.in_transaction)          # never commits or ends the caller's transaction
        self.assertLess(elapsed, 0.5)                           # budget is 100 ms; generous for slow CI machines

    def test_not_approved(self):
        out = self.create()
        res = self.check(self.row(out["draft_uid"])["id"])
        self.assertEqual((res["ok"], res["code"]), (False, "E_QC_NOT_APPROVED"))

    def test_hash_mismatch_after_text_change(self):
        row = self.approved()
        with db.tx(self.conn):
            self.conn.execute("UPDATE drafts SET body = body || ' Extra words.' WHERE id = ?", (row["id"],))
        res = self.check(row["id"])
        self.assertEqual(res["code"], "E_QC_HASH_MISMATCH")

    def test_hash_mismatch_after_signature_change(self):
        row = self.approved()
        self.write_config({"owner": {"signature": {"full_name": "Different Name", "links": []}}})
        self.assertEqual(self.check(row["id"])["code"], "E_QC_HASH_MISMATCH")

    def test_expired(self):
        row = self.approved()
        self.clock.advance(days=20)
        self.assertEqual(self.check(row["id"])["code"], "E_DRAFT_EXPIRED")

    def test_stale_research(self):
        row = self.approved()
        with db.tx(self.conn):
            self.conn.execute("UPDATE drafts SET expires_at = '2027-01-01T00:00:00Z' WHERE id = ?", (row["id"],))
        self.clock.advance(days=16)
        res = self.check(row["id"])
        self.assertEqual(res["code"], "E_RESEARCH_STALE", res)

    def test_new_duplicate_blocks(self):
        row = self.approved()
        with db.tx(self.conn):
            insert_action(self.conn, kind="cold_email", status="unknown", company_id=self.company_id,
                          contact_id=self.contact_id, recipient="alex.rivera@kestrel.example")
        res = self.check(row["id"])
        self.assertEqual(res["code"], "E_QC_LINT_FAILED")
        self.assertTrue(any(b[0].startswith("L-DEDUP-") for b in res["blocks"]))

    def test_reviewed_text_only(self):
        row = self.approved()
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM qc_results WHERE stage = 'review'")
        self.assertEqual(self.check(row["id"])["code"], "E_QC_NOT_APPROVED")


if __name__ == "__main__":
    unittest.main()
