"""Dispatcher and cycles (design 1.2, 1.3, 13.2): the day plan respects windows, spacing and browser
separation; no trigger when paused, broken, without work, with a stale or old guard heartbeat or a drifted cron
job; preflight go and no-go; cycle end releases leases, claims and tokens; the openclaw helper parses output
defensively, scrubs agent markers from its children, checks cron jobs against the manifest and runs QC reviews
as restricted one-shot cron jobs (CLI route 6.3)."""
from __future__ import annotations

import copy
import json
import os
import stat
import sys
from unittest import mock

import tests  # noqa: F401
from jobhunter import breakers, canon, config, cycles, db, dispatch, locks, ocrun, paths
from jobhunter.errors import Denied
from tests.fakes.u1 import TUESDAY_NOON, write_config, write_heartbeat
from tests.helpers import HomeTestCase, insert_action, insert_job

FAKE_OC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "core", "fake_openclaw.py")

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
        self.verified = []
        self.drift = {}

    def fake_run(self, job_id):
        self.calls.append(job_id)
        return {"ok": True, "error": None}

    def fake_verify(self, key, listing=None):
        self.verified.append(key)
        if key in self.drift:
            raise Denied("E_CRON_DRIFT", "drift", data={"key": key, "fields": self.drift[key]})
        return "job-x"

    def tick(self, work=1):
        return dispatch.tick(self.conn, work_count=lambda c, lane: work, cron_run=self.fake_run,
                             verify_job=self.fake_verify)


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

    def test_old_guard_heartbeat_and_drift_skip_the_lane(self):
        """CLI route 6.3.1 and M8: a heartbeat without identity proof version 2 is guard_missing; a lane whose
        cron job drifted from the manifest is skipped as cron_drift with an audit event; the job is checked
        before every run."""
        self.clock.set("2026-09-28T10:00:00Z")
        write_heartbeat(proof_version=None)
        self.plant()
        self.tick()
        self.assertEqual(self.status_of("evaluator")[-1], ("skipped", "guard_missing"))
        self.assertEqual(self.verified, [])
        write_heartbeat()
        self.drift = {"jobhunter:evaluate": ["tools", "message_sha256"]}
        self.clock.advance(minutes=1)
        self.plant()
        res = self.tick()
        self.assertEqual(self.status_of("evaluator")[-1], ("skipped", "cron_drift"))
        self.assertEqual(res["skipped"][-1]["reason"], "cron_drift")
        self.assertEqual((self.calls, self.verified), ([], ["jobhunter:evaluate"]))
        events = []
        for name in os.listdir(paths.logs_dir()):
            if name.startswith("events-"):
                with open(os.path.join(paths.logs_dir(), name)) as fh:
                    events += [json.loads(line) for line in fh if line.strip()]
        drift = [e for e in events if e["kind"] == "cron_drift"]
        self.assertEqual((drift[-1]["lane"], drift[-1]["fields"]), ("evaluator", ["tools", "message_sha256"]))
        self.assertTrue(self.conn.execute("SELECT 1 FROM notifications WHERE dedupe_key LIKE 'cron_drift:%'").fetchone())
        self.drift = {}
        self.clock.advance(minutes=1)
        self.plant()
        self.tick()
        self.assertEqual(self.status_of("evaluator")[-1][0], "triggered")
        self.assertEqual(self.calls, ["job-evaluate"])

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
        self.assertEqual(res["reply"], "CYCLE_DONE")
        self.assertFalse(locks.is_held(self.conn, "browser"))

    def test_lane_hints_end_with_cycle_done(self):
        """CLI route M6, V16: `cycle end` (the last call of every lane cycle) and a preflight no-go tell the agent
        to reply CYCLE_DONE; a stop signature names the final word neutrally. Never NO_REPLY."""
        from jobhunter.errors import CYCLE_DONE, FINAL_WORD_HINT
        from tests.helpers import agent_cli
        rc, env = agent_cli("jobhunter-applier", ["preflight", "--lane", "applier"], env_extra={"DISPLAY": ":0"})
        self.assertEqual((rc == 0, env["code"], env["next"]), (False, "E_PROFILE_UNCONFIRMED", "reply CYCLE_DONE"))
        rc, env = agent_cli("jobhunter-scout", ["preflight", "--lane", "scout"], env_extra={"DISPLAY": ":0"})
        self.assertEqual((rc, env["code"], env["data"]["go"]), (0, "OK", True), env)
        cid = env["data"]["cycle_id"]
        page = {"platform": "linkedin", "url": "https://www.linkedin.com/checkpoint/challenge/x",
                "title": "Security Verification", "http_status": None, "text": "Let's do a quick check"}
        f = self.home.write_agent_file("scout", "detect.json", json.dumps(page))
        rc, env = agent_cli("jobhunter-scout", ["--cycle", cid, "detect", "--file", f])
        self.assertEqual((rc, env["code"], env["next"]),
                         (5, "E_STOP_DETECTED", "end the cycle now and " + FINAL_WORD_HINT))
        rc, env = agent_cli("jobhunter-scout", ["cycle", "end", "--cycle", cid])
        self.assertEqual((rc, env["code"], env["data"]["reply"], env["next"]),
                         (0, "OK", CYCLE_DONE, "reply CYCLE_DONE"))
        self.assertEqual(CYCLE_DONE, "CYCLE_DONE")
        self.assertNotIn("NO_REPLY", json.dumps(env))

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
        self.fake_oc('echo \'noise {"ok": true, "run": {"status": "ok", "summary": "{\\"verdict\\": \\"pass\\"}"}} trailing\'')
        r = ocrun.cron_run_wait("job-1", 60)
        self.assertTrue(r["ok"])
        self.assertIn("verdict", r["text"])
        self.assertFalse(hasattr(ocrun, "agent_turn"))          # no jobhunter agent starts with `openclaw agent`
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

    def fake_oc_output(self, text: str) -> None:
        out = os.path.join(self.home.dir, "oc-stdout.txt")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.fake_oc('cat "%s"' % out)

    def test_cron_list_larger_than_20kb_is_read_whole(self):
        """LIVE D8: a real `cron list --all --json` is about 58 KB (19 jobhunter jobs plus OpenClaw's own). A
        stdout cut to its tail parsed only the trailing deliveryPreviews object, so verify_job said the job of
        every key was gone."""
        jobs = []
        for i in range(19):
            row = copy.deepcopy(AGENT_ROW)
            row.update(id="job-jh-%02d" % i, declarationKey="jobhunter:lane-%02d" % i,
                       state={"lastRunAtMs": 1790000000000 + i, "lastStatus": "ok", "note": "n" * 1200})
            jobs.append(row)
        for i in range(12):
            jobs.append({"id": "oc-own-%02d" % i, "name": "openclaw maintenance %d" % i, "enabled": True,
                         "agentId": "main", "payload": {"kind": "agentTurn", "message": "m" * 1500}})
        target = copy.deepcopy(AGENT_ROW)
        jobs.insert(0, target)                                  # the job under test is near the start
        listing = {"jobs": jobs, "total": len(jobs),
                   "deliveryPreviews": {j["id"]: {"mode": "none", "summary": "s" * 80} for j in jobs}}
        text = "[openclaw] config loaded\n" + json.dumps(listing, indent=2) + "\n"
        self.assertGreater(len(text), 50000)
        self.fake_oc_output(text)
        write_manifest({"jobhunter:evaluate": AGENT_SPEC}, {"jobhunter:evaluate": "job-evaluate"})
        r = ocrun.run(ocrun.oc_argv("cron", "list", "--all", "--json"), 60)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["stdout"]), len(text))
        got = ocrun.cron_list()
        self.assertTrue(got["ok"], got["error"])
        self.assertEqual([j["id"] for j in got["doc"]["jobs"]], [j["id"] for j in jobs])
        self.assertEqual(ocrun.verify_job("jobhunter:evaluate"), "job-evaluate")
        # drift is still found in a large listing
        jobs[0]["payload"]["toolsAllow"] = ["*"]
        self.fake_oc_output(json.dumps({"jobs": jobs, "deliveryPreviews": {}}))
        d = self.assertDenied("E_CRON_DRIFT", ocrun.verify_job, "jobhunter:evaluate")
        self.assertEqual(d.data["fields"], ["tools"])

    def test_cron_run_wait_reads_a_long_run_record_whole(self):
        doc = {"ok": True, "run": {"status": "ok", "summary": '{"verdict": "pass"}', "transcript": "t" * 60000}}
        self.fake_oc_output(json.dumps(doc) + "\n")
        r = ocrun.cron_run_wait("job-1", 60)
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(json.loads(r["text"]), {"verdict": "pass"})
        self.assertEqual(json.loads(r["raw"]), doc)

    def test_oversized_stdout_fails_and_is_never_cut(self):
        self.fake_oc_output(json.dumps({"jobs": [{"id": "x", "pad": "p" * 5000}]}))
        with mock.patch.object(ocrun, "STDOUT_MAX_BYTES", 1000):
            r = ocrun.run(ocrun.oc_argv("cron", "list", "--all", "--json"), 60)
            self.assertFalse(r["ok"])
            self.assertEqual(r["stdout"], "")
            self.assertIn("larger than 1000 bytes", r["error"])
            got = ocrun.cron_list()
            self.assertFalse(got["ok"])
            self.assertIsNone(got["doc"])
            d = self.assertDenied("E_OPENCLAW_CALL", ocrun.verify_job, "jobhunter:evaluate", None, "job-x",
                                  AGENT_SPEC)
            self.assertIn("larger than", d.message)


AGENT_SPEC = {"agent": "jobhunter-evaluator", "session": "isolated", "kind": "agent", "tools": ["exec", "read", "write"],
              "model": "anthropic/claude-sonnet-5", "fallbacks": [], "thinking": "medium", "timeout_s": 1200,
              "message_sha256": ocrun.message_sha256("Run exactly one evaluator cycle."), "delivery": "none"}
AGENT_ROW = {"id": "job-evaluate", "declarationKey": "jobhunter:evaluate", "enabled": False,
             "agentId": "jobhunter-evaluator", "sessionTarget": "isolated", "delivery": {"mode": "none"},
             "payload": {"kind": "agentTurn", "message": "Run exactly one evaluator cycle.",
                         "model": "anthropic/claude-sonnet-5", "fallbacks": [], "thinking": "medium",
                         "timeoutSeconds": 1200, "toolsAllow": ["write", "exec", "read"]}}
COMMAND_SPEC = {"kind": "command", "argv": ["/usr/bin/python3", "/r/scripts/jh.py", "dispatch", "tick", "--quiet"],
                "cwd": "/r", "env_sha256": ocrun.env_sha256({}), "timeout_s": 120}
COMMAND_ROW = {"id": "job-dispatch", "declarationKey": "jobhunter:dispatch", "agentId": None,
               "sessionTarget": "isolated",
               "payload": {"kind": "command", "argv": list(COMMAND_SPEC["argv"]), "cwd": "/r", "timeoutSeconds": 120}}
QC_SPEC = {"agent": "jobhunter-qc", "session": "isolated", "kind": "agent-oneshot", "tools": [],
           "model": "anthropic/claude-sonnet-5", "fallbacks": [], "thinking": "low", "timeout_s": 600,
           "message_sha256": None, "delivery": "none"}


def write_manifest(specs: dict, ids: dict | None = None) -> None:
    os.makedirs(paths.state_dir(), exist_ok=True)
    with open(os.path.join(paths.state_dir(), "install-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "cron_jobs": ids or {}, "cron_specs": specs}, fh)


class TestCronDrift(HomeTestCase):
    def test_every_field_is_compared(self):
        self.assertEqual(ocrun.job_drift(AGENT_SPEC, AGENT_ROW), [])
        self.assertEqual(ocrun.job_drift(COMMAND_SPEC, COMMAND_ROW), [])
        agent_cases = [
            ("tools", lambda r: r["payload"].update(toolsAllow=["*"])),
            ("tools", lambda r: r["payload"].pop("toolsAllow")),
            ("tools", lambda r: r["payload"].update(toolsAllow=["exec", "read", "write", "browser"])),
            ("message_sha256", lambda r: r["payload"].update(message="Do something else.")),
            ("model", lambda r: r["payload"].update(model="other/model")),
            ("agent", lambda r: r.update(agentId="jobhunter-scout")),
            ("session", lambda r: r.update(sessionTarget="main")),
            ("timeout_s", lambda r: r["payload"].update(timeoutSeconds=99)),
            ("fallbacks", lambda r: r["payload"].update(fallbacks=["x/y"])),
            ("thinking", lambda r: r["payload"].update(thinking="high")),
            ("delivery", lambda r: r.update(delivery={"mode": "announce"})),
            ("kind", lambda r: r["payload"].update(kind="systemEvent")),
        ]
        for field, change in agent_cases:
            with self.subTest(field=field):
                row = copy.deepcopy(AGENT_ROW)
                change(row)
                self.assertIn(field, ocrun.job_drift(AGENT_SPEC, row))
        for field, change in (("argv", lambda p: p["argv"].append("--extra")), ("cwd", lambda p: p.update(cwd="/tmp")),
                              ("env_sha256", lambda p: p.update(env={"PYTHONPATH": "/x"})),
                              ("timeout_s", lambda p: p.update(timeoutSeconds=5))):
            with self.subTest(field=field):
                row = copy.deepcopy(COMMAND_ROW)
                change(row["payload"])
                self.assertEqual(ocrun.job_drift(COMMAND_SPEC, row), [field])
        # enabled and the schedule are not compared; the QC one-shot spec has no message hash of its own
        row = copy.deepcopy(AGENT_ROW)
        row.update(enabled=True, schedule={"kind": "cron", "expr": "*/5 * * * *"})
        self.assertEqual(ocrun.job_drift(AGENT_SPEC, row), [])
        qc_row = copy.deepcopy(AGENT_ROW)
        qc_row.update(agentId="jobhunter-qc")
        qc_row["payload"].update(toolsAllow=[], thinking="low", timeoutSeconds=600)
        self.assertEqual(ocrun.job_drift(QC_SPEC, qc_row), [])

    def test_a_listed_message_with_a_file_mention_always_drifts(self):
        # D12: Claude Code attaches the file an `@<path>` in the prompt names, outside every tool
        for msg in ("Run one cycle. @/etc/hosts", "@~/.ssh/id_ed25519 run", 'see @"/etc/hosts"',
                    "x\u3002@/etc/hosts", "(@/etc/hosts)", "\uff20/etc/hosts"):
            with self.subTest(msg=msg):
                row = copy.deepcopy(AGENT_ROW)
                row["payload"]["message"] = msg
                spec = dict(AGENT_SPEC, message_sha256=ocrun.message_sha256(msg))
                self.assertEqual(ocrun.job_drift(spec, row), ["message_mention"])
                self.assertEqual(ocrun.job_drift(QC_SPEC, dict(row, agentId="jobhunter-qc", payload=dict(
                    row["payload"], toolsAllow=[], thinking="low", timeoutSeconds=600))), ["message_mention"])
        row = copy.deepcopy(AGENT_ROW)
        row["payload"]["message"] = "Mail a.b@example.com about it. @ end"
        spec = dict(AGENT_SPEC, message_sha256=ocrun.message_sha256(row["payload"]["message"]))
        self.assertEqual(ocrun.job_drift(spec, row), [])

    def test_declared_agent_messages_hold_no_file_mention(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "openclaw", "crons.json"), encoding="utf-8") as fh:
            jobs = json.load(fh)["jobs"]
        messages = [j["message"] for j in jobs if isinstance(j.get("message"), str)]
        self.assertTrue(messages)
        for msg in messages:
            self.assertFalse(ocrun.has_file_mention(msg), msg[:80])

    def test_verify_job_names_fields_never_values(self):
        write_manifest({"jobhunter:evaluate": AGENT_SPEC}, {"jobhunter:evaluate": "job-evaluate"})
        self.assertEqual(ocrun.verify_job("jobhunter:evaluate", {"jobs": [AGENT_ROW]}), "job-evaluate")
        row = copy.deepcopy(AGENT_ROW)
        row["payload"].update(message="SECRET-TEXT", toolsAllow=["*"])
        d = self.assertDenied("E_CRON_DRIFT", ocrun.verify_job, "jobhunter:evaluate", {"jobs": [row]})
        self.assertEqual(d.data["fields"], ["tools", "message_sha256"])
        self.assertNotIn("SECRET", json.dumps(d.to_dict()))
        d = self.assertDenied("E_CRON_DRIFT", ocrun.verify_job, "jobhunter:evaluate", {"jobs": []})
        self.assertEqual(d.data["fields"], ["missing"])
        d = self.assertDenied("E_CRON_DRIFT", ocrun.verify_job, "jobhunter:scout", {"jobs": [AGENT_ROW]})
        self.assertEqual(d.data["fields"], ["spec"])
        write_manifest({"jobhunter:evaluate": AGENT_SPEC}, {})
        d = self.assertDenied("E_CRON_DRIFT", ocrun.verify_job, "jobhunter:evaluate", {"jobs": [AGENT_ROW]})
        self.assertEqual(d.data["fields"], ["id"])


class TestMentions(HomeTestCase):
    def test_neutralize_mentions(self):
        cases = [
            ("@/etc/hosts", "@ /etc/hosts"),
            ("check @/etc/hosts now", "check @ /etc/hosts now"),
            ('read @"/etc/hosts" please', 'read @ "/etc/hosts" please'),
            ("a\n@~/.ssh/id_ed25519", "a\n@ ~/.ssh/id_ed25519"),
            ("\t@../../private/guard.key", "\t@ ../../private/guard.key"),
            ("x\u3002@/etc/hosts", "x\u3002@ /etc/hosts"),
            ("x\u00a0@/etc/hosts", "x\u00a0@ /etc/hosts"),
            ("(@/etc/hosts)", "(@ /etc/hosts)"),
            ("@@/etc/hosts", "@ @ /etc/hosts"),
            ("\uff20/etc/hosts \ufe6b/x", "\uff20 /etc/hosts \ufe6b /x"),
            ("Write to jane.doe+jobs@example.com today", "Write to jane.doe+jobs@example.com today"),
            ("a@ b, trailing @", "a@ b, trailing @"),
            ("no at sign", "no at sign"),
            ("", ""),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(ocrun.neutralize_mentions(text), want)
                self.assertFalse(ocrun.has_file_mention(want))
                self.assertEqual(ocrun.neutralize_mentions(want), want)          # a fixed point

    def test_no_live_at_sign_survives(self):
        # every short string over a hostile alphabet: the result never has a token that starts with an at sign
        import itertools
        import re
        alphabet = ["@", "a", " ", "\n", "\u3002", "/", '"', "\uff20", ".", "\u00a0"]
        token = re.compile(r'(^|[\s\u3001\u3002\uff01\uff1f])[@\uff20\ufe6b]("[^"]+"|[^\s]+)')
        for n in range(1, 6):
            for chars in itertools.product(alphabet, repeat=n):
                out = ocrun.neutralize_mentions("".join(chars))
                self.assertIsNone(token.search(out), repr(out))
                self.assertFalse(ocrun.has_file_mention(out), repr(out))
                self.assertEqual(out.replace("@ ", "@").replace("\uff20 ", "\uff20"),
                                 "".join(chars).replace("@ ", "@").replace("\uff20 ", "\uff20"))


class FakeOcCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        write_heartbeat()
        self.oc_dir = os.path.join(self.home.dir, "fake-oc")
        os.makedirs(self.oc_dir)
        wrapper = os.path.join(self.home.dir, "openclaw")
        with open(wrapper, "w") as fh:
            fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_OC))
        os.chmod(wrapper, 0o700)
        h = paths.home()
        h.update(oc_bin=wrapper, oc_profile="jhtest")
        with open(paths.home_file(), "w") as fh:
            json.dump(h, fh)
        self.env = mock.patch.dict(os.environ, {"FAKE_OC_DIR": self.oc_dir, "FAKE_OC_MODE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def mode(self, *modes):
        os.environ["FAKE_OC_MODE"] = ",".join(modes)

    def calls(self):
        try:
            with open(os.path.join(self.oc_dir, "calls.jsonl")) as fh:
                return [json.loads(line)[2:4] for line in fh]
        except OSError:
            return []

    def jobs(self):
        try:
            with open(os.path.join(self.oc_dir, "jobs.json")) as fh:
                return json.load(fh)
        except OSError:
            return []

    def message(self, text="Review this draft. Reply with JSON only.\n"):
        path = os.path.join(self.home.dir, "msg.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


class TestQcTurn(FakeOcCase):
    def setUp(self):
        super().setUp()
        write_manifest({"jobhunter:qc-review": QC_SPEC})

    def test_one_shot_order_and_reply(self):
        r = ocrun.qc_turn("review:Q1:attempt1", self.message(), 300)
        self.assertTrue(r["ok"], r)
        self.assertEqual(json.loads(r["text"]), {"verdict": "pass"})
        self.assertRegex(r["uid"], r"^[0-9a-f]{16}$")
        self.assertEqual(self.calls(), [["cron", "add"], ["cron", "list"], ["cron", "run"], ["cron", "rm"]])
        self.assertEqual(self.jobs(), [])                       # the one-shot job is removed
        with open(os.path.join(self.oc_dir, "calls.jsonl")) as fh:
            add = json.loads(fh.readline())
        self.assertEqual(add[:2], ["--profile", "jhtest"])
        self.assertEqual(add[add.index("--tools") + 1], "")
        self.assertEqual(add[add.index("--agent") + 1], "jobhunter-qc")
        self.assertEqual(add[add.index("--session") + 1], "isolated")
        self.assertIn("--disabled", add)
        self.assertIn("--no-deliver", add)
        self.assertIn("--message=Review this draft. Reply with JSON only.", add)
        self.assertTrue(add[add.index("--declaration-key") + 1].startswith("jobhunter:qc-review-"))
        self.assertNotIn("agent", [c[0] for c in self.calls()])

    def test_a_packet_with_file_mentions_reaches_openclaw_neutralized(self):
        # D12: draft, recipient and research text come from web pages and mail
        packet = ("Review this draft. Reply with JSON only.\nDraft: Hi, see @/etc/hosts and @\"~/.ssh/id_ed25519\"\n"
                  "Research:\u3002@../../private/guard.key (@/etc/passwd)\nRecipient: jane.doe@example.com\n")
        r = ocrun.qc_turn("s", self.message(packet), 300)
        self.assertTrue(r["ok"], r)
        with open(os.path.join(self.oc_dir, "calls.jsonl")) as fh:
            add = json.loads(fh.readline())
        msg = [a for a in add if a.startswith("--message=")]
        self.assertEqual(len(msg), 1)
        sent = msg[0][len("--message="):]
        self.assertFalse(ocrun.has_file_mention(sent))
        for bad in ("@/etc/hosts", '@"~/', "@../", "@/etc/passwd"):
            self.assertNotIn(bad, sent)
        self.assertIn("@ /etc/hosts", sent)
        self.assertIn("jane.doe@example.com", sent)                 # an address keeps its form
        self.assertTrue(all(not a.startswith("@") for a in add))

    def test_reply_from_cron_runs_and_failures_still_remove_the_job(self):
        self.mode("no_run_text")
        r = ocrun.qc_turn("s", self.message(), 300)
        self.assertEqual(json.loads(r["text"])["from"], "runs")
        self.assertEqual(self.calls()[-2:], [["cron", "runs"], ["cron", "rm"]])
        self.mode("run_fail")
        r = ocrun.qc_turn("s", self.message(), 300)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["text"])
        self.assertEqual(self.calls()[-1], ["cron", "rm"])
        self.mode("widen")                          # OpenClaw stored a wider tool list than asked: never run
        self.assertDenied("E_CRON_DRIFT", ocrun.qc_turn, "s", self.message(), 300)
        self.assertEqual(self.calls()[-2:], [["cron", "list"], ["cron", "rm"]])
        self.mode("rm_fail")
        self.assertTrue(ocrun.qc_turn("s", self.message(), 300)["ok"])       # a failed rm is logged, not raised
        self.assertEqual(self.jobs()[0]["declarationKey"][:20], "jobhunter:qc-review-")
        self.mode("add_fail")
        n = len(self.calls())
        r = ocrun.qc_turn("s", self.message(), 300)
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls()[n:], [["cron", "add"]])
        self.assertEqual(self.jobs()[0]["declarationKey"][:20], "jobhunter:qc-review-")   # only the rm_fail leftover

    def test_a_reply_the_run_record_cut_is_a_clear_failure(self):
        """OpenClaw 2026.9.8 keeps 2000 characters of the reply plus U+2026 in the run record: never a text."""
        self.mode("cut_summary")
        r = ocrun.qc_turn("s", self.message(), 300)
        self.assertEqual((r["ok"], r["text"], r["cut"]), (False, None, True), r)
        self.assertIn("cut the reply at 2000 characters", r["error"])
        self.assertEqual(self.calls()[-2:], [["cron", "run"], ["cron", "rm"]])     # no `cron runs` fallback
        self.assertEqual(self.jobs(), [])

    def test_run_text_never_returns_a_cut_text(self):
        whole = '{"verdict": "pass"}'
        cut = "x" * 2000 + "\u2026"
        self.assertEqual(ocrun.run_text({"run": {"summary": whole}}), whole)
        self.assertEqual(ocrun.run_reply({"run": {"summary": whole}}), {"text": whole, "cut": False})
        for doc in ({"run": {"summary": cut}}, {"entries": [{"summary": cut + "\n"}]},
                    {"run": {"summary": "y" * 1950 + "\u2026"}}):
            self.assertIsNone(ocrun.run_text(doc))
            self.assertEqual(ocrun.run_reply(doc), {"text": None, "cut": True})
        # a short text that ends with an ellipsis, or a long one without it, is a whole reply
        for text in ("Done\u2026", "z" * 2500):
            self.assertEqual(ocrun.run_text({"run": {"summary": text}}), text)
        self.assertEqual(ocrun.js_len("\U0001F600"), 2)          # JavaScript string length (UTF-16 units)
        self.assertTrue(ocrun.summary_cut("\U0001F600" * 1000 + "\u2026"))
        self.assertEqual(ocrun.run_reply(None), {"text": None, "cut": False})

    def test_preconditions_and_fqc(self):
        write_heartbeat(proof_version=None)
        self.assertDenied("E_GUARD_MISSING", ocrun.qc_turn, "s", self.message(), 300)
        self.assertEqual(self.calls(), [])
        write_heartbeat()
        self.assertDenied("E_VALIDATION", ocrun.qc_turn, "s", self.message("x" * (200 * 1024 + 1)), 300)
        self.assertDenied("E_VALIDATION", ocrun.qc_turn, "s", self.message("   \n"), 300)
        write_manifest({})
        self.assertDenied("E_CRON_DRIFT", ocrun.qc_turn, "s", self.message(), 300)
        self.assertEqual(self.calls(), [])
        write_manifest({"jobhunter:qc-review": QC_SPEC})
        h = paths.home()
        h["cli_route"] = {"carriers": ["argv", "env"], "qc_reply": "file"}
        with open(paths.home_file(), "w") as fh:
            json.dump(h, fh)
        self.assertTrue(ocrun.qc_turn("s", self.message(), 300)["ok"])
        with open(os.path.join(self.oc_dir, "calls.jsonl")) as fh:
            add = json.loads(fh.readline())
        self.assertEqual(add[add.index("--tools") + 1], "write")


class TestPreflightAndDispatchDrift(FakeOcCase):
    def test_preflight(self):
        write_manifest({"jobhunter:evaluate": AGENT_SPEC}, {"jobhunter:evaluate": "job-1"})
        rows = [dict(copy.deepcopy(AGENT_ROW), id="job-1")]
        with open(os.path.join(self.oc_dir, "jobs.json"), "w") as fh:
            json.dump(rows, fh)
        self.assertEqual(ocrun.preflight("jobhunter:evaluate"), "job-1")
        write_heartbeat(proof_version=None)
        self.assertDenied("E_GUARD_MISSING", ocrun.preflight, "jobhunter:evaluate")
        write_heartbeat(age_s=900)
        self.assertDenied("E_GUARD_MISSING", ocrun.preflight, "jobhunter:evaluate")
        write_heartbeat()
        self.mode("widen")
        d = self.assertDenied("E_CRON_DRIFT", ocrun.preflight, "jobhunter:evaluate")
        self.assertEqual(d.data["fields"], ["tools"])

    def test_dispatch_lists_once_and_skips_only_the_drifted_lane(self):
        specs, ids, rows = {}, {}, []
        for lane, key in (("evaluator", "jobhunter:evaluate"), ("replies", "jobhunter:replies")):
            spec = dict(AGENT_SPEC, agent=cycles.LANE_AGENTS[lane])
            row = copy.deepcopy(AGENT_ROW)
            row.update(id="job-" + lane, declarationKey=key, agentId=cycles.LANE_AGENTS[lane])
            if lane == "replies":
                row["payload"]["message"] = "edited outside Job Hunter"
            specs[key], ids[key] = spec, "job-" + lane
            rows.append(row)
        write_manifest(specs, ids)
        with open(os.path.join(self.oc_dir, "jobs.json"), "w") as fh:
            json.dump(rows, fh)
        self.clock.set("2026-09-29T10:00:00Z")
        write_heartbeat()
        with db.tx(self.conn):
            for lane in dispatch.LANES:
                self.conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status, reason) VALUES "
                                  "('2026-09-29', ?, '2026-09-29T23:59:00Z', 'skipped', 'test')", (lane,))
            for lane in ("evaluator", "replies"):
                self.conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status) VALUES "
                                  "('2026-09-29', ?, '2026-09-29T09:55:00Z', 'planned')", (lane,))
        ran = []
        res = dispatch.tick(self.conn, work_count=lambda c, lane: 1,
                            cron_run=lambda job_id: ran.append(job_id) or {"ok": True})
        self.assertEqual(ran, ["job-evaluator"])
        self.assertEqual([(s["lane"], s["reason"]) for s in res["skipped"]], [("replies", "cron_drift")])
        self.assertEqual([c for c in self.calls() if c == ["cron", "list"]], [["cron", "list"]])


class TestOcrunEnvAndPolicy(FakeOcCase):
    def test_children_get_no_agent_markers(self):
        rec = os.path.join(self.home.dir, "env.txt")
        script = os.path.join(self.home.dir, "envdump")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nenv > '%s'\n" % rec)
        os.chmod(script, 0o700)
        with mock.patch.dict(os.environ, {"OPENCLAW_SHELL": "exec", "JH_AGENT_ID": "jobhunter-scout",
                                          "JH_AGENT_PROOF": "p", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                                          "OPENCLAW_MCP_TOKEN": "t", "JOBHUNTER_DB": "/x", "KEEP_ME": "1"}):
            self.assertTrue(ocrun.run([script], 30)["ok"])
        with open(rec) as fh:
            names = {line.split("=", 1)[0] for line in fh if "=" in line}
        self.assertIn("KEEP_ME", names)
        for gone in ("OPENCLAW_SHELL", "JH_AGENT_ID", "JH_AGENT_PROOF", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                     "OPENCLAW_MCP_TOKEN", "JOBHUNTER_DB"):
            self.assertNotIn(gone, names)

    POLICY = {"effectivePolicy": {"note": "x", "scopes": [
        {"scopeLabel": "tools.exec", "configPath": "tools.exec", "agentId": "main",
         "mode": {"effective": "full"}, "security": {"effective": "full", "host": "full"},
         "ask": {"effective": "off", "host": "off"}, "askFallback": {"effective": "full"}},
        {"scopeLabel": "agent:jobhunter-evaluator", "configPath": "agents.entries.jobhunter-evaluator.tools.exec",
         "agentId": "jobhunter-evaluator", "mode": {"effective": "allowlist"},
         "security": {"effective": "allowlist", "host": "allowlist"}, "ask": {"effective": "off", "host": "off"},
         "askFallback": {"effective": "deny"}},
        {"scopeLabel": "agent:jobhunter-scout", "configPath": "agents.entries.jobhunter-scout.tools.exec",
         "agentId": "jobhunter-scout", "mode": {"effective": "allowlist"},
         "security": {"effective": "allowlist", "host": "allowlist"},
         "ask": {"effective": "always", "host": "on-miss"}, "askFallback": {"effective": "deny"}},
        {"scopeLabel": "agent:jobhunter-qc", "agentId": "jobhunter-qc", "mode": {"effective": "deny"},
         "security": {"effective": "deny", "host": "deny"}, "ask": {"effective": "off", "host": "off"},
         "askFallback": {"effective": "deny"}},
        {"scopeLabel": "agent:helper", "agentId": "helper", "mode": {"effective": "allowlist"},
         "security": {"effective": "allowlist"}, "ask": {"effective": "off", "host": "off"},
         "askFallback": {"effective": "deny"}},
        {"scopeLabel": "agent:asker", "agentId": "asker", "mode": {"effective": "allowlist"},
         "security": {"effective": "allowlist"}, "ask": {"effective": "on-miss"}, "askFallback": {"effective": "deny"}},
        {"scopeLabel": "agent:node1", "agentId": "node1", "mode": {"effective": "unknown"},
         "security": {"effective": "unknown"}, "ask": {"effective": "unknown"}, "askFallback": {"effective": "unknown"}}]}}

    def test_effective_exec_and_boundary(self):
        with open(os.path.join(self.oc_dir, "exec-policy.json"), "w") as fh:
            json.dump(self.POLICY, fh)
        os.environ["FAKE_OC_ELEVATED"] = "jobhunter-scout"
        ev = ocrun.effective_exec("jobhunter-evaluator")
        self.assertEqual(ev, {"agent": "jobhunter-evaluator", "security": "allowlist", "ask": "off", "mode": "allowlist",
                              "elevated": False, "approvals_ask": "off", "approvals_fallback": "deny", "ok": True})
        self.assertEqual(ocrun.exec_policy_problems("jobhunter-evaluator", ev), [])
        sc = ocrun.effective_exec("jobhunter-scout")
        self.assertEqual(ocrun.exec_policy_problems("jobhunter-scout", sc), ["ask", "elevated", "approvals ask"])
        qc = ocrun.effective_exec("jobhunter-qc")
        self.assertEqual(ocrun.exec_policy_problems("jobhunter-qc", qc), [])
        missing = ocrun.effective_exec("jobhunter-applier")                       # no scope: fail closed
        self.assertFalse(missing["ok"])
        self.assertEqual(ocrun.exec_policy_problems("jobhunter-applier", missing),
                         ["security", "ask", "approvals ask", "approvals askFallback"])
        self.assertEqual(ocrun.agents_list(), ["helper", "jobhunter-scout", "main"])
        bad = [a["agent"] for a in ocrun.unconfined_agents(self.POLICY, ["main", "helper", "newcomer"])]
        self.assertEqual(bad, ["asker", "main", "newcomer", "node1"])     # newcomer inherits the global scope
        self.assertEqual(ocrun.plugins_enabled(), ["approval-bot", "jobhunter-guard"])
        self.assertEqual({c[0] for c in self.calls()}, {"exec-policy", "sandbox", "config", "plugins"})


if __name__ == "__main__":
    import unittest
    unittest.main()
