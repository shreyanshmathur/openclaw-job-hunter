"""U4: base resume model, structured dates, text rules, tailoring and the R-* checks (design 6.4, 12.5, 13.2)."""
from __future__ import annotations

import copy
import json
import os
import unittest

import tests  # noqa: F401
from jobhunter.errors import Denied
from jobhunter.resume import fonts_helvetica as FH
from jobhunter.resume import model as M
from jobhunter.resume import tailor as T

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profile")
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)


def load(name: str) -> dict:
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def base() -> dict:
    return M.validate(load("base.json"))


FACTS = {
    "P1": "Built the COD return-risk model at Tidemark Logistics; RTO fell from 18% to 13% in two quarters.",
    "P2": "Built weekly returns forecasts in SQL and Python for 40 warehouses at Tidemark Logistics.",
}


def tailored(name: str = "tailor_light.json") -> dict:
    t = load(name)
    t["job_uid"] = "JAAAAAAA"
    return t


class TestValidate(unittest.TestCase):
    def test_fixture_is_valid_and_normalized(self):
        b = base()
        self.assertEqual(b["version"], 1)
        self.assertEqual(b["experience"][0]["dates"], {"start": "2022-07", "end": "present"})
        self.assertEqual(b["languages"], [])
        self.assertEqual(len(b["dash_conversions"]), 2)
        self.assertEqual(len(M.base_sha256(b)), 64)

    def test_unknown_key_is_schema_error(self):
        raw = load("base.json")
        raw["hobbies"] = ["chess"]
        with self.assertRaises(Denied) as cm:
            M.validate(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_dash_in_bullet_is_refused(self):
        for dash in (EN_DASH, EM_DASH, chr(0x2212)):
            raw = load("base.json")
            raw["experience"][0]["bullets"][0]["text"] = "Built forecasts %s for 40 warehouses." % dash
            with self.assertRaises(Denied) as cm:
                M.validate(raw)
            self.assertEqual(cm.exception.code, "E_VALIDATION")
            self.assertIn("DASH", " ".join(cm.exception.data["errors"]))

    def test_spaced_hyphen_is_refused_but_hyphenated_words_are_fine(self):
        raw = load("base.json")
        raw["experience"][0]["bullets"][0]["text"] = "Built forecasts - for 40 warehouses."
        with self.assertRaises(Denied):
            M.validate(raw)
        raw = load("base.json")
        raw["experience"][0]["bullets"][0]["text"] = "Built end-to-end forecasts for 40 warehouses."
        M.validate(raw)

    def test_dash_conversion_original_may_keep_its_dash(self):
        b = base()
        self.assertIn(EN_DASH, b["dash_conversions"][0]["original"])
        raw = load("base.json")
        raw["dash_conversions"][0]["replacement"] = "Jul 2022 %s Present" % EN_DASH
        with self.assertRaises(Denied):
            M.validate(raw)

    def test_dates(self):
        raw = load("base.json")
        raw["experience"][0]["dates"] = {"start": "07/2022", "end": "present"}
        with self.assertRaises(Denied) as cm:
            M.validate(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        raw = load("base.json")
        raw["experience"][1]["dates"] = {"start": "2021-09", "end": "2021-01"}
        with self.assertRaises(Denied):
            M.validate(raw)
        raw = load("base.json")
        raw["experience"][1]["dates"] = {"start": "2019", "end": "2020"}
        M.validate(raw)

    def test_bullet_ids_belong_to_their_role(self):
        raw = load("base.json")
        raw["experience"][1]["bullets"][0]["id"] = "E1.B9"
        with self.assertRaises(Denied) as cm:
            M.validate(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_character_outside_winansi_is_refused(self):
        raw = load("base.json")
        raw["experience"][0]["bullets"][0]["text"] = "Shipped a model " + chr(0x1F680)
        with self.assertRaises(Denied):
            M.validate(raw)

    def test_accented_names_are_allowed(self):
        raw = load("base.json")
        raw["contact"]["full_name"] = "Zo" + chr(0xEB) + " Rivera"
        raw["contact"]["first_name"] = "Zo" + chr(0xEB)
        b = M.validate(raw)
        self.assertEqual(b["contact"]["first_name"], "Zo" + chr(0xEB))


class TestDatesAndText(unittest.TestCase):
    def test_ranges_never_contain_a_dash(self):
        self.assertEqual(M.format_range({"start": "2020-01", "end": "present"}), "Jan 2020 to Present")
        self.assertEqual(M.format_range({"start": "2019-03", "end": "2021-11"}), "Mar 2019 to Nov 2021")
        self.assertEqual(M.format_range({"start": "2019", "end": "2021"}), "2019 to 2021")
        self.assertEqual(M.format_range({"end": "2022-05"}), "May 2022")
        self.assertEqual(M.format_range({"start": "2020-01", "end": "present"},
                                        {"range_word": "until", "present_word": "Now", "date_format": "MMMM YYYY"}),
                         "January 2020 until Now")
        self.assertEqual(M.format_range({"start": "2020-01", "end": "2020-01"}), "Jan 2020")

    def test_plain_text_view(self):
        txt = M.render_txt(M.to_render_model(base()))
        self.assertIn("Data Analyst, Tidemark Logistics, Springfield, Jul 2022 to Present", txt)
        self.assertIn("  Built weekly returns forecasts in SQL and Python for 40 warehouses.", txt)
        self.assertEqual(M.char_findings(txt), [])
        self.assertNotRegex(txt, r"(?m)^\s*[-*+]\s")
        self.assertTrue(all(ord(c) < 128 for c in txt))

    def test_review_text_lists_every_dash_conversion(self):
        out = M.review_text(base())
        self.assertIn("Dash conversions (2)", out)
        self.assertIn("original:    Jul 2022 %s Present" % EN_DASH, out)
        self.assertIn("replacement: COD return-risk model; RTO fell", out)

    def test_numbers(self):
        self.assertEqual(M.numbers("RTO fell from 18% to 13% in two quarters"),
                         {("18", "%"), ("13", "%"), ("2", "")})
        self.assertEqual(M.numbers("saved 1,200 hours and 2.5M rupees"), {("1200", ""), ("2.5", "m")})
        self.assertEqual(M.numbers("worked 5 months"), {("5", "")})
        self.assertEqual(M.numbers_subset({("13", "")}, {("13", "%")}), set())
        self.assertEqual(M.numbers_subset({("13", "k")}, {("13", "%")}), {("13", "k")})

    def test_assert_printable(self):
        m = M.to_render_model(base())
        M.assert_printable(m)
        m["experience"][0]["bullets"][0]["text"] = "a %s b" % EN_DASH
        with self.assertRaises(ValueError):
            M.assert_printable(m)


class TestFonts(unittest.TestCase):
    def test_tables(self):
        for f in FH.FONTS:
            self.assertEqual(len(FH.WIDTHS[f]), 256)
        self.assertEqual(FH.WIDTHS["Helvetica"][ord("A")], 667)
        self.assertEqual(FH.WIDTHS["Helvetica-Bold"][ord("a")], 556)
        self.assertEqual(FH.WIDTHS["Helvetica"][ord(" ")], 278)
        self.assertAlmostEqual(FH.string_width("Hi", "Helvetica", 10), (722 + 222) / 100.0)
        self.assertGreater(FH.string_width("Rivera", "Helvetica-Bold", 10), FH.string_width("Rivera", "Helvetica", 10))

    def test_encode(self):
        self.assertEqual(FH.encode("caf" + chr(0xE9)), b"caf\xe9")
        with self.assertRaises(ValueError):
            FH.encode(chr(0x4E2D))
        with self.assertRaises(ValueError):
            FH.encode("tab\there")


class TestTailor(unittest.TestCase):
    def setUp(self):
        self.base = base()

    def run_lint(self, t, facts=FACTS, pages=1, extra=None):
        t = T.validate_schema(t, self.base)
        rendered = T.apply(self.base, t)
        return T.lint(self.base, t, rendered, facts=facts, extra_text=extra, pages=pages, max_pages=2), rendered

    def rules(self, res):
        return [b[0] for b in res["blocks"]]

    def test_light_tailor_passes_and_applies(self):
        res, m = self.run_lint(tailored())
        self.assertTrue(res["pass"], res)
        e1 = m["experience"][0]
        self.assertEqual([b["id"] for b in e1["bullets"]], ["E1.B2", "E1.B1", "E1.B3"])
        self.assertEqual(e1["title"], "Data Analyst")
        self.assertEqual(e1["dates"], self.base["experience"][0]["dates"])
        # role E2 is not listed: it keeps every base bullet
        self.assertEqual(m["experience"][1]["bullets"], self.base["experience"][1]["bullets"])
        self.assertEqual(m["skills"][:3], ["Python", "SQL", "Forecasting"])
        self.assertEqual(sorted(m["skills"]), sorted(self.base["skills"]))
        self.assertEqual(m["summary"], "Analyst who builds forecasting and risk models in SQL and Python.")
        self.assertEqual(m["sections_order"][:3], ["summary", "experience", "skills"])

    def test_full_tailor_passes(self):
        res, m = self.run_lint(tailored("tailor_full.json"))
        self.assertTrue(res["pass"], res)
        self.assertIn("cut RTO from 18% to 13%", m["experience"][0]["bullets"][0]["text"])

    def test_invented_number_is_caught(self):
        t = tailored("tailor_full.json")
        t["experience"][0]["bullets"][0]["text"] = "Built a return-risk model that cut RTO from 18% to 9% in one quarter."
        res, _m = self.run_lint(t)
        self.assertFalse(res["pass"])
        self.assertIn("R-NEW-NUMBER", self.rules(res))
        detail = [b[1] for b in res["blocks"] if b[0] == "R-NEW-NUMBER"][0]
        self.assertIn("9%", detail)

    def test_new_skill_in_skills_line_is_caught(self):
        t = tailored()
        t["skills_order"] = ["Python", "dbt"]
        res, m = self.run_lint(t)
        self.assertIn("R-NEW-SKILL", self.rules(res))
        self.assertNotIn("dbt", m["skills"])

    def test_new_tool_in_rephrased_bullet_is_caught(self):
        t = tailored("tailor_full.json")
        t["experience"][0]["bullets"][1]["text"] = "Forecast weekly returns for 40 warehouses using dbt and Snowflake."
        res, _m = self.run_lint(t)
        blocks = [b for b in res["blocks"] if b[0] == "R-NEW-SKILL"]
        self.assertTrue(blocks)
        self.assertIn("dbt", blocks[0][1])
        self.assertIn("snowflake", blocks[0][1])

    def test_tool_named_in_extra_info_is_allowed(self):
        t = tailored("tailor_full.json")
        t["experience"][0]["bullets"][1]["text"] = "Forecast weekly returns for 40 warehouses in SQL and Airflow."
        res, _m = self.run_lint(t)
        self.assertIn("R-NEW-SKILL", self.rules(res))
        res, _m = self.run_lint(t, extra="Tools I use: Airflow, SQL.")
        self.assertTrue(res["pass"], res)

    def test_light_mode_keeps_bullets_verbatim(self):
        t = tailored()
        t["experience"][0]["bullets"][0]["text"] = "Built a model for COD returns; RTO fell from 18% to 13% in two quarters."
        res, _m = self.run_lint(t)
        self.assertIn("R-LIGHT-REPHRASE", self.rules(res))

    def test_unknown_and_duplicate_bullets(self):
        t = tailored()
        t["experience"][0]["bullets"] = [{"from": "E2.B1", "text": None}, {"from": "E1.B1", "text": None},
                                         {"from": "E1.B1", "text": None}]
        res, m = self.run_lint(t)
        self.assertIn("R-BULLET-UNKNOWN", self.rules(res))
        self.assertIn("R-BULLET-DUP", self.rules(res))
        self.assertEqual([b["id"] for b in m["experience"][0]["bullets"]], ["E1.B1"])

    def test_role_needs_a_bullet(self):
        t = tailored()
        t["experience"][0]["bullets"] = []
        res, _m = self.run_lint(t)
        self.assertIn("R-ROLE-EMPTY", self.rules(res))

    def test_unknown_role_and_project(self):
        t = tailored()
        t["experience"].append({"role_id": "E9", "bullets": [{"from": "E9.B1", "text": None}]})
        t["projects"] = [{"project_id": "PR7", "include": True}]
        res, _m = self.run_lint(t)
        self.assertIn("R-ROLE-UNKNOWN", self.rules(res))
        self.assertIn("R-PROJECT-UNKNOWN", self.rules(res))

    def test_summary_rules(self):
        t = tailored()
        t["summary"] = {"text": "Analyst with 7 years of forecasting in SQL.", "fact_ids": ["P2"]}
        res, _m = self.run_lint(t)
        self.assertIn("R-SUMMARY-NUMBER", self.rules(res))
        t["summary"] = {"text": "Analyst who builds forecasts.", "fact_ids": ["P99"]}
        res, _m = self.run_lint(t)
        self.assertIn("R-SUMMARY-FACT", self.rules(res))
        t["summary"] = {"text": "Analyst who builds forecasts.", "fact_ids": []}
        res, _m = self.run_lint(t)
        self.assertIn("R-SUMMARY-FACT", self.rules(res))

    def test_dash_in_tailored_text(self):
        t = tailored("tailor_full.json")
        t["experience"][0]["bullets"][1]["text"] = "Forecast weekly returns %s 40 warehouses, SQL and Python." % EM_DASH
        res, _m = self.run_lint(t)
        self.assertIn("R-DASH", self.rules(res))

    def test_page_limit(self):
        res, _m = self.run_lint(tailored(), pages=3)
        self.assertIn("R-PAGES", self.rules(res))

    def test_project_can_be_left_out(self):
        t = tailored()
        t["projects"] = [{"project_id": "PR1", "include": False}]
        res, m = self.run_lint(t)
        self.assertTrue(res["pass"], res)
        self.assertEqual(m["projects"], [])

    def test_schema_errors(self):
        for bad in ({"mode": "wild"}, {"extra": 1}, {"experience": [{"role_id": "E1", "bullets": [{"text": "x"}]}]},
                    {"sections_order": ["hobbies"]}, {"summary": {"text": "x", "fact_ids": ["X1"]}}):
            t = tailored()
            t.update(bad)
            with self.assertRaises(Denied) as cm:
                T.validate_schema(t, self.base)
            self.assertEqual(cm.exception.code, "E_SCHEMA", bad)

    def test_identity_checks_catch_a_tampered_render(self):
        t = T.validate_schema(tailored(), self.base)
        rendered = T.apply(self.base, t)
        bad = copy.deepcopy(rendered)
        bad["experience"][0]["title"] = "Senior Data Scientist"
        bad["experience"][0]["dates"] = {"start": "2019-01", "end": "present"}
        bad["contact"]["email"] = "someone@example.com"
        res = T.lint(self.base, t, bad, facts=FACTS, pages=1)
        rules = self.rules(res)
        for r in ("R-TITLE-CHANGED", "R-DATES-CHANGED", "R-CONTACT-CHANGED"):
            self.assertIn(r, rules)

    def test_changes_against_the_base(self):
        t = T.validate_schema(tailored(), self.base)
        lines = T.changes(self.base, T.apply(self.base, t))
        self.assertIn("E1 (Tidemark Logistics): bullets E1.B2, E1.B1, E1.B3; left out E1.B4", lines)
        self.assertIn("Summary: Analyst who builds forecasting and risk models in SQL and Python.", lines)
        self.assertTrue(any(x.startswith("Skills order: Python, SQL, Forecasting") for x in lines))
        self.assertEqual(T.changes(self.base, M.to_render_model(self.base)), [])

    def test_base_lint(self):
        self.assertTrue(T.lint_base(self.base, pages=1)["pass"])
        self.assertIn("R-PAGES", [b[0] for b in T.lint_base(self.base, pages=3)["blocks"]])


if __name__ == "__main__":
    unittest.main()
