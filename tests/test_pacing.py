"""Pacing (design 4.3): gaps since the last write per platform, pace wait never sleeps more than 50 s,
dwell targets stored for the guard, deterministic jitter."""
from __future__ import annotations

import tests  # noqa: F401
from jobhunter import canon, config, db, locks, pacing
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase, insert_action


class PacingCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)


class TestGaps(PacingCase):
    def test_gmail_gap_with_jitter(self):
        pacing.pace_check(self.conn, "gmail", "cold_email")
        insert_action(self.conn, kind="cold_email", platform="gmail", contact_id=None)
        d = self.assertDenied("E_PACING", pacing.pace_check, self.conn, "gmail", "followup_email")
        self.assertTrue(600 <= d.data["gap_s"] <= 1200, d.data)
        again = self.assertDenied("E_PACING", pacing.pace_check, self.conn, "gmail", "cold_email")
        self.assertEqual(d.data["gap_s"], again.data["gap_s"])      # same draw for the same previous send
        self.clock.advance(seconds=d.data["gap_s"])
        pacing.pace_check(self.conn, "gmail", "cold_email")

    def test_linkedin_floor_and_easy_apply(self):
        insert_action(self.conn, kind="li_withdraw", platform="linkedin", contact_id=None)
        d = self.assertDenied("E_PACING", pacing.pace_check, self.conn, "linkedin", "li_invite")
        self.assertEqual(d.data["gap_s"], 45)
        self.clock.advance(seconds=46)
        pacing.pace_check(self.conn, "linkedin", "li_invite")
        insert_action(self.conn, kind="application", platform="linkedin", contact_id=None)
        self.clock.advance(seconds=100)
        d = self.assertDenied("E_PACING", pacing.pace_check, self.conn, "linkedin", "application")
        self.assertEqual(d.data["gap_s"], 180)

    def test_board_site_gap(self):
        insert_action(self.conn, kind="application", platform="greenhouse", contact_id=None)
        d = self.assertDenied("E_PACING", pacing.pace_check, self.conn, "lever", "application")
        self.assertTrue(240 <= d.data["gap_s"] <= 540)
        pacing.pace_check(self.conn, "naukri", "application")     # another site

    def test_imports_do_not_pace(self):
        insert_action(self.conn, kind="cold_email", platform="gmail", route="import", status="imported",
                      contact_id=None)
        pacing.pace_check(self.conn, "gmail", "cold_email")


class TestPaceWait(PacingCase):
    def test_never_sleeps_over_50_seconds(self):
        insert_action(self.conn, kind="li_withdraw", platform="linkedin", contact_id=None)
        total = 0
        for _ in range(20):
            r = pacing.pace_wait(self.conn, "linkedin", "write", max_s=500, agent_id="jobhunter-outreach",
                                 sleep=self.sleep)
            self.assertLessEqual(r["slept_s"], 50)
            self.clock.advance(seconds=r["slept_s"])
            total += r["slept_s"]
            if r["remaining_s"] == 0:
                break
        self.assertTrue(all(s <= 50 for s in self.slept))
        self.assertTrue(90 <= total <= 301, total)
        r = pacing.pace_wait(self.conn, "linkedin", "write", agent_id="jobhunter-outreach", sleep=self.sleep)
        self.assertEqual(r["remaining_s"], 0)          # the stored target is reused until the next write

    def test_dwell_needs_armed_token_and_is_stored(self):
        self.assertDenied("E_PRECONDITION", pacing.pace_wait, self.conn, "linkedin", "dwell",
                          agent_id="jobhunter-outreach", sleep=self.sleep)
        aid = insert_action(self.conn, kind="li_withdraw", platform="linkedin", status="armed", contact_id=None,
                            agent_id="jobhunter-outreach")
        self.conn.execute("UPDATE actions SET armed_at = ? WHERE id = ?", (canon.now(), aid))
        r = pacing.pace_wait(self.conn, "linkedin", "dwell", agent_id="jobhunter-outreach", sleep=self.sleep)
        self.assertTrue(5 <= r["remaining_s"] + r["slept_s"] <= 40)
        row = locks.get(self.conn, "pace:jobhunter-outreach:dwell")
        self.assertIsNotNone(row)
        self.assertEqual(r["lock"], "pace:jobhunter-outreach:dwell")

    def test_pace_wait_refuses_inside_transaction(self):
        with db.tx(self.conn):
            self.assertDenied("E_INTERNAL", pacing.pace_wait, self.conn, "linkedin", "write", sleep=self.sleep)

    def test_draw_bounds(self):
        for _ in range(200):
            v = pacing.draw(90, 300, 150)
            self.assertTrue(90 <= v <= 300)
        self.assertEqual(pacing.draw(5, 5), 5)

    def test_config_floors_cannot_be_lowered_by_file(self):
        write_config({"linkedin.delays_sec.write_floor": 5})
        self.assertEqual(config.load(self.conn)["linkedin"]["delays_sec"]["write_floor"], 45)


if __name__ == "__main__":
    import unittest
    unittest.main()
