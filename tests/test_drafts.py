"""U3: draft create and revise, routing by code, dedup pre-check, budget, send text, CLI envelope (5.1, 5.4, 12.4)."""
from __future__ import annotations

import copy
import io
import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import canon, cli, db, drafts
from jobhunter.auth import Caller as _FallbackCaller
from jobhunter.commands import drafts as cmd_drafts, qc as cmd_qc
from jobhunter.errors import Denied
from tests.fakes.u3 import GOOD_BODY, QCTestCase, agent_cli, agent_cli_child
from tests.helpers import insert_action, insert_contact, insert_thread

PROFILE_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profile")
BAD_BODY = GOOD_BODY.replace("We saw the same pattern", "We saw \u2014 the same pattern")


class TestCreate(QCTestCase):
    def test_create_clean_draft(self):
        out = self.create()
        self.assertEqual(out["status"], "drafted", out["lint"])
        self.assertTrue(out["lint"]["pass"])
        row = self.row(out["draft_uid"])
        self.assertRegex(row["draft_uid"], r"^D[A-Z2-7]{7}$")
        self.assertEqual(row["recipient"], "alex.rivera@kestrel.example")    # derived by code
        self.assertEqual(row["company_id"], self.company_id)
        self.assertEqual(row["send_route"], "browser")  # gmail.route defaults to web_ui
        self.assertEqual(row["text_sha256"], canon.sha256_text(drafts.send_text(self.conn, row["id"])))
        qr = self.conn.execute("SELECT * FROM qc_results WHERE draft_id = ?", (row["id"],)).fetchall()
        self.assertEqual([(r["stage"], r["attempt"], r["passed"]) for r in qr], [("lint", 1, 1)])

    def test_app_password_route_uses_mailer(self):
        self.write_config({"gmail": {"route": "app_password"}})
        out = self.create()
        self.assertEqual(out["status"], "drafted", out["lint"])
        self.assertEqual(self.row(out["draft_uid"])["send_route"], "mailer")

    def test_web_ui_route_uses_browser(self):
        self.write_config({"gmail": {"route": "web_ui"}})
        out = self.create()
        self.assertEqual(self.row(out["draft_uid"])["send_route"], "browser")

    def test_recipient_from_file_ignored(self):
        out = self.create(recipient={"first_name": "Mallory", "email": "attacker@example.com"})
        self.assertEqual(self.row(out["draft_uid"])["recipient"], "alex.rivera@kestrel.example")

    def test_unknown_key_refused(self):
        with self.assertRaises(Denied) as cm:
            self.create(to="attacker@example.com")
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    def test_lint_failure_is_stored(self):
        out = self.create(body=BAD_BODY)
        self.assertEqual(out["status"], "lint_failed")
        self.assertIn("C-DASH", [b[0] for b in out["lint"]["blocks"]])
        self.assertEqual(self.row(out["draft_uid"])["status"], "lint_failed")

    def test_hook_evidence_comes_from_the_stored_fact(self):
        f = self.draft_file()
        f["hook"]["snippet"] = "something the agent made up about pincode-level models"
        with self.assertRaises(Denied) as cm:
            self.create(hook=f["hook"])
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        f = self.draft_file()
        f["hook"]["retrieved_at"] = "2026-09-27"
        f["hook"]["source_url"] = "https://www.linkedin.com/posts/elsewhere"
        out = self.create(hook=f["hook"])
        hook = drafts.payload_of(self.row(out["draft_uid"]))["hook"]
        self.assertEqual(hook["retrieved_at"], "2026-09-26")
        self.assertEqual(hook["source_url"], "https://www.linkedin.com/posts/example-person-activity-1")

    def test_fact_of_another_person_refused(self):
        with db.tx(self.conn):
            other = insert_contact(self.conn, company_id=self.company_id, full_name="Sam Lee", email="sam@kestrel.example")
            self.conn.execute("UPDATE research_facts SET subject_id = ? WHERE fact_uid = ?", (other, self.fact_uid))
        with self.assertRaises(Denied) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "E_VALIDATION")

    def test_agent_kind_scope(self):
        with self.assertRaises(Denied) as cm, db.tx(self.conn):
            drafts.create_draft(self.conn, self.draft_file(), None, _FallbackCaller("agent", "jobhunter-applier"))
        self.assertEqual(cm.exception.code, "E_CALLER_NOT_ALLOWED")

    def test_one_open_first_touch_draft_per_person(self):
        self.create()
        with self.assertRaises(Denied) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "E_DUP_DRAFT")

    def test_dedup_person_and_company(self):
        with db.tx(self.conn):
            insert_action(self.conn, kind="cold_email", status="sent", company_id=self.company_id,
                          contact_id=self.contact_id, recipient="alex.rivera@kestrel.example")
        with self.assertRaises(Denied) as cm:
            self.create()
        self.assertIn(cm.exception.code, ("E_COMPANY_COOLDOWN", "E_DUP_PERSON"))
        self.assertTrue(cm.exception.data["lint_rule"].startswith("L-DEDUP-"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM drafts").fetchone()[0], 0)

    def test_dnc_and_skip(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET do_not_contact = 1 WHERE id = ?", (self.contact_id,))
        with self.assertRaises(Denied) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "E_CONTACT_DNC")
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET do_not_contact = 0 WHERE id = ?", (self.contact_id,))
            drafts.add_target_skip(self.conn, "contact:" + self.contact_uid, "dropped_qc", 30)
        with self.assertRaises(Denied) as cm:
            self.create()
        self.assertEqual(cm.exception.code, "E_TARGET_SKIPPED")

    def test_followup_routing_from_thread(self):
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.company_id,
                                contact_id=self.contact_id, recipient="alex.rivera@kestrel.example")
            tid = insert_thread(self.conn, aid, contact_id=self.contact_id, company_id=self.company_id)
            self.conn.execute("UPDATE threads SET subject = 'Pincode-level RTO models' WHERE id = ?", (tid,))
        tk = self.conn.execute("SELECT thread_key FROM threads WHERE id = ?", (tid,)).fetchone()[0]
        self.write_config({"owner": {"signature": {"full_name": "Rohan Das", "links": ["https://example.com/rohan"]}}})
        body = ("Hi Alex,\n\nAdding one thing to my note below. I wrote up how we chose pincode features at Tidemark,"
                " one page: https://example.com/rto-notes\n\nIf the timing is wrong, no problem at all.\n\nThanks,")
        with db.tx(self.conn):
            out = drafts.create_draft(self.conn, {"kind": "followup_email", "thread_key": tk, "body": body,
                                                  "contact_uid": "PAAAAAAA", "subject": "Something new"}, None,
                                      _FallbackCaller("agent", "jobhunter-outreach"))
        row = self.row(out["draft_uid"])
        self.assertEqual(row["contact_id"], self.contact_id)             # from the thread, not the file
        self.assertEqual(row["recipient"], "alex.rivera@kestrel.example")
        self.assertEqual(row["subject"], "Re: Pincode-level RTO models")
        self.assertTrue(out["lint"]["pass"], out["lint"])

    def test_form_answer_send_text(self):
        body = ("Kestrel's post on pincode-level models describes a problem I worked on at Tidemark. RTO fell from "
                "18% to 13% over two quarters after we rebuilt the return-risk model. I want to do that work on a "
                "larger order book, and this role is that work.")
        f = {"kind": "form_answer", "job_uid": self.job_uid, "field_label": "Why do you want to join?",
             "body": body, "field_char_limit": 1000, "hook": None, "claims": []}
        with db.tx(self.conn):
            out = drafts.create_draft(self.conn, f, None, _FallbackCaller("agent", "jobhunter-applier"))
        row = self.row(out["draft_uid"])
        self.assertEqual(row["send_route"], "none")
        text = drafts.send_text(self.conn, row["id"])
        self.assertEqual(json.loads(text)["fields"][0]["label"], "Why do you want to join?")

    def test_signature_in_send_text_and_hash(self):
        self.write_config({"owner": {"first_name": "Rohan", "last_name": "Das",
                                     "signature": {"full_name": "Rohan Das", "phone": "",
                                                   "links": ["https://example.com/rohan"]}}})
        out = self.create()
        row = self.row(out["draft_uid"])
        text = drafts.send_text(self.conn, row["id"])
        self.assertTrue(text.endswith("Rohan Das\nhttps://example.com/rohan"))
        self.assertTrue(text.startswith("Subject: Pincode-level RTO models\n\n"))
        self.assertEqual(row["text_sha256"], canon.sha256_text(text))

    def test_inmail_subject_in_send_text_and_hash(self):
        self.write_config({"owner": {"first_name": "Rohan", "last_name": "Das",
                                     "signature": {"full_name": "Rohan Das", "phone": "",
                                                   "links": ["https://example.com/rohan"]}}})
        out = self.create(kind="inmail", channel="inmail", subject="Returns forecasting")
        row = self.row(out["draft_uid"])
        self.assertEqual(row["subject"], "Returns forecasting")
        text = drafts.send_text(self.conn, row["id"])
        self.assertEqual(text, canon.canonical_send_text("inmail", "Returns forecasting", row["body"], None, None))
        self.assertTrue(text.startswith("Subject: Returns forecasting\n\n"))
        self.assertNotIn("Rohan Das", text)                             # no email signature on an InMail
        self.assertEqual(row["text_sha256"], canon.sha256_text(text))
        # the approved hash depends on the subject: a changed subject is a different text
        other = drafts.compute_send_text("inmail", "Something else", row["body"], drafts.payload_of(row))
        self.assertNotEqual(canon.sha256_text(other), row["text_sha256"])

    def test_inmail_hash_matches_gate_read_back(self):
        from jobhunter import gate
        out = self.create(kind="inmail", channel="inmail", subject="Returns forecasting")
        row = self.row(out["draft_uid"])
        act = {"kind": "inmail", "platform": "linkedin", "token": "TAAAAAAAAAAA"}
        typed = "Subject: Returns forecasting\n\n" + row["body"] + "\n"
        self.assertEqual(canon.sha256_text(gate._observed_canonical(self.conn, act, typed)), row["text_sha256"])
        typed_other = "Subject: Returns forecasting soon\n\n" + row["body"]
        self.assertNotEqual(canon.sha256_text(gate._observed_canonical(self.conn, act, typed_other)),
                            row["text_sha256"])
        body_only = gate._observed_canonical(self.conn, act, row["body"])
        self.assertNotEqual(canon.sha256_text(body_only), row["text_sha256"])

    def test_inmail_revision_rehashes_subject(self):
        out = self.create(kind="inmail", channel="inmail", subject="Returns forecasting", body=BAD_BODY)
        self.assertEqual(out["status"], "lint_failed")
        with db.tx(self.conn):
            drafts.revise_draft(self.conn, out["draft_uid"],
                                self.draft_file(kind="inmail", channel="inmail", subject="RTO models at Kestrel"))
        row = self.row(out["draft_uid"])
        self.assertEqual(row["subject"], "RTO models at Kestrel")
        self.assertEqual(row["text_sha256"], canon.sha256_text(
            canon.canonical_send_text("inmail", "RTO models at Kestrel", row["body"], None, None)))


class TestRevise(QCTestCase):
    def test_revise_after_lint_failure_and_budget(self):
        out = self.create(body=BAD_BODY)
        uid = out["draft_uid"]
        with db.tx(self.conn):
            r2 = drafts.revise_draft(self.conn, uid, self.draft_file(body=BAD_BODY))
        self.assertEqual((r2["attempt"], r2["status"]), (2, "lint_failed"))
        with db.tx(self.conn):
            r3 = drafts.revise_draft(self.conn, uid, self.draft_file(body=BAD_BODY))
        self.assertEqual((r3["attempt"], r3["status"]), (3, "dropped_qc"))
        skip = self.conn.execute("SELECT * FROM target_skips WHERE target_key = ?",
                                 ("contact:" + self.contact_uid,)).fetchone()
        self.assertIsNotNone(skip)
        self.assertEqual(skip["until"], canon.ts_add(canon.now(), days=30))
        with self.assertRaises(Denied) as cm, db.tx(self.conn):
            drafts.revise_draft(self.conn, uid, self.draft_file())
        self.assertEqual(cm.exception.code, "E_QC_BUDGET_EXHAUSTED")

    def test_revise_fixes_lint(self):
        out = self.create(body=BAD_BODY)
        with db.tx(self.conn):
            r2 = drafts.revise_draft(self.conn, out["draft_uid"], self.draft_file())
        self.assertEqual((r2["attempt"], r2["status"]), (2, "drafted"))
        rows = self.conn.execute("SELECT attempt, passed FROM qc_results WHERE stage = 'lint' ORDER BY attempt").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, 0), (2, 1)])

    def test_revise_only_after_failure_and_routing_fixed(self):
        out = self.create()
        with self.assertRaises(Denied) as cm, db.tx(self.conn):
            drafts.revise_draft(self.conn, out["draft_uid"], self.draft_file())
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        out2 = self.create.__func__  # noqa: F841  (keeps flake quiet)
        with db.tx(self.conn):
            self.conn.execute("UPDATE drafts SET status = 'superseded' WHERE draft_uid = ?", (out["draft_uid"],))
        bad = self.create(body=BAD_BODY)
        with self.assertRaises(Denied) as cm, db.tx(self.conn):
            drafts.revise_draft(self.conn, bad["draft_uid"], self.draft_file(kind="inmail", channel="inmail"))
        self.assertEqual(cm.exception.code, "E_VALIDATION")


class TestResumeFilename(QCTestCase):
    """drafts.resume_filename is the stager's name (jobhunter.resume.file_stem with the base.json fallback)."""

    def setUp(self):
        super().setUp()
        from jobhunter import resume as R
        self.R = R
        with open(os.path.join(PROFILE_FIX, "base.json"), "r", encoding="utf-8") as fh:
            self.base = json.load(fh)
        R.save_base(self.base)

    def cfg(self, first: str = "", last: str = "", pattern: str | None = None) -> dict:
        cfg = copy.deepcopy(drafts._settings(self.conn))
        cfg["owner"] = dict(cfg.get("owner") or {}, first_name=first, last_name=last)
        if pattern is not None:
            cfg["resume"] = dict(cfg.get("resume") or {}, filename=pattern)
        return cfg

    def staged(self, cfg: dict) -> str:
        """The name R.stage gives the upload copy for this config."""
        return self.R.file_stem(self.R.load_base()["contact"], self.R.resume_config(cfg), cfg) + ".pdf"

    def test_blank_owner_names_fall_back_to_base_json(self):
        cfg = self.cfg()
        self.assertEqual(drafts.resume_filename(cfg), "Alex_Rivera_Resume.pdf")
        self.assertEqual(drafts.resume_filename(cfg), self.staged(cfg))

    def test_only_a_first_name_uses_base_json(self):
        cfg = self.cfg("Sam")
        self.assertEqual(drafts.resume_filename(cfg), "Alex_Rivera_Resume.pdf")
        self.assertEqual(drafts.resume_filename(cfg), self.staged(cfg))

    def test_pattern_is_normalised_like_the_stager(self):
        cfg = self.cfg("Sam", "Lee", "{first}__{last}_resume.PDF")
        self.assertEqual(drafts.resume_filename(cfg), "Sam_Lee_resume.pdf")
        self.assertEqual(drafts.resume_filename(cfg), self.staged(cfg))

    def test_owner_names_win_when_both_are_set(self):
        cfg = self.cfg("Sam", "Lee")
        self.assertEqual(drafts.resume_filename(cfg), "Sam_Lee_Resume.pdf")
        self.assertEqual(drafts.resume_filename(cfg), self.staged(cfg))

    def test_no_base_and_no_owner_names(self):
        os.remove(self.R.base_path())
        cfg = self.cfg()
        self.assertEqual(drafts.resume_filename(cfg),
                         self.R.file_stem({}, self.R.resume_config(cfg), cfg) + ".pdf")

    def test_variant_attachment_and_form_field_carry_the_staged_name(self):
        cfg = self.cfg()
        with db.tx(self.conn):
            self.conn.execute(
                "INSERT INTO resume_variants (variant_uid, job_id, mode, base_sha256, pdf_path, txt_path, pdf_sha256,"
                " created_at) VALUES ('VAAAAAAA', ?, 'light', 'x', '/nonexistent.pdf', '/nonexistent.txt', 'y', ?)",
                (self.job_id, canon.now()))
        v = drafts.variant_info(self.conn, "VAAAAAAA", cfg)
        self.assertEqual(v["filename"], self.staged(cfg))
        text = canon.canonical_send_text("application_package", None, None, [{"label": "Name", "value": "x"}], None,
                                         {"variant_uid": "VAAAAAAA", "filename": v["filename"], "sha256": "y"})
        self.assertIn("Alex_Rivera_Resume.pdf", text)
        self.assertNotIn("Candidate", text)


class TestResumeLintNames(QCTestCase):
    def test_resume_lint_context_has_base_and_payload_names(self):
        from jobhunter import resume as R
        with open(os.path.join(PROFILE_FIX, "base.json"), "r", encoding="utf-8") as fh:
            base = json.load(fh)
        R.save_base(base)
        body = "Zo\u00eb Mar\u00edn\nData Analyst, Tidemark Logistics, Jul 2022 to Present\n"
        f = {"kind": "resume", "channel": "resume", "job_uid": self.job_uid, "subject": None, "body": body,
             "hook": None, "claims": [], "links": [],
             "payload": {"resume_variant_uid": "VAAAAAAA", "names": ["Zo\u00eb Mar\u00edn", "Tidemark Logistics"],
                         "base": {}, "tailored": {}}}
        from jobhunter.auth import Caller as _FallbackCaller
        with db.tx(self.conn):
            out = drafts.create_draft(self.conn, f, None, _FallbackCaller("agent", "jobhunter-applier"))
        self.assertNotIn("R-C-NON-ASCII", [b[0] for b in out["lint"]["blocks"]])
        ctx = drafts.lint_context(self.conn, self.row(out["draft_uid"])["id"])
        for n in ("Zo\u00eb Mar\u00edn", "Alex Rivera", "Alex", "Rivera"):
            self.assertIn(n, ctx["names"])


class TestCli(QCTestCase):
    def run_cli(self, argv, agent="jobhunter-outreach"):
        if agent:
            return agent_cli(self.home, agent, argv, [cmd_drafts, cmd_qc])
        out = io.StringIO()
        rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out, modules=[cmd_drafts, cmd_qc])
        return rc, json.loads(out.getvalue())

    def run_human(self, argv):
        """--human from the terminal, as ./jobhunter edit runs it (no agent environment)."""
        out = io.StringIO()
        rc = cli.main(["--human"] + argv, env={}, stdin=io.StringIO(""), stdout=out, modules=[cmd_drafts, cmd_qc])
        return rc, out.getvalue()

    def test_human_field_prints_only_the_raw_value(self):
        uid = self.create()["draft_uid"]
        rc, text = self.run_human(["draft", "show", uid, "--field", "body"])
        self.assertEqual(rc, 0)
        self.assertEqual(text, GOOD_BODY + "\n")          # what ./jobhunter edit hands to $EDITOR
        rc, text = self.run_human(["draft", "show", uid, "--field", "subject"])
        self.assertEqual(text, "Pincode-level RTO models\n")
        rc, text = self.run_human(["draft", "show", uid, "--field", "send_text"])
        self.assertEqual(text, drafts.send_text(self.conn, self.row(uid)["id"]) + "\n")
        rc, env = self.run_cli(["draft", "show", uid, "--field", "body"])     # JSON mode keeps the envelope
        self.assertEqual(env["data"], {"draft_uid": uid, "field": "body", "value": GOOD_BODY})

    def test_human_field_form_prints_the_value_as_json(self):
        uid = self.create()["draft_uid"]
        rc, text = self.run_human(["draft", "show", uid, "--field", "form"])
        self.assertEqual(json.loads(text), {"fields": [], "resume_filename": None})

    def test_human_show_without_field_keeps_the_summary(self):
        uid = self.create()["draft_uid"]
        rc, text = self.run_human(["draft", "show", uid])
        self.assertEqual(rc, 0)
        self.assertIn('"draft_uid": "%s"' % uid, text)

    def test_create_via_cli(self):
        path = self.home.write_agent_file("outreach", "draft.json", json.dumps(self.draft_file()))
        rc, env = self.run_cli(["draft", "create", "--file", path])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        uid = env["data"]["draft_uid"]
        rc, env = self.run_cli(["draft", "show", uid, "--field", "send_text"])
        self.assertEqual(rc, 0)
        self.assertTrue(env["data"]["value"].startswith("Subject: "))
        rc, env = self.run_cli(["draft", "show", uid], agent="jobhunter-applier")
        self.assertEqual(env["code"], "E_NOT_FOUND")

    def test_lint_failure_exit_6_and_row_kept(self):
        path = self.home.write_agent_file("outreach", "bad.json", json.dumps(self.draft_file(body=BAD_BODY)))
        rc, env = self.run_cli(["draft", "create", "--file", path])
        self.assertEqual((rc, env["code"]), (6, "E_QC_LINT_FAILED"))
        self.assertEqual(self.row(env["data"]["draft_uid"])["status"], "lint_failed")

    def test_path_outside_workspace_refused(self):
        rc, env = self.run_cli(["draft", "create", "--file", "/etc/hosts"])
        self.assertEqual(env["code"], "E_PATH_NOT_ALLOWED")

    def test_agent_call_in_a_real_python_i_child(self):
        """The guard starts jh.py as `<PY> -I jh.py --agent-proof <T> ...` with the env proof (CLI-ROUTE-DESIGN 5):
        both carriers in a real isolated interpreter reach the outreach agent's draft commands."""
        from jobhunter import auth
        if not hasattr(auth, "argv_proof"):
            self.skipTest("proof version 2 is not there yet (claude-cli route core, U1)")
        uid = self.create()["draft_uid"]
        rc, env, isolated = agent_cli_child(self.home, "jobhunter-outreach", ["draft", "show", uid, "--field", "body"])
        self.assertEqual(isolated, 1)
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        self.assertEqual(env["data"]["value"], GOOD_BODY)
        rc, env, _iso = agent_cli_child(self.home, "jobhunter-applier", ["draft", "show", uid])
        self.assertEqual(env["code"], "E_NOT_FOUND")              # still scoped to the agent the proofs name

    def test_agent_call_without_python_i_is_refused(self):
        from jobhunter import auth
        if not hasattr(auth, "argv_proof"):
            self.skipTest("proof version 2 is not there yet (claude-cli route core, U1)")
        uid = self.create()["draft_uid"]
        rc, env, isolated = agent_cli_child(self.home, "jobhunter-outreach", ["draft", "show", uid], isolated=False)
        self.assertEqual(isolated, 0)
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))
        self.assertIn("python -I", env["message"])

    def test_agents_cannot_approve(self):
        rc, env = self.run_cli(["approve", "ACDE"])
        self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED")
        rc, env = self.run_cli(["approve", "ACDE"], agent=None)     # system caller: needs the PIN
        self.assertEqual(env["code"], "E_HUMAN_ONLY")


if __name__ == "__main__":
    unittest.main()
