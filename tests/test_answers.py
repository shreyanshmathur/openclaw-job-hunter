"""U4: application form answer bank, "never invent" (design 6.5, 12.20)."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import answers as A
from jobhunter import cli, db, jobstate
from jobhunter import profile as P
from jobhunter import resume as R
from jobhunter.errors import Denied
from tests.helpers import HomeTestCase, insert_job
from tests.test_profile import REQUIRED_ANSWERS, fixture


_REAL_FROM_HUMAN = A._from_human


def q(label, field_type="text", choices=None, job_uid=None):
    out = {"label": label, "field_type": field_type}
    if choices is not None:
        out["choices"] = choices
    if job_uid:
        out["job_uid"] = job_uid
    return out


class BankCase(HomeTestCase):
    confirm = True

    def setUp(self):
        super().setUp()
        p = mock.patch.object(R, "full_config", return_value={})
        p.start()
        self.addCleanup(p.stop)
        with db.tx(self.conn):
            P.record_salary(self.conn, fixture("salary.json"))
            P.record_inference(self.conn, fixture("inference.json"))
        if self.confirm:
            for qid, value in REQUIRED_ANSWERS:
                with db.tx(self.conn):
                    P.answer(self.conn, qid, value)


class TestLookup(BankCase):
    def test_bank_is_seeded_from_the_profile_and_resume(self):
        bank = {e["key"]: e for e in A.load()["answers"]}
        self.assertEqual(bank["notice_period_days"]["value"], "30")
        self.assertEqual(bank["total_experience_years"]["value"], "3")
        self.assertEqual(bank["expected_ctc"]["value"], "72000")
        self.assertEqual(bank["full_name"]["value"], "Alex Rivera")
        self.assertEqual(bank["full_name"]["source"], "resume")
        self.assertEqual(bank["linkedin_url"]["value"], "https://www.linkedin.com/in/example-alex")
        self.assertEqual(bank["willing_to_relocate"]["value"], "No")
        self.assertEqual(bank["current_ctc"]["value"], "")
        self.assertEqual(os.stat(A.bank_path()).st_mode & 0o777, 0o600)

    def test_found(self):
        r = A.lookup(q("Notice period (days)*", "number"))
        self.assertEqual((r["found"], r["key"], r["value"], r["source"]), (True, "notice_period_days", "30",
                                                                           "user_confirmed"))
        r = A.lookup(q("Full name"))
        self.assertEqual((r["found"], r["value"]), (True, "Alex Rivera"))
        r = A.lookup(q("Email address:"))
        self.assertEqual(r["value"], "alex.rivera@example.com")
        r = A.lookup(q("LinkedIn Profile URL (optional)"))
        self.assertEqual(r["key"], "linkedin_url")
        r = A.lookup(q("Expected CTC"))
        self.assertTrue(r["found"])
        self.assertTrue(r["sensitive"])

    def test_choices(self):
        r = A.lookup(q("Notice period", "choice", ["Immediate", "15 days", "30 days", "60 days"]))
        self.assertEqual((r["found"], r["value"]), (True, "30 days"))
        r = A.lookup(q("Notice period", "choice", ["Immediate", "1 month", "2 months"]))
        self.assertEqual((r["found"], r["reason"]), (False, "choice_mismatch"))
        r = A.lookup(q("Are you willing to relocate?", "choice", ["Yes", "No"]))
        self.assertEqual(r["value"], "No")
        r = A.lookup(q("Are you willing to relocate?", "boolean"))
        self.assertEqual(r["value"], "No")

    def test_types(self):
        r = A.lookup(q("Full name", "number"))
        self.assertEqual((r["found"], r["reason"]), (False, "type_mismatch"))
        r = A.lookup(q("Full name", "date"))
        self.assertEqual((r["found"], r["reason"]), (False, "type_mismatch"))

    def test_never_stored_labels_always_go_to_the_person(self):
        for label, rule in (("Date of Birth", "date_of_birth"), ("Home address line 1", "home_address"),
                            ("PIN code", "home_address"), ("Passport number", "government_id"),
                            ("PAN", "government_id"), ("Create a password", "password"),
                            ("I certify that the information above is true", "attestation"),
                            ("Username", "account_creation")):
            r = A.lookup(q(label))
            self.assertFalse(r["found"], label)
            self.assertEqual((r["reason"], r["rule"]), ("always_human", rule), label)

    def test_missing_and_ambiguous(self):
        self.assertEqual(A.lookup(q("Favourite colour"))["reason"], "no_match")
        self.assertEqual(A.lookup(q("Current salary"))["reason"], "empty_value")
        self.assertTrue(A.lookup(q("Current salary"))["sensitive"])
        r = A.lookup(q("GitHub or portfolio link"))
        self.assertEqual(r["value"], "https://github.com/example-alex")     # the empty portfolio entry is ignored
        A.add("portfolio_url", "https://alex.example.com")
        r = A.lookup(q("GitHub or portfolio link"))
        self.assertEqual((r["found"], r["reason"]), (False, "ambiguous"))

    def test_expected_pay_only_when_allowed(self):
        with db.tx(self.conn):
            P.answer(self.conn, "Q8", "no")
        r = A.lookup(q("Expected salary"))
        self.assertEqual((r["found"], r["reason"]), (False, "empty_value"))

    def test_unconfirmed_source_is_never_served(self):
        bank = A.load()
        bank["answers"].append({"key": "visa_type", "patterns": ["visa type"], "value": "H1B", "source": "inferred",
                                "sensitive": False})
        A.save(bank)
        self.assertEqual(A.lookup(q("Visa type"))["reason"], "unconfirmed_source")

    def test_question_schema(self):
        for bad in ({"label": ""}, {"label": "x", "field_type": "slider"}, {"label": "x", "extra": 1},
                    {"label": "x", "field_type": "choice"}, {"label": "x", "job_uid": "nope"}):
            with self.assertRaises(Denied) as cm:
                A.lookup(bad)
            self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_add(self):
        res = A.add("start_date_note", "Available after the notice period", patterns=["start date"])
        self.assertEqual(res["patterns"], ["start date"])
        self.assertEqual(A.lookup(q("Preferred start date"))["value"], "Available after the notice period")
        for bad in (("Bad Key", "x", None), ("k1", "", None), ("k1", "x", ["("]), ("k1", "a " + chr(0x2014) + " b", None)):
            with self.assertRaises(Denied):
                A.add(*bad)

    def test_person_entries_are_not_overwritten_by_profile_sync(self):
        A.add("notice_period_days", "45")
        with db.tx(self.conn):
            P.answer(self.conn, "Q10", "30 days")
        self.assertEqual(A.lookup(q("Notice period"))["value"], "45")

    def test_eeo_topics(self):
        self.assertEqual(A.lookup(q("Gender"))["reason"], "empty_value")
        with db.tx(self.conn):
            P.answer(self.conn, "Q21", "gender: Prefer not to say; veteran: No")
        r = A.lookup(q("Gender"))
        self.assertEqual((r["found"], r["value"], r["sensitive"]), (True, "Prefer not to say", True))


class TestHumanLoop(BankCase):
    def setUp(self):
        super().setUp()
        self.job_id = insert_job(self.conn, status="eligible")
        self.job_uid = self.conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0]
        self.handed = []
        p = mock.patch.object(A, "_to_human", side_effect=lambda conn, uid, reason: self.handed.append((uid, reason))
                              or True)
        p.start()
        self.addCleanup(p.stop)
        self.returned = []
        p = mock.patch.object(A, "_from_human", side_effect=self._record_return)
        p.start()
        self.addCleanup(p.stop)

    def _record_return(self, conn, uid, reason):
        self.returned.append((uid, reason, conn.in_transaction))
        return True

    def park(self, reason="answer_question"):
        """What U6 applyq.to_human does to the job (the real one is patched out here)."""
        with db.tx(self.conn):
            jobstate.set_job_status(self.conn, self.job_id, "needs_human", reason, "applyq")

    def status(self):
        return self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0]

    def test_question_goes_to_the_person_and_the_answer_is_reused(self):
        question = q("Do you have a valid driving licence for two-wheelers?", "choice", ["Yes", "No"], self.job_uid)
        self.assertFalse(A.lookup(question)["found"])
        with db.tx(self.conn):
            t = A.request_human(self.conn, question, "no_match")
        self.assertTrue(t["job_to_human"])
        self.assertEqual(self.handed, [(self.job_uid, "answer_question")])
        task = self.conn.execute("SELECT * FROM human_tasks WHERE task_uid = ?", (t["human_task"],)).fetchone()
        self.assertEqual(task["kind"], "answer_question")
        self.assertEqual(task["job_id"], self.job_id)
        self.assertIn("driving licence", task["question"])
        self.assertEqual(json.loads(task["detail"])["choices"], ["Yes", "No"])
        with db.tx(self.conn):
            again = A.request_human(self.conn, question, "no_match")
        self.assertEqual(again["human_task"], t["human_task"])
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                P.answer(self.conn, t["human_task"], "Maybe")
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        with db.tx(self.conn):
            res = P.answer(self.conn, t["human_task"], "Yes", sensitive_ok=False, by="chat")
        self.assertEqual(res["stored_value"], "Yes")
        # the job never reached needs_human (hand-off patched out), so there is nothing to give back
        self.assertFalse(res["job_released"])
        self.assertEqual(self.returned, [])
        self.assertIsNotNone(self.conn.execute("SELECT done_at FROM human_tasks WHERE task_uid = ?",
                                               (t["human_task"],)).fetchone()[0])
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                P.answer(self.conn, t["human_task"], "No")
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        r = A.lookup(question)
        self.assertEqual((r["found"], r["value"], r["source"]), (True, "Yes", "user_confirmed"))
        # U4 never writes job statuses itself (design 2.5)
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0],
                         "eligible")

    def test_answering_the_last_question_gives_the_job_back(self):
        q1 = q("Do you hold a two-wheeler licence?", "choice", ["Yes", "No"], self.job_uid)
        q2 = q("Are you willing to work weekend shifts?", "choice", ["Yes", "No"], self.job_uid)
        with db.tx(self.conn):
            t1 = A.request_human(self.conn, q1, "no_match")
            t2 = A.request_human(self.conn, q2, "no_match")
        self.park()
        with db.tx(self.conn):
            r1 = P.answer(self.conn, t1["human_task"], "Yes", sensitive_ok=False, by="chat")
        self.assertFalse(r1["job_released"])        # q2 is still open
        self.assertEqual(self.returned, [])
        with db.tx(self.conn):
            r2 = P.answer(self.conn, t2["human_task"], "No", sensitive_ok=False, by="chat")
        self.assertTrue(r2["job_released"])
        # through U6's single writer, inside the same transaction as the answer
        self.assertEqual(self.returned, [(self.job_uid, "answered", True)])
        self.assertEqual(self.status(), "needs_human")   # U4 itself never writes the status

    def test_job_parked_for_another_reason_stays_with_the_person(self):
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour", job_uid=self.job_uid), "no_match")
        self.park("captcha_visible")
        with db.tx(self.conn):
            res = P.answer(self.conn, t["human_task"], "Blue", by="chat")
        self.assertFalse(res["job_released"])
        self.assertEqual(self.returned, [])

    def test_an_always_human_question_keeps_the_job_parked(self):
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour", job_uid=self.job_uid), "no_match")
            A.request_human(self.conn, q("Date of birth", job_uid=self.job_uid), "always_human")
        self.park()
        with db.tx(self.conn):
            res = P.answer(self.conn, t["human_task"], "Blue", by="chat")
        self.assertFalse(res["job_released"])
        self.assertEqual(self.returned, [])

    def test_question_without_a_job_releases_nothing(self):
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour"), "no_match")
        with db.tx(self.conn):
            res = P.answer(self.conn, t["human_task"], "Blue", by="chat")
        self.assertFalse(res["job_released"])
        self.assertEqual(self.returned, [])

    def test_a_failed_release_rolls_the_answer_back(self):
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour", job_uid=self.job_uid), "no_match")
        self.park()
        with mock.patch.object(A, "_from_human", side_effect=Denied("E_BAD_TRANSITION", "no")):
            with self.assertRaises(Denied):
                with db.tx(self.conn):
                    P.answer(self.conn, t["human_task"], "Blue", by="chat")
        self.assertIsNone(self.conn.execute("SELECT done_at FROM human_tasks WHERE task_uid = ?",
                                            (t["human_task"],)).fetchone()[0])
        with db.tx(self.conn):
            res = P.answer(self.conn, t["human_task"], "Blue", by="chat")
        self.assertTrue(res["job_released"])

    def test_release_calls_u6_by_job_uid(self):
        from jobhunter import applyq
        calls = []
        with mock.patch.object(applyq, "return_from_human", create=True,
                               side_effect=lambda conn, uid, reason: calls.append((uid, reason))):
            self.assertTrue(_REAL_FROM_HUMAN(self.conn, self.job_uid, "answered"))
        self.assertEqual(calls, [(self.job_uid, "answered")])

    def test_missing_u6_function_is_not_an_error(self):
        with mock.patch.dict(sys.modules, {"jobhunter.applyq": None}):
            self.assertFalse(_REAL_FROM_HUMAN(self.conn, self.job_uid, "answered"))

    def test_job_in_flight_is_not_handed_over(self):
        self.conn.execute("UPDATE jobs SET status = 'apply_queued' WHERE id = ?", (self.job_id,))
        self.conn.execute("UPDATE jobs SET status = 'applying' WHERE id = ?", (self.job_id,))
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour", job_uid=self.job_uid), "no_match")
        self.assertFalse(t["job_to_human"])
        self.assertEqual(self.handed, [])

    def test_sensitive_task_answer_needs_the_terminal(self):
        question = q("What are your salary expectations for this role?", job_uid=self.job_uid)
        with db.tx(self.conn):
            t = A.request_human(self.conn, question, "no_match")
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                P.answer(self.conn, t["human_task"], "80000", sensitive_ok=False, by="chat")
        self.assertEqual(cm.exception.code, "E_HUMAN_ONLY")

    def test_always_human_answers_are_never_stored(self):
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Date of birth", job_uid=self.job_uid), "always_human")
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                P.answer(self.conn, t["human_task"], "1990-01-01")
        self.assertEqual(cm.exception.code, "E_VALIDATION")


def _real_applyq() -> bool:
    try:
        from jobhunter import applyq
        return callable(getattr(applyq, "to_human", None))
    except ImportError:
        return False


@unittest.skipUnless(_real_applyq(), "U6 applyq not installed")
class TestRealApplyq(BankCase):
    """Integration: the hand-off goes through U6's single writer."""

    def test_queued_job_goes_to_needs_human(self):
        job_id = insert_job(self.conn, status="eligible")
        job_uid = self.conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour", job_uid=job_uid), "no_match")
        self.assertTrue(t["job_to_human"])
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0],
                         "needs_human")


def _real_return() -> bool:
    try:
        from jobhunter import applyq
        return callable(getattr(applyq, "return_from_human", None))
    except ImportError:
        return False


@unittest.skipUnless(_real_applyq() and _real_return(), "U6 applyq.return_from_human not installed")
class TestRealApplyqReturn(BankCase):
    """Integration: answering the form question gives the job back through U6's single writer."""

    def test_answered_job_leaves_needs_human(self):
        job_id = insert_job(self.conn, status="eligible")
        job_uid = self.conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        with db.tx(self.conn):
            t = A.request_human(self.conn, q("Favourite colour", job_uid=job_uid), "no_match")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0],
                         "needs_human")
        with db.tx(self.conn):
            res = P.answer(self.conn, t["human_task"], "Blue", by="chat")
        self.assertTrue(res["job_released"])
        self.assertNotEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0],
                            "needs_human")


class _Caller:
    def __init__(self, cls, agent_id=None):
        self.cls = cls
        self.agent_id = agent_id


class TestCli(BankCase):
    def run_cli(self, argv, caller):
        out = io.StringIO()
        with mock.patch.object(cli, "classify", return_value=caller), mock.patch.object(cli, "require"):
            rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_answers_get(self):
        self.handed = []
        p = mock.patch.object(A, "_to_human", side_effect=lambda conn, uid, reason: self.handed.append((uid, reason))
                              or True)
        p.start()
        self.addCleanup(p.stop)
        applier = _Caller("agent", "jobhunter-applier")
        job_id = insert_job(self.conn, status="apply_queued")
        job_uid = self.conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        f = self.home.write_agent_file("applier", "q1.json", json.dumps(q("Notice period (days)", "number", None,
                                                                          job_uid)))
        rc, env = self.run_cli(["answers", "get", "--file", f], applier)
        self.assertEqual((rc, env["data"]["value"]), (0, "30"))
        f = self.home.write_agent_file("applier", "q2.json", json.dumps(q("Date of birth", job_uid=job_uid)))
        rc, env = self.run_cli(["answers", "get", "--file", f], applier)
        self.assertEqual((rc, env["code"]), (9, "E_NOT_FOUND"))
        self.assertFalse(env["data"]["found"])
        self.assertTrue(env["data"]["human_task"].startswith("H"))
        self.assertEqual(env["data"]["reason"], "always_human")
        self.assertEqual(self.handed, [(job_uid, "answer_question")])

    def test_answers_add_is_for_the_person(self):
        rc, env = self.run_cli(["answers", "add", "--key", "hear_about_us", "--value", "Company careers page",
                                "--patterns", json.dumps(["how did you hear"])], _Caller("human"))
        self.assertEqual(rc, 0, env)
        self.assertEqual(A.lookup(q("How did you hear about us?"))["value"], "Company careers page")
        rc, env = self.run_cli(["answers", "add", "--key", "x_y", "--value", "v"], _Caller("system"))
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"))


if __name__ == "__main__":
    unittest.main()
