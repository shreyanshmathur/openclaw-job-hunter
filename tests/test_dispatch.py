"""Dispatcher and cycles (design 1.2, 1.3, 13.2): the day plan respects windows, spacing and browser
separation; no trigger when paused, broken, without work or with a stale guard heartbeat; preflight go and
no-go; cycle end releases leases, claims and tokens; the openclaw helper parses output defensively."""
from __future__ import annotations

import json
import os
import stat

import tests  # noqa: F401
from jobhunter import breakers, canon, config, cycles, db, dispatch, locks, ocrun, paths
from tests.fakes.u1 import TUESDAY_NOON, write_config, write_heartbeat
from tests.helpers import HomeTestCase, insert_action, insert_job

MONDAY_0005 = "2026-09-28T00:05:00Z"


def set_job_ids():
    h = paths.home()
    h["cron_jobs"] = {k: "job-%s" % k.split(":")[1] for k in dispatch.JOB_KEYS.values()}
    with open(paths.home_file(), "w") as fh:
        json.dump(h, fh)


class DispatchCase(HomeTestCase):
    start_ts = MONDAY_0005

    def setUp(self):
        super().setUp()
        write_config()
        write_heartbeat()
        set_job_ids()
        self.calls = []

    def fake_run(self, job_id):
        self.calls.append(job_id)
        return {"ok": True, "error": None}

    def tick(self, work=1):
        return dispatch.tick(self.conn, work_count=lambda c, lane: work, cron_run=self.fake_run)


class TestPlan(DispatchCase):
    def test_plan_respects_windows_spacing_and_browser_gap(self):
        cfg = config.load(self.conn)
        for _ in range(10):
            with db.tx(self.conn):
                self.conn.execute("DELETE FROM dispatch_slots")
                dispatch.plan_day(self.conn, "2026-09-28", cfg)
            rows = self.conn.execute("SELECT lane, slot_at, status FROM dispatch_slots").fetchall()
            browser = []
            for lane in dispatch.LANES:
                spec = cfg["dispatch"]["lanes"][lane]
                mine = sorted(r["slot_at"] for r in rows if r["lane"] == lane)
                self.assertLessEqual(len(mine), spec["cycles_per_day"][1])
                for s in mine:
                    hm = s[11:16]
                    self.assertTrue(spec["window"][0] <= hm <= spec["window"][1], (lane, s))
                for a, b in zip(mine, mine[1:]):
                    self.assertGreaterEqual(canon.seconds_between(a, b), spec["min_spacing_minutes"] * 60)
                if lane in dispatch.BROWSER_LANES:
                    browser += [r["slot_at"] for r in rows if r["lane"] == lane and r["status"] != "skipped"]
            browser.sort()
            for a, b in zip(browser, browser[1:]):
                self.assertGreaterEqual(canon.seconds_between(a, b), 35 * 60)

    def test_weekend_days(self):
        cfg = config.load(self.conn)
        with db.tx(self.conn):
            out = dispatch.plan_day(self.conn, "2026-10-03", cfg)       # Saturday
        self.assertEqual({s["lane"] for s in out} - {"evaluator", "replies"}, set())

    def test_plan_is_idempotent(self):
        cfg = config.load(self.conn)
        with db.tx(self.conn):
            first = dispatch.plan_day(self.conn, "2026-09-28", cfg)
            again = dispatch.plan_day(self.conn, "2026-09-28", cfg)
        self.assertTrue(first)
        self.assertEqual(again, [])


class TestTick(DispatchCase):
    def setUp(self):
        super().setUp()
        # one placeholder row per lane, so the tick's own day plan leaves these tests alone
        with db.tx(self.conn):
            for i, lane in enumerate(dispatch.LANES):
                self.conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status, reason) VALUES "
                                  "('2026-09-28', ?, ?, 'skipped', 'test')", (lane, "2026-09-28T23:5%dZ" % i))

    def plant(self, lane="evaluator", at=None):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status) VALUES (?, ?, ?, 'planned')",
                              ("2026-09-28", lane, at or canon.now()))

    def status_of(self, lane):
        return [tuple(r) for r in self.conn.execute("SELECT status, reason FROM dispatch_slots WHERE lane = ? AND "
                                                    "slot_at <= ?", (lane, canon.now()))]

    def test_trigger_when_everything_is_fine(self):
        self.clock.set("2026-09-28T10:00:00Z")
        write_heartbeat()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status) VALUES "
                              "('2026-09-28', 'evaluator', '2026-09-28T09:55:00Z', 'planned')")
        res = self.tick()
        self.assertEqual([t["lane"] for t in res["triggered"]], ["evaluator"])
        self.assertEqual(self.calls, ["job-evaluate"])
        self.assertEqual(self.status_of("evaluator")[0][0], "triggered")

    def test_no_trigger_when_paused_broken_idle_or_guard_missing(self):
        self.clock.set("2026-09-28T10:00:00Z")
        cases = []
        with db.tx(self.conn):
            breakers.pause(self.conn, "all", None, "human")
        self.plant()
        self.tick()
        cases.append(self.status_of("evaluator")[-1])
        with db.tx(self.conn):
            breakers.unpause(self.conn, "all")
            breakers.trip(self.conn, "global", "clock_skew", "x")
        self.clock.advance(minutes=1)
        self.plant()
        self.tick()
        cases.append(self.status_of("evaluator")[-1])
        with db.tx(self.conn):
            self.conn.execute("UPDATE breakers SET state = 'closed'")
        self.clock.advance(minutes=1)
        self.plant()
        self.tick(work=0)
        cases.append(self.status_of("evaluator")[-1])
        os.unlink(os.path.join(paths.guard_dir(), "heartbeat.json"))
        self.clock.advance(minutes=1)
        self.plant()
        self.tick()
        cases.append(self.status_of("evaluator")[-1])
        self.assertEqual([c[1] for c in cases], ["paused", "breaker_global", "no_work", "guard_missing"])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.conn.execute("SELECT 1 FROM notifications WHERE dedupe_key LIKE 'guard_missing:%'").fetchone())

    def test_browser_lease_waits_then_missed(self):
        self.clock.set("2026-09-28T10:00:00Z")
        with db.tx(self.conn):
            locks.acquire(self.conn, "browser", "C20260928T095000ZAAAA", 3600)
        self.plant("applier")
        res = self.tick()
        self.assertEqual(res["waiting"][0]["lane"], "applier")
        self.clock.advance(minutes=50)
        self.tick()
        self.assertEqual(self.status_of("applier")[0][0], "missed")

    def test_outside_browser_hours(self):
        self.clock.set("2026-09-28T21:00:00Z")
        self.plant("scout")
        self.tick()
        self.assertEqual(self.status_of("scout")[0], ("skipped", "outside_hours"))


class TestPreflightAndEnd(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        write_heartbeat()

    def preflight(self, lane, agent):
        with db.tx(self.conn):
            return cycles.preflight(self.conn, lane, agent, env={"DISPLAY": ":0"})

    def test_go_and_lease(self):
        r = self.preflight("scout", "jobhunter-scout")
        self.assertTrue(r["go"], r)
        self.assertRegex(r["cycle_id"], r"^C[0-9]{8}T[0-9]{6}Z[A-Z2-7]{4}$")
        r2 = self.preflight("applier", "jobhunter-applier")
        self.assertEqual((r2["go"], r2["code"]), (False, "E_PROFILE_UNCONFIRMED"))
        with db.tx(self.conn):
            db.meta_set(self.conn, "profile_version", "v1", "system")
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"jobhunter.profile": type(sys)("fakeprofile")}):
            sys.modules["jobhunter.profile"].status = lambda: {"confirmed": True}
            r3 = self.preflight("applier", "jobhunter-applier")
        self.assertEqual((r3["go"], r3["code"]), (False, "E_LOCKED"))
        with db.tx(self.conn):
            res = cycles.end(self.conn, r["cycle_id"], {"counts": {"pages": 3}}, "jobhunter-scout")
        self.assertEqual(res["reply"], "NO_REPLY")
        self.assertFalse(locks.is_held(self.conn, "browser"))

    def test_identity_required_only_for_agents_that_can_check(self):
        """preflight asks for an identity check only where the lane's agent has `identity check` in acl.json."""
        from tests.fakes.u1 import enable_linkedin
        enable_linkedin(self.conn)
        write_config({"channels.linkedin.enabled": True})
        r = self.preflight("scout", "jobhunter-scout")
        self.assertTrue(r["go"], r)
        self.assertEqual(r["identity_required"], [])
        with open(paths.ACL_FILE, encoding="utf-8") as fh:
            acl = json.load(fh)
        self.assertNotIn("identity check", acl["agents"]["jobhunter-scout"]["commands"])
        for lane in ("outreach", "replies"):
            self.assertIn("identity check", acl["agents"][cycles.LANE_AGENTS[lane]]["commands"])

    def test_no_go_reasons(self):
        with db.tx(self.conn):
            breakers.pause(self.conn, "all", None, "human")
        self.assertEqual(self.preflight("evaluator", "jobhunter-evaluator")["code"], "E_PAUSED")
        with db.tx(self.conn):
            breakers.unpause(self.conn, "all")
        os.unlink(os.path.join(paths.guard_dir(), "heartbeat.json"))
        self.assertEqual(self.preflight("evaluator", "jobhunter-evaluator")["code"], "E_GUARD_MISSING")
        write_heartbeat()
        self.clock.set("2026-10-03T12:00:00Z")      # Saturday
        write_heartbeat()
        self.assertEqual(self.preflight("scout", "jobhunter-scout")["code"], "E_OUTSIDE_HOURS")
        self.assertDenied("E_CALLER_NOT_ALLOWED", self.preflight, "applier", "jobhunter-scout")
        rows = self.conn.execute("SELECT status FROM cycles").fetchall()
        self.assertTrue(all(r[0] == "no_go" for r in rows))

    def test_browser_lanes_need_consent_for_their_login_sites(self):
        from jobhunter import identity
        from tests.helpers import clear_consent, write_consent
        write_config({"gmail.route": "web_ui", "channels.linkedin.enabled": False})
        clear_consent()
        r = self.preflight("replies", "jobhunter-outreach")          # Gmail in the browser, LinkedIn off
        self.assertEqual((r["go"], r["code"]), (False, "E_CONSENT_MISSING"))
        self.assertEqual(r["consent"], {"allowed": [], "missing": ["gmail"]})
        self.assertIn("gmail", r["reasons"][0])
        self.assertNotIn("consent", self.preflight("evaluator", "jobhunter-evaluator"))    # no browser
        write_config({"gmail.route": "web_ui", "boards.sites.naukri.discover": "browser"})
        r = self.preflight("scout", "jobhunter-scout")
        self.assertEqual((r["code"], r["consent"]["missing"]), ("E_CONSENT_MISSING", ["naukri"]))
        from tests.fakes.u1 import enable_linkedin
        enable_linkedin(self.conn)
        write_config({"gmail.route": "web_ui", "channels.linkedin.enabled": True})
        write_consent(["gmail"])
        r = self.preflight("replies", "jobhunter-outreach")
        self.assertTrue(r["go"], r)
        self.assertEqual(r["consent"], {"allowed": ["gmail"], "missing": ["linkedin"]})
        self.assertEqual(r["identity_required"], ["gmail"])           # never a site without consent
        with db.tx(self.conn):
            cycles.end(self.conn, r["cycle_id"], None)
        # a lane with work that needs no login (ATS forms) still goes, and lists the missing sites
        cfg = config.load(self.conn)
        lc = identity.lane_consent(cfg, "applier", {"sites": {}})
        self.assertEqual((lc["sites"], lc["allowed"], lc["public_work"]), (["gmail", "linkedin"], [], True))

    def test_preflight_catches_a_consent_revoked_outside_the_commands(self):
        from tests.helpers import write_consent
        self.preflight("evaluator", "jobhunter-evaluator")              # records the active sites
        write_consent(["gmail"])                                       # LinkedIn and the boards were taken back
        self.preflight("evaluator", "jobhunter-evaluator")
        scopes = {r[0] for r in self.conn.execute("SELECT scope FROM breakers WHERE state = 'open' AND "
                                                  "reason_code = 'consent_revoked'")}
        self.assertIn("linkedin", scopes)
        self.assertIn("site:naukri", scopes)
        self.assertNotIn("gmail", scopes)

    def test_clock_backwards_trips_global(self):
        with db.tx(self.conn):
            db.meta_set(self.conn, "max_seen_ts", canon.ts_add(canon.now(), minutes=30), "system")
        r = self.preflight("evaluator", "jobhunter-evaluator")
        self.assertEqual(r["code"], "E_CLOCK_SKEW")
        self.assertEqual(self.conn.execute("SELECT reason_code FROM breakers WHERE scope = 'global'").fetchone()[0],
                         "clock_skew")

    def test_end_releases_claims_and_tokens(self):
        r = self.preflight("scout", "jobhunter-scout")
        cid = r["cycle_id"]
        j = insert_job(self.conn, status="evaluating")
        self.conn.execute("UPDATE jobs SET claimed_by = ? WHERE id = ?", (cid, j))
        aid = insert_action(self.conn, kind="li_withdraw", platform="linkedin", status="reserved", contact_id=None,
                            agent_id="jobhunter-scout")
        self.conn.execute("UPDATE actions SET cycle_id = ? WHERE id = ?", (cid, aid))
        with db.tx(self.conn):
            res = cycles.end(self.conn, cid, None)
        self.assertEqual((res["released"]["jobs"], res["released"]["tokens"]), (1, 1))
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (j,)).fetchone()[0], "eval_queued")
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE id = ?", (aid,)).fetchone()[0], "unknown")

    def test_new_cycle_closes_a_token_a_dead_cycle_left_open(self):
        """Design 0 principle 2: a crash between the Send click and confirm leaves an armed token; once the lease
        lapses, the agent's next browser cycle (here a replies cycle of the same agent) must not inherit a live
        token that the guard would count as open. It becomes unknown and the dead cycle ends as error."""
        from jobhunter import gate
        from tests.helpers import insert_cycle
        old = insert_cycle(self.conn, "outreach", started_at=canon.ts_add(canon.now(), minutes=-36))
        aid = insert_action(self.conn, kind="li_withdraw", platform="linkedin", status="armed", contact_id=None,
                            agent_id="jobhunter-outreach", reserved_at=canon.ts_add(canon.now(), minutes=-5))
        self.conn.execute("UPDATE actions SET cycle_id = ?, lane = 'outreach', armed_at = ? WHERE id = ?",
                          (old, canon.now(), aid))
        other = insert_action(self.conn, kind="li_withdraw", platform="linkedin", status="reserved",
                              contact_id=None, agent_id="jobhunter-applier")
        tok = self.conn.execute("SELECT token FROM actions WHERE id = ?", (aid,)).fetchone()[0]
        # the dead cycle's lease is still unexpired: no go, and nothing is touched
        with db.tx(self.conn):
            self.assertTrue(locks.acquire(self.conn, "browser", old, 60))
        r = self.preflight("replies", "jobhunter-outreach")
        self.assertEqual((r["go"], r["code"]), (False, "E_LOCKED"))
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE id = ?", (aid,)).fetchone()[0], "armed")
        # the lease lapsed (35 minutes without a renew) while the token (30 minutes from reserve) is still live
        self.clock.advance(minutes=2)
        write_heartbeat()
        r = self.preflight("replies", "jobhunter-outreach")
        self.assertTrue(r["go"], r)
        self.assertEqual(r["stale"], {"tokens": [tok], "cycles": [old]})
        a = self.conn.execute("SELECT status, note FROM actions WHERE id = ?", (aid,)).fetchone()
        self.assertEqual(a["status"], "unknown")
        self.assertTrue(a["note"].startswith("stale_cycle: cycle %s" % old), a["note"])
        c = self.conn.execute("SELECT status, ended_at FROM cycles WHERE cycle_id = ?", (old,)).fetchone()
        self.assertEqual(c["status"], "error")
        self.assertIsNotNone(c["ended_at"])
        with db.tx(self.conn):
            self.assertIsNone(gate.open_token(self.conn, "jobhunter-outreach"))   # what the guard reads
        # another agent's token is not this lane's business
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE id = ?", (other,)).fetchone()[0],
                         "reserved")
        # the next cycle of the same agent finds nothing left to close
        with db.tx(self.conn):
            cycles.end(self.conn, r["cycle_id"], None)
        r2 = self.preflight("replies", "jobhunter-outreach")
        self.assertTrue(r2["go"], r2)
        self.assertNotIn("stale", r2)


class TestOcrun(HomeTestCase):
    def fake_oc(self, script: str) -> None:
        path = os.path.join(self.home.dir, "fake-openclaw")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + script + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        h = paths.home()
        h["oc_bin"] = path
        h["oc_profile"] = "jhtest"
        with open(paths.home_file(), "w") as fh:
            json.dump(h, fh)

    def message_file(self, text: str) -> str:
        path = os.path.join(self.home.dir, "msg.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_argv_and_parsing(self):
        msg = self.message_file("Approval needed\n")
        self.fake_oc('echo "log line"; echo \'{"ok": true, "messageId": "m1"}\'')
        self.assertEqual(ocrun.oc_argv("cron", "run", "x")[1:], ["--profile", "jhtest", "cron", "run", "x"])
        self.assertTrue(ocrun.message_send("whatsapp", "+10000000000", msg)["ok"])
        self.assertTrue(ocrun.cron_run("job-1")["ok"])
        self.fake_oc('echo \'noise {"result": {"text": "{\\"verdict\\": \\"pass\\"}"}} trailing\'')
        r = ocrun.agent_turn("jobhunter-qc", "review-x", "/tmp/none", 5)
        self.assertTrue(r["ok"])
        self.assertIn("verdict", r["text"])
        self.fake_oc("echo not json; exit 0")
        self.assertFalse(ocrun.message_send("whatsapp", "+10000000000", msg)["ok"])
        self.fake_oc("echo boom 1>&2; exit 3")
        r = ocrun.cron_run("job-1")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])

    def test_message_send_uses_only_known_flags(self):
        """openclaw message send has -m/--message and no --message-file (captured help, OpenClaw 2026.9.5)."""
        rec = os.path.join(self.home.dir, "argv.bin")
        self.fake_oc('printf \'%%s\\0\' "$@" > "%s"; echo \'{"ok": true}\'' % rec)
        body = "- Approve A1 (reply A1 OK)\nline two\n"
        res = ocrun.message_send("telegram", "123456", self.message_file(body))
        self.assertTrue(res["ok"], res)
        with open(rec, "rb") as fh:
            argv = fh.read().decode("utf-8").split("\0")[:-1]
        self.assertEqual(argv[:4], ["--profile", "jhtest", "message", "send"])
        flags = [a.split("=", 1)[0] for a in argv[4:] if a.startswith("-")]
        self.assertTrue(set(flags) <= ocrun.MESSAGE_SEND_FLAGS, flags)
        self.assertNotIn("--message-file", argv)
        rest = argv[4:]
        self.assertEqual(rest[rest.index("--channel") + 1], "telegram")
        self.assertEqual(rest[rest.index("--target") + 1], "123456")
        self.assertIn("--message=" + body.rstrip("\n"), rest)   # the file's text, in the = form
        self.assertIn("--json", rest)
        # the known flag set is the captured help's option list
        self.assertTrue({"--message", "--target", "--channel", "--json"} <= ocrun.MESSAGE_SEND_FLAGS)
        self.assertNotIn("--message-file", ocrun.MESSAGE_SEND_FLAGS)

    def test_message_send_refuses_unreadable_or_empty_file(self):
        self.fake_oc('echo \'{"ok": true}\'')
        self.assertFalse(ocrun.message_send("telegram", "1", os.path.join(self.home.dir, "missing.txt"))["ok"])
        self.assertFalse(ocrun.message_send("telegram", "1", self.message_file("  \n"))["ok"])

    def test_missing_binary(self):
        h = paths.home()
        h["oc_bin"] = "/nonexistent/openclaw"
        with open(paths.home_file(), "w") as fh:
            json.dump(h, fh)
        self.assertFalse(ocrun.cron_run("x")["ok"])
        self.assertEqual(ocrun.last_json('a {"x": 1} b {"y": [2]} c'), {"y": [2]})


if __name__ == "__main__":
    import unittest
    unittest.main()
