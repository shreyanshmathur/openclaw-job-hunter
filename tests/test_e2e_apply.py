"""E2E (INT): discover -> evaluate -> apply on an ATS form and on a job board, with the real modules of U1 to U6.

The flow is the applier cycle of design 1.3.4 driven through the real `jh` CLI in a temp home: U4 onboarding
fixtures, scout `job add`, evaluator `eval next/record`, applier `apply next`, `resume plan/build`, QC review
(`qc review start`, `qc worker`, `qc review wait`), `answers get`, the application_package draft, the owner's
approval with the PIN, `gate precheck-plan/precheck`, `detect`, `gate reserve`, `resume stage`, `gate arm`,
`gate confirm`, and the Sheet rows. Only the reviewer's model turn (FakeReviewer) and the detached worker spawn
(qc.SPAWN) are replaced.
"""
from __future__ import annotations

import json
import unittest

import tests  # noqa: F401
from jobhunter import sheets_rows
from jobhunter import qc as qcpkg
from tests.fakes.u3 import FakeReviewer, install_reviewer_hashes
from tests.fixtures.e2e.support import AP, ApplyWorld, e2e_fixture, install_reviewer

WHY = "Why do you want to work here?"
WHY_ANSWER = "I like building returns forecasting models."


class E2EApplyBase(unittest.TestCase):
    def setUp(self):
        self.w = ApplyWorld()
        self.addCleanup(self.w.stop)
        self.spawned = []
        prev = qcpkg.SPAWN
        qcpkg.SPAWN = self.spawned.append
        self.addCleanup(setattr, qcpkg, "SPAWN", prev)
        self.reviewer = install_reviewer(self, FakeReviewer("pass"))

    def discover(self, ingest_name: str, **config_over) -> str:
        w = self.w
        w.onboard(**config_over)
        install_reviewer_hashes(w.conn)
        results = w.add_jobs(e2e_fixture(ingest_name))
        self.assertEqual([r["outcome"] for r in results], ["new"], results)
        job_uid = results[0]["job_uid"]
        rec = w.evaluate_all()
        self.assertEqual([(r["job_uid"], r["verdict"], r["status"]) for r in rec], [(job_uid, "apply", "eligible")])
        return job_uid

    def job(self, job_uid: str):
        return self.w.one("SELECT id, status, status_reason, apply_route FROM jobs WHERE job_uid = ?", job_uid)

    def open_tasks(self, job_id: int) -> list:
        return self.w.all("SELECT kind FROM human_tasks WHERE job_id = ? AND done_at IS NULL ORDER BY id", job_id)

    def assert_applied(self, job_uid: str, out: dict, variant_uid: str, pkg_uid: str) -> None:
        w = self.w
        self.assertEqual(out["confirm"]["status"], "sent")
        self.assertEqual(set(out["confirm"]), {"status", "token", "thread_key", "followup_due_at"})
        job = self.job(job_uid)
        self.assertEqual((job["status"], job["status_reason"]), ("applied", "sent:" + out["token"]))
        variant_id = w.one("SELECT id FROM resume_variants WHERE variant_uid = ?", variant_uid)[0]
        pkg_id = w.one("SELECT id FROM drafts WHERE draft_uid = ?", pkg_uid)[0]
        apps = w.all("SELECT a.token, ap.route, ap.resume_variant_id, ap.package_draft_id FROM applications ap "
                     "JOIN actions a ON a.id = ap.action_id")
        self.assertEqual(apps, [(out["token"], job["apply_route"], variant_id, pkg_id)])
        self.assertIsNotNone(apps[0][2], "applications.resume_variant_id must be set")
        self.assertEqual(w.one("SELECT status FROM drafts WHERE id = ?", pkg_id)[0], "sent")
        # the package names exactly the file that was staged and read back on the page
        payload = json.loads(w.one("SELECT payload_json FROM drafts WHERE id = ?", pkg_id)[0])
        self.assertEqual(payload["attachment"]["variant_uid"], variant_uid)
        self.assertEqual(payload["attachment"]["filename"], out["staged"]["filename"])
        self.assertEqual(w.one("SELECT removed_at IS NOT NULL FROM staged_files WHERE token = ?", out["token"])[0], 1)
        # the Sheet mirrors it
        rows = sheets_rows.build_rows(w.conn, "applications", None)
        self.assertEqual(len(rows), 1, rows)
        jobs_rows = dict(sheets_rows.build_rows(w.conn, "jobs", None))
        self.assertIn(job_uid, jobs_rows)
        self.assertTrue(jobs_rows[job_uid].get("applied_on"), jobs_rows[job_uid])


class TestAtsFormApplication(E2EApplyBase):
    def run_ats_flow(self, **config_over) -> dict:
        w = self.w
        job_uid = self.discover("ingest_greenhouse.json", **config_over)
        job_id = self.job(job_uid)["id"]
        self.assertEqual(self.job(job_uid)["apply_route"], "ats_form")

        # applier cycle 1: tailor, then the form asks a question the answer bank cannot answer
        cyc = w.preflight("applier", AP)
        item = w.claim(cyc, job_uid)
        self.assertEqual((item["needs"], item["route"]), ("package", "ats_form"))
        built = w.build_resume(cyc, job_uid)
        rc, env = w.answer(cyc, job_uid, "Email")
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        self.assertTrue(env["data"]["found"])
        rc, env = w.answer(cyc, job_uid, WHY)
        self.assertEqual(env["code"], "E_NOT_FOUND", env)
        self.assertTrue(env["data"]["job_to_human"], env)
        task_uid = env["data"]["human_task"]
        job = self.job(job_uid)
        self.assertEqual((job["status"], job["status_reason"]), ("needs_human", "answer_question"))
        self.assertEqual(self.open_tasks(job_id), [("answer_question",)])
        w.end_cycle(cyc, AP)
        inbox = w.ok(["inbox"])
        self.assertEqual([t["kind"] for t in inbox.get("tasks") or []] +
                         [q.get("kind", "answer_question") for q in inbox.get("questions") or []],
                         ["answer_question"], inbox)

        # the owner answers: the job goes back to eligible and nothing is left for them to do by hand
        res = w.ok(["profile", "answer", "--field", task_uid, "--value", WHY_ANSWER], "human")
        self.assertTrue(res["job_released"], res)
        job = self.job(job_uid)
        self.assertEqual((job["status"], job["status_reason"]), ("eligible", "answered"))
        self.assertEqual(self.open_tasks(job_id), [])
        self.assertEqual(w.all("SELECT kind FROM human_tasks WHERE kind = 'apply_manually'"), [])
        self.assertEqual(w.ok(["inbox"]).get("tasks") or [], [])

        # applier cycle 2: the bank now answers both fields; package through QC
        cyc = w.preflight("applier", AP)
        w.claim(cyc, job_uid)
        fields = []
        for label, key in (("Email", "email"), (WHY, None)):
            rc, env = w.answer(cyc, job_uid, label)
            self.assertEqual((rc, env["code"]), (0, "OK"), env)
            fields.append({"label": label, "type": "text", "value": env["data"]["value"],
                           "answer_key": env["data"]["key"]})
        self.assertEqual(fields[1]["value"], WHY_ANSWER)
        pkg_uid = w.package(cyc, job_uid, built["variant_uid"], fields)
        w.end_cycle(cyc, AP)

        approved = w.approve_all()
        self.assertEqual([a["draft_uid"] for a in approved], [pkg_uid])

        # applier cycle 3: submit the approved package
        cyc = w.preflight("applier", AP)
        item = w.claim(cyc, job_uid)
        self.assertEqual(item["needs"], "submit", item)
        url = "https://boards.greenhouse.io/kestrelcommerce/jobs/4000001"
        out = w.submit(cyc, job_uid, pkg_uid, built["variant_uid"], platform="greenhouse",
                       detect_platform="greenhouse", page_url=url, fields=fields)
        w.end_cycle(cyc, AP)
        self.assert_applied(job_uid, out, built["variant_uid"], pkg_uid)
        self.assertEqual(self.open_tasks(job_id), [])
        return out

    def test_ats_form_application_with_owner_names(self):
        out = self.run_ats_flow()
        self.assertEqual(out["staged"]["filename"], "Sam_Lee_Resume.pdf")

    def test_ats_form_application_with_blank_owner_names(self):
        # config.example.json ships blank owner names: the names come from the base resume's contact block, and the
        # package, the staged upload copy and the observed read-back must still name the same file
        out = self.run_ats_flow(**{"owner.first_name": "", "owner.last_name": ""})
        self.assertEqual(out["staged"]["filename"], "Alex_Rivera_Resume.pdf")


class TestBoardApplication(E2EApplyBase):
    def test_naukri_in_app_application(self):
        w = self.w
        job_uid = self.discover("ingest_naukri.json", **{"boards.sites.naukri.apply": "browser",
                                                         "boards.sites.naukri.discover": "browser"})
        self.assertEqual(self.job(job_uid)["apply_route"], "board_inapp")
        cyc = w.preflight("applier", AP)
        item = w.claim(cyc, job_uid)
        self.assertEqual((item["needs"], item["reason"]), ("package", "board_inapp"))
        built = w.build_resume(cyc, job_uid)
        rc, env = w.answer(cyc, job_uid, "Email")
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        fields = [{"label": "Email", "type": "text", "value": env["data"]["value"], "answer_key": "email"}]
        pkg_uid = w.package(cyc, job_uid, built["variant_uid"], fields)
        w.end_cycle(cyc, AP)
        w.approve_all()
        cyc = w.preflight("applier", AP)
        self.assertEqual(w.claim(cyc, job_uid)["needs"], "submit")
        url = "https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-remote-2-to-4-years-290926000001"
        out = w.submit(cyc, job_uid, pkg_uid, built["variant_uid"], platform="naukri", detect_platform="site:naukri",
                       page_url=url, fields=fields)
        w.end_cycle(cyc, AP)
        self.assertEqual(out["detect"].get("verdict"), "clear", out["detect"])
        self.assertEqual(w.one("SELECT platform FROM actions WHERE token = ?", out["token"])[0], "naukri")
        self.assert_applied(job_uid, out, built["variant_uid"], pkg_uid)


if __name__ == "__main__":
    unittest.main()
