"""Ceilings, warm-up, clamps and config authority (design 4.1 to 4.4, 13.2): rolling windows, UTC-day and
PST-month resets, gauge max rule, warm-up weeks, adaptive clamps, hostile config clamped, lower and raise."""
from __future__ import annotations

import json
import os

import tests  # noqa: F401
from jobhunter import canon, ceilings, config, db, hardmax
from jobhunter.errors import Denied
from tests.fakes.u1 import TUESDAY_NOON, enable_linkedin, write_config
from tests.helpers import HomeTestCase, insert_action

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cfg_with(conn, **over):
    write_config(over)
    return config.load(conn)


class CeilingCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config({"boards.daily_jitter_pct": 10})

    def add(self, kind, n, platform=None, spread_minutes=0, **kw):
        for i in range(n):
            insert_action(self.conn, kind=kind, status=kw.get("status", "sent"), platform=platform,
                          reserved_at=self.clock.ago(minutes=spread_minutes * (i + 1)), li_note=kw.get("li_note", 0),
                          li_msg_seq=kw.get("li_msg_seq"))

    def denied(self, platform, kind, **kw):
        try:
            ceilings.check(self.conn, platform, kind, **kw)
        except Denied as d:
            return d
        return None


class TestLinkedInInvites(CeilingCase):
    def setUp(self):
        super().setUp()
        enable_linkedin(self.conn)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES "
                              "('linkedin', 'all', ?, 1)", (self.clock.ago(days=60),))

    def test_150_invites_in_26_hours_impossible(self):
        for tier in ("conservative", "moderate"):
            with db.tx(self.conn):
                self.conn.execute("DELETE FROM actions")
                db.meta_set(self.conn, "tier_linkedin", tier, "human")
            sent = 0
            for minute in range(0, 26 * 60, 12):
                self.clock.set(canon.ts_add(TUESDAY_NOON, minutes=minute))
                if self.denied("linkedin", "li_invite") is None:
                    insert_action(self.conn, kind="li_invite", platform="linkedin")
                    sent += 1
            self.assertLessEqual(sent, 2 * hardmax.HARD_MAX["linkedin.ceilings.*.invites.day"])
            self.assertLess(sent, 150)

    def test_rolling_day_and_hour(self):
        self.add("li_invite", 3, "linkedin", spread_minutes=5)
        d = self.denied("linkedin", "li_invite")
        self.assertEqual((d.code, d.data["window"]), ("E_CEILING", "hour"))
        self.assertGreater(d.retry_after, 0)
        self.clock.advance(minutes=61)
        self.assertIsNone(self.denied("linkedin", "li_invite"))

    def test_weekly_gauge_max_rule(self):
        with db.tx(self.conn):
            ceilings.usage_gauge(self.conn, "linkedin", "li_invites_sent_7d", 35)
        d = self.denied("linkedin", "li_invite")
        self.assertEqual((d.code, d.data["window"]), ("E_CEILING", "week"))

    def test_notes_quota_free_account(self):
        for i in range(3):
            insert_action(self.conn, kind="li_invite", platform="linkedin", li_note=1, li_msg_seq=1,
                          reserved_at=self.clock.ago(days=2 + i), contact_id=None)
        d = self.denied("linkedin", "li_invite", li_note=1)
        self.assertEqual((d.code, d.data["window"]), ("E_CEILING", "month"))
        self.assertIsNone(self.denied("linkedin", "li_invite", li_note=0))

    def test_pending_invites_hysteresis(self):
        with db.tx(self.conn):
            ceilings.usage_gauge(self.conn, "linkedin", "li_pending_invites", 150)
        self.assertEqual(self.denied("linkedin", "li_invite").code, "E_CEILING")
        self.clock.advance(minutes=1)
        with db.tx(self.conn):
            ceilings.usage_gauge(self.conn, "linkedin", "li_pending_invites", 130)
        self.assertIsNotNone(self.denied("linkedin", "li_invite"))
        self.clock.advance(minutes=1)
        with db.tx(self.conn):
            ceilings.usage_gauge(self.conn, "linkedin", "li_pending_invites", 110)
        self.assertIsNone(self.denied("linkedin", "li_invite"))

    def test_adaptive_acceptance(self):
        with db.tx(self.conn):
            for i in range(20):
                aid = insert_action(self.conn, kind="li_invite", platform="linkedin", reserved_at=self.clock.ago(days=10),
                                    contact_id=None)
                state = "invite_accepted" if i < 6 else "invite_pending"
                self.conn.execute("INSERT INTO threads (thread_key, channel, first_action_id, state, created_at, "
                                  "updated_at) VALUES (?, 'linkedin', ?, ?, ?, ?)", ("li:P%07d" % i, aid, state,
                                                                                     canon.now(), canon.now()))
        acc = ceilings.acceptance(self.conn, config.load(self.conn))
        self.assertEqual((acc["invites_aged"], acc["accepted"]), (20, 6))
        rows = ceilings.budget(self.conn, "linkedin", "li_invite")["rows"]
        day = [r for r in rows if r["item"] == "li_invite" and r["window"] == "day"][0]
        self.assertTrue(day["clamp_reason"] and "acceptance" in day["clamp_reason"], day)
        with db.tx(self.conn):
            self.conn.execute("UPDATE threads SET state = 'invite_pending'")
        d = self.denied("linkedin", "li_invite")
        self.assertIn("acceptance", d.message)


class TestWarmupAndResets(CeilingCase):
    def test_linkedin_warmup_week_one(self):
        enable_linkedin(self.conn)
        self.add("li_message", 4, "linkedin", spread_minutes=30, li_msg_seq=1)
        d = self.denied("linkedin", "li_message")
        self.assertEqual((d.code, d.data["warmup_week"]), ("E_WARMUP", 1))

    def test_gmail_warmup_steps(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES "
                              "('gmail', 'cold', ?, 1)", (self.clock.ago(days=15),))
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 3)
        rows = ceilings.budget(self.conn, "gmail", "cold_email")["rows"]
        cold = [r for r in rows if r["item"] == "cold emails" and r["window"] == "day"][0]
        self.assertEqual(cold["warmup_week"], 3)
        self.assertTrue(13 <= cold["limit"] <= 15, cold)          # ramp 15, tier 15 jittered by at most 10%
        with db.tx(self.conn):
            self.conn.execute("UPDATE warmup SET restarted_at = ?, restart_week = 1", (canon.now(),))
        rows = ceilings.budget(self.conn, "gmail", "cold_email")["rows"]
        cold = [r for r in rows if r["item"] == "cold emails" and r["window"] == "day"][0]
        self.assertEqual((cold["limit"], cold["warmup_week"]), (5, 1))

    def test_easy_apply_utc_day_reset(self):
        enable_linkedin(self.conn)
        write_config({"channels.linkedin.writes.easy_apply": True, "boards.daily_jitter_pct": 10})
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES "
                              "('linkedin', 'all', ?, 1)", (self.clock.ago(days=60),))
        self.clock.set("2026-09-29T23:30:00Z")
        for i in range(4):
            insert_action(self.conn, kind="application", platform="linkedin", reserved_at=self.clock.ago(hours=i * 5 + 1),
                          contact_id=None)
        rows = {(r["window"]): r for r in ceilings.budget(self.conn, "linkedin", "application")["rows"]
                if r["item"] == "application"}
        self.assertEqual(rows["utc_day"]["used"], 4)
        self.clock.set("2026-09-30T00:30:00Z")
        rows = {(r["window"]): r for r in ceilings.budget(self.conn, "linkedin", "application")["rows"]
                if r["item"] == "application"}
        self.assertEqual(rows["utc_day"]["used"], 0)

    def test_people_search_pst_month(self):
        enable_linkedin(self.conn)
        self.clock.set("2026-10-01T05:00:00Z")      # still September in Los Angeles
        start = ceilings.window_start("pst_month", config.load(self.conn))
        self.assertEqual(start, "2026-09-01T07:00:00Z")
        self.clock.set("2026-10-01T08:00:00Z")
        self.assertEqual(ceilings.window_start("pst_month", config.load(self.conn)), "2026-10-01T07:00:00Z")

    def test_usage_add_read_ceiling(self):
        enable_linkedin(self.conn)
        with db.tx(self.conn):
            for _ in range(8):
                ceilings.usage_add(self.conn, "linkedin", "profile_view", 1, cycle_id="C20260929T120000ZAAAA")
            self.assertDenied("E_CEILING", ceilings.usage_add, self.conn, "linkedin", "profile_view", 1,
                              "C20260929T120000ZAAAA")
            for _ in range(20):
                ceilings.usage_add(self.conn, "site:naukri", "page_view", 1)
            self.assertDenied("E_CEILING", ceilings.usage_add, self.conn, "site:naukri", "page_view", 1)

    def test_clamp_meta(self):
        with db.tx(self.conn):
            db.meta_set(self.conn, "clamp:gmail", json.dumps({"factor": 0.5, "until": canon.ts_add(canon.now(), days=7)}),
                        "system")
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES "
                              "('gmail', 'cold', ?, 1)", (self.clock.ago(days=60),))
        rows = ceilings.budget(self.conn, "gmail", "cold_email")["rows"]
        total = [r for r in rows if r["item"] == "gmail total"][0]
        self.assertLessEqual(total["limit"], 12)
        self.assertIn("gmail x0.5", total["clamp_reason"])


class TestConfigAuthority(CeilingCase):
    def test_defaults_equal_example_file(self):
        with open(os.path.join(REPO_ROOT, "config.example.json")) as fh:
            self.assertEqual(json.load(fh), hardmax.DEFAULTS)

    def test_defaults_within_hard_limits(self):
        eff, ctx = config.compute({}, {})
        self.assertEqual(ctx.clamped, [])
        self.assertEqual(ctx.warnings, [])

    def test_hostile_config_clamped(self):
        hostile = {"gmail": {"ceilings": {"conservative": {"cold_day": 500, "min_gap_minutes": 0, "hour": -1,
                                                            "company_cooldown_days": 1}}},
                   "linkedin": {"ceilings": {"conservative": {"invites": {"week": 1000}}}, "engagement_likes_comments": 5,
                                "warmup_weeks": [{"invites": 99}]},
                   "approval": {"mode": "inherit"}, "browser": {"profile": "default", "dwell_seconds": [0, 1]},
                   "boards": {"sites": {"indeed": {"apply": "browser"}}}}
        eff, ctx = config.compute(hostile, {"approval_mode": "auto", "tier_gmail": "moderate"})
        g = eff["gmail"]["ceilings"]["conservative"]
        self.assertEqual((g["cold_day"], g["min_gap_minutes"], g["company_cooldown_days"]), (15, 10, 90))
        self.assertEqual(g["hour"], -1)     # a nonsense limit only tightens: nothing is sent
        self.assertEqual(eff["linkedin"]["ceilings"]["conservative"]["invites"]["week"], 35)
        self.assertEqual(eff["linkedin"]["engagement_likes_comments"], 0)
        self.assertEqual(eff["linkedin"]["warmup_weeks"][0]["invites"], 3)
        self.assertEqual(eff["browser"]["profile"], "jobhunter")
        self.assertEqual(eff["browser"]["dwell_seconds"], [5, 40])
        self.assertEqual(eff["boards"]["sites"]["indeed"]["apply"], "never")
        self.assertEqual((eff["approval"]["mode"], eff["gmail"]["tier"]), ("auto", "moderate"))
        eff, _ = config.compute({"approval": {"mode": "human"}, "gmail": {"tier": "conservative"}},
                                {"approval_mode": "auto", "tier_gmail": "moderate"})
        self.assertEqual((eff["approval"]["mode"], eff["gmail"]["tier"]), ("human", "conservative"))

    def test_stop_rule_keys_only_stricter(self):
        # 4.2.1 class F: the rolling bounce window and the acceptance clamps cannot be switched off by a file edit
        for path in ("gmail.bounce_stop.rolling_window_sends", "linkedin.adaptive.acceptance_window_days",
                     "linkedin.adaptive.min_invites_aged_7d"):
            with self.subTest(path=path):
                self.assertNotEqual(hardmax.key_class(path), "free")
        eff, ctx = config.compute({"gmail": {"bounce_stop": {"rolling_window_sends": 19}},
                                   "linkedin": {"adaptive": {"acceptance_window_days": 7,
                                                             "min_invites_aged_7d": 100000}}}, {})
        self.assertEqual(eff["gmail"]["bounce_stop"]["rolling_window_sends"], 20)
        self.assertEqual(eff["linkedin"]["adaptive"]["acceptance_window_days"], 30)
        self.assertEqual(eff["linkedin"]["adaptive"]["min_invites_aged_7d"], 20)
        self.assertEqual(len(ctx.clamped), 3)
        eff, ctx = config.compute({"gmail": {"bounce_stop": {"rolling_window_sends": 50}},
                                   "linkedin": {"adaptive": {"min_invites_aged_7d": 10}}}, {})
        self.assertEqual((eff["gmail"]["bounce_stop"]["rolling_window_sends"],
                          eff["linkedin"]["adaptive"]["min_invites_aged_7d"]), (50, 10))
        self.assertEqual(ctx.clamped, [])

    def test_placeholder_signature_link_never_reaches_an_email(self):
        self.assertEqual(hardmax.DEFAULTS["owner"]["signature"]["links"], [])
        raw = hardmax.defaults()
        raw["owner"]["signature"]["links"] = ["https://www.linkedin.com/in/your-handle", "https://example.com/me"]
        eff, ctx = config.compute(raw, {})
        self.assertEqual(eff["owner"]["signature"]["links"], ["https://example.com/me"])
        self.assertTrue(any("placeholder" in w for w in ctx.warnings), ctx.warnings)

    def test_empty_meta_is_strictest_authority(self):
        eff, _ = config.compute({}, {})
        self.assertEqual((eff["approval"]["mode"], eff["gmail"]["tier"], eff["linkedin"]["tier"],
                          eff["channels"]["linkedin"]["enabled"]), ("human", "conservative", "conservative", False))

    def test_lower_and_raise(self):
        res = config.lower("gmail.ceilings.conservative.cold_day", "10", self.conn)
        self.assertEqual(res["new"], 10)
        self.assertEqual(config.load(self.conn)["gmail"]["ceilings"]["conservative"]["cold_day"], 10)
        self.assertDenied("E_VALIDATION", config.lower, "gmail.ceilings.conservative.cold_day", "12", self.conn)
        self.assertDenied("E_VALIDATION", config.lower, "gmail.ceilings.conservative.min_gap_minutes", "5", self.conn)
        self.assertDenied("E_VALIDATION", config.lower, "timezone", "UTC", self.conn)
        self.assertDenied("E_VALIDATION", config.lower, "approval.mode", "inherit", self.conn)
        config.lower("channels.email_outreach.enabled", "false", self.conn)
        with db.tx(self.conn):
            r = config.raise_(self.conn, "gmail.ceilings.conservative.cold_day", "100")
        self.assertEqual((r["new"], r["clamped"]), (30, True))
        self.assertEqual(config.load(self.conn)["gmail"]["ceilings"]["conservative"]["cold_day"], 30)
        with db.tx(self.conn):
            r = config.raise_(self.conn, "gmail.ceilings.conservative.min_gap_minutes", "1")
        self.assertEqual(r["new"], 3)
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", config.raise_, self.conn, "approval.mode", "auto")

    def test_apply_writes_trigger_meta_and_lowers_authority(self):
        write_config({"gmail.ceilings.conservative.company_cooldown_days": 120, "approval.mode": "human"})
        with db.tx(self.conn):
            db.meta_set(self.conn, "approval_mode", "auto", "human")
            res = config.apply(self.conn)
        self.assertEqual(res["meta"]["company_email_cooldown_days"], "120")
        self.assertEqual(db.meta_get(self.conn, "approval_mode"), "human")


if __name__ == "__main__":
    import unittest
    unittest.main()
