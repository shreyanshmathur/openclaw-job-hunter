"""U4: resume plan, build, base build, staging for upload and unstaging (design 6.4, 12.5, 12.10, 13.2)."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import cli, db, jobstate, paths
from jobhunter import profile as P
from jobhunter import resume as R
from jobhunter.canon import sha256_file
from jobhunter.errors import Denied
from tests.fakes.u4.drafts_fake import FakeDrafts, pass_qc
from tests.helpers import HomeTestCase, insert_action, insert_draft, insert_job

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profile")


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def config(tailoring: str = "light", first: str = "", last: str = "", max_pages: int = 2) -> dict:
    return {"owner": {"first_name": first, "last_name": last},
            "resume": {"tailoring": tailoring, "formats": ["pdf", "docx"], "page_size": "A4", "max_pages": max_pages,
                       "filename": "{first}_{last}_Resume", "range_word": "to", "present_word": "Present"}}


class ResumeCase(HomeTestCase):
    tailoring = "light"

    def setUp(self):
        super().setUp()
        self.fake = FakeDrafts()
        self.cfg = config(self.tailoring)
        p1 = mock.patch.object(R, "full_config", side_effect=lambda: self.cfg)
        p2 = mock.patch.object(R, "_drafts", side_effect=lambda: self.fake)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        with db.tx(self.conn):
            P.record_salary(self.conn, fixture("salary.json"))
            P.record_inference(self.conn, fixture("inference.json"))
        self.job_id = insert_job(self.conn, title="Data Analyst")
        self.job_uid = self.conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO job_texts (job_id, jd_text, jd_sha256, fetched_at) VALUES (?, ?, 'x', ?)",
                          (self.job_id, "We want SQL and Python for forecasting. Experience with dbt is a plus.",
                           self.clock.now()))

    def tailor(self, name: str = "tailor_light.json") -> dict:
        t = fixture(name)
        t["job_uid"] = self.job_uid
        return t

    def build(self, name: str = "tailor_light.json") -> dict:
        with db.tx(self.conn):
            return R.build(self.conn, self.job_uid, self.tailor(name), cycle_id=None, caller=None)

    def approve_for_application(self, res: dict, job_id=None, status="reserved") -> str:
        """QC-pass the resume draft, create an approved package naming the variant and an open token."""
        job_id = job_id or self.job_id
        with db.tx(self.conn):
            pass_qc(self.conn, res["draft_uid"])
            pkg = insert_draft(self.conn, kind="application_package", status="awaiting_approval", job_id=job_id,
                               send_route="browser", channel="application_package", subject=None)
            self.conn.execute("UPDATE drafts SET payload_json = ? WHERE id = ?",
                              (json.dumps({"payload": {"resume_variant_uid": res["variant_uid"]}}), pkg))
            jobstate.set_draft_status(self.conn, pkg, "approved", "test", "human:cli")
            aid = insert_action(self.conn, kind="application", status=status, job_id=job_id, draft_id=pkg,
                                agent_id="jobhunter-applier", platform="greenhouse", route="browser")
        return self.conn.execute("SELECT token FROM actions WHERE id = ?", (aid,)).fetchone()[0]


class TestBuild(ResumeCase):
    def test_build_writes_variant_files_and_a_resume_draft(self):
        res = self.build()
        self.assertTrue(res["variant_uid"].startswith("V"))
        self.assertTrue(res["lint"]["pass"])
        for key in ("pdf_path", "docx_path", "txt_path"):
            self.assertTrue(os.path.isfile(res[key]), key)
            self.assertTrue(res[key].startswith(os.path.join(paths.private_dir(), "resume", "variants")))
        self.assertEqual(os.path.basename(res["pdf_path"]), "Alex_Rivera_Resume.pdf")
        self.assertEqual(os.stat(res["pdf_path"]).st_mode & 0o777, 0o600)
        row = self.conn.execute("SELECT * FROM resume_variants WHERE variant_uid = ?", (res["variant_uid"],)).fetchone()
        self.assertEqual(row["job_id"], self.job_id)
        self.assertEqual(row["mode"], "light")
        self.assertEqual(row["pdf_sha256"], sha256_file(res["pdf_path"]))
        self.assertEqual(json.loads(row["tailor_json"])["mode"], "light")
        d = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (row["draft_id"],)).fetchone()
        self.assertEqual(d["draft_uid"], res["draft_uid"])
        self.assertEqual(d["kind"], "resume")
        call = self.fake.calls[-1]["draft"]
        self.assertEqual(call["kind"], "resume")
        self.assertEqual(call["channel"], "resume")
        self.assertEqual(call["job_uid"], self.job_uid)
        self.assertIn("Jul 2022 to Present", call["body"])
        pl = call["payload"]
        self.assertEqual(pl["resume_variant_uid"], res["variant_uid"])
        self.assertEqual(pl["page_count"], 1)
        self.assertEqual(pl["max_pages"], 2)
        self.assertEqual(pl["base"]["roles"][0]["id"], "E1")
        self.assertEqual(pl["tailored"]["roles"][0]["bullets"][0],
                         {"from": "E1.B2", "text": fixture("base.json")["experience"][0]["bullets"][1]["text"]})
        self.assertIn("E1 (Tidemark Logistics): bullets E1.B2, E1.B1, E1.B3; left out E1.B4", pl["changes"])
        self.assertEqual(pl["tailored"]["summary"]["fact_ids"], ["P2", "P1"])
        self.assertEqual(pl["education"] if "education" in pl else pl["tailored"]["education"],
                         pl["base"]["education"])
        self.assertLessEqual(len(call["links"]), 5)
        self.assertEqual(set(call) - {"kind", "channel", "job_uid", "contact_uid", "thread_key", "subject", "body",
                                      "is_reply", "field_char_limit", "hook", "claims", "links", "payload"}, set())

    def test_lint_failure_writes_nothing(self):
        t = self.tailor("tailor_full.json")
        self.cfg = config("full")
        t["experience"][0]["bullets"][0]["text"] = "Built a return-risk model that cut RTO from 18% to 9%."
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                R.build(self.conn, self.job_uid, t)
        self.assertEqual(cm.exception.code, "E_QC_LINT_FAILED")
        self.assertIn("R-NEW-NUMBER", [b[0] for b in cm.exception.data["lint"]["blocks"]])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM resume_variants").fetchone()[0], 0)
        self.assertEqual(self.fake.calls, [])

    def test_failed_draft_creation_leaves_no_files(self):
        def boom(conn, draft, cycle_id, caller):
            raise Denied("E_DUP_DRAFT", "refused")
        self.fake.create_draft = boom
        with self.assertRaises(Denied):
            self.build()
        vdir = R.variants_dir()
        self.assertEqual(os.listdir(vdir) if os.path.isdir(vdir) else [], [])

    def test_mode_must_be_enabled(self):
        with self.assertRaises(Denied) as cm:
            self.build("tailor_full.json")
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        self.cfg = config("full")
        self.assertEqual(self.build("tailor_full.json")["mode"], "full")

    def test_tailoring_off_refuses_build(self):
        self.cfg = config("off")
        with self.assertRaises(Denied) as cm:
            self.build()
        self.assertEqual(cm.exception.code, "E_PRECONDITION")

    def test_job_mismatch_and_unknown_job(self):
        t = self.tailor()
        t["job_uid"] = "JBBBBBBB"
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                R.build(self.conn, self.job_uid, t)
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                R.build(self.conn, "JBBBBBBB", t)
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")

    def test_rebuild_leaves_draft_statuses_to_the_drafts_module(self):
        first = self.build()
        second = self.build()
        self.assertNotEqual(first["variant_uid"], second["variant_uid"])
        for d in (first, second):
            st = self.conn.execute("SELECT status FROM drafts WHERE draft_uid = ?", (d["draft_uid"],)).fetchone()[0]
            self.assertEqual(st, "drafted")      # U4 never writes draft statuses (design 2.5)

    def test_owner_names_from_config_name_the_file(self):
        self.cfg = config("light", first="Sam", last="Okafor")
        res = self.build()
        self.assertEqual(os.path.basename(res["pdf_path"]), "Sam_Okafor_Resume.pdf")


class TestPlan(ResumeCase):
    def test_plan_copies_inputs_into_the_applier_work_folder(self):
        data = R.plan(self.conn, self.job_uid, cycle_id="C20260927T050000ZABCD")
        work = os.path.join(paths.ws_dir("applier"), "work", "C20260927T050000ZABCD")
        for key in ("base_path", "jd_path", "facts_path"):
            self.assertTrue(data[key].startswith(work + os.sep), key)
            self.assertTrue(os.path.isfile(data[key]))
            paths.ensure_agent_path(data[key], "jobhunter-applier")
        with open(data["base_path"], encoding="utf-8") as fh:
            self.assertNotIn("dash_conversions", json.load(fh))
        with open(data["facts_path"], encoding="utf-8") as fh:
            self.assertIn("P1", json.load(fh))
        c = data["constraints"]
        self.assertEqual(data["mode"], "light")
        self.assertEqual(c["modes_allowed"], ["light"])
        self.assertFalse(c["rephrase_allowed"])
        self.assertEqual(c["role_bullets"]["E1"], ["E1.B1", "E1.B2", "E1.B3", "E1.B4"])
        self.assertIn("SQL", c["skills"])
        self.assertIsNone(data["base_variant_uid"])

    def test_plan_mode_off_names_the_qc_passed_base_variant(self):
        base = R.load_base()
        R.mark_reviewed(base)
        with db.tx(self.conn):
            b = R.build_base(self.conn)
        self.cfg = config("off")
        self.assertIsNone(R.plan(self.conn, self.job_uid)["base_variant_uid"])
        with db.tx(self.conn):
            pass_qc(self.conn, b["draft_uid"])
        self.assertEqual(R.plan(self.conn, self.job_uid)["base_variant_uid"], b["variant_uid"])

    def test_plan_unknown_job(self):
        with self.assertRaises(Denied) as cm:
            R.plan(self.conn, "JBBBBBBB")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")


class TestBaseBuild(ResumeCase):
    def test_needs_review_then_builds_once_per_version(self):
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                R.build_base(self.conn)
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        R.mark_reviewed(R.load_base())
        with db.tx(self.conn):
            b = R.build_base(self.conn)
        self.assertFalse(b["reused"])
        row = self.conn.execute("SELECT * FROM resume_variants WHERE variant_uid = ?", (b["variant_uid"],)).fetchone()
        self.assertIsNone(row["job_id"])
        self.assertEqual(row["mode"], "base")
        self.assertIsNone(self.fake.calls[-1]["draft"]["job_uid"])
        self.assertIsNone(self.fake.calls[-1]["draft"]["payload"]["tailored"]["summary"])
        with db.tx(self.conn):
            again = R.build_base(self.conn)
        self.assertTrue(again["reused"])
        self.assertEqual(again["variant_uid"], b["variant_uid"])

    def test_changed_base_needs_a_new_review(self):
        base = R.load_base()
        R.mark_reviewed(base)
        self.assertTrue(R.review_state()["reviewed"])
        base["skills"].append("Statistics")
        R.save_base(base)
        self.assertFalse(R.review_state()["reviewed"])


class TestStage(ResumeCase):
    def setUp(self):
        super().setUp()
        self.res = self.build()

    def stage(self, token, variant=None):
        with db.tx(self.conn):
            return R.stage(self.conn, variant or self.res["variant_uid"], token)

    def test_stage_copies_the_approved_pdf_and_unstage_removes_it(self):
        token = self.approve_for_application(self.res)
        data = self.stage(token)
        root = R.upload_root()
        self.assertEqual(data["upload_path"], os.path.join(root, "Alex_Rivera_Resume.pdf"))
        self.assertEqual(data["filename"], "Alex_Rivera_Resume.pdf")
        self.assertFalse(os.path.islink(data["upload_path"]))
        self.assertEqual(sha256_file(data["upload_path"]), self.res["pdf_sha256"])
        self.assertEqual(data["sha256"], self.res["pdf_sha256"])
        self.assertNotEqual(os.stat(data["upload_path"]).st_ino, os.stat(self.res["pdf_path"]).st_ino)
        row = self.conn.execute("SELECT * FROM staged_files WHERE token = ?", (token,)).fetchone()
        self.assertEqual(row["path"], data["upload_path"])
        self.assertIsNone(row["removed_at"])
        again = self.stage(token)
        self.assertTrue(again["reused"])
        with db.tx(self.conn):
            R.unstage(self.conn, token)
        self.assertFalse(os.path.exists(data["upload_path"]))
        row = self.conn.execute("SELECT * FROM staged_files WHERE token = ?", (token,)).fetchone()
        self.assertIsNotNone(row["removed_at"])
        with db.tx(self.conn):
            R.unstage(self.conn, token)            # idempotent
            R.unstage(self.conn, "TAAAAAAAAAAA")   # unknown token: nothing to do
        self.assertTrue(os.path.isfile(self.res["pdf_path"]))

    def test_stage_refuses_before_qc(self):
        with db.tx(self.conn):
            pkg = insert_draft(self.conn, kind="application_package", status="approved", job_id=self.job_id,
                               send_route="browser", channel="application_package", subject=None)
            self.conn.execute("UPDATE drafts SET payload_json = ? WHERE id = ?",
                              (json.dumps({"payload": {"resume_variant_uid": self.res["variant_uid"]}}), pkg))
            aid = insert_action(self.conn, kind="application", status="reserved", job_id=self.job_id, draft_id=pkg)
        token = self.conn.execute("SELECT token FROM actions WHERE id = ?", (aid,)).fetchone()[0]
        with self.assertRaises(Denied) as cm:
            self.stage(token)
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        self.assertIn("QC", cm.exception.message)
        self.assertFalse(os.path.exists(os.path.join(R.upload_root(), "Alex_Rivera_Resume.pdf")))

    def test_stage_refuses_a_closed_token_or_an_unknown_one(self):
        token = self.approve_for_application(self.res, status="sent")
        with self.assertRaises(Denied) as cm:
            self.stage(token)
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        with self.assertRaises(Denied) as cm:
            self.stage("TAAAAAAAAAAA")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")
        with self.assertRaises(Denied) as cm:
            self.stage(token, variant="VAAAAAAA")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")

    def test_stage_refuses_another_jobs_variant(self):
        other = insert_job(self.conn, title="Business Analyst")
        token = self.approve_for_application(self.res, job_id=other)
        with self.assertRaises(Denied) as cm:
            self.stage(token)
        self.assertEqual(cm.exception.code, "E_PRECONDITION")

    def test_stage_refuses_a_package_that_names_another_variant(self):
        token = self.approve_for_application(self.res)
        second = self.build()
        with db.tx(self.conn):
            pass_qc(self.conn, second["draft_uid"])
        with self.assertRaises(Denied) as cm:
            self.stage(token, variant=second["variant_uid"])
        self.assertEqual(cm.exception.code, "E_PRECONDITION")

    def test_tampered_pdf_is_refused(self):
        token = self.approve_for_application(self.res)
        with open(self.res["pdf_path"], "ab") as fh:
            fh.write(b"% extra\n")
        with self.assertRaises(Denied) as cm:
            self.stage(token)
        self.assertEqual(cm.exception.code, "E_QC_HASH_MISMATCH")

    def test_unstage_open_for_housekeeping(self):
        token = self.approve_for_application(self.res)
        path = self.stage(token)["upload_path"]
        with db.tx(self.conn):
            self.assertEqual(R.unstage_open(self.conn, older_than_s=3600), 0)
        self.clock.advance(hours=2)
        with db.tx(self.conn):
            self.assertEqual(R.unstage_open(self.conn, older_than_s=3600), 1)
        self.assertFalse(os.path.exists(path))

    def test_unstage_after_the_upload_root_moved(self):
        token = self.approve_for_application(self.res)
        path = self.stage(token)["upload_path"]
        hf = paths.home_file()
        with open(hf, encoding="utf-8") as fh:
            h = json.load(fh)
        h["upload_root"] = os.path.join(paths.root(), "elsewhere")
        with open(hf, "w", encoding="utf-8") as fh:
            json.dump(h, fh)
        with db.tx(self.conn):
            R.unstage(self.conn, token)
        self.assertFalse(os.path.exists(path))

    def test_upload_root_from_home_json(self):
        hf = paths.home_file()
        with open(hf, encoding="utf-8") as fh:
            h = json.load(fh)
        h["upload_root"] = os.path.join(paths.root(), "gateway-tmp", "uploads")
        with open(hf, "w", encoding="utf-8") as fh:
            json.dump(h, fh)
        self.assertEqual(R.upload_root(), os.path.realpath(h["upload_root"]))
        token = self.approve_for_application(self.res)
        self.assertTrue(self.stage(token)["upload_path"].startswith(os.path.realpath(h["upload_root"])))


class _Caller:
    def __init__(self, cls, agent_id=None):
        self.cls = cls
        self.agent_id = agent_id


def _real_drafts_available() -> bool:
    try:
        from jobhunter import drafts
        return callable(getattr(drafts, "create_draft", None))
    except ImportError:
        return False


@unittest.skipUnless(_real_drafts_available(), "U3 drafts module not installed")
class TestRealDrafts(ResumeCase):
    """The resume draft contract against U3's real create_draft and R-* linter (integration)."""

    def setUp(self):
        super().setUp()
        from jobhunter import drafts
        self.fake = drafts

    def test_tailored_and_base_variants_pass_the_qc_linter(self):
        res = self.build()
        self.assertEqual(res["draft_status"], "drafted", res.get("draft_lint"))
        self.assertTrue(res["draft_lint"]["pass"], res["draft_lint"])
        R.mark_reviewed(R.load_base())
        with db.tx(self.conn):
            b = R.build_base(self.conn)
        self.assertTrue(b["draft_lint"]["pass"], b["draft_lint"])


class TestCli(ResumeCase):
    def run_cli(self, argv, caller):
        out = io.StringIO()
        with mock.patch.object(cli, "classify", return_value=caller), mock.patch.object(cli, "require"):
            rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_agent_build_stage_unstage(self):
        applier = _Caller("agent", "jobhunter-applier")
        f = self.home.write_agent_file("applier", "tailor.json", json.dumps(self.tailor()))
        rc, env = self.run_cli(["resume", "build", "--job", self.job_uid, "--file", f], applier)
        self.assertEqual(rc, 0, env)
        vuid = env["data"]["variant_uid"]
        res = {"variant_uid": vuid, "draft_uid": env["data"]["draft_uid"]}
        token = self.approve_for_application(res)
        rc, env = self.run_cli(["resume", "stage", "--variant", vuid, "--token", token], applier)
        self.assertEqual(rc, 0, env)
        self.assertTrue(os.path.isfile(env["data"]["upload_path"]))
        rc, env = self.run_cli(["resume", "unstage", "--token", token], applier)
        self.assertEqual((rc, env["data"]["removed"]), (0, True))
        rc, env = self.run_cli(["resume", "unstage", "--token", token], _Caller("system"))
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))

    def test_agent_file_outside_work_is_refused(self):
        outside = os.path.join(paths.root(), "tailor.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump(self.tailor(), fh)
        rc, env = self.run_cli(["resume", "build", "--job", self.job_uid, "--file", outside],
                               _Caller("agent", "jobhunter-applier"))
        self.assertEqual((rc, env["code"]), (10, "E_PATH_NOT_ALLOWED"))

    def test_lint_failure_exit_code(self):
        t = self.tailor()
        t["skills_order"] = ["Rust"]
        f = self.home.write_agent_file("applier", "tailor.json", json.dumps(t))
        rc, env = self.run_cli(["resume", "build", "--job", self.job_uid, "--file", f],
                               _Caller("agent", "jobhunter-applier"))
        self.assertEqual((rc, env["code"]), (6, "E_QC_LINT_FAILED"))
        self.assertIn("R-NEW-SKILL", json.dumps(env["data"]))

    def test_plan_and_base_build(self):
        rc, env = self.run_cli(["resume", "plan", "--job", self.job_uid], _Caller("agent", "jobhunter-applier"))
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["mode"], "light")
        rc, env = self.run_cli(["resume", "base-build"], _Caller("system"))
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        R.mark_reviewed(R.load_base())
        rc, env = self.run_cli(["resume", "base-build"], _Caller("system"))
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli(["resume", "base-build"], _Caller("system"))
        self.assertEqual(env["code"], "NOTHING_TO_DO")

    def test_draft_lint_failure_is_exit_6(self):
        self.fake.status = "lint_failed"
        f = self.home.write_agent_file("applier", "tailor.json", json.dumps(self.tailor()))
        rc, env = self.run_cli(["resume", "build", "--job", self.job_uid, "--file", f],
                               _Caller("agent", "jobhunter-applier"))
        self.assertEqual((rc, env["code"]), (6, "E_QC_LINT_FAILED"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM resume_variants").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
