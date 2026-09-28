"""Breakers, pause, detection signatures and identity checks (design 4.5, 12.11, 12.12)."""
from __future__ import annotations

import glob
import json
import os
import re

import tests  # noqa: F401
from jobhunter import breakers, canon, ceilings, db, detect, identity, paths
from jobhunter.errors import Denied
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase

DETECT_DIR = os.path.dirname(detect.__file__)


class TestSignatureFiles(HomeTestCase):
    def test_files_parse_and_regexes_are_portable(self):
        files = sorted(glob.glob(os.path.join(DETECT_DIR, "*.json")))
        self.assertEqual(sorted(os.path.basename(f) for f in files), sorted(detect.FILES))
        for f in files:
            with open(f, encoding="ascii") as fh:
                data = json.load(fh)
            for sig in data["signatures"] + data["error_after_click"]:
                with self.subTest(file=f, sig=sig["id"]):
                    matchers = [k for k in ("url", "title", "text", "smtp", "http_status") if k in sig]
                    self.assertEqual(len(matchers), 1)
                    for k in ("url", "title", "text", "smtp"):
                        if k in sig:
                            rx = sig[k]
                            re.compile(rx)
                            self.assertNotIn("(?", rx)         # no inline flags, lookaround or named groups
                            self.assertNotRegex(rx, r"\\[AZzG]")
                    if sig.get("trip"):
                        self.assertIn(sig["reason_code"], breakers.POLICIES)

    def test_file_for_platform(self):
        self.assertEqual(detect.file_for("linkedin"), "linkedin.json")
        self.assertEqual(detect.file_for("gmail"), "gmail.json")
        self.assertEqual(detect.file_for("greenhouse"), "ats.json")
        self.assertEqual(detect.file_for("site:naukri"), "boards.json")


class DetectCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()

    def run_detect(self, payload, source="agent"):
        with db.tx(self.conn):
            return detect.detect(self.conn, payload, source, None)

    def breaker(self, scope):
        return self.conn.execute("SELECT * FROM breakers WHERE scope = ?", (scope,)).fetchone()


class TestDetect(DetectCase):
    def test_linkedin_checkpoint_trips_with_cooldown(self):
        res = self.run_detect({"platform": "linkedin", "url": "https://www.linkedin.com/checkpoint/challenge/x",
                               "title": "Security Verification", "http_status": None, "text": "Let's do a quick check"})
        self.assertEqual((res["verdict"], res["tripped"], res["scope"]), ("stop", True, "linkedin"))
        b = self.breaker("linkedin")
        self.assertEqual((b["state"], b["reason_code"], b["resume_policy"]), ("open", "li_challenge", "warmup_week1"))
        self.assertEqual(b["min_cooldown_until"], canon.ts_add(canon.now(), hours=72))
        self.assertTrue(os.path.exists(b["evidence_path"]))
        self.assertTrue(self.conn.execute("SELECT 1 FROM notifications WHERE priority = 'high'").fetchone())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM breaker_events").fetchone()[0], 1)
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["linkedin.invites"])
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["linkedin"])

    def test_invite_limit_scope_and_clear_page(self):
        res = self.run_detect({"platform": "linkedin", "text": "You've reached the weekly invitation limit"})
        self.assertEqual(res["scope"], "linkedin.invites")
        breakers.check_breakers(self.conn, ["linkedin.messages"])
        res = self.run_detect({"platform": "linkedin", "url": "https://www.linkedin.com/in/example-person",
                               "text": "Example Person. Head of Analytics."})
        self.assertEqual((res["verdict"], res["tripped"]), ("clear", False))
        self.assertIsNotNone(detect.recent_clear(self.conn, "linkedin"))

    def test_http_status_and_sites(self):
        res = self.run_detect({"platform": "linkedin", "http_status": 999})
        self.assertEqual(res["code"], "li_http_429")
        res = self.run_detect({"platform": "site:naukri", "text": "Please verify you are human (captcha)"})
        self.assertEqual((res["scope"], self.breaker("site:naukri")["min_cooldown_until"]),
                         ("site:naukri", canon.ts_add(canon.now(), hours=24)))

    def test_ats_captcha_needs_human_without_breaker(self):
        res = self.run_detect({"platform": "greenhouse", "text": "Please complete the CAPTCHA to submit"})
        self.assertEqual((res["verdict"], res["tripped"], res["job_needs_human"]), ("stop", False, "captcha_visible"))
        self.assertIsNone(self.breaker("ats"))

    def test_payload_validation(self):
        with db.tx(self.conn):
            self.assertDenied("E_SCHEMA", detect.detect, self.conn, {"platform": "linkedin", "html": "x"}, "agent")
            self.assertDenied("E_SCHEMA", detect.detect, self.conn, {"platform": "LinkedIn!"}, "agent")
            self.assertDenied("E_VALIDATION", detect.detect, self.conn, {"platform": "linkedin"}, "model")
        long = self.run_detect({"platform": "linkedin", "text": "x" * 30000})
        self.assertEqual(long["verdict"], "clear")

    def test_error_after_click_and_smtp(self):
        self.assertEqual(detect.error_after_click("linkedin", "Something went wrong. Try again later."),
                         "li_error_generic")
        self.assertIsNone(detect.error_after_click("linkedin", "Invitation sent"))
        self.assertEqual(detect.smtp_signature("550 5.4.5 Daily user sending limit exceeded")["reason_code"],
                         "gmail_sending_limit")
        self.assertEqual(detect.smtp_signature("535 5.7.8 Username and Password not accepted")["reason_code"],
                         "gmail_auth_failed")
        self.assertIsNone(detect.smtp_signature("250 2.0.0 OK"))


class TestBreakers(DetectCase):
    def test_reset_refused_before_cooldown_then_applies_policy(self):
        with db.tx(self.conn):
            breakers.trip(self.conn, "linkedin", "li_challenge", "checkpoint")
        with db.tx(self.conn):
            d = self.assertDenied("E_PRECONDITION", breakers.reset, self.conn, "linkedin", "ok", "human")
        self.assertGreater(d.retry_after, 70 * 3600)
        self.clock.advance(hours=73)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES "
                              "('linkedin', 'all', ?, 1)", (self.clock.ago(days=40),))
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 6)
        with db.tx(self.conn):
            res = breakers.reset(self.conn, "linkedin", "logged in again", "human")
        self.assertEqual(res["state"], "closed")
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 1)
        breakers.check_breakers(self.conn, ["linkedin"])

    def test_clamp_policy(self):
        with db.tx(self.conn):
            breakers.trip(self.conn, "linkedin.invites", "li_invite_limit", "weekly limit")
        self.clock.advance(days=8)
        with db.tx(self.conn):
            breakers.reset(self.conn, "linkedin.invites", "ok", "human")
        f, why = ceilings.clamp(self.conn, "linkedin.invites")
        self.assertEqual(f, 0.5)
        self.clock.advance(days=15)
        self.assertEqual(ceilings.clamp(self.conn, "linkedin.invites")[0], 1.0)

    def test_api_breaker_auto_close(self):
        with db.tx(self.conn):
            breakers.trip(self.conn, "api:greenhouse", "http_429", "rate limited")
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["api:greenhouse"])
        self.clock.advance(hours=2)
        breakers.check_breakers(self.conn, ["api:greenhouse"])
        with db.tx(self.conn):
            self.assertEqual(breakers.close_expired(self.conn), ["api:greenhouse"])

    def test_pause_all_and_area(self):
        with db.tx(self.conn):
            breakers.pause(self.conn, "all", "holiday", "chat")
        self.assertTrue(os.path.exists(paths.paused_file()))
        self.assertDenied("E_PAUSED", breakers.check_breakers, self.conn, ["gmail"])
        with db.tx(self.conn):
            breakers.unpause(self.conn, "all")
            breakers.pause(self.conn, "linkedin", None, "human")
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["linkedin.messages"])
        breakers.check_breakers(self.conn, ["gmail"])
        with db.tx(self.conn):
            breakers.unpause(self.conn, "linkedin")
            self.assertDenied("E_VALIDATION", breakers.pause, self.conn, "everything", None, "human")
        breakers.check_breakers(self.conn, ["linkedin"])

    def test_global_blocks_everything_and_scope_validation(self):
        with db.tx(self.conn):
            breakers.trip(self.conn, "global", "audit_mismatch", "x")
            self.assertDenied("E_VALIDATION", breakers.trip, self.conn, "everything", "x", "y")
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["site:naukri"])
        self.assertEqual(breakers.scopes_for("greenhouse", "application")[:2], ["global", "ats"])
        self.assertIn("gmail.cold", breakers.scopes_for("gmail", "cold_email"))


class TestEnrichScopes(DetectCase):
    """The email finder's scopes (U10): enrich and enrich:<provider>, pause enrich, auto-close trips."""

    def test_scopes_pause_and_parents(self):
        for scope in ("enrich", "enrich:hunter", "enrich:zerobounce"):
            self.assertTrue(breakers.valid_scope(scope), scope)
        for scope in ("enrich:", "enrich:X", "enrichment", "enrich:a"):
            self.assertFalse(breakers.valid_scope(scope), scope)
        self.assertIn("enrich", breakers.PAUSE_AREAS)
        with db.tx(self.conn):
            breakers.trip(self.conn, "enrich", "bounce_strikes", "three bounces in 30 days")
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["enrich:hunter"])
        with db.tx(self.conn):
            breakers.reset(self.conn, "enrich", "checked", "human")
            breakers.pause(self.conn, "enrich", "saving credits", "human")
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["enrich:tomba"])
        breakers.check_breakers(self.conn, ["gmail"])
        with db.tx(self.conn):
            breakers.unpause(self.conn, "enrich")
        breakers.check_breakers(self.conn, ["enrich:tomba"])
        statuses = {r["scope"]: r for r in breakers.status(self.conn)}
        self.assertEqual(statuses["enrich"]["state"], "closed")

    def test_auto_close_trip_and_no_downgrade(self):
        until = canon.ts_add(canon.now(), hours=6)
        with db.tx(self.conn):
            res = breakers.trip(self.conn, "enrich:hunter", "quota_exhausted", "monthly credits used",
                                requires_human=False, auto_close_at=until)
        self.assertEqual((res["requires_human"], res["auto_close_at"]), (False, until))
        n = self.conn.execute("SELECT count(*) FROM notifications").fetchone()[0]
        self.assertEqual(n, 0)                         # a stop that ends by itself sends no alert
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["enrich:hunter"])
        self.clock.advance(hours=7)
        breakers.check_breakers(self.conn, ["enrich:hunter"])
        with db.tx(self.conn):
            self.assertEqual(breakers.close_expired(self.conn), ["enrich:hunter"])
        # an auto-close trip never turns a stop that waits for the owner into one that ends by itself
        with db.tx(self.conn):
            breakers.trip(self.conn, "enrich:tomba", "auth_failed", "key refused")
            res = breakers.trip(self.conn, "enrich:tomba", "quota_exhausted", "x", requires_human=False,
                                auto_close_at=canon.ts_add(canon.now(), hours=1))
        self.assertTrue(res["requires_human"])
        self.clock.advance(hours=2)
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn, ["enrich:tomba"])


class TestConsentBreakers(DetectCase):
    def test_consent_revoked_policy(self):
        pol = breakers.POLICIES["consent_revoked"]
        self.assertEqual((pol["scope"], pol["cooldown"]), (None, 0))
        with db.tx(self.conn):
            breakers.trip(self.conn, "site:naukri", "consent_revoked", "consent for Naukri was revoked")
            res = breakers.reset(self.conn, "site:naukri", "allowed again", "human")    # no waiting time
        self.assertEqual(res["state"], "closed")
        self.assertEqual(identity.breaker_scope("gmail"), "gmail")
        self.assertEqual(identity.breaker_scope("yc"), "site:yc")
        self.assertTrue(breakers.valid_scope(identity.breaker_scope("yc")))


class TestSecondTripsAndClamps(DetectCase):
    """A milder second trip never replaces a stricter resume policy; reset applies every trip's policy; a new
    clamp never weakens an active one (4.5)."""

    def trip(self, scope, code, detail="x"):
        with db.tx(self.conn):
            return breakers.trip(self.conn, scope, code, detail)

    def reset(self, scope):
        with db.tx(self.conn):
            return breakers.reset(self.conn, scope, "ok", "human")

    def test_restricted_then_security_email_keeps_manual_week(self):
        self.trip("linkedin", "li_restricted")
        self.trip("linkedin", "li_security_email")
        b = self.breaker("linkedin")
        self.assertEqual((b["reason_code"], b["resume_policy"]), ("li_restricted", "manual_7d_then_warmup_week1"))
        self.clock.advance(days=7, minutes=1)
        res = self.reset("linkedin")
        self.assertIn("clamp:linkedin=0.0 for 7 days", res["applied"])
        self.assertEqual(ceilings.clamp(self.conn, "linkedin")[0], 0.0)
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 1)
        self.clock.advance(days=7, minutes=1)            # the manual-only week is over: warm-up week 1 starts now
        self.assertEqual(ceilings.clamp(self.conn, "linkedin")[0], 1.0)
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 1)
        self.clock.advance(days=7)
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 2)

    def test_challenge_then_429_applies_both_policies(self):
        self.trip("linkedin", "li_challenge")
        self.trip("linkedin", "li_http_429")
        self.assertEqual(self.breaker("linkedin")["resume_policy"], "warmup_week1")
        self.clock.advance(hours=73)
        res = self.reset("linkedin")
        self.assertEqual(res["reasons"], ["li_challenge", "li_http_429"])
        self.assertIn("warmup_week1", res["applied"])
        self.assertEqual(ceilings.clamp(self.conn, "linkedin")[0], 0.5)

    def test_later_reset_does_not_weaken_an_active_clamp(self):
        self.trip("linkedin", "li_restricted")
        self.clock.advance(days=7, minutes=1)
        self.reset("linkedin")
        self.trip("linkedin", "li_logged_out")
        self.reset("linkedin")
        self.assertEqual(ceilings.clamp(self.conn, "linkedin")[0], 0.0)
        self.clock.advance(days=2)
        self.assertEqual(ceilings.clamp(self.conn, "linkedin")[0], 0.0)

    def test_shorter_stricter_clamp_then_longer_weaker_one(self):
        with db.tx(self.conn):
            breakers.set_clamp(self.conn, "linkedin.invites", 0.5, canon.ts_add(canon.now(), days=14), "half")
            v = breakers.set_clamp(self.conn, "linkedin.invites", 0.0, canon.ts_add(canon.now(), days=1), "stop")
        self.assertEqual((v["factor"], len(v["then"])), (0.0, 1))
        self.assertEqual(ceilings.clamp(self.conn, "linkedin.invites")[0], 0.0)
        self.clock.advance(days=2)
        self.assertEqual(ceilings.clamp(self.conn, "linkedin.invites")[0], 0.5)
        self.clock.advance(days=13)
        self.assertEqual(ceilings.clamp(self.conn, "linkedin.invites")[0], 1.0)


class TestSeverityAndWords(DetectCase):
    def test_most_severe_signature_wins(self):
        _v, sig = detect.match({"platform": "linkedin", "url": "https://www.linkedin.com/checkpoint/challenge/x",
                                "text": "Your account has been temporarily restricted after unusual activity"})
        self.assertEqual((sig["id"], sig["reason_code"]), ("li_restricted_text", "li_restricted"))
        _v, sig = detect.match({"platform": "linkedin", "text": "temporarily restricted. Complete the security "
                                "verification"})
        self.assertEqual(sig["reason_code"], "li_restricted")
        _v, sig = detect.match({"platform": "gmail", "text": "You have reached a limit for sending mail. Verify "
                                "it's you"})
        self.assertEqual(sig["reason_code"], "gmail_security")
        _v, sig = detect.match({"platform": "linkedin", "url": "https://www.linkedin.com/checkpoint/challenge/x"})
        self.assertEqual(sig["reason_code"], "li_challenge")

    def test_alert_and_detail_have_no_codes_or_urls(self):
        res = self.run_detect({"platform": "site:naukri", "url": "https://www.naukri.com/data-analyst-jobs",
                               "title": "x", "text": "Please complete the captcha"})
        self.assertTrue(res["tripped"])
        b = self.breaker("site:naukri")
        self.assertEqual(b["detail"], "seen on naukri.com")
        text = self.conn.execute("SELECT text FROM notifications WHERE dedupe_key LIKE 'breaker:site:naukri:%'"
                                 ).fetchone()[0]
        self.assertTrue(text.startswith("Stopped Site: Naukri: The job site showed a security check"), text)
        self.assertNotIn("site_", text)
        self.assertNotIn("https://", text)
        with open(b["evidence_path"]) as fh:
            self.assertIn("https://www.naukri.com/data-analyst-jobs", fh.read())

    def alert(self, scope):
        return self.conn.execute("SELECT text FROM notifications WHERE dedupe_key LIKE ? ORDER BY id DESC",
                                 ("breaker:%s:%%" % scope,)).fetchone()[0]

    def test_an_expired_session_is_not_a_security_prompt(self):
        """A Google sign-in page (the session ended) is gmail_logged_out; a verification challenge stays
        gmail_security and wins when both match. A board's login wall is site_logged_out, not site_challenge."""
        cases = [("https://accounts.google.com/v3/signin/identifier?continue=x", "gmail_logged_out"),
                 ("https://accounts.google.com/ServiceLogin?service=mail", "gmail_logged_out"),
                 ("https://accounts.google.com/AccountChooser?continue=x", "gmail_logged_out"),
                 ("https://accounts.google.com/v3/signin/challenge/pwd?TL=x", "gmail_logged_out"),
                 ("https://accounts.google.com/v3/signin/challenge/totp?TL=x", "gmail_security"),
                 ("https://accounts.google.com/signin/v2/challenge/ipp", "gmail_security")]
        for url, code in cases:
            with self.subTest(url=url):
                self.assertEqual(detect.match({"platform": "gmail", "url": url})[1]["reason_code"], code)
        _v, sig = detect.match({"platform": "gmail", "url": cases[0][0], "text": "Verify it's you"})
        self.assertEqual(sig["reason_code"], "gmail_security")
        for payload, code in (({"url": "https://www.naukri.com/nlogin/login"}, "site_logged_out"),
                              ({"url": "https://www.naukri.com/jobs", "text": "Please login to continue"},
                               "site_logged_out"),
                              ({"url": "https://www.naukri.com/jobs", "text": "Your session has expired"},
                               "site_logged_out"),
                              ({"url": "https://www.naukri.com/nlogin/login", "text": "captcha"}, "site_challenge")):
            with self.subTest(payload=payload):
                payload["platform"] = "site:naukri"
                self.assertEqual(detect.match(payload)[1]["reason_code"], code)

    def test_logged_out_stops_name_the_fix_and_need_no_wait(self):
        """Change request item 4: the chat alert carries the one thing to do (the Sheet's 'What you need to do'
        words), and a logged-out session can be reset as soon as the owner logged in again."""
        from jobhunter import sheets_labels
        res = self.run_detect({"platform": "gmail", "url": "https://accounts.google.com/v3/signin/identifier?x",
                               "title": "Sign in", "text": "Sign in to continue to Gmail"})
        self.assertTrue(res["tripped"])
        b = self.breaker("gmail")
        self.assertEqual((b["reason_code"], b["resume_policy"]), ("gmail_logged_out", None))
        self.assertLessEqual(b["min_cooldown_until"], b["tripped_at"])
        text = self.alert("gmail")
        self.assertIn(sheets_labels.todo_sentence("gmail", "gmail_logged_out", True, waiting=False), text)
        self.assertIn("./jobhunter breaker reset gmail", text)
        self.assertNotIn("verify it is you", text)
        self.assertNotIn("not before", text)
        with db.tx(self.conn):
            self.assertEqual(breakers.reset(self.conn, "gmail", "logged in again", "human")["state"], "closed")
        res = self.run_detect({"platform": "site:naukri", "url": "https://www.naukri.com/nlogin/login", "title": "x",
                               "text": "Login"})
        b = self.breaker("site:naukri")
        self.assertEqual(b["reason_code"], "site_logged_out")
        self.assertLessEqual(b["min_cooldown_until"], b["tripped_at"])
        self.assertIn("./jobhunter breaker reset site:naukri", self.alert("site:naukri"))
        self.run_detect({"platform": "linkedin", "url": "https://www.linkedin.com/login", "title": "x", "text": "x"})
        self.assertIn("./jobhunter browser login linkedin", self.alert("linkedin"))
        # a stop with a waiting time names it and the reset after it
        self.run_detect({"platform": "site:indeed", "url": "https://www.indeed.com/jobs", "title": "x",
                         "text": "Please complete the captcha"})
        text = self.alert("site:indeed")
        self.assertIn("(not before ", text)
        self.assertIn("./jobhunter breaker reset site:indeed after the waiting time", text)

    def test_area_labels_match_the_sheet(self):
        from jobhunter import sheets_labels
        for scope in ("api:recruitee", "pause:linkedin", "site:linkedin_jobs", "linkedin.invites", "ats", "global"):
            with self.subTest(scope=scope):
                self.assertEqual(breakers.area_label(scope), sheets_labels.scope_label(scope))
        self.assertEqual(breakers.area_label("pause:linkedin"), "Paused by you: LinkedIn")

    def test_pause_site_needs_a_checked_site_name(self):
        for bad in ("site:naukri.com", "site:nonexistent", "site:linkedin_jobs", "site:ats_forms"):
            with self.subTest(scope=bad), db.tx(self.conn):
                self.assertDenied("E_VALIDATION", breakers.pause, self.conn, bad, None, "human")
        with db.tx(self.conn):
            breakers.pause(self.conn, "site:naukri", None, "human")
        self.assertDenied("E_BREAKER_OPEN", breakers.check_breakers, self.conn,
                          breakers.scopes_for("naukri", "application"))


class TestWarmupPromotion(DetectCase):
    def start(self, platform, days_ago, week=1):
        lane = "cold" if platform == "gmail" else "all"
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES (?, ?, ?, ?)",
                              (platform, lane, self.clock.ago(days=days_ago), week))

    def test_linkedin_week_with_a_stop_is_not_promoted(self):
        self.start("linkedin", 5)
        with db.tx(self.conn):
            breakers.trip(self.conn, "linkedin", "li_http_429", "429")
        self.clock.advance(days=2, hours=1)
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 1)
        self.clock.advance(days=7)
        self.assertEqual(ceilings.warmup_week(self.conn, "linkedin"), 2)

    def test_gmail_leaves_week_3_only_when_clean(self):
        self.start("gmail", 14, week=1)
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 3)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO exclusions (type, value_raw, value_key, source, created_at, updated_at) "
                              "VALUES ('email', 'x@example.org', 'email_norm:x@example.org', 'complaint', ?, ?)",
                              (canon.now(), canon.now()))
        self.clock.advance(days=7)
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 3)
        self.clock.advance(days=7)
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 4)

    def test_long_gmail_pause_restarts_at_week_2(self):
        self.start("gmail", 15, week=1)
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 3)
        with db.tx(self.conn):
            breakers.pause(self.conn, "gmail", "holiday", "human")
        self.clock.advance(days=30)
        with db.tx(self.conn):
            res = breakers.unpause(self.conn, "gmail")
        self.assertEqual(res["applied"], ["warmup_week_2"])
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 2)
        with db.tx(self.conn):
            breakers.pause(self.conn, "all", "short", "human")
        self.clock.advance(days=2)
        with db.tx(self.conn):
            self.assertNotIn("applied", breakers.unpause(self.conn, "all"))


class TestEmailHealth(DetectCase):
    def thread(self, reply_class, days_ago=0):
        from tests.helpers import insert_action
        aid = insert_action(self.conn, kind="cold_email", platform="gmail", contact_id=None,
                            reserved_at=self.clock.ago(days=days_ago + 1))
        self.conn.execute("INSERT INTO threads (thread_key, channel, first_action_id, state, reply_class, reply_at, "
                          "created_at, updated_at) VALUES (?, 'email', ?, 'bounced', ?, ?, ?, ?)",
                          ("em:T%011d" % aid, aid, reply_class, self.clock.ago(days=days_ago), canon.now(), canon.now()))

    def test_two_bounces_in_a_day_trip_cold_email(self):
        self.thread("bounce")
        with db.tx(self.conn):
            self.assertEqual(breakers.email_health(self.conn)["tripped"], [])
        self.thread("bounce")
        with db.tx(self.conn):
            self.assertEqual(breakers.email_health(self.conn)["tripped"], ["gmail_bounces_24h"])
        self.assertEqual(self.breaker("gmail.cold")["state"], "open")

    def test_one_complaint_cuts_the_cold_cap(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO exclusions (type, value_raw, value_key, source, created_at, updated_at) "
                              "VALUES ('email', 'x@example.org', 'email_norm:x@example.org', 'complaint', ?, ?)",
                              (canon.now(), canon.now()))
            res = breakers.email_health(self.conn)
        self.assertEqual(res["clamped"], ["gmail.cold"])
        self.assertEqual(ceilings.clamp(self.conn, "gmail.cold")[0], 0.5)


class TestIdentity(DetectCase):
    def test_linkedin_identity(self):
        with db.tx(self.conn):
            res = identity.identity_check(self.conn, "linkedin", {"display_name": "Example Owner",
                                                                  "profile_url": "https://www.linkedin.com/in/Example-Owner/"})
        self.assertTrue(res["ok"])
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                identity.identity_check(self.conn, "linkedin", {"display_name": "Someone Else",
                                                                "profile_url": "https://www.linkedin.com/in/example-someone-else"})
        self.assertEqual(cm.exception.code, "E_IDENTITY_MISMATCH")
        self.assertEqual(self.breaker("linkedin")["state"], "open")     # the trip survived the refusal

    def test_gmail_identity_and_missing_owner(self):
        with db.tx(self.conn):
            identity.identity_check(self.conn, "gmail", {"account_email": "Owner@Example.com"})
        write_config({"owner.gmail_address": "you@example.com"})
        with db.tx(self.conn):
            self.assertDenied("E_CONFIG_INVALID", identity.identity_check, self.conn, "gmail",
                              {"account_email": "owner@example.com"})


if __name__ == "__main__":
    import unittest
    unittest.main()
