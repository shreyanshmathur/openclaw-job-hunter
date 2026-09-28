"""U3: QC worker: verdicts, retry and reviewer_unavailable, timeout, orphan recovery, budget, drain, wait, golden."""
from __future__ import annotations

import io
import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import cli, db, drafts
from jobhunter import qc as qcpkg
from jobhunter.commands import drafts as cmd_drafts, qc as cmd_qc
from jobhunter.qc import review, worker
from tests.fakes.u3 import GOOD_BODY, FakeReviewer, QCTestCase

BAD_BODY = GOOD_BODY.replace("pattern", "pattern \u2014 again")


class TestVerdicts(QCTestCase):
    def test_pass_goes_to_awaiting_approval_in_human_mode(self):
        out = self.create()
        res = self.run_review(out["draft_uid"])
        self.assertEqual(res["verdict"], "pass")
        row = self.row(out["draft_uid"])
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertIsNotNone(row["expires_at"])
        qr = self.conn.execute("SELECT * FROM qc_results WHERE draft_id = ? AND stage = 'review'", (row["id"],)).fetchone()
        self.assertEqual((qr["passed"], qr["model_verdict"], qr["code_verdict"]), (1, "pass", "pass"))
        self.assertEqual(qr["text_sha256"], row["text_sha256"])
        self.assertEqual(qr["weighted_score"], 4.7)
        code = self.conn.execute("SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL",
                                 (row["id"],)).fetchone()[0]
        self.assertRegex(code, r"^[ACDEFGHJKMNPQRTUVWXY34679]{4}$")
        note = self.conn.execute("SELECT * FROM notifications WHERE kind = 'approval'").fetchone()
        self.assertIn("Approve %s?" % code, note["text"])
        self.assertIn("/jh approve %s" % code, note["text"])
        job = self.conn.execute("SELECT * FROM qc_jobs").fetchone()
        self.assertEqual((job["state"], job["verdict"]), ("done", "pass"))
        self.assertFalse(os.path.exists(job["packet_path"]))           # packet deleted after the verdict
        self.assertEqual(self.reviewer.calls[0]["agent"], "jobhunter-qc")
        self.assertTrue(self.reviewer.calls[0]["session_key"].startswith("review-"))

    def test_auto_mode_approves(self):
        self.set_auto()
        out = self.create()
        self.run_review(out["draft_uid"])
        row = self.row(out["draft_uid"])
        self.assertEqual((row["status"], row["approved_by"]), ("approved", "auto"))
        self.assertIsNotNone(row["send_after"])

    def test_auto_mode_but_salary_is_always_human(self):
        self.set_auto()
        body = GOOD_BODY.replace("Would a 15 minute call", "Happy to share my salary range. Would a 15 minute call")
        out = self.create(body=body)
        self.run_review(out["draft_uid"])
        self.assertEqual(self.row(out["draft_uid"])["status"], "awaiting_approval")

    def test_fail_then_budget(self):
        self.reviewer.mode = "fail"
        out = self.create()
        uid = out["draft_uid"]
        self.run_review(uid)
        self.assertEqual(self.row(uid)["status"], "review_failed")
        for attempt in (2, 3):
            with db.tx(self.conn):
                r = drafts.revise_draft(self.conn, uid, self.draft_file())
            self.assertEqual(r["attempt"], attempt)
            self.run_review(uid)
        row = self.row(uid)
        self.assertEqual(row["status"], "dropped_qc")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM target_skips WHERE target_key = ?",
                                               ("contact:" + self.contact_uid,)).fetchone())
        rows = self.conn.execute("SELECT attempt, stage, passed FROM qc_results WHERE draft_id = ? ORDER BY id",
                                 (row["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "lint", 1), (1, "review", 0), (2, "lint", 1), (2, "review", 0),
                                                    (3, "lint", 1), (3, "review", 0)])

    def test_model_pass_with_unsupported_claim_is_fail(self):
        self.reviewer.mode = "untruthful"
        out = self.create()
        self.run_review(out["draft_uid"])
        qr = self.conn.execute("SELECT * FROM qc_results WHERE stage = 'review'").fetchone()
        self.assertEqual((qr["model_verdict"], qr["code_verdict"], qr["passed"]), ("pass", "fail", 0))
        self.assertIn("truthful", json.loads(qr["gates_failed"]))
        self.assertEqual(self.row(out["draft_uid"])["status"], "review_failed")

    def test_garbage_and_wrong_nonce_are_fail_verdicts(self):
        for mode in ("garbage", "wrong_nonce"):
            with self.subTest(mode=mode):
                with db.tx(self.conn):
                    self.conn.execute("UPDATE drafts SET status = 'superseded' WHERE status NOT IN ('superseded')")
                self.reviewer.mode = mode
                out = self.create()
                res = self.run_review(out["draft_uid"])
                self.assertEqual(res["verdict"], "fail")
                self.assertEqual(self.row(out["draft_uid"])["status"], "review_failed")
                self.assertEqual(self.row(out["draft_uid"])["attempt"], 1)


class TestErrors(QCTestCase):
    def test_error_retries_once_then_unavailable(self):
        self.reviewer.mode = "error"
        out = self.create()
        res = self.run_review(out["draft_uid"])
        self.assertTrue(res.get("reviewer_unavailable"), res)
        self.assertEqual(len(self.reviewer.calls), 2)
        row = self.row(out["draft_uid"])
        self.assertEqual((row["status"], row["status_reason"], row["attempt"]),
                         ("review_failed", "reviewer_unavailable", 1))      # budget untouched
        tries = [tuple(r) for r in self.conn.execute("SELECT try_no, state FROM qc_jobs ORDER BY id")]
        self.assertEqual(tries, [(1, "failed"), (2, "failed")])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM qc_results WHERE stage = 'review'").fetchone())
        # a later cycle starts the review again and it passes
        self.reviewer.mode = "pass"
        res = self.run_review(out["draft_uid"])
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(self.row(out["draft_uid"])["status"], "awaiting_approval")

    def test_error_then_success_on_retry(self):
        self.reviewer.modes = ["timeout", "pass"]
        out = self.create()
        res = self.run_review(out["draft_uid"])
        self.assertEqual(res["verdict"], "pass")
        states = [tuple(r) for r in self.conn.execute("SELECT try_no, state FROM qc_jobs ORDER BY id")]
        self.assertEqual(states, [(1, "timeout"), (2, "done")])

    def test_orphan_recovery(self):
        out = self.create()
        job = self.start_review(out["draft_uid"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE qc_jobs SET state = 'running', started_at = ?, heartbeat_at = ? WHERE qjob_uid = ?",
                              (self.clock.now(), self.clock.now(), job["qjob_uid"]))
        self.clock.advance(seconds=30)
        with db.tx(self.conn):
            self.assertEqual(worker.recover_orphans(self.conn), 0)
        self.clock.advance(seconds=45)
        out2 = worker.drain(300)
        self.assertEqual(out2["recovered"], 1)
        self.assertEqual(out2["ran"][0]["verdict"], "pass")        # the retry (try 2) ran
        self.assertEqual(self.row(out["draft_uid"])["status"], "awaiting_approval")

    def test_tamper_between_start_and_run(self):
        out = self.create()
        job = self.start_review(out["draft_uid"])
        with open(review.agents_md_path(), "a", encoding="utf-8") as fh:
            fh.write("tampered\n")
        res = worker.run_job(job["qjob_uid"])
        self.assertEqual(self.reviewer.calls, [])
        self.assertTrue(res.get("reviewer_unavailable"))
        alert = self.conn.execute("SELECT * FROM notifications WHERE priority = 'high'").fetchone()
        self.assertIn("reviewer", alert["text"])

    def test_drain_runs_queued_jobs(self):
        uids = []
        for name, email in (("Sam Lee", "sam@kestrel.example"),):
            from tests.helpers import insert_company, insert_contact
            with db.tx(self.conn):
                co = insert_company(self.conn, name="Tidewater Freight", domain="tidewater.example")
                ct = insert_contact(self.conn, company_id=co, full_name=name, email=email)
                self.conn.execute("UPDATE research_facts SET subject_id = subject_id")
            uids.append(ct)
        a = self.create()
        self.start_review(a["draft_uid"])
        res = worker.drain(300)
        self.assertEqual([r["verdict"] for r in res["ran"]], ["pass"])
        self.assertEqual(worker.drain(300)["ran"], [])

    def test_drain_respects_time_budget(self):
        a = self.create()
        self.start_review(a["draft_uid"])
        self.assertEqual(worker.drain(30)["ran"], [])       # a review (60 s minimum) does not fit


class TestWaitAndCli(QCTestCase):
    def cli(self, argv, agent="jobhunter-outreach"):
        out = io.StringIO()
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": agent} if agent else {}
        rc = cli.main(argv, env=env, stdin=io.StringIO(""), stdout=out, modules=[cmd_drafts, cmd_qc])
        return rc, json.loads(out.getvalue())

    def test_start_wait_flow(self):
        a = self.create()
        rc, env = self.cli(["qc", "review", "start", "--draft", a["draft_uid"]])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        qjob = env["data"]["qjob_uid"]
        self.assertEqual(self.spawned, [qjob])                     # spawned after COMMIT
        rc, env = self.cli(["qc", "review", "wait", "--job", qjob, "--max", "0"])
        self.assertEqual((rc, env["code"], env["data"]["state"]), (0, "PENDING", "queued"))
        worker.run_job(qjob)
        rc, env = self.cli(["qc", "review", "wait", "--job", qjob, "--max", "5"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        self.assertEqual(env["data"]["verdict"], "pass")
        self.assertEqual(env["data"]["draft_status"], "awaiting_approval")

    def test_wait_reports_failure_with_issues(self):
        self.reviewer.mode = "fail"
        a = self.create()
        job = self.start_review(a["draft_uid"])
        worker.run_job(job["qjob_uid"])
        rc, env = self.cli(["qc", "review", "wait", "--job", job["qjob_uid"]])
        self.assertEqual((rc, env["code"]), (6, "E_QC_REVIEW_FAILED"))
        self.assertTrue(env["data"]["issues"])
        self.assertEqual(env["data"]["rewrite_brief"], "be specific")

    def test_wait_follows_retry(self):
        self.reviewer.modes = ["error", "pass"]
        a = self.create()
        job = self.start_review(a["draft_uid"])
        worker.run_job(job["qjob_uid"])
        st = review.wait(self.conn, job["qjob_uid"], 0)
        self.assertEqual((st["state"], st["verdict"]), ("done", "pass"))
        self.assertNotEqual(st["qjob_uid"], job["qjob_uid"])

    def test_worker_is_system_only(self):
        rc, env = self.cli(["qc", "worker", "--drain"])
        self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED")
        rc, env = self.cli(["qc", "worker", "--drain", "--max-seconds", "300"], agent=None)
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))

    def test_tamper_alert_survives_refusal(self):
        a = self.create()
        with db.tx(self.conn):
            db.meta_set(self.conn, "qc_agents_md_sha256", "0" * 64, "install")
        rc, env = self.cli(["qc", "review", "start", "--draft", a["draft_uid"]])
        self.assertEqual((rc, env["code"]), (11, "E_REVIEWER_TAMPERED"))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM notifications WHERE priority = 'high'").fetchone())


class TestGolden(QCTestCase):
    def test_golden_run_with_perfect_reviewer(self):
        labels = {gid: label for gid, label, _ in worker.golden_items()}
        self.assertEqual(len(labels), 20)
        self.assertEqual(sorted(labels.values()).count("good"), 10)

        def oracle(agent, session_key, message_file, timeout_s):
            with open(message_file, encoding="utf-8") as fh:
                text = fh.read()
            gid = os.path.basename(message_file)[:-4]
            rev = FakeReviewer("pass" if labels[gid] == "good" else "fail")
            return rev(agent, session_key, message_file, timeout_s) if text else {"ok": False}

        out = worker.golden(model="test-model", agent_turn=oracle)
        self.assertEqual(out["agreement"], "20/20")
        self.assertTrue(out["golden_last"].startswith("20/20|test-model|"))
        out = worker.golden(agent_turn=FakeReviewer("pass"))
        self.assertEqual(out["agreement"], "10/20")


if __name__ == "__main__":
    unittest.main()
