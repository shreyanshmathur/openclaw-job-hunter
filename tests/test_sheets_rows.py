"""Row builders (design 7.3): shared labels, the Code.gs column contract, watermarks and value conventions."""
from __future__ import annotations

import json
import unittest

import tests  # noqa: F401
from jobhunter import sheets_labels as L
from jobhunter import sheets_rows as R
from jobhunter import status as S
from tests.fakes.u5 import config, seed
from tests.fixtures.sheets.fake_webapp import FakeSheet
from tests.helpers import HomeTestCase


def ctx_for(cfg=None) -> R.RowCtx:
    cfg = cfg or config()
    return R.RowCtx(config=cfg, tz=S.tzinfo(cfg), style=cfg["sheets"]["person_name_style"],
                    store_text=cfg["sheets"]["store_message_text"], skipped_days=cfg["sheets"]["skipped_tab_days"],
                    experience=2.0, now=S.canon.now())


class TestRowBuilders(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.s = seed(self.conn, self.clock)
        self.ctx = ctx_for()

    def rows(self, tab, since=None, ctx=None):
        return {rid: row for rid, row, _ in R.build_rows_stamped(self.conn, tab, since, ctx or self.ctx)}

    def test_every_tab_matches_the_code_gs_contract(self):
        sheet = FakeSheet("x" * 64)
        for tab in L.TABLE_ORDER:
            for rid, row, stamp in R.build_rows_stamped(self.conn, tab, None, self.ctx):
                with self.subTest(tab=tab, rid=rid):
                    self.assertTrue(isinstance(rid, str) and 0 < len(rid) <= 64)
                    self.assertTrue(stamp)
                    sheet._check_row(tab, rid, row)
        self.assertEqual(sheet.violations, [])

    def test_build_rows_is_the_12_10_form(self):
        rows = R.build_rows(self.conn, "jobs", None)
        self.assertTrue(all(len(x) == 2 and isinstance(x[1], dict) for x in rows))

    def test_approvals(self):
        rows = self.rows("approvals")
        self.assertIn("DAAAAAA2", rows)
        r = rows["DAAAAAA2"]
        self.assertEqual(r["code"], "A7K2")
        self.assertEqual(r["what"], "Cold email")
        self.assertEqual(r["to"], "Sam L. (Data Lead)")
        self.assertEqual(r["company"], "Tidewater Labs")
        self.assertEqual(r["subject"], "Returns forecasting at Tidewater")
        self.assertIn("Hi Sam", r["message"])
        self.assertEqual(r["qc"], 4.35)
        self.assertEqual(r["status"], "Awaiting approval")
        self.assertEqual(r["decision"], "")
        # decided by the person in the last 7 days: still listed, now Sent
        self.assertEqual(rows["DAAAAAA3"]["status"], "Sent")
        self.conn.execute("UPDATE drafts SET approved_by = 'auto', updated_at = ? WHERE id = ?",
                          (self.clock.ago(seconds=5), self.s.d_sent))
        self.assertNotIn("DAAAAAA3", self.rows("approvals"))   # QC approved it, the person never saw it

    def test_message_text_can_stay_local(self):
        cfg = config(sheets__store_message_text=False)
        rows = self.rows("approvals", ctx=ctx_for(cfg))
        self.assertEqual(rows["DAAAAAA2"]["message"], R.KEPT_LOCAL)
        self.assertEqual(self.rows("outreach", ctx=ctx_for(cfg))["TAAAAAAAAAAA2"]["message"], R.KEPT_LOCAL)

    def test_jobs(self):
        rows = self.rows("jobs")
        self.assertNotIn("JAAAAAA5", rows)   # new
        self.assertNotIn("JAAAAAA3", rows)   # pre-filter rejected goes to Skipped
        good = rows["JAAAAAA2"]
        self.assertEqual(good["fit"], 82)
        self.assertEqual(good["verdict"], "Good fit")
        self.assertEqual(good["status"], "Good fit")
        self.assertEqual(good["source"], "Company site (Greenhouse)")
        self.assertEqual(good["work_mode"], "Hybrid")
        self.assertEqual(good["posting"], {"text": "Open posting", "url": "https://job-boards.greenhouse.io/example/jobs/1"})
        border = rows["JAAAAAA4"]
        self.assertEqual(border["verdict"], "Borderline")
        self.assertIn("Years of experience do not match", border["gates"])
        self.assertIn("dbt certification", border["gates"])
        applied = rows["JAAAAAA6"]
        self.assertEqual(applied["status"], "Applied")
        self.assertEqual(applied["applied_on"], "2026-09-27")

    def test_skipped_uses_reason_sentences_and_window(self):
        rows = self.rows("skipped")
        self.assertEqual(rows["JAAAAAA3"]["reason"], "Needs 5 or more years; you have 2")
        self.assertEqual(rows["JAAAAAA3"]["your_call"], "")
        self.assertNotIn("JAAAAAA7", rows)   # older than skipped_tab_days
        self.assertIn("JAAAAAA7", R.skipped_expired(self.conn, self.ctx))

    def test_applications(self):
        r = self.rows("applications")["JAAAAAA6"]
        self.assertEqual(r["how"], "Company form (Greenhouse)")
        self.assertEqual(r["resume"], "Lightly tailored: Alex_Rivera_Resume.pdf")
        self.assertEqual(r["status"], "Applied")
        self.assertEqual(r["proof"], "Thanks for applying")
        self.assertEqual(r["outcome"], "No response yet")
        self.assertEqual(r["follow_up"], "None")

    def test_outreach(self):
        r = self.rows("outreach")["TAAAAAAAAAAA2"]
        self.assertEqual(r["channel"], "Email")
        self.assertEqual(r["person"], "Alex R.")
        self.assertEqual(r["their_role"], "Head of Analytics")
        self.assertEqual(r["profile"]["url"], "https://www.linkedin.com/in/example-person")
        self.assertIn("Their LinkedIn post (18 Sep 2026): pincode-level models", r["why_them"])
        self.assertEqual(r["status"], "Sent")
        self.assertEqual(r["reply"], "Positive reply")
        self.assertEqual(r["approved_by"], "You (terminal)")
        self.assertEqual(r["follow_up_due"], "2026-10-02")

    def test_followups(self):
        r = self.rows("followups")[self.s.thread_key]
        self.assertEqual(r["reply_type"], "Positive reply")
        self.assertEqual(r["what_they_said"], "Asks for a call next week.")
        self.assertEqual(r["next_step"], "They replied: your move")
        self.assertEqual(r["channel"], "Email")

    def test_qc_log(self):
        rows = self.rows("qc")
        r = rows["DAAAAAA2-1"]
        self.assertEqual(r["attempt"], "1 of 3")
        self.assertEqual(r["lint"], "Passed")
        self.assertEqual(r["reviewer"], "Passed")
        self.assertEqual(r["score"], 4.35)
        self.assertEqual(r["lowest"], "brevity")
        self.assertEqual(r["top_issue"], "Opening could be shorter")
        self.assertIn("Warning: S-LEN: long", r["lint_findings"])
        self.assertEqual(r["final"], "Awaiting approval")
        self.assertEqual(r["hash"], "ab" * 6)

    def test_lint_findings_read_as_rule_and_detail(self):
        # qc.lint stores [[rule, detail]] pairs; the QC log shows "RULE: detail", never a Python list
        text = R._findings(json.dumps([["L-DASH", "em dash at 12"], ["L-BANNED", "delve"]]),
                           json.dumps([["L-LONG", "182 words"]]))
        self.assertEqual(text, "L-DASH: em dash at 12\nL-BANNED: delve\nWarning: L-LONG: 182 words")
        self.assertNotIn("[", text)
        self.assertEqual(R._findings(json.dumps([["C-EXCLAMATION", ""], ["L-X", None]]), "[]"),
                         "C-EXCLAMATION\nL-X")
        self.assertEqual(R._findings("[]", json.dumps([{"code": "S-LEN", "message": "long"}])), "Warning: S-LEN: long")
        self.assertEqual(R._findings(None, "not json"), "")

    def test_daily_counts_one_row_per_local_day(self):
        rows = self.rows("daily")
        today = rows["D2026-09-27"]
        self.assertEqual(today["date"], "2026-09-27")
        self.assertEqual(today["applications"], 1)
        self.assertEqual(today["emails"], 1)
        self.assertEqual(today["replies"], 1)
        self.assertEqual(today["positive"], 1)
        self.assertEqual(today["stops"], 1)
        self.assertEqual(today["cycles"], 1)
        self.assertIn("D2026-08-18", rows)   # the 40 days old job starts the range

    def test_alerts(self):
        rows = self.rows("alerts")
        li = rows["A%d" % self.s.be_li]
        self.assertEqual(li["area"], "LinkedIn")
        self.assertEqual(li["status"], "Open")
        self.assertEqual(li["severity"], "Stopped")
        self.assertIn("CAPTCHA", li["what"])
        self.assertIn("Checkpoint page", li["what"])
        self.assertIn("breaker reset linkedin", li["todo"])
        self.assertTrue(li["until"])
        gm = rows["A%d" % self.s.be_gm]
        self.assertEqual(gm["status"], "Resolved")
        self.assertTrue(gm["resolved"])
        self.assertIsNone(gm["until"])

    def test_alert_without_cooldown_has_no_until_and_names_ats_like_the_chat_alert(self):
        from jobhunter import breakers as B
        B.trip(self.conn, "gmail", "gmail_security", "Critical security alert", by="system")
        self.clock.advance(minutes=5)
        B.trip(self.conn, "ats", "ats_blocked", "The form showed a bot check", by="guard")
        rows = self.rows("alerts")
        ids = {r[0]: r[1] for r in self.conn.execute(
            "SELECT scope, max(id) FROM breaker_events WHERE event = 'trip' GROUP BY scope")}
        gm = rows["A%d" % ids["gmail"]]
        self.assertEqual(gm["status"], "Open")
        self.assertEqual(gm["severity"], "Stopped")
        # human reset only: min_cooldown_until equals the trip time, which must not show as 'Paused until'
        tripped, until = self.conn.execute(
            "SELECT tripped_at, min_cooldown_until FROM breakers WHERE scope = 'gmail'").fetchone()
        self.assertLessEqual(until, tripped)
        self.assertIsNone(gm["until"])
        self.assertIn("breaker reset gmail", gm["todo"])
        self.assertNotIn("waiting time", gm["todo"])
        # a stop with a real cooldown keeps its 'Paused until', and 'ats' has the same name as in the chat alert
        ats = rows["A%d" % ids["ats"]]
        self.assertEqual(ats["area"], "Job forms")
        self.assertEqual(ats["area"], B.area_label("ats"))
        self.assertTrue(ats["until"])
        self.assertGreater(ats["until"], ats["time"])
        self.assertIn("waiting time", ats["todo"])
        sheet = FakeSheet("x" * 64)
        for rid, row, _ in R.build_rows_stamped(self.conn, "alerts", None, self.ctx):
            sheet._check_row("alerts", rid, row)
        self.assertEqual(sheet.violations, [])

    def test_watermark_selects_only_changed_rows(self):
        later = self.clock.advance(hours=2)
        for tab in ("jobs", "approvals", "outreach", "followups", "applications", "qc", "alerts", "skipped"):
            self.assertEqual(self.rows(tab, since=later), {}, tab)
        self.conn.execute("UPDATE jobs SET title = 'Lead Data Analyst', updated_at = ? WHERE id = ?",
                          (self.clock.advance(minutes=1), self.s.j_good))
        self.assertEqual(list(self.rows("jobs", since=later)), ["JAAAAAA2"])
        # a change in a joined table (company rename) also re-sends the job
        self.conn.execute("UPDATE companies SET display_name = 'Kestrel Commerce Ltd', updated_at = ? WHERE id = ?",
                          (self.clock.advance(minutes=1), self.s.k2))
        self.assertIn("JAAAAAA4", self.rows("jobs", since=later))

    def test_person_name_style_full(self):
        rows = self.rows("outreach", ctx=ctx_for(config(sheets__person_name_style="full")))
        self.assertEqual(rows["TAAAAAAAAAAA2"]["person"], "Alex Rivera")


if __name__ == "__main__":
    unittest.main()
