"""E2E (INT): the U6 browser transcripts, re-recorded in the tool-call shapes OpenClaw hands a plugin, replayed
through the real jobhunter-guard runtime (U8, decide() behind GuardRuntime.evaluate and observe) while the real core
(U1 to U6) runs every jh.py call the guard lets through.

tests/fixtures/e2e/guard_bridge.ts runs the plugin's GuardRuntime under `node` (Node 24 or later runs the
TypeScript directly, as the plugin README documents) against the temp install: the real acl.json, guard-hosts.json,
detect files and driver manifest of this repo, the temp home.json and guard.key, and the ledger the core writes.
Each step goes to the guard first; an allowed exec runs through cli.main with the environment the guard gives
the agent (JH_AGENT_ID and the HMAC proof), an allowed write writes the file, and a browser result goes back to
the guard's observer. Steps that expect a G_* code are mutations and never run. Skipped without Node 24.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import auth, canon, cli, ocrun, paths
from jobhunter import qc as qcpkg
from tests.fakes.u3 import FakeReviewer, install_reviewer_hashes
from tests.fixtures.e2e.support import AP, E2E, ApplyWorld, e2e_fixture

NODE = shutil.which("node")
FIELDS = ("First Name", "Last Name", "Email", "Notice period (days)")
VAR_RE = re.compile(r"\{([A-Z_]+|F:[^{}]+|DRIVER:[a-z_]+)\}")


def node_major() -> int:
    if not NODE:
        return 0
    try:
        out = subprocess.run([NODE, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        return int(out.stdout.decode().strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


class GuardBridge:
    """The real GuardRuntime in a node process, one JSON line per request."""

    def __init__(self, config: dict, now: str):
        self.proc = subprocess.Popen([NODE, os.path.join(E2E, "guard_bridge.ts")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=paths.REPO)
        self.ask({"op": "init", "config": config, "now": now})

    def ask(self, msg: dict) -> dict:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError("guard bridge exited: %s" % self.proc.stderr.read().decode("utf-8", "replace")[-3000:])
        out = json.loads(line.decode("utf-8"))
        if "error" in out:
            raise AssertionError("guard bridge: %s" % out["error"])
        return out

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except (OSError, subprocess.SubprocessError):
            self.proc.kill()
        for s in (self.proc.stdout, self.proc.stderr):
            s.close()


class Replay:
    """Replays one transcript: guard decision first, then the real effect of an allowed call."""

    def __init__(self, test: unittest.TestCase, world, bridge: GuardBridge, transcript: dict, vars_: dict):
        self.t = test
        self.w = world
        self.g = bridge
        self.tr = transcript
        self.vars = dict(vars_)
        self.agent = transcript["agent"]
        self.session = transcript["session"]
        role = self.agent[len("jobhunter-"):]
        h = paths.home()
        self.jh_prefix = "%s %s/scripts/jh.py " % (h["python"], paths.REPO)
        self.vars.update({"PY": h["python"], "REPO": paths.REPO, "WS": os.path.join(h["ws_root"], role)})
        self.outcomes = []
        self.jh = []

    def sub(self, v):
        if isinstance(v, str):
            def one(m):
                key = m.group(1)
                if key.startswith("DRIVER:"):
                    with open(os.path.join(paths.REPO, "drivers", key[7:] + ".js"), "r", encoding="utf-8") as fh:
                        return fh.read()
                if key == "NOW":
                    return canon.now()
                if key not in self.vars:
                    raise AssertionError("%s: no value for {%s} yet" % (self.tr["name"], key))
                return str(self.vars[key])
            return VAR_RE.sub(one, v)
        if isinstance(v, list):
            return [self.sub(x) for x in v]
        if isinstance(v, dict):
            return {k: self.sub(x) for k, x in v.items()}
        return v

    def run_jh(self, command: str, env: dict):
        self.t.assertTrue(command.startswith(self.jh_prefix), command)
        argv = shlex.split(command[len(self.jh_prefix):])
        out = io.StringIO()
        rc = cli.main(argv, env=dict(env, OPENCLAW_SHELL="1"), stdin=io.StringIO(""), stdout=out)
        envelope = json.loads(out.getvalue().strip().splitlines()[-1])
        self.jh.append((argv, rc, envelope.get("code")))
        return rc, envelope

    def run(self) -> None:
        for i, raw in enumerate(self.tr["steps"]):
            where = "%s step %d" % (self.tr["name"], i + 1)
            for _attempt in range(6):
                step = self.sub({k: v for k, v in raw.items() if k != "note"})
                if not self.once(step, where):
                    break

    def once(self, step: dict, where: str) -> bool:
        """One tool call. Returns True when the step asks to be repeated (repeat_while)."""
        self.g.ask({"op": "clock", "now": canon.now()})
        dec = self.g.ask({"op": "call", "agent": self.agent, "session": self.session, "tool": step["tool"],
                          "params": step["params"]})
        want = step.get("expect", "allow")
        self.outcomes.append((step["tool"], dec["outcome"]))
        self.t.assertEqual(dec["outcome"], want, "%s: %s" % (where, dec.get("reason")))
        if want != "allow":
            return False
        params = dec.get("params") or step["params"]
        if step["tool"] == "exec":
            env = self.g.ask({"op": "env", "agent": self.agent, "session": self.session, "runId": "run-e2e"})["env"]
            rc, envelope = self.run_jh(params["command"], env)
            code = step.get("jh_code")
            if code:
                self.t.assertEqual(envelope.get("code"), code, "%s: %s" % (where, json.dumps(envelope)[:1200]))
            else:
                self.t.assertEqual(rc, 0, "%s: %s" % (where, json.dumps(envelope)[:1200]))
            data = envelope.get("data") or {}
            for var, key in (step.get("save") or {}).items():
                self.vars[var] = data[key]
            rw = step.get("repeat_while")
            return bool(rw and data.get(rw))
        if step["tool"] == "write":
            path = params["path"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(params["content"])
            return False
        if "result" in step:
            obs = self.g.ask({"op": "result", "agent": self.agent, "session": self.session, "tool": step["tool"],
                              "params": params, "result": step["result"], "error": None})
            self.t.assertEqual(obs["stopped"], bool(step.get("expect_stop")), where + " stop")
        return False


@unittest.skipUnless(node_major() >= 24, "needs node 24 or later (the guard plugin runs TypeScript directly)")
class GuardReplayBase(unittest.TestCase):
    def setUp(self):
        self.w = ApplyWorld()
        self.addCleanup(self.w.stop)
        prev = qcpkg.SPAWN
        qcpkg.SPAWN = [].append
        self.addCleanup(setattr, qcpkg, "SPAWN", prev)
        p = mock.patch.object(ocrun, "agent_turn", FakeReviewer("pass"))
        p.start()
        self.addCleanup(p.stop)
        self.w.onboard()
        install_reviewer_hashes(self.w.conn)
        self.assertTrue(auth.create_guard_key())

    def start_guard(self) -> GuardBridge:
        h = paths.home()
        g = GuardBridge({"repo": paths.REPO, "python": h["python"], "homeFile": paths.home_file(),
                         "publicReadonlyAgents": ["main"]}, canon.now())
        self.addCleanup(g.close)
        # the guard's own heartbeat (not the U1 test fake) is what lets the lanes start
        hb = os.path.join(paths.guard_dir(), "heartbeat.json")
        if os.path.exists(hb):
            os.remove(hb)
        self.assertTrue(g.ask({"op": "heartbeat"})["ok"])
        with open(hb, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["install_id"], h["install_id"])
        return g

    def guard_log(self) -> list:
        path = os.path.join(paths.logs_dir(), "guard-%s.jsonl" % canon.now()[:7])
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class TestAtsApplyReplay(GuardReplayBase):
    def prepare(self) -> dict:
        """An approved application package for a Greenhouse job, made by the real lanes (see test_e2e_apply)."""
        w = self.w
        job_uid = w.add_jobs(e2e_fixture("ingest_greenhouse.json"))[0]["job_uid"]
        w.evaluate_all()
        cyc = w.preflight("applier", AP)
        w.claim(cyc, job_uid)
        built = w.build_resume(cyc, job_uid)
        fields = []
        for label in FIELDS:
            rc, env = w.answer(cyc, job_uid, label)
            self.assertEqual(env["code"], "OK", env)
            fields.append({"label": label, "type": "text", "value": env["data"]["value"], "answer_key": env["data"]["key"]})
        pkg_uid = w.package(cyc, job_uid, built["variant_uid"], fields)
        w.end_cycle(cyc, AP)
        w.approve_all()
        url = w.one("SELECT apply_url FROM jobs WHERE job_uid = ?", job_uid)[0]
        v = {"JOB": job_uid, "DRAFT": pkg_uid, "VARIANT": built["variant_uid"], "URL": url,
             "FIELDS_JSON": json.dumps([{"label": f["label"], "value": f["value"]} for f in fields])}
        v.update({"F:" + f["label"]: f["value"] for f in fields})
        return v

    def replay(self, form_flags: str):
        v = self.prepare()
        v["FORM_FLAGS"] = form_flags
        g = self.start_guard()
        r = Replay(self, self.w, g, e2e_fixture("guard_ats_apply.json"), v)
        r.run()
        return r, v

    def test_read_form_page_flags_do_not_stop_the_session(self):
        # drivers/read_form.js (U6) reports {"captcha_visible": false, "account_wall": false} next to the observed
        # values. The guard's observer (U8) reads an allowlisted driver's flags by value and ignores key names, so
        # the key "captcha_visible" must not match the ats_captcha text signature of detect/ats.json (U1): the
        # whole cycle runs as it does without the flags and the submit after gate arm is allowed.
        r, _v = self.replay('"captcha_visible": false, "account_wall": false, ')
        blocked = [o for _t, o in r.outcomes if o != "allow"]
        self.assertEqual(blocked, ["G_NO_TOKEN", "G_UPLOAD_PATH", "G_NOT_ARMED", "G_NOT_ARMED", "G_NO_TOKEN"])
        self.assertEqual(self.w.one("SELECT status FROM actions WHERE token = ?", r.vars["TOKEN"])[0], "sent")

    def test_ats_apply_cycle_through_guard_and_core(self):
        w = self.w
        r, v = self.replay("")        # read_form output without the page flags (see the test above)
        token = r.vars["TOKEN"]
        blocked = [o for _t, o in r.outcomes if o != "allow"]
        self.assertEqual(blocked, ["G_NO_TOKEN", "G_UPLOAD_PATH", "G_NOT_ARMED", "G_NOT_ARMED", "G_NO_TOKEN"])
        self.assertTrue(w.slept, "pace wait --kind dwell drew a dwell")
        # the guard's token log (12.17): the upload and the four fields as fill, one commit click
        with open(os.path.join(paths.guard_dir(), token + ".jsonl"), "r", encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        fills = [(x["class"], x["action"], x["name"]) for x in lines if x["class"] == "fill"]
        self.assertEqual(fills, [("fill", "click", "Apply for this job"), ("fill", "upload", "Attach")] +
                         [("fill", "type", f) for f in FIELDS])
        commits = [(x["action"], x["name"]) for x in lines if x["class"] == "commit"]
        self.assertEqual(commits[-1], ("click", "Submit application"))
        self.assertLessEqual(len(commits), 2)
        for x in lines:
            self.assertEqual((x["token"], x["agent"], x["host"]), (token, AP, "boards.greenhouse.io"))
        # the core recorded the application the guard let through
        act = w.one("SELECT kind, status, platform, agent_id FROM actions WHERE token = ?", token)
        self.assertEqual(act[:], ("application", "sent", "greenhouse", AP))
        app = w.one("SELECT resume_variant_id, package_draft_id FROM applications")
        self.assertIsNotNone(app["resume_variant_id"])
        self.assertEqual(w.one("SELECT status FROM jobs WHERE job_uid = ?", v["JOB"])[0], "applied")
        self.assertEqual(w.one("SELECT removed_at IS NOT NULL FROM staged_files WHERE token = ?", token)[0], 1)
        # every exec ran as the agent the guard vouched for
        self.assertTrue(all(code in ("OK", "NOTHING_TO_DO") for _a, _rc, code in r.jh), r.jh)
        # the guard log has the decisions but no page text
        log = self.guard_log()
        codes = [e.get("code") for e in log if e.get("decision") == "block"]
        self.assertEqual(sorted(codes), sorted(blocked))
        self.assertNotIn("Thank you for applying", json.dumps(log))
        self.assertNotIn(v["F:Email"], json.dumps(log))


@unittest.skipUnless(node_major() >= 24, "needs node 24 or later (the guard plugin runs TypeScript directly)")
class TestDriverOutputIsNotAPage(unittest.TestCase):
    """The guard's observer on the output of the allowlisted read_form driver, on an ATS host and on a job board."""

    def setUp(self):
        from tests.helpers import TempHome
        self.home = TempHome(clock="2026-09-29T09:00:00Z").start()
        self.addCleanup(self.home.stop)
        auth.create_guard_key()
        h = paths.home()
        self.g = GuardBridge({"repo": paths.REPO, "python": h["python"], "homeFile": paths.home_file(),
                              "publicReadonlyAgents": ["main"]}, canon.now())
        self.addCleanup(self.g.close)

    def observe_read_form(self, url: str, captcha: bool = False, wall: bool = False) -> dict:
        session = "agent:jobhunter-applier:cron:" + url.split("/")[2]
        with open(os.path.join(paths.REPO, "drivers", "read_form.js"), "r", encoding="utf-8") as fh:
            fn = fh.read()
        for params, text in (({"action": "navigate", "targetUrl": url}, "- heading \"Data Analyst\" [level=1]"),
                             ({"action": "act", "kind": "evaluate", "fn": fn},
                              json.dumps({"observed": {"fields": [{"label": "Email", "value": "alex.rivera@example.com"}],
                                                       "resume_filename_visible": "Alex_Rivera_Resume.pdf"},
                                          "required_empty": [], "captcha_visible": captcha, "account_wall": wall,
                                          "validation_errors": []}))):
            self.assertEqual(self.g.ask({"op": "call", "agent": AP, "session": session, "tool": "browser",
                                         "params": params})["outcome"], "allow")
            out = self.g.ask({"op": "result", "agent": AP, "session": session, "tool": "browser", "params": params,
                              "result": {"content": [{"type": "text", "text": text}],
                                         "details": {"ok": True, "targetId": "t1", "url": url}}, "error": None})
        return out

    def test_read_form_flags_on_a_board_do_not_trip_the_site(self):
        # Same cause as TestAtsApplyReplay.test_read_form_page_flags_do_not_stop_the_session, worse effect: the
        # boards.json site_captcha signature trips (trip true), so the session stops and the guard runs
        # `jh.py detect --source guard`, which opens the site:naukri breaker on a clean application form.
        out = self.observe_read_form("https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-1")
        self.assertEqual((out["stopped"], out["softStopped"]), (False, False), out)

    def test_read_form_flag_values_still_stop(self):
        # The flags are read by value: a true captcha_visible on the board is site_captcha (the session stops), on
        # an ATS host it is the job-level ats_captcha soft stop; a true account_wall on an ATS host soft-stops too.
        out = self.observe_read_form("https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-1",
                                     captcha=True)
        self.assertTrue(out["stopped"], out)
        out = self.observe_read_form("https://boards.greenhouse.io/kestrelcommerce/jobs/4011", captcha=True)
        self.assertEqual((out["stopped"], out["softStopped"]), (False, True), out)
        out = self.observe_read_form("https://jobs.lever.co/kestrelcommerce/4012", wall=True)
        self.assertEqual((out["stopped"], out["softStopped"]), (False, True), out)


class TestStopPageReplay(GuardReplayBase):
    def test_stop_page_stops_the_session_and_trips_the_breaker(self):
        w = self.w
        g = self.start_guard()
        r = Replay(self, w, g, e2e_fixture("guard_stop_page.json"), {})
        r.run()
        self.assertEqual([o for _t, o in r.outcomes],
                         ["allow", "allow", "G_STOPPED", "allow", "allow", "G_STOPPED", "G_STOPPED", "allow"])
        # the guard reported the stop itself (R5); run that call against the real core as the plugin would
        calls = g.ask({"op": "jh_calls"})["calls"]
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]
        self.assertEqual((argv[0], argv[1], argv[3:]), ("detect", "--file", ["--source", "guard"]))
        stop_file = argv[2]
        self.assertTrue(stop_file.startswith(paths.guard_dir() + os.sep), stop_file)
        with open(stop_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["platform"], "linkedin")
        out = io.StringIO()
        rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
        envelope = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertIn(envelope["code"], ("OK", "E_STOP_DETECTED"), envelope)
        self.assertEqual(envelope["data"]["verdict"], "stop")
        self.assertEqual(w.one("SELECT state FROM breakers WHERE scope = 'linkedin'")[0], "open")
        sources = sorted(x[0] for x in w.all("SELECT source FROM detections WHERE verdict = 'stop'"))
        self.assertEqual(sources, ["agent", "guard"])
        self.assertEqual(w.one("SELECT ended_at IS NOT NULL FROM cycles WHERE cycle_id = ?", r.vars["CYCLE"])[0], 1)
        self.assertEqual(rc != 0, envelope["code"] != "OK")


if __name__ == "__main__":
    unittest.main()
