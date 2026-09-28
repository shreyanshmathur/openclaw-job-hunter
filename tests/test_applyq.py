"""U6 apply queue: work list order, claims with a lease, release, human sweep, site modes, CLI."""
from __future__ import annotations

import io
import json
import unittest

import tests  # noqa: F401
from jobhunter import applyq, canon, cli, db
from jobhunter.events import open_human_task
from tests.fakes.u6 import U6TestCase, deps, set_config
from tests.helpers import insert_action, insert_company, insert_draft, insert_job

CYCLE = "C20260927T050000ZAAAA"


class ApplyBase(U6TestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)

    def job(self, route="ats_form", source="greenhouse", status="eligible", **kw):
        jid = insert_job(self.conn, company_id=self.co, status=status, source=source, **kw)
        self.conn.execute("UPDATE jobs SET apply_route = ? WHERE id = ?", (route, jid))
        return jid

    def claim(self, limit=3, cycle=CYCLE):
        with db.tx(self.conn):
            return applyq.claim(self.conn, limit, cycle)

    def status(self, jid):
        return self.row("SELECT status, claimed_by FROM jobs WHERE id = ?", jid)


class TestClaim(ApplyBase):
    def test_claim_eligible_job(self):
        jid = self.job()
        self.assertEqual(applyq.work_count(self.conn), 1)
        items = self.claim()
        self.assertEqual([(i["job_uid"], i["needs"]) for i in items], [(self.uid("jobs", jid), "package")])
        self.assertEqual(tuple(self.status(jid)), ("apply_queued", CYCLE))
        self.assertEqual(self.claim(), [], "a claimed job is not handed out again while the lease runs")
        self.assertEqual(applyq.work_count(self.conn), 0)
        self.clock.advance(minutes=applyq.LEASE_MINUTES + 1)
        self.assertEqual(len(self.claim(cycle="C20260927T060000ZBBBB")), 1)

    def test_release(self):
        jid = self.job()
        self.claim()
        with db.tx(self.conn):
            applyq.release(self.conn, self.uid("jobs", jid))
        self.assertEqual(tuple(self.status(jid)), ("eligible", None))
        self.claim()
        insert_draft(self.conn, kind="application_package", status="review_pending", job_id=jid,
                     send_route="browser", channel="form", subject=None)
        with db.tx(self.conn):
            applyq.release(self.conn, self.uid("jobs", jid))
        self.assertEqual(tuple(self.status(jid)), ("apply_queued", None))
        self.assertDenied("E_NOT_FOUND", applyq.release, self.conn, "JAAAAAAA")

    def test_order_submit_revise_new(self):
        new = self.job()
        ready = self.job(status="apply_queued")
        insert_draft(self.conn, kind="application_package", status="approved", job_id=ready, send_route="browser",
                     channel="form", subject=None)
        fix = self.job(status="apply_queued")
        insert_draft(self.conn, kind="application_package", status="lint_failed", job_id=fix, send_route="browser",
                     channel="form", subject=None)
        waiting = self.job(status="apply_queued")
        insert_draft(self.conn, kind="application_package", status="review_pending", job_id=waiting,
                     send_route="browser", channel="form", subject=None)
        items = self.claim(limit=5)
        self.assertEqual([i["needs"] for i in items], ["submit", "revise", "package"])
        self.assertEqual([i["job_uid"] for i in items], [self.uid("jobs", j) for j in (ready, fix, new)])
        self.assertIsNotNone(items[0]["package_draft"])

    def test_exclusions_of_the_queue(self):
        done = self.job()
        insert_action(self.conn, kind="application", company_id=None, job_id=done)
        never = self.job()
        self.conn.execute("UPDATE jobs SET human_call = 'never' WHERE id = ?", (never,))
        skipped = self.job()
        stamp = canon.now()
        self.conn.execute("INSERT INTO target_skips (target_key, reason, until, created_at, updated_at) VALUES "
                          "(?, 'no_hook', ?, ?, ?)", ("job:" + self.uid("jobs", skipped), canon.ts_add(stamp, days=30),
                                                      stamp, stamp))
        self.assertEqual(applyq.work_count(self.conn), 0)
        ok = self.job()
        self.conn.execute("UPDATE companies SET contact_state = 'active_thread'")
        self.assertEqual(applyq.work_count(self.conn), 0)
        self.conn.execute("UPDATE companies SET contact_state = 'contacted'")
        self.assertEqual([i["job_uid"] for i in self.claim()], [self.uid("jobs", ok)])
        set_config("channels.applications.enabled", False)
        self.assertEqual(applyq.work_count(self.conn), 0)


class TestHumanSweep(ApplyBase):
    def test_human_routes_are_swept(self):
        human = self.job(route="human")
        wd = self.job(route="ats_form", source="workday")
        board_off = self.job(route="board_inapp", source="naukri")
        easy = self.job(route="easy_apply", source="linkedin_jobs")
        ok_board = self.job(route="board_inapp", source="instahyre")
        self.assertEqual(applyq.work_count(self.conn), 5)
        wl = applyq.work_list(self.conn, 10)
        self.assertEqual(wl["human_queue"], 4)
        items = self.claim(limit=5)
        self.assertEqual([i["job_uid"] for i in items], [self.uid("jobs", ok_board)])
        for jid in (human, wd, board_off, easy):
            self.assertEqual(self.status(jid)[0], "needs_human")
        self.assertEqual(self.row("SELECT count(*) FROM human_tasks WHERE kind = 'apply_manually'")[0], 4)
        self.assertEqual(applyq.work_count(self.conn), 0)

    def test_ats_human_queue_flag(self):
        jid = self.job()
        with db.tx(self.conn):
            db.meta_set(self.conn, "ats_human_queue:greenhouse", "1", "system")
        self.claim()
        self.assertEqual(self.status(jid)[0], "needs_human")

    def test_email_and_unknown_routes(self):
        mail = self.job(route="email")
        self.conn.execute("UPDATE jobs SET apply_email = 'careers@kestrel.example' WHERE id = ?", (mail,))
        unknown = self.job(route="unknown", source="hn_whoishiring")
        needs = {i["job_uid"]: i["needs"] for i in self.claim(limit=5)}
        self.assertEqual(needs, {self.uid("jobs", mail): "email", self.uid("jobs", unknown): "open_posting"})

    def test_to_human(self):
        jid = self.job()
        self.claim()
        with db.tx(self.conn):
            applyq.to_human(self.conn, self.uid("jobs", jid), "captcha_visible")
        self.assertEqual(tuple(self.status(jid)), ("needs_human", None))
        self.assertEqual(self.row("SELECT detail FROM human_tasks")[0], "captcha_visible")

    def test_answer_question_hand_over_opens_no_apply_manually(self):
        jid = self.job()
        self.claim()
        with db.tx(self.conn):
            question = open_human_task(self.conn, "answer_question", "A form asks: Favourite colour", job_id=jid)
            applyq.to_human(self.conn, self.uid("jobs", jid), "answer_question")
        self.assertEqual(tuple(self.status(jid)), ("needs_human", None))
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT task_uid, kind FROM human_tasks")],
                         [(question, "answer_question")], "U4 opened the only task the person needs")

    def test_reconcile_tasks_count(self):
        deps.RECONCILE_TASKS.append({"token": "TAAAAAAAAAAA", "kind": "application", "method": "ats_page"})
        self.assertEqual(applyq.work_count(self.conn), 1)
        self.assertEqual(len(applyq.work_list(self.conn, 5)["reconcile"]), 1)


class TestReturnFromHuman(ApplyBase):
    def parked(self, reason="answer_question"):
        """An eligible job claimed, then handed to the person with an open answer_question task."""
        jid = self.job()
        self.claim()
        with db.tx(self.conn):
            task = open_human_task(self.conn, "answer_question", "A form asks: Favourite colour", job_id=jid)
            applyq.to_human(self.conn, self.uid("jobs", jid), reason)
        return jid, task

    def back(self, jid, reason="answered", by="human:chat"):
        with db.tx(self.conn):
            return applyq.return_from_human(self.conn, self.uid("jobs", jid), reason, by)

    def close(self, task_uid):
        self.conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'answered' WHERE task_uid = ?",
                          (canon.now(), task_uid))

    def test_waits_for_open_questions_then_returns_to_the_queue(self):
        jid, task = self.parked()
        res = self.back(jid)
        self.assertEqual((res["returned"], res["status"], res["open_tasks"]), (False, "needs_human", [task]))
        self.assertEqual(self.status(jid)[0], "needs_human")
        self.close(task)
        res = self.back(jid)
        self.assertEqual((res["returned"], res["status"], res["closed_tasks"]), (True, "eligible", []))
        self.assertEqual(tuple(self.row("SELECT status, status_reason, claimed_by FROM jobs WHERE id = ?", jid)),
                         ("eligible", "answered", None))
        self.assertEqual(applyq.work_count(self.conn), 1)
        self.assertEqual([i["job_uid"] for i in self.claim(cycle="C20260927T060000ZBBBB")], [self.uid("jobs", jid)])

    def test_three_argument_call_used_by_u4(self):
        jid, task = self.parked()
        self.close(task)
        with db.tx(self.conn):
            res = applyq.return_from_human(self.conn, self.uid("jobs", jid), "answered")
        self.assertTrue(res["returned"])
        self.assertEqual(self.status(jid)[0], "eligible")

    def test_closes_the_duplicate_apply_manually_task_of_the_same_hand_over(self):
        jid, task = self.parked()
        with db.tx(self.conn):   # the task an older to_human opened for the answer_question hand-over
            dup = open_human_task(self.conn, "apply_manually", "Apply by hand: Data Analyst at Kestrel.", job_id=jid,
                                  detail="answer_question")
        self.close(task)
        res = self.back(jid)
        self.assertEqual((res["returned"], res["closed_tasks"]), (True, [dup]))
        self.assertEqual(tuple(self.row("SELECT resolution FROM human_tasks WHERE task_uid = ?", dup)),
                         ("returned_to_queue",))
        self.assertEqual(self.row("SELECT count(*) FROM human_tasks WHERE done_at IS NULL")[0], 0)

    def test_other_open_tasks_keep_the_job_with_the_person(self):
        jid, task = self.parked()
        with db.tx(self.conn):   # a hand-over by someone else (job set-status): the person applies by hand
            other = open_human_task(self.conn, "apply_manually", "Apply yourself (account required)", job_id=jid,
                                    detail="https://job-boards.greenhouse.io/example/jobs/1")
        self.close(task)
        res = self.back(jid)
        self.assertEqual((res["returned"], res["open_tasks"]), (False, [other]))
        self.assertEqual(self.status(jid)[0], "needs_human")
        self.assertIsNone(self.row("SELECT done_at FROM human_tasks WHERE task_uid = ?", other)[0])

    def test_captcha_hand_over_is_returned_only_with_its_task_closed_by_the_call(self):
        jid = self.job()
        self.claim()
        with db.tx(self.conn):
            applyq.to_human(self.conn, self.uid("jobs", jid), "captcha_visible")
        manual = self.row("SELECT task_uid FROM human_tasks WHERE kind = 'apply_manually'")[0]
        res = self.back(jid, reason="human_retry", by="human:cli")
        self.assertEqual((res["returned"], res["closed_tasks"]), (True, [manual]))

    def test_jobs_not_waiting_are_left_alone(self):
        jid = self.job()
        res = self.back(jid)
        self.assertEqual((res["returned"], res["status"]), (False, "eligible"))
        applied = self.job(status="apply_queued")
        self.conn.execute("UPDATE jobs SET status = 'applying' WHERE id = ?", (applied,))
        self.assertEqual(self.back(applied)["status"], "applying")
        self.assertDenied("E_NOT_FOUND", applyq.return_from_human, self.conn, "JAAAAAAA", "answered", "human:chat")


class TestApplyCli(ApplyBase):
    def run_cli(self, argv):
        out = io.StringIO()
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-applier"}
        rc = cli.main(argv, env=env, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_next_and_release(self):
        jid = self.job()
        rc, out = self.run_cli(["--cycle", CYCLE, "apply", "next", "--limit", "2"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["data"]["items"][0]["job_uid"], self.uid("jobs", jid))
        rc, out = self.run_cli(["apply", "next"])
        self.assertEqual((rc, out["code"]), (0, "NOTHING_TO_DO"))
        rc, out = self.run_cli(["apply", "release", "--job", self.uid("jobs", jid)])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status(jid)[0], "eligible")

    def test_outreach_agent_may_not_claim(self):
        out = io.StringIO()
        rc = cli.main(["apply", "next"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-outreach"},
                      stdin=io.StringIO(""), stdout=out)
        self.assertEqual((rc, json.loads(out.getvalue())["code"]), (11, "E_CALLER_NOT_ALLOWED"))


if __name__ == "__main__":
    unittest.main()
