"""U2: evaluator packets, scorecard validation and verdicts, requeue, stats and the feasibility monitor."""
from __future__ import annotations

import copy
import io
import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import canon, cli, db, evaluate, jobs, jobstate, paths
from tests.fakes.u2 import install, templates
from tests.fakes.u2.agentrun import agent_call
from tests.helpers import HomeTestCase

CYCLE = "C20260927T050000ZAAAA"
JD = ("Kestrel Commerce is hiring a Data Analyst for the returns team in Bengaluru. "
      "2-4 years of experience with SQL and Python for forecasting. Experience with dbt is a plus. "
      "Build dashboards in Tableau for regional teams.")


def scorecard(uid, **over):
    sc = {
        "job_uid": uid,
        "gates": {"must_have_missing": [], "years_gap": False, "location_incompatible": False,
                  "comp_below_floor": False, "role_family_excluded": False, "requires_account_creation": False,
                  "role_closed": False},
        "criteria": {
            "role_family": {"score": 5, "evidence": "Title matches Data analytics"},
            "skills": {"score": 4, "matches": [{"jd_quote": "SQL and  python for forecasting", "fact_id": "P1"}],
                       "gaps": [{"jd_quote": "Experience with dbt is a plus", "note": "not in profile"}]},
            "seniority": {"score": 4, "evidence": "2 to 4 years; profile has 3"},
            "domain": {"score": 4, "evidence": "e-commerce returns"},
            "location": {"score": 5, "evidence": "Bengaluru"},
            "compensation": {"score": 3, "evidence": "not disclosed"},
            "company": {"score": 3, "evidence": "unknown stage"}},
        "reason_text": "Forecasting with SQL and Python matches the returns work; dbt is a gap.",
        "must_have_quotes": [],
        "model": "example/model-1",
    }
    for k, v in over.items():
        sc[k] = v
    return sc


class EvalCase(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.deps = install(self)
        self.uids = self.add_jobs(3)

    def add_jobs(self, n, title="Data Analyst", start=100):
        items = [{"source_url": "https://www.naukri.com/job-listings-data-analyst-kestrel-%d-2509260%05d" % (i, i),
                  "company": "Kestrel Commerce %d" % i, "title": title, "location": "Bengaluru",
                  "work_mode": "hybrid", "posted_at": "2026-09-2%d" % (i % 6), "jd_text": JD,
                  "native_ids": {"board_job_id": "2509260%05d" % i}} for i in range(start, start + n)]
        with db.tx(self.conn):
            res = jobs.ingest(self.conn, {"source": "naukri", "jobs": items}, None, "browser")
        return [r["job_uid"] for r in res]

    def status(self, uid):
        return self.conn.execute("SELECT status FROM jobs WHERE job_uid = ?", (uid,)).fetchone()[0]

    def claim(self, limit=15, cycle=CYCLE):
        with db.tx(self.conn):
            return evaluate.claim(self.conn, limit, cycle)

    def record(self, uid, sc):
        with db.tx(self.conn):
            return evaluate.record(self.conn, uid, sc)


class TestClaim(EvalCase):
    def test_packets_and_brief(self):
        packets = self.claim(2)
        self.assertEqual(len(packets), 2)
        for p in packets:
            self.assertEqual(self.status(p["job_uid"]), "evaluating")
            self.assertEqual(os.path.dirname(p["packet_path"]), paths.work_dir("evaluator", CYCLE))
            with open(p["packet_path"]) as fh:
                pk = json.load(fh)
            self.assertEqual(pk["job_uid"], p["job_uid"])
            self.assertEqual(pk["jd_text"], JD)
            self.assertEqual(pk["profile_version"], "pv-test-1")
            self.assertIn("untrusted", pk["jd_note"])
            with open(pk["brief_path"]) as fh:
                brief = fh.read()
            self.assertIn("P1: Built SQL and Python forecasting", brief)
            self.assertIn("Data analytics: titles Data Analyst, Business Analyst", brief)
            self.assertNotIn("{{", brief)
            brief.encode("ascii")
        row = self.conn.execute("SELECT claimed_by, claimed_until FROM jobs WHERE job_uid = ?",
                                (packets[0]["job_uid"],)).fetchone()
        self.assertEqual(row["claimed_by"], CYCLE)
        self.assertEqual(row["claimed_until"], canon.ts_add(canon.now(), minutes=30))

    def test_batch_size_and_other_cycles(self):
        self.deps.cfg["evaluator"]["batch_size"] = 2
        self.assertEqual(len(self.claim(15)), 2)
        self.assertEqual(len(self.claim(15, "C20260927T050100ZBBBB")), 1)
        self.assertDenied("E_CLAIMED", self.claim, 15, "C20260927T050200ZCCCC")
        self.clock.advance(minutes=31)                      # leases expire: jobs return to the queue
        self.assertEqual(len(self.claim(15, "C20260927T053200ZDDDD")), 2)

    def test_nothing_to_do_and_unconfirmed(self):
        self.claim(15)
        self.clock.advance(minutes=1)
        with db.tx(self.conn):
            self.assertDenied("E_CLAIMED", evaluate.claim, self.conn, 5, "C20260927T050100ZBBBB")
        self.deps.profile = copy.deepcopy(self.deps.profile)
        self.deps.profile["confirmed"] = False
        with db.tx(self.conn):
            self.assertDenied("E_PROFILE_UNCONFIRMED", evaluate.claim, self.conn, 5, CYCLE)

    def test_release_and_never(self):
        uid = self.claim(1)[0]["job_uid"]
        with db.tx(self.conn):
            evaluate.release(self.conn, uid)
            evaluate.release(self.conn, uid)     # no-op on a queued job
        self.assertEqual(self.status(uid), "eval_queued")
        with db.tx(self.conn):
            jobs.set_human_call(self.conn, uid, "never", "human:cli")
        claimed = [p["job_uid"] for p in self.claim(15)]
        self.assertNotIn(uid, claimed)


class TestRecord(EvalCase):
    def setUp(self):
        super().setUp()
        self.claim(15)

    def test_good_fit(self):
        uid = self.uids[0]
        res = self.record(uid, scorecard(uid))
        self.assertEqual((res["score"], res["verdict"], res["status"], res["clamped"]), (84, "apply", "eligible",
                                                                                         False))
        self.assertEqual(self.status(uid), "eligible")
        ev = self.conn.execute("SELECT * FROM evaluations e JOIN jobs j ON j.id = e.job_id WHERE j.job_uid = ?",
                               (uid,)).fetchone()
        self.assertEqual((ev["stage"], ev["score"], ev["reason_code"], ev["model"]), ("llm", 84, "fit_good",
                                                                                      "example/model-1"))
        self.assertIsNone(ev["claimed_by"])
        stored = json.loads(ev["scorecard_json"])
        self.assertEqual(stored["code"]["criteria_after_checks"]["skills"], 4)

    def test_invented_quote_and_fact_clamp(self):
        uid = self.uids[0]
        sc = scorecard(uid)
        sc["criteria"]["skills"]["matches"].append({"jd_quote": "ten years of Spark", "fact_id": "P1"})
        sc["criteria"]["skills"]["matches"].append({"jd_quote": "Tableau for regional teams", "fact_id": "P9"})
        res = self.record(uid, sc)
        self.assertTrue(res["clamped"])
        self.assertEqual(res["score"], 74)
        self.assertEqual(res["status"], "eligible")
        kinds = sorted(f["kind"] for f in res["evidence_failures"])
        self.assertEqual(kinds, ["quote_not_in_jd", "unknown_fact_id"])

    def test_gates(self):
        a, b, c = self.uids
        sc = scorecard(a)
        sc["gates"]["must_have_missing"] = [{"jd_quote": "Build dashboards in Tableau", "why": "none"}]
        res = self.record(a, sc)
        self.assertEqual((res["verdict"], res["status"], res["gates_failed"]), ("skip", "rejected",
                                                                                ["must_have_missing"]))
        sc = scorecard(b)
        sc["gates"]["must_have_missing"] = [{"jd_quote": "a valid pilot license", "why": "invented"}]
        res = self.record(b, sc)
        self.assertEqual((res["verdict"], res["status"]), ("apply", "eligible"))   # unverifiable gate dropped
        self.assertTrue(res["clamped"])
        sc = scorecard(c)
        sc["gates"]["requires_account_creation"] = True
        res = self.record(c, sc)
        self.assertEqual((res["verdict"], res["status"]), ("human_only", "needs_human"))
        self.assertTrue(res["task_uid"])

    def test_thresholds(self):
        a, b = self.uids[:2]
        sc = scorecard(a)
        for k in sc["criteria"]:
            sc["criteria"][k]["score"] = 3               # 60: borderline
        self.assertEqual(self.record(a, sc)["status"], "borderline")
        sc = scorecard(b)
        for k in sc["criteria"]:
            sc["criteria"][k]["score"] = 2               # 40: skip
        res = self.record(b, sc)
        self.assertEqual((res["verdict"], res["status"]), ("skip", "rejected"))
        self.deps.cfg["evaluator"]["thresholds"] = {"apply": 10, "borderline": 5}   # floors 60 and 45 still apply
        self.assertEqual(evaluate.thresholds(self.deps.cfg), {"apply": 60, "borderline": 45})

    def test_human_call_overrides_score(self):
        a, b = self.uids[:2]
        with db.tx(self.conn):
            jobs.set_human_call(self.conn, a, "apply_anyway", "human:sheet")
        sc = scorecard(a)
        for k in sc["criteria"]:
            sc["criteria"][k]["score"] = 1
        self.assertEqual(self.record(a, sc)["status"], "eligible")
        with db.tx(self.conn):
            self.conn.execute("UPDATE jobs SET human_call = 'never' WHERE job_uid = ?", (b,))
        self.assertEqual(self.record(b, scorecard(b))["status"], "closed")

    def test_schema_errors(self):
        uid = self.uids[0]
        bad = scorecard(uid, extra=1)
        self.assertDenied("E_SCHEMA", self.record, uid, bad)
        bad = scorecard(uid)
        bad["criteria"]["skills"]["score"] = 7
        d = self.assertDenied("E_SCHEMA", self.record, uid, bad)
        self.assertIn("criteria.skills.score must be an integer from 0 to 5", d.data["errors"])
        bad = scorecard(uid)
        del bad["gates"]["role_closed"]
        self.assertDenied("E_SCHEMA", self.record, uid, bad)
        self.assertDenied("E_SCHEMA", self.record, uid, scorecard(self.uids[1]))
        self.assertDenied("E_SCHEMA", self.record, uid, scorecard(uid, reason_text="x" * 500))
        self.assertDenied("E_NOT_FOUND", self.record, "JAAAAAAA", scorecard("JAAAAAAA"))
        self.record(uid, scorecard(uid))
        self.assertDenied("E_PRECONDITION", self.record, uid, scorecard(uid))

    def test_score_rounding(self):
        w = evaluate.weights({})
        self.assertEqual(evaluate.score_of({k: 5 for k in evaluate.CRITERIA}, w), 100)
        self.assertEqual(evaluate.score_of({k: 0 for k in evaluate.CRITERIA}, w), 0)
        odd = {"role_family": 3, "skills": 3, "seniority": 3, "domain": 3, "location": 3, "compensation": 3,
               "company": 4}
        self.assertEqual(evaluate.score_of(odd, w), 61)    # 60.99... rounds to 61


class TestRequeueStats(EvalCase):
    def test_requeue_one_and_since_profile_change(self):
        self.claim(15)
        a = self.uids[0]
        sc = scorecard(a)
        for k in sc["criteria"]:
            sc["criteria"][k]["score"] = 1
        self.record(a, sc)
        self.assertEqual(self.status(a), "rejected")
        with db.tx(self.conn):
            self.assertEqual(evaluate.requeue(self.conn, job_uid=a), 1)
            self.assertDenied("E_USAGE", evaluate.requeue, self.conn)
        self.assertEqual(self.status(a), "eval_queued")
        # a pre-filter rejection that passes under a changed profile is queued again
        rej = self.add_jobs(1, title="Director of Analytics", start=300)[0]
        self.assertEqual(self.status(rej), "prefilter_rejected")
        prof = copy.deepcopy(self.deps.profile)
        prof["profile_version"] = "pv-test-2"
        prof["fields"]["seniority"]["value"]["max"] = "director"
        prof["fields"]["role_families"]["value"][0]["exclude"] = ["intern", "sales"]
        self.deps.profile = prof
        with db.tx(self.conn):
            n = evaluate.requeue(self.conn, since_profile_change=True)
        self.assertEqual(n, 1)
        self.assertEqual(self.status(rej), "eval_queued")

    def test_stats_and_feasibility(self):
        self.deps.cfg["evaluator"]["feasibility_alert"] = {"min_evaluations": 5, "gate_share": 0.40}
        many = self.add_jobs(5, title="Data Analyst", start=400)
        with db.tx(self.conn):
            for uid in many:
                jid = self.conn.execute("SELECT id FROM jobs WHERE job_uid = ?", (uid,)).fetchone()[0]
                jobstate.set_job_status(self.conn, jid, "evaluating", "t", "test")
                jobstate.set_job_status(self.conn, jid, "rejected", "years_gap", "test")
                self.conn.execute("INSERT INTO evaluations (job_id, stage, score, verdict, reason_code, reason_text, "
                                  "gates_failed, profile_version, evaluated_at, updated_at) VALUES (?, 'llm', 40, "
                                  "'skip', 'years_gap', 'x', '[\"years_gap\"]', 'pv-test-1', ?, ?)",
                                  (jid, canon.now(), canon.now()))
        st = evaluate.stats(self.conn, 14)
        self.assertEqual(st["rejections"], 5)
        self.assertEqual(st["by_reason"]["years_gap"], {"n": 5, "share": 1.0})
        self.assertEqual(st["by_gate"]["years_gap"]["n"], 5)
        with db.tx(self.conn):
            flagged = evaluate.feasibility_check(self.conn, self.deps.cfg)
            again = evaluate.feasibility_check(self.conn, self.deps.cfg)
        self.assertEqual(flagged, ["years_gap"])
        self.assertEqual(again, ["years_gap"])
        tasks = self.conn.execute("SELECT count(*) FROM human_tasks WHERE kind = 'relax_gate'").fetchone()[0]
        notes = self.conn.execute("SELECT count(*) FROM notifications WHERE dedupe_key LIKE 'feasibility:%'"
                                  ).fetchone()[0]
        self.assertEqual((tasks, notes), (1, 1))


class TestEvalCommands(EvalCase):
    def run_cli(self, argv, agent="jobhunter-evaluator"):
        if agent:
            # a guarded agent call: python -I, argv and env proof for one session (helpers of U1)
            return agent_call(agent, argv, deps=self.deps)
        out = io.StringIO()
        rc = cli.main(argv, env={"PATH": "/usr/bin"}, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_next_record_stats(self):
        rc, env = self.run_cli(["eval", "next", "--limit", "2"])
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))       # no cycle given and none running
        rc, env = self.run_cli(["--cycle", CYCLE, "eval", "next", "--limit", "2"])
        self.assertEqual(rc, 0, env)
        packets = env["data"]["packets"]
        self.assertEqual(len(packets), 2)
        uid = packets[0]["job_uid"]
        path = self.home.write_agent_file("evaluator", "%s/%s.scorecard.json" % (CYCLE, uid),
                                          json.dumps(scorecard(uid)))
        rc, env = self.run_cli(["--cycle", CYCLE, "eval", "record", "--job", uid, "--file", path])
        self.assertEqual((rc, env["data"]["verdict"]), (0, "apply"), env)
        rc, env = self.run_cli(["eval", "release", "--job", packets[1]["job_uid"]])
        self.assertEqual(rc, 0)
        rc, env = self.run_cli(["eval", "stats", "--days", "7"])
        self.assertEqual(env["data"]["stats"]["by_stage"]["llm"], {"apply": 1})
        rc, env = self.run_cli(["eval", "requeue", "--job", uid], agent=None)
        self.assertEqual((rc, env["data"]["requeued"]), (0, 1))
        rc, env = self.run_cli(["eval", "requeue", "--job", uid])
        self.assertNotEqual(rc, 0)   # agents may not requeue


class TestEvalNothingWaiting(EvalCase):
    def add_jobs(self, n, title="Data Analyst", start=100):
        return []                                                # no job is waiting for evaluation

    def test_nothing_to_do_says_cycle_done(self):
        rc, env = agent_call("jobhunter-evaluator", ["--cycle", CYCLE, "eval", "next", "--limit", "2"],
                             deps=self.deps)
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"), env)
        self.assertEqual(env["next"], "run cycle end and reply CYCLE_DONE")
        self.assertNotIn("NO_REPLY", json.dumps(env))


class TestEvaluatorTemplate(unittest.TestCase):
    def test_tools_paragraph_and_final_word(self):
        text = templates.check_agents_template(self, "evaluator", browser=False)
        flat = templates.flat(text)
        self.assertIn("read the packet with the read tool, write the scorecard to the packet's `scorecard_path` "
                      "with the write tool", flat)
        self.assertIn("follow skill `jobhunter-profile` only", flat)
        self.assertIn("`__WS__/work/probe/` during a tool check", flat)

    def test_evaluate_skill_wording(self):
        skill = templates.read("evaluator", "skills", "jobhunter-evaluate", "SKILL.template.md")
        lines = skill.splitlines()
        step7 = [ln for ln in lines if ln.startswith("7. ")]
        self.assertEqual(step7, ["7. Write the scorecard JSON to `scorecard_path` with the write tool (whole file). "
                                 "Only the keys shown in the brief."])
        flat = templates.flat(skill)
        self.assertIn("1. Read the packet with the read tool, at the absolute path `eval next` gave you.", flat)
        self.assertIn("fix the file once (there is no edit tool: write the whole file again) and record again", flat)
        self.assertNotIn("NO_REPLY", skill)
        self.assertNotIn("~/", skill)
        self.assertNotIn("--agent-proof", skill)
        skill.encode("ascii")


if __name__ == "__main__":
    unittest.main()
