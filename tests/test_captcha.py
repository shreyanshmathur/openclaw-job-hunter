"""U1: the CAPTCHA hand-off (FEATURES-OTP-ACCOUNTS-CAPTCHA 3): opening a task with each token state, codes that
never collide with approval codes, the limits and the repeat breaker, the owner's continue with its read-only check
(fake CDP), the timeout, and captcha.handoff false keeping the old behaviour."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import approvals, canon, captcha, cdp, db, gate
from jobhunter.errors import Denied
from tests.fakes.u1.fake_browser_steps import applier_state, write_owner_config
from tests.fakes.u6.fake_cdp import FakeCdp
from tests.helpers import HomeTestCase, insert_action, insert_precheck


class CaptchaCase(HomeTestCase):
    start_ts = "2026-10-06T09:00:00Z"

    def setUp(self):
        super().setUp()
        write_owner_config()
        self.st = applier_state(self.conn)
        self.cdp = FakeCdp().start()
        self.addCleanup(self.cdp.stop)
        cdp.set_test_port(self.cdp.port)
        self.addCleanup(cdp.set_test_port, None)
        self.tab = self.cdp.add_tab("wd_captcha")

    def token(self, status="reserved"):
        pc = insert_precheck(self.conn, kind="application", platform="workday", job_id=self.st["job_id"])
        aid = insert_action(self.conn, kind="application", status=status, job_id=self.st["job_id"],
                            agent_id="jobhunter-applier", platform="workday", route="browser",
                            draft_id=self.st["draft_id"], precheck_id=pc)
        self.conn.execute("UPDATE jobs SET status = 'applying' WHERE id = ?", (self.st["job_id"],))
        return self.conn.execute("SELECT token FROM actions WHERE id = ?", (aid,)).fetchone()[0]

    def open(self, token=None, job_id=None):
        with db.tx(self.conn):
            return captcha.open_task(self.conn, job_id=job_id or self.st["job_id"], tab_id=self.tab, token=token,
                                     opened_by="guard", url=self.cdp.tabs[self.tab].url, title="Kestrel Careers")

    def job(self):
        return tuple(self.conn.execute("SELECT status, status_reason, claimed_by FROM jobs WHERE id = ?",
                                       (self.st["job_id"],)).fetchone())


class TestOpen(CaptchaCase):
    def test_reserved_token_is_released(self):
        tok = self.token()
        t = self.open(tok)
        self.assertEqual(t["token_outcome"], "released")
        self.assertEqual(tuple(self.conn.execute("SELECT status, fail_reason FROM actions WHERE token = ?",
                                                 (tok,)).fetchone()), ("failed", "form_blocked_before_submit"))
        self.assertEqual(self.job(), ("needs_human", "captcha_wait", None))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM human_tasks WHERE done_at IS NULL")]
        self.assertEqual(kinds, ["captcha"])
        note = self.conn.execute("SELECT priority, kind, text FROM notifications WHERE dedupe_key = ?",
                                 ("captcha:" + t["code"],)).fetchone()
        self.assertEqual(note[:2], ("high", "alert"))
        self.assertIn("/jh continue %s" % t["code"], note[2])
        self.assertIn("./jobhunter continue %s" % t["code"], note[2])
        # the same job again: the open task is returned
        self.assertEqual(self.open(tok)["code"], t["code"])

    def test_armed_without_a_commit_line(self):
        tok = self.token("armed")
        self.assertEqual(self.open(tok)["token_outcome"], "released")

    def test_armed_with_a_commit_line(self):
        tok2 = self.token("armed")
        gate.append_code_line(tok2, cls="commit", action="code_resubmit", host="kestrel.wd5.myworkdayjobs.com",
                              name="Submit")
        t = self.open(tok2)
        self.assertEqual(t["token_outcome"], "unknown")
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE token = ?", (tok2,)).fetchone()[0],
                         "unknown")

    def test_no_token(self):
        self.assertEqual(self.open()["token_outcome"], "none")

    def test_screenshot_after_commit(self):
        t = self.open()
        out = captcha.finish_open(self.conn, t)
        self.assertTrue(out["screenshot"])
        row = self.conn.execute("SELECT screenshot_path FROM captcha_tasks WHERE id = ?", (t["id"],)).fetchone()
        media = self.conn.execute("SELECT media_path FROM notifications WHERE dedupe_key = ?",
                                  ("captcha:" + t["code"],)).fetchone()[0]
        self.assertEqual(row[0], media)
        self.assertIn("Page.captureScreenshot", self.cdp.methods())

    def test_codes_never_collide(self):
        t = self.open()
        self.assertRegex(t["code"], "^[%s]{4}$" % approvals.CODE_ALPHABET)
        with db.tx(self.conn):
            seen = {t["code"]}
            for _ in range(50):
                seen.add(captcha._new_code(self.conn))
        self.assertGreater(len(seen), 40)
        # an approval code is never handed out while a CAPTCHA code is open
        import secrets
        real = secrets.choice
        seq = iter(list(t["code"]) + list("ACDE"))
        from tests.helpers import insert_draft
        d = insert_draft(self.conn, status="awaiting_approval")
        try:
            approvals.secrets.choice = lambda alphabet: next(seq)
            with db.tx(self.conn):
                code = approvals.issue_code(self.conn, d)
        finally:
            approvals.secrets.choice = real
        self.assertEqual(code, "ACDE")


class TestLimits(CaptchaCase):
    def test_handoff_off_keeps_the_old_way(self):
        write_owner_config(**{"captcha.handoff": False})
        self.assertIsNone(self.open())
        self.assertEqual(self.job()[:2], ("needs_human", "captcha_visible"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM human_tasks")]
        self.assertEqual(kinds, ["apply_manually"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM captcha_tasks").fetchone()[0], 0)

    def test_max_open(self):
        write_owner_config(**{"captcha.max_open": 1})
        self.open()
        other = applier_state(self.conn, url="https://kestrel2.wd5.myworkdayjobs.com/en-US/External/job/x_JR-2")
        with db.tx(self.conn):
            self.assertIsNone(captcha.open_task(self.conn, job_id=other["job_id"], tab_id=None, token=None,
                                                opened_by="agent"))

    def test_repeat_breaker_and_timeouts_count(self):
        first = self.open()
        self.clock.advance(hours=2, seconds=1)
        with db.tx(self.conn):
            items = captcha.expire(self.conn)
        self.assertEqual([i["code"] for i in items], [first["code"]])
        self.assertEqual(self.job()[:2], ("closed", "captcha_timeout"))
        with db.tx(self.conn):
            self.conn.execute("UPDATE jobs SET status = 'eligible' WHERE id = ?", (self.st["job_id"],))
        self.assertIsNotNone(self.open())                  # the second in 24 hours
        with db.tx(self.conn):
            self.conn.execute("UPDATE captcha_tasks SET status = 'resolved' WHERE status = 'open'")
        self.assertIsNone(self.open())                     # the third trips ats:workday
        row = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'ats:workday'").fetchone()
        self.assertEqual(tuple(row), ("open", "captcha_repeat"))


class TestContinue(CaptchaCase):
    def test_unknown_code(self):
        with self.assertRaises(Denied) as cm:
            captcha.continue_task(self.conn, "QQQQ", "human:cli")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")

    def test_check_fails_then_passes(self):
        t = self.open(self.token())
        with self.assertRaises(Denied) as cm:
            captcha.continue_task(self.conn, t["code"], "human:cli")
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        self.assertEqual(cm.exception.data["check"], {"tab_found": True, "host_ok": True, "captcha_gone": False,
                                                       "stop": None})
        self.assertEqual(self.conn.execute("SELECT status FROM captcha_tasks").fetchone()[0], "open")
        self.cdp.set_page(self.tab, "wd_create")
        res = captcha.continue_task(self.conn, t["code"], "human:cli")
        self.assertTrue(res["resumed"])
        self.assertEqual(self.job()[:2], ("eligible", "captcha_resolved"))
        self.assertEqual(self.conn.execute("SELECT resolution FROM human_tasks WHERE kind = 'captcha'").fetchone()[0],
                         "captcha_solved")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM meta WHERE key = 'dispatch_nudge:applier'").fetchone())
        # the check only read the page
        self.assertFalse([e for e in self.cdp.log if e["method"].startswith("Input.")])

    def test_tripping_page_cancels_the_task(self):
        t = self.open()
        self.cdp.set_page(self.tab, "wd_sms")
        with self.assertRaises(Denied) as cm:
            captcha.continue_task(self.conn, t["code"], "human:cli")
        self.assertEqual(cm.exception.code, "E_STOP_DETECTED")
        self.assertEqual(self.conn.execute("SELECT status FROM captcha_tasks").fetchone()[0], "cancelled")
        self.assertEqual(self.conn.execute("SELECT state FROM breakers WHERE scope = 'ats:workday'").fetchone()[0],
                         "open")

    def test_timeout_closes_the_tab(self):
        t = self.open()
        self.clock.advance(minutes=121)
        res = captcha.expire_all(self.conn)
        self.assertEqual(res, {"timed_out": [t["code"]], "tabs_closed": [t["code"]]})
        self.assertNotIn(self.tab, self.cdp.tabs)
        note = [r[0] for r in self.conn.execute("SELECT text FROM notifications WHERE dedupe_key LIKE "
                                                "'captcha_timeout:%'")]
        self.assertEqual(note, ["The CAPTCHA for Kestrel Commerce was not solved in 2 hours; the job is skipped."])


if __name__ == "__main__":
    unittest.main()
