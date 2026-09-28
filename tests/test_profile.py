"""U4: profile import, salary record, inference record, answers, confirmation, feasibility (design 6, 8, 12.6,
12.9, 12.16)."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import cli, db, paths
from jobhunter import profile as P
from jobhunter import resume as R
from jobhunter.errors import Denied
from jobhunter.resume import docx as D
from jobhunter.resume import model as M
from jobhunter.resume import pdf as PDF
from tests.helpers import HomeTestCase

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profile")

REQUIRED_ANSWERS = [
    ("Q1", "Data analytics: Data Analyst, Business Analyst\nOperations analytics: Operations Analyst"),
    ("Q2", "intern, director"),
    ("Q3", "associate to senior"),
    ("Q4", "3"),
    ("Q5", "USD per year"),
    ("Q6", "60k"),
    ("Q7", "72000"),
    ("Q8", "yes"),
    ("Q10", "30 days"),
    ("Q11", "Springfield, remote"),
    ("Q12", "remote, hybrid"),
    ("Q14", "US: citizen; other: needs_sponsorship"),
]


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


class ProfileCase(HomeTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(R, "full_config", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def write_resume_text(self):
        base = M.validate(fixture("base.json"))
        text = M.render_txt(M.to_render_model(base))
        d = os.path.join(paths.private_dir(), "resume")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "resume.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return text

    def infer(self, data=None, salary=True):
        with db.tx(self.conn):
            if salary:
                P.record_salary(self.conn, fixture("salary.json"))
            return P.record_inference(self.conn, data or fixture("inference.json"))

    def answer_all(self, answers=REQUIRED_ANSWERS, **kw):
        out = None
        for qid, value in answers:
            with db.tx(self.conn):
                out = P.answer(self.conn, qid, value, **kw)
        return out


class TestImport(ProfileCase):
    def _files(self):
        base = M.to_render_model(M.validate(fixture("base.json")))
        pdf_path = os.path.join(self.home.dir, "upload", "resume.pdf")
        docx_path = os.path.join(self.home.dir, "upload", "resume.docx")
        PDF.render(base, pdf_path)
        D.render(base, docx_path)
        return pdf_path, docx_path

    def test_import_pdf_and_extra_info(self):
        pdf_path, _docx = self._files()
        with mock.patch("jobhunter.pdftext._pdftotext", return_value=None):
            data = P.import_resume(pdf_path, os.path.join(FIX, "extra_info.md"))
        self.assertEqual(data["extract_method"], "stdlib_pdf")
        self.assertEqual(data["quality"], "good")
        self.assertTrue(os.path.isfile(os.path.join(paths.private_dir(), "resume", "original.pdf")))
        with open(data["text_path"], encoding="utf-8") as fh:
            self.assertIn("Tidemark Logistics", fh.read())
        self.assertEqual(os.stat(data["text_path"]).st_mode & 0o777, 0o600)
        self.assertTrue(os.path.isfile(P.extra_info_path()))
        onboarding = os.path.join(paths.ws_dir("evaluator"), "work", "onboarding")
        self.assertTrue(os.path.isfile(os.path.join(onboarding, "resume.txt")))
        self.assertTrue(os.path.isfile(os.path.join(onboarding, "extra_info.md")))
        self.assertEqual(P.status()["input_files"]["resume_original"],
                         os.path.join(paths.private_dir(), "resume", "original.pdf"))

    def test_import_docx_replaces_the_old_original(self):
        pdf_path, docx_path = self._files()
        with mock.patch("jobhunter.pdftext._pdftotext", return_value=None):
            P.import_resume(pdf_path)
        data = P.import_resume(docx_path)
        self.assertEqual(data["extract_method"], "docx_zip")
        self.assertFalse(os.path.exists(os.path.join(paths.private_dir(), "resume", "original.pdf")))
        self.assertTrue(os.path.exists(os.path.join(paths.private_dir(), "resume", "original.docx")))

    def test_poor_extraction_keeps_pasted_text(self):
        pasted = self.write_resume_text()
        thin = os.path.join(self.home.dir, "thin.txt")
        with open(thin, "w", encoding="utf-8") as fh:
            fh.write("scan")
        data = P.import_resume(thin)
        self.assertEqual(data["quality"], "poor")
        self.assertTrue(data["kept_pasted_text"])
        with open(P.resume_text_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), pasted)

    def test_bad_type(self):
        bad = os.path.join(self.home.dir, "resume.rtf")
        with open(bad, "w") as fh:
            fh.write("x")
        with self.assertRaises(Denied):
            P.import_resume(bad)


class TestSalary(ProfileCase):
    def test_record_and_validate(self):
        with db.tx(self.conn):
            data = P.record_salary(self.conn, fixture("salary.json"))
        self.assertEqual(data["sources_recorded"], 2)
        self.assertIn("https://salaries.example.org/data-analyst-springfield", P.salary_urls())
        self.assertTrue(os.path.isfile(os.path.join(paths.ws_dir("evaluator"), "work", "onboarding", "salary.json")))
        for mutate in (lambda d: d.update(extra=1),
                       lambda d: d["sources"][0].update(url="http://insecure.example.org/x"),
                       lambda d: d["sources"][0].update(low=90000),
                       lambda d: d.update(sources=d["sources"] * 4),
                       lambda d: d["sources"][0].update(currency="usd")):
            bad = fixture("salary.json")
            mutate(bad)
            with self.assertRaises(Denied) as cm:
                P.validate_salary(bad)
            self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_salary_packet_for_the_scout(self):
        p = P.write_salary_packet(["Data Analyst"], "Springfield")
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"role_titles": ["Data Analyst"], "city": "springfield"})
        paths.ensure_agent_path(p, "jobhunter-scout")


class TestInference(ProfileCase):
    def test_record_inference(self):
        self.write_resume_text()
        data = self.infer()
        self.assertEqual(data["facts"], 5)
        self.assertEqual(data["profile_version"], "")
        self.assertIn("role_families", data["fields_inferred"])
        self.assertTrue(os.path.isfile(R.base_path()))
        self.assertEqual(R.load_base()["experience"][0]["employer"], "Tidemark Logistics")
        self.assertEqual(sorted(P.facts()), ["P1", "P2", "P3", "P4", "P5"])
        task = self.conn.execute("SELECT * FROM human_tasks WHERE kind = 'confirm_profile'").fetchone()
        self.assertIsNotNone(task)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM notifications WHERE kind = 'question'").fetchone())
        # nothing inferred is confirmed
        conf = P.load_confirmed()
        self.assertFalse(conf["confirmed"])
        self.assertEqual(conf["fields"], {})
        self.assertEqual(conf["profile_version"], "")
        raw = P.load_raw()
        self.assertEqual(raw["fields"]["role_families"]["source"], "inferred")
        qs = {q["id"]: q for q in P.questions()}
        self.assertIn("Data analytics: Data Analyst, Business Analyst", qs["Q1"]["default"])
        self.assertEqual(qs["Q4"]["default"], "3")
        self.assertEqual(qs["Q6"]["default"], "60000")
        self.assertEqual(qs["Q22"]["default"], "alex.rivera@example.com")
        self.assertIn("X1", qs)

    def test_invented_number_in_a_fact_is_refused(self):
        self.write_resume_text()
        data = fixture("inference.json")
        data["facts"]["P3"]["text"] = "Automated the carrier scorecard, saving 25 hours a week."
        with self.assertRaises(Denied) as cm:
            self.infer(data)
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        self.assertIn("P3: 25", cm.exception.data["errors"])

    def test_invented_year_in_base_resume_is_refused(self):
        self.write_resume_text()
        data = fixture("inference.json")
        data["base_resume"]["experience"][1]["dates"] = {"start": "2017-01", "end": "2021-06"}
        with self.assertRaises(Denied) as cm:
            self.infer(data)
        self.assertEqual(cm.exception.code, "E_VALIDATION")

    def test_salary_page_must_be_recorded(self):
        with self.assertRaises(Denied) as cm:
            self.infer(salary=False)
        self.assertEqual(cm.exception.code, "E_EVIDENCE_MISSING")

    def test_schema_errors(self):
        for mutate in (lambda d: d.update(unknown=1),
                       lambda d: d["facts"].update(P9={"text": "x", "source": "a blog post"}),
                       lambda d: d["role_families"][0].update(evidence_fact_ids=["P42"]),
                       lambda d: d["salary_band"].update(floor=90000),
                       lambda d: d.update(experience_years={"value": 3})):
            data = fixture("inference.json")
            mutate(data)
            with self.assertRaises(Denied) as cm:
                self.infer(data)
            self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_base_resume_with_a_dash_is_refused(self):
        data = fixture("inference.json")
        data["base_resume"]["experience"][0]["bullets"][0]["text"] = "Built forecasts " + chr(0x2014) + " weekly."
        with self.assertRaises(Denied) as cm:
            self.infer(data)
        self.assertEqual(cm.exception.code, "E_VALIDATION")


class TestAnswers(ProfileCase):
    def setUp(self):
        super().setUp()
        self.infer()

    def test_confirming_every_required_answer(self):
        with db.tx(self.conn):
            out = P.answer(self.conn, "Q1", REQUIRED_ANSWERS[0][1])
        self.assertIn("Q2", out["remaining_required"])
        out = self.answer_all()
        self.assertEqual(out["remaining_required"], [])
        conf = P.load_confirmed()
        self.assertTrue(conf["confirmed"])
        pv = conf["profile_version"]
        self.assertEqual(len(pv), 64)
        self.assertEqual(db.meta_get(self.conn, "profile_version"), pv)
        f = {k: v["value"] for k, v in conf["fields"].items()}
        self.assertTrue(all(v["source"] == "user_confirmed" for v in conf["fields"].values()))
        self.assertEqual(f["seniority"], {"min": "associate", "max": "senior"})
        self.assertEqual(f["experience_years"], 3.0)
        self.assertEqual(f["salary"], {"currency": "USD", "period": "year", "floor": 60000, "target": 72000,
                                       "may_state_expected_in_forms": True})
        self.assertEqual(f["notice_period_days"], 30)
        self.assertEqual(f["locations"], {"cities": ["springfield", "remote"], "work_modes": ["remote", "hybrid"],
                                          "relocate": False})
        self.assertEqual(f["work_authorization"], {"US": "citizen", "other": "needs_sponsorship"})
        fam = f["role_families"][0]
        self.assertEqual(fam["titles"], ["Data Analyst", "Business Analyst"])
        self.assertIn("analyst", fam["include"])
        self.assertEqual(fam["exclude"], ["director", "intern"])
        self.assertIsNotNone(self.conn.execute("SELECT done_at FROM human_tasks WHERE kind = 'confirm_profile'")
                             .fetchone()[0])
        with open(P.profile_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["profile_version"], pv)
        st = P.status()
        self.assertTrue(st["confirmed"])
        self.assertEqual(st["missing"], [])

    def test_changing_an_answer_bumps_the_version(self):
        self.answer_all()
        pv = P.load_confirmed()["profile_version"]
        with db.tx(self.conn):
            out = P.answer(self.conn, "notice_period_days", "2 months")
        self.assertTrue(out["requeue_suggested"])
        self.assertEqual(P.load_confirmed()["fields"]["notice_period_days"]["value"], 60)
        self.assertNotEqual(P.load_confirmed()["profile_version"], pv)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key LIKE 'profile:changed:%'").fetchone())

    def test_parsing(self):
        cases = [
            ("Q6", "15 lakh", 1500000), ("Q6", "1,250,000", 1250000), ("Q6", "120k", 120000),
            ("Q10", "immediate", 0), ("Q10", "2 weeks", 14), ("Q3", "senior", None), ("Q8", "no", None),
            ("Q12", "on-site", None), ("Q5", "EUR monthly", None),
        ]
        for qid, value, want in cases:
            with db.tx(self.conn):
                P.answer(self.conn, qid, value)
            if want is not None:
                stored = P.load_raw()["answers"][qid]["value"]
                self.assertEqual(stored, want, (qid, value))
        raw = P.load_raw()["answers"]
        self.assertEqual(raw["Q3"]["value"], {"min": "senior", "max": "senior"})
        self.assertIs(raw["Q8"]["value"], False)
        self.assertEqual(raw["Q12"]["value"], ["onsite"])
        self.assertEqual(raw["Q5"]["value"], {"currency": "EUR", "period": "month"})

    def test_invalid_answers(self):
        for qid, value in (("Q3", "wizard"), ("Q8", "maybe"), ("Q14", "Mars: citizen"), ("Q12", "space"),
                           ("Q6", "a lot"), ("Q10", "soon"), ("Q1", "Data " + chr(0x2013) + " analytics: Analyst"),
                           ("Q22", "not-an-email")):
            with self.assertRaises(Denied) as cm:
                with db.tx(self.conn):
                    P.answer(self.conn, qid, value)
            self.assertEqual(cm.exception.code, "E_VALIDATION", (qid, value))
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                P.answer(self.conn, "Q99", "x")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")

    def test_sensitive_answers_need_the_terminal(self):
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                P.answer(self.conn, "Q9", "50000", sensitive_ok=False, by="chat")
        self.assertEqual(cm.exception.code, "E_HUMAN_ONLY")
        with db.tx(self.conn):
            P.answer(self.conn, "Q9", "50000", sensitive_ok=True)
        with db.tx(self.conn):
            P.answer(self.conn, "Q9", "skip")
        self.assertNotIn("Q9", P.load_raw()["answers"])

    def test_open_question_answer_becomes_a_fact(self):
        with db.tx(self.conn):
            out = P.answer(self.conn, "X1", "I ran them with one other analyst.")
        self.assertEqual(out["field"], "X1")
        fx = P.facts()
        new = [k for k, v in fx.items() if "one other analyst" in v]
        self.assertEqual(len(new), 1)
        self.assertTrue(P.load_raw()["facts"][new[0]]["source"].startswith("user_confirmed"))
        self.assertEqual(P.questions(only_open=True)[-1]["id"] != "X1", True)

    def test_answers_survive_a_new_inference(self):
        self.answer_all()
        pv = P.load_confirmed()["profile_version"]
        self.infer()
        self.assertEqual(P.load_confirmed()["profile_version"], pv)
        self.assertTrue(P.load_confirmed()["confirmed"])

    def test_feasibility(self):
        self.answer_all()
        self.assertTrue(P.feasibility()["ok"])
        with db.tx(self.conn):
            P.answer(self.conn, "Q6", "150000")
            P.answer(self.conn, "Q7", "160000")
            P.answer(self.conn, "Q3", "director")
            P.answer(self.conn, "Q2", "analyst")
        res = P.feasibility()
        self.assertFalse(res["ok"])
        texts = " ".join(c["text"] for c in res["conflicts"])
        self.assertIn("above the highest researched pay", texts)
        self.assertIn("Director roles usually ask for about 10 years", texts)
        self.assertIn("avoid 'analyst'", texts)
        with db.tx(self.conn):
            P.answer(self.conn, "Q7", "100000")
        self.assertIn("Your salary floor is above your target.", [c["text"] for c in P.feasibility()["conflicts"]])

    def test_interview_accepts_defaults(self):
        answers = {"Q2": "none", "Q3": "", "Q5": "", "Q7": "", "Q8": "", "Q10": "immediate", "Q14": "US: citizen"}
        lines = []
        for q in P.questions():
            lines.append(answers.get(q["id"], ""))
        inp = io.StringIO("\n".join(lines) + "\n")
        out = io.StringIO()
        res = P.interview(lambda: db.connect(), inp, out)
        self.assertTrue(res["complete"], (res, out.getvalue()[-500:]))
        f = {k: v["value"] for k, v in P.load_confirmed()["fields"].items()}
        self.assertEqual(f["experience_years"], 3.0)
        self.assertEqual(f["notice_period_days"], 0)
        self.assertEqual(f["roles_avoid"], [])
        self.assertEqual(f["salary"]["floor"], 60000)
        self.assertIs(f["salary"]["may_state_expected_in_forms"], False)
        self.assertIn("default: 3", out.getvalue())


class _Caller:
    def __init__(self, cls, agent_id=None):
        self.cls = cls
        self.agent_id = agent_id


class TestCli(ProfileCase):
    def run_cli(self, argv, caller=None):
        out = io.StringIO()
        with mock.patch.object(cli, "classify", return_value=caller or _Caller("system")), \
                mock.patch.object(cli, "require"):
            rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_agent_records_and_system_reads(self):
        scout = _Caller("agent", "jobhunter-scout")
        f = self.home.write_agent_file("scout", "salary.json", json.dumps(fixture("salary.json")))
        rc, env = self.run_cli(["profile", "salary-record", "--file", f], scout)
        self.assertEqual((rc, env["data"]["sources_recorded"]), (0, 2))
        ev = _Caller("agent", "jobhunter-evaluator")
        f = self.home.write_agent_file("evaluator", "inference.json", json.dumps(fixture("inference.json")))
        rc, env = self.run_cli(["profile", "infer-record", "--file", f], ev)
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli(["profile", "status"], ev)
        self.assertEqual((rc, env["data"]["confirmed"]), (0, False))
        rc, env = self.run_cli(["profile", "questions", "--format", "chat", "--only-open"])
        self.assertEqual(rc, 0)
        self.assertIn("Q1 (required)", env["data"]["text"])
        rc, env = self.run_cli(["profile", "facts"])
        self.assertEqual(sorted(env["data"]["facts"]), ["P1", "P2", "P3", "P4", "P5"])
        rc, env = self.run_cli(["profile", "answer", "--field", "Q4", "--value", "3"], _Caller("chat"))
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli(["profile", "answer", "--field", "Q9", "--value", "50000"], _Caller("chat"))
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"))
        rc, env = self.run_cli(["profile", "feasibility"])
        self.assertEqual(rc, 0)

    def test_agent_path_confinement(self):
        outside = os.path.join(self.home.dir, "salary.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump(fixture("salary.json"), fh)
        rc, env = self.run_cli(["profile", "salary-record", "--file", outside], _Caller("agent", "jobhunter-scout"))
        self.assertEqual((rc, env["code"]), (10, "E_PATH_NOT_ALLOWED"))


if __name__ == "__main__":
    unittest.main()
