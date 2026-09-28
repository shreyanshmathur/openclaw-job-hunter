"""U2: job ingest (12.1), keys and aliases, duplicates, exclusions, the human call, `job` commands.

U1's keys, companies, people and exclusions are used for real; the confirmed profile and the effective config come
from tests/fakes/u2."""
from __future__ import annotations

import copy
import io
import json
import os

import tests  # noqa: F401
from jobhunter import cli, db, jobs, paths
from tests.fakes.u2 import fixture_json, install
from tests.helpers import HomeTestCase


def item(**kw):
    base = {"source_url": "https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-bengaluru-250926000001",
            "company": "Kestrel Commerce", "title": "Data Analyst", "location": "Bengaluru", "work_mode": "hybrid",
            "posted_at": "2026-09-25", "jd_text": "Kestrel Commerce needs SQL and Python for forecasting.",
            "native_ids": {"board_job_id": "250926000001"}}
    base.update(kw)
    return base


class IngestCase(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.deps = install(self)

    def ingest(self, payload, via="browser", **kw):
        with db.tx(self.conn):
            return jobs.ingest(self.conn, payload, None, via, **kw)

    def job(self, uid):
        return self.conn.execute("SELECT * FROM jobs WHERE job_uid = ?", (uid,)).fetchone()


class TestIngest(IngestCase):
    def test_browser_file(self):
        res = self.ingest(fixture_json("ingest_browser.json"))
        self.assertEqual([r["outcome"] for r in res], ["new", "prefilter_rejected"])
        self.assertEqual(res[1]["reason_code"], "seniority_title")
        a = self.job(res[0]["job_uid"])
        self.assertEqual(a["status"], "eval_queued")
        self.assertEqual(a["canonical_key"], "board:naukri:250926000001")
        self.assertEqual(a["apply_route"], "board_inapp")
        self.assertEqual(a["discovered_via"], "browser")
        self.assertEqual(a["norm_city"], "bengaluru")
        self.assertEqual((a["years_min"], a["years_max"]), (2, 4))
        self.assertIsNotNone(a["company_id"])
        text = self.conn.execute("SELECT jd_text FROM job_texts WHERE job_id = ?", (a["id"],)).fetchone()[0]
        self.assertIn("forecasting", text)
        team = self.conn.execute("SELECT c.full_name, h.relation FROM job_hiring_team h JOIN contacts c ON "
                                 "c.id = h.contact_id WHERE h.job_id = ?", (a["id"],)).fetchall()
        self.assertEqual([tuple(r) for r in team], [("Alex Rivera", "hiring_manager")])
        b = self.job(res[1]["job_uid"])
        self.assertEqual((b["status"], b["status_reason"]), ("prefilter_rejected", "seniority_title"))
        ev = self.conn.execute("SELECT stage, verdict, reason_code, reason_text, profile_version FROM evaluations "
                               "WHERE job_id = ?", (b["id"],)).fetchone()
        self.assertEqual((ev["stage"], ev["verdict"], ev["reason_code"]), ("prefilter", "skip", "seniority_title"))
        self.assertIn("intern", ev["reason_text"])
        self.assertEqual(ev["profile_version"], "pv-test-1")

    def test_second_ingest_is_a_duplicate(self):
        payload = fixture_json("ingest_browser.json")
        first = self.ingest(payload)
        again = self.ingest(payload)
        self.assertEqual([r["outcome"] for r in again], ["duplicate", "duplicate"])
        self.assertEqual(again[0]["duplicate_of"], first[0]["job_uid"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)

    def test_url_variant_and_alias_added(self):
        first = self.ingest({"source": "naukri", "jobs": [item()]})
        variant = item(source_url="https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-bengaluru-"
                                  "250926000001?src=jobsearchDesk&utm_source=x",
                       apply_url="https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345")
        res = self.ingest({"source": "naukri", "jobs": [variant]})
        self.assertEqual(res[0]["outcome"], "alias_added")
        self.assertEqual(res[0]["job_uid"], first[0]["job_uid"])
        keys = [r[0] for r in self.conn.execute("SELECT key FROM job_keys WHERE job_id = ?",
                                                (self.job(first[0]["job_uid"])["id"],))]
        self.assertIn("ats:greenhouse:4012345", keys)
        # the ATS lane later sees the same job by its ATS URL: a duplicate, not a second job
        gh = item(source_url="https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345", native_ids={})
        res = self.ingest({"source": "greenhouse", "jobs": [gh]}, via="api")
        self.assertEqual((res[0]["outcome"], res[0]["job_uid"]), ("alias_added", first[0]["job_uid"]))
        res = self.ingest({"source": "greenhouse", "jobs": [gh]}, via="api")
        self.assertEqual((res[0]["outcome"], res[0]["job_uid"]), ("duplicate", first[0]["job_uid"]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_fingerprint_duplicate_across_sources(self):
        first = self.ingest({"source": "naukri", "jobs": [item()]})
        other = item(source_url="https://www.linkedin.com/jobs/view/4100200399", native_ids={},
                     location="Bangalore, Karnataka")
        res = self.ingest({"source": "linkedin_jobs", "jobs": [other]})
        self.assertEqual(res[0]["outcome"], "duplicate")
        self.assertEqual(res[0]["reason_code"], "fingerprint")
        self.assertEqual(res[0]["duplicate_of"], first[0]["job_uid"])
        row = self.job(res[0]["job_uid"])
        self.assertEqual(row["status"], "duplicate")
        # a repeat of the duplicate's own URL reports the original job
        res = self.ingest({"source": "linkedin_jobs", "jobs": [other]})
        self.assertEqual(res[0]["duplicate_of"], first[0]["job_uid"])

    def test_exclusions(self):
        from jobhunter import exclusions
        with db.tx(self.conn):
            exclusions.add(self.conn, "company", "Kestrel Commerce", "current employer")
            exclusions.add(self.conn, "job_url",
                           "https://jobs.lever.co/tidemark/5d3c1a2b-0000-4000-8000-000000000001", "applied by hand")
        res = self.ingest({"source": "naukri", "jobs": [
            item(company="Kestrel Commerce Pvt Ltd"),
            item(source_url="https://jobs.lever.co/tidemark/5d3c1a2b-0000-4000-8000-000000000001/apply",
                 company="Tidemark Analytics", native_ids={})]})
        self.assertEqual([(r["outcome"], r["reason_code"]) for r in res],
                         [("excluded", "excluded_company"), ("excluded", "excluded_job_url")])
        self.assertEqual(self.job(res[0]["job_uid"])["status"], "excluded")

    def test_schema_errors_refuse_the_whole_file(self):
        before = self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
        d = self.assertDenied("E_SCHEMA", self.ingest, {"source": "naukri", "jobs": [item(), item(salary_text="x")]})
        self.assertEqual(d.data["errors"][0]["input_index"], 1)
        self.assertDenied("E_SCHEMA", self.ingest, {"source": "naukri", "jobs": [item(source_url="ftp://x")]})
        self.assertDenied("E_SCHEMA", self.ingest, {"source": "naukri", "jobs": [item(tenant={"ats": "lever",
                                                                                               "tenant": "x"})]})
        self.assertDenied("E_SCHEMA", self.ingest, {"source": "Naukri!", "jobs": [item()]})
        self.assertDenied("E_SCHEMA", self.ingest, {"source": "naukri", "jobs": []})
        self.assertDenied("E_INVENTED_KEY", self.ingest,
                          {"source": "naukri", "jobs": [item(native_ids={"board_job_id": "999999999999"})]})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], before)

    def test_api_lane_reports_bad_keys_per_item(self):
        res = self.ingest({"source": "naukri", "jobs": [item(native_ids={"board_job_id": "999999999999"}),
                                                        item(source_url="https://example.com/jobs/7", native_ids={})]},
                          via="api")
        self.assertEqual([r["outcome"] for r in res], ["error", "new"])
        self.assertEqual(res[0]["reason_code"], "e_invented_key")

    def test_apply_routes(self):
        res = self.ingest({"source": "glassdoor", "jobs": [item(
            source_url="https://www.glassdoor.co.in/job-listing/data-analyst?jobListingId=1009000000",
            native_ids={"board_job_id": "1009000000"}, apply_route_hint="board_inapp", title="Business Analyst")]})
        self.assertEqual(self.job(res[0]["job_uid"])["apply_route"], "human")
        post = item(source_url="https://www.linkedin.com/posts/example-person_hiring-activity-7300000000000000001-abcd",
                    native_ids={"post_id": "7300000000000000001"}, title="Hiring post: analytics team",
                    apply_email="Jobs@Wrenfield.example", company="Wrenfield",
                    jd_text="We are hiring a data analyst. Send your CV to jobs@wrenfield.example",
                    hiring_team=[{"name": "Alex Rivera", "title": "Founder", "linkedin_url": None,
                                  "relation": "poster"}])
        res = self.ingest({"source": "linkedin_post", "jobs": [post]})
        row = self.job(res[0]["job_uid"])
        self.assertEqual((row["apply_route"], row["apply_email"]), ("email", "jobs@wrenfield.example"))
        self.assertEqual(row["canonical_key"], "post:linkedin:7300000000000000001")

    def test_mailto_apply_url_becomes_apply_email(self):
        res = self.ingest({"source": "naukri", "jobs": [item(apply_url="mailto:careers@kestrel.example?subject=x")]})
        row = self.job(res[0]["job_uid"])
        self.assertEqual((row["apply_email"], row["apply_url"]), ("careers@kestrel.example", None))

    def test_jd_is_truncated(self):
        res = self.ingest({"source": "naukri", "jobs": [item(jd_text="x" * (jobs.JD_MAX + 50))]})
        n = self.conn.execute("SELECT length(jd_text) FROM job_texts t JOIN jobs j ON j.id = t.job_id "
                              "WHERE j.job_uid = ?", (res[0]["job_uid"],)).fetchone()[0]
        self.assertEqual(n, jobs.JD_MAX)

    def test_unconfirmed_profile_still_ingests(self):
        self.deps.profile = False
        res = self.ingest({"source": "naukri", "jobs": [item(title="Backend Engineer")]})
        self.assertEqual(self.job(res[0]["job_uid"])["status"], "eval_queued")

    def test_api_lane_waits_for_the_jd(self):
        gh = {"source_url": "https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345", "company": "Kestrel "
              "Commerce", "title": "Data Analyst", "location": "Bengaluru", "posted_at": "2026-09-22",
              "tenant": {"ats": "greenhouse", "tenant": "kestrelcommerce"}}
        gh2 = dict(gh, source_url="https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012346",
                   title="Business Analyst")
        res = self.ingest({"source": "greenhouse", "jobs": [gh, gh2]}, via="api", fetch_jd_later=True)
        a, b = self.job(res[0]["job_uid"]), self.job(res[1]["job_uid"])
        self.assertEqual((a["status"], b["status"]), ("new", "new"))
        self.assertEqual(a["apply_route"], "ats_form")
        tenant = self.conn.execute("SELECT alias_key FROM company_aliases WHERE company_id = ? AND kind = 'ats'",
                                   (a["company_id"],)).fetchone()
        self.assertEqual(tenant[0], "ats:greenhouse:kestrelcommerce")
        with db.tx(self.conn):
            s1 = jobs.attach_jd(self.conn, a["id"], "Hybrid in Bengaluru. SQL and Python for forecasting.")
            s2 = jobs.attach_jd(self.conn, b["id"], "You need 9+ years of experience.")
        self.assertEqual((s1, s2), ("eval_queued", "prefilter_rejected"))
        self.assertEqual(self.job(res[1]["job_uid"])["status_reason"], "years_required")

    def test_attach_jd_can_apply_false_is_stale(self):
        gh = {"source_url": "https://kestrelfreight.wd5.myworkdayjobs.com/External/job/Bengaluru/Data-Analyst_JR-1",
              "company": "kestrelfreight", "title": "Data Analyst", "location": "Bengaluru",
              "tenant": {"ats": "workday", "tenant": "kestrelfreight.wd5/External"}}
        res = self.ingest({"source": "workday", "jobs": [gh]}, via="api", fetch_jd_later=True)
        row = self.job(res[0]["job_uid"])
        self.assertEqual(row["apply_route"], "human")
        with db.tx(self.conn):
            st = jobs.attach_jd(self.conn, row["id"], "Some text", can_apply=False)
        self.assertEqual(st, "prefilter_rejected")


class TestApplyEmailDomain(IngestCase):
    """The apply email's registrable domain is a company key (2.4.2), so an application email to a company already
    emailed under another name shares its company, and so its company cooldown."""

    def setUp(self):
        super().setUp()
        from jobhunter import companies
        self.companies = companies
        with db.tx(self.conn):
            self.owner = companies.resolve(self.conn, name="Kestrel Commerce", domain="kestrel.example",
                                           source="email")

    def job_item(self, n, **kw):
        base = dict(source_url="https://example.com/jobs/%d" % n, native_ids={}, company="KC Retail Systems",
                    apply_email="careers@kestrel.example")
        base.update(kw)
        return item(**base)

    def company_of(self, res):
        return self.job(res[0]["job_uid"])["company_id"]

    def count(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def alias_owner(self, key):
        row = self.conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", (key,)).fetchone()
        return self.companies.survivor(self.conn, row[0]) if row else None

    def test_new_name_joins_the_domain_owner_without_a_merge(self):
        before = self.count("SELECT count(*) FROM companies")
        res = self.ingest({"source": "naukri", "jobs": [self.job_item(1)]})
        self.assertEqual(res[0]["outcome"], "new")
        self.assertEqual(self.company_of(res), self.owner)
        self.assertEqual(self.count("SELECT count(*) FROM companies"), before)
        self.assertEqual(self.count("SELECT count(*) FROM company_merges"), 0)
        self.assertEqual(self.alias_owner("id:kcretailsystems"), self.owner)

    def test_known_name_merges_into_the_domain_owner(self):
        with db.tx(self.conn):
            other = self.companies.resolve(self.conn, name="KC Retail Systems", source="job_board")
        self.assertNotEqual(other, self.owner)
        res = self.ingest({"source": "naukri", "jobs": [self.job_item(2)]})
        self.assertEqual(self.company_of(res), self.owner)
        self.assertEqual(self.companies.survivor(self.conn, other), self.owner)
        m = self.conn.execute("SELECT from_id, to_id, strength FROM company_merges").fetchall()
        self.assertEqual([tuple(r) for r in m], [(other, self.owner, "exact")])

    def test_company_domain_and_a_different_mail_domain_both_key_the_company(self):
        res = self.ingest({"source": "naukri", "jobs": [self.job_item(3, company_domain="kcretail.example")]})
        self.assertEqual(self.company_of(res), self.owner)
        self.assertEqual(self.alias_owner("dom:kcretail.example"), self.owner)
        self.assertEqual(self.alias_owner("dom:kestrel.example"), self.owner)
        self.assertEqual(self.alias_owner("id:kcretailsystems"), self.owner)
        src = self.conn.execute("SELECT source FROM company_aliases WHERE alias_key = 'dom:kcretail.example'")
        self.assertEqual(src.fetchone()[0], "job_board")

    def test_same_domain_as_company_domain_is_a_plain_resolve(self):
        res = self.ingest({"source": "naukri", "jobs": [self.job_item(4, company_domain="kestrel.example")]})
        self.assertEqual(self.company_of(res), self.owner)
        self.assertEqual(self.count("SELECT count(*) FROM company_merges"), 0)

    def test_companies_marked_distinct_stay_apart_and_the_job_still_ingests(self):
        with db.tx(self.conn):
            other = self.companies.resolve(self.conn, name="KC Retail Systems", source="job_board")
            a, b = sorted((other, self.owner))
            self.conn.execute("INSERT INTO company_distinct (a_id, b_id, by, created_at) VALUES "
                              "(?, ?, 'human', '2026-09-01T00:00:00Z')", (a, b))
        tasks = self.count("SELECT count(*) FROM human_tasks WHERE kind = 'confirm_company_merge'")
        res = self.ingest({"source": "naukri", "jobs": [self.job_item(5)]})
        self.assertEqual(res[0]["outcome"], "new")
        self.assertEqual(self.company_of(res), other)
        self.assertEqual(self.companies.survivor(self.conn, other), other)
        self.assertEqual(self.alias_owner("dom:kestrel.example"), self.owner)
        self.assertEqual(self.count("SELECT count(*) FROM company_merges"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM human_tasks WHERE kind = 'confirm_company_merge'"), tasks)

    def test_freemail_and_unknown_domains(self):
        res = self.ingest({"source": "naukri", "jobs": [
            self.job_item(6, company="Wrenfield Retail", apply_email="wrenfield.hiring@" + "gmail.com")]})
        self.assertNotEqual(self.company_of(res), self.owner)
        self.assertIsNone(self.alias_owner("dom:gmail.com"))
        # a domain nobody owns yet becomes a key of the new company
        res = self.ingest({"source": "naukri", "jobs": [
            self.job_item(7, company="Tidemark Analytics", apply_email="jobs@tidemark.example")]})
        self.assertNotEqual(self.company_of(res), self.owner)
        self.assertEqual(self.alias_owner("dom:tidemark.example"), self.company_of(res))

    def test_a_known_name_with_a_new_mail_domain_gains_the_key(self):
        first = self.ingest({"source": "naukri", "jobs": [
            self.job_item(8, company="Tidemark Analytics", apply_email=None)]})
        cid = self.company_of(first)
        res = self.ingest({"source": "naukri", "jobs": [
            self.job_item(9, company="Tidemark Analytics", apply_email="talent@tidemark.example")]})
        self.assertEqual(self.company_of(res), cid)
        self.assertEqual(self.alias_owner("dom:tidemark.example"), cid)
        src = self.conn.execute("SELECT source FROM company_aliases WHERE alias_key = 'dom:tidemark.example'")
        self.assertEqual(src.fetchone()[0], "email")
        self.assertEqual(self.count("SELECT count(*) FROM company_merges"), 0)


class TestAliasHumanCallStatus(IngestCase):
    def setUp(self):
        super().setUp()
        res = self.ingest({"source": "naukri", "jobs": [
            item(), item(source_url="https://www.naukri.com/job-listings-data-analyst-tidemark-analytics-bengaluru-"
                                    "250926000009", company="Tidemark Analytics",
                         native_ids={"board_job_id": "250926000009"}, title="Junior Data Analyst")]})
        self.a, self.b = res[0]["job_uid"], res[1]["job_uid"]

    def test_add_alias(self):
        with db.tx(self.conn):
            r = jobs.add_alias(self.conn, self.a, "https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345")
            again = jobs.add_alias(self.conn, self.a, "https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345")
        self.assertEqual((r["key"], r["kind"]), ("ats:greenhouse:4012345", "ats"))
        self.assertEqual(again["added"], [])
        with db.tx(self.conn):
            d = self.assertDenied("E_DUP_JOB", jobs.add_alias, self.conn, self.b,
                                  "https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345")
        self.assertEqual(d.data["duplicate_of"], self.a)
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", jobs.add_alias, self.conn, self.a, "http://insecure.example/x")
            self.assertDenied("E_NOT_FOUND", jobs.add_alias, self.conn, "JAAAAAAA", "https://example.com/x")

    def test_human_call(self):
        self.assertEqual(self.job(self.b)["status"], "prefilter_rejected")
        with db.tx(self.conn):
            jobs.set_human_call(self.conn, self.b, "apply_anyway", "human:sheet")
        row = self.job(self.b)
        self.assertEqual((row["status"], row["human_call"]), ("eligible", "apply_anyway"))
        with db.tx(self.conn):
            jobs.set_human_call(self.conn, self.b, "never", "human:sheet")
        self.assertEqual((self.job(self.b)["status"], self.job(self.b)["human_call"]), ("closed", "never"))
        with db.tx(self.conn):
            jobs.set_human_call(self.conn, self.b, "apply_anyway", "human:chat")
        self.assertEqual(self.job(self.b)["status"], "eligible")
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", jobs.set_human_call, self.conn, self.a, "maybe", "human:cli")
            jobs.set_human_call(self.conn, self.a, "never", "human:cli")    # eval_queued -> closed
        self.assertEqual(self.job(self.a)["status"], "closed")

    def test_apply_anyway_never_beats_an_exclusion(self):
        from jobhunter import jobstate
        with db.tx(self.conn):
            jobstate.set_job_status(self.conn, self.job(self.a)["id"], "excluded", "excluded_company", "test")
            self.assertDenied("E_EXCLUDED", jobs.set_human_call, self.conn, self.a, "apply_anyway", "human:cli")

    def test_set_status(self):
        from jobhunter import evaluate
        with db.tx(self.conn):
            evaluate.claim(self.conn, 5, "C20260927T050000ZAAAA", profile=self.deps.profile, config=self.deps.cfg)
            self.assertDenied("E_CALLER_NOT_ALLOWED", jobs.set_status, self.conn, self.a, "closed", "whatever",
                              "jobhunter-applier", agent=True)
            self.assertDenied("E_CALLER_NOT_ALLOWED", jobs.set_status, self.conn, self.a, "needs_human", "bored",
                              "jobhunter-applier", agent=True)
            res = jobs.set_status(self.conn, self.a, "needs_human", "account_required", "jobhunter-applier",
                                  agent=True)
        self.assertTrue(res["task_uid"].startswith("H"))
        self.assertEqual(self.job(self.a)["status"], "needs_human")
        task = self.conn.execute("SELECT kind, job_id FROM human_tasks WHERE task_uid = ?",
                                 (res["task_uid"],)).fetchone()
        self.assertEqual(task["kind"], "apply_manually")

    def test_show_and_list(self):
        info = jobs.show(self.conn, self.a)
        self.assertEqual(info["job_uid"], self.a)
        self.assertEqual(info["company"]["display_name"], "Kestrel Commerce")
        self.assertIn({"key": "board:naukri:250926000001", "kind": "board"}, info["keys"])
        self.assertIn("forecasting", info["jd_text"])
        rows = jobs.list_jobs(self.conn, "prefilter_rejected", 10)
        self.assertEqual([r["job_uid"] for r in rows], [self.b])
        self.assertDenied("E_VALIDATION", jobs.list_jobs, self.conn, "bogus", 10)


class TestJobCommands(IngestCase):
    def run_cli(self, argv, agent=None):
        out = io.StringIO()
        env = {"PATH": "/usr/bin"}
        if agent:
            env.update({"OPENCLAW_SHELL": "1", "JH_AGENT_ID": agent})
        rc = cli.main(argv, env=env, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_scout_job_add(self):
        path = self.home.write_agent_file("scout", "C20260927T050000ZAAAA/naukri.json",
                                          json.dumps(fixture_json("ingest_browser.json")))
        rc, env = self.run_cli(["--cycle", "C20260927T050000ZAAAA", "job", "add", "--file", path],
                               agent="jobhunter-scout")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["counts"], {"new": 1, "prefilter_rejected": 1})
        self.assertEqual(env["cycle_id"], "C20260927T050000ZAAAA")
        # a file outside the agent's work folder is refused
        outside = os.path.join(paths.root(), "private", "x.json")
        with open(outside, "w") as fh:
            fh.write("{}")
        rc, env = self.run_cli(["job", "add", "--file", outside], agent="jobhunter-scout")
        self.assertEqual((rc, env["code"]), (10, "E_PATH_NOT_ALLOWED"))

    def test_alias_file_and_show_via_cli(self):
        res = self.ingest({"source": "naukri", "jobs": [item()]})
        uid = res[0]["job_uid"]
        path = self.home.write_agent_file("applier", "C20260927T050000ZAAAA/alias.json",
                                          json.dumps({"url": "https://jobs.lever.co/kestrel/"
                                                             "5d3c1a2b-0000-4000-8000-0000000000ff"}))
        rc, env = self.run_cli(["job", "alias", "add", uid, "--file", path], agent="jobhunter-applier")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["kind"], "ats")
        bad = self.home.write_agent_file("applier", "C20260927T050000ZAAAA/bad.json", json.dumps({"link": "x"}))
        rc, env = self.run_cli(["job", "alias", "add", uid, "--file", bad], agent="jobhunter-applier")
        self.assertEqual(env["code"], "E_SCHEMA")
        rc, env = self.run_cli(["job", "show", uid], agent="jobhunter-evaluator")
        self.assertEqual((rc, env["data"]["job"]["job_uid"]), (0, uid))
        rc, env = self.run_cli(["job", "human-call", uid, "never"])
        self.assertEqual((rc, env["data"]["status"]), (0, "closed"))
        rc, env = self.run_cli(["job", "list", "--status", "closed"])
        self.assertEqual([j["job_uid"] for j in env["data"]["jobs"]], [uid])
        rc, env = self.run_cli(["job", "human-call", uid, "never"], agent="jobhunter-scout")
        self.assertNotEqual(rc, 0)

    def test_system_caller_can_add_a_file_anywhere(self):
        path = os.path.join(paths.root(), "exports", "ingest.json")
        payload = copy.deepcopy(fixture_json("ingest_browser.json"))
        payload["discovered_via"] = "human"
        with open(path, "w") as fh:
            json.dump(payload, fh)
        rc, env = self.run_cli(["job", "add", "--file", path])
        self.assertEqual(rc, 0, env)
        uid = env["data"]["results"][0]["job_uid"]
        self.assertEqual(self.job(uid)["discovered_via"], "human")


class TestSharedKeyLinking(IngestCase):
    """One listing whose keys match two stored jobs (its board page is job 1, its apply URL is job 2 stored under
    a company name that resolved to another company): the two rows are linked instead of both staying live."""
    GH_URL = "https://job-boards.greenhouse.io/kestrellabs/jobs/4012345"

    def setUp(self):
        super().setUp()
        # the board names a parent brand, the ATS the hiring entity: two companies, so no fingerprint match
        self.li = item(source_url="https://www.linkedin.com/jobs/view/3712345678/", company="Harborline Group",
                       title="Data Analyst", native_ids={"board_job_id": "3712345678"})
        self.gh = item(source_url=self.GH_URL, company="Kestrel Labs", title="Data Analyst",
                       native_ids={"ats_job_id": "4012345"})

    def two_jobs(self, gh_via="browser", **kw):
        r1 = self.ingest({"source": "linkedin", "jobs": [self.li]})
        gh = self.gh
        if gh_via == "api":
            gh = {k: v for k, v in gh.items() if k not in ("jd_text", "native_ids")}
            gh["tenant"] = {"ats": "greenhouse", "tenant": "kestrellabs"}
        r2 = self.ingest({"source": "greenhouse", "jobs": [gh]}, via=gh_via, **kw)
        a, b = self.job(r1[0]["job_uid"]), self.job(r2[0]["job_uid"])
        self.assertNotEqual(a["company_id"], b["company_id"])     # the company split the issue needs
        return a, b

    def relink(self):
        return self.ingest({"source": "linkedin", "jobs": [dict(self.li, apply_url=self.GH_URL)]})

    def linked_events(self):
        import glob
        out = []
        for path in sorted(glob.glob(os.path.join(paths.logs_dir(), "events-*.jsonl"))):
            with open(path) as fh:
                out += [e for e in (json.loads(line) for line in fh if line.strip()) if e["kind"] == "job_linked"]
        return out

    def test_second_row_is_closed_as_a_duplicate_of_the_first(self):
        a, b = self.two_jobs()
        self.assertEqual((a["status"], b["status"]), ("eval_queued", "eval_queued"))
        res = self.relink()
        self.assertEqual((res[0]["outcome"], res[0]["job_uid"], res[0]["reason_code"], res[0]["duplicate_of"]),
                         ("duplicate", a["job_uid"], "shared_key", a["job_uid"]))
        a2, b2 = self.job(a["job_uid"]), self.job(b["job_uid"])
        self.assertEqual((a2["status"], a2["duplicate_of"]), ("eval_queued", None))
        self.assertEqual((b2["status"], b2["status_reason"], b2["duplicate_of"]), ("closed", "duplicate", a["id"]))
        ev = self.linked_events()
        self.assertEqual([(e["job_uid"], e["duplicates"]) for e in ev], [(a["job_uid"], [b["job_uid"]])])
        # the closed row cannot be revived by the human call, and `job show` names the original
        with db.tx(self.conn):
            d = self.assertDenied("E_DUP_JOB", jobs.set_human_call, self.conn, b["job_uid"], "apply_anyway",
                                  "human:cli")
        self.assertEqual(d.data["duplicate_of"], a["job_uid"])
        self.assertEqual(jobs.show(self.conn, b["job_uid"])["duplicate_of"], a["job_uid"])
        # repeating the listing is idempotent; the second row's own URL reports the original
        again = self.relink()
        self.assertEqual((again[0]["outcome"], again[0]["reason_code"], again[0]["duplicate_of"]),
                         ("duplicate", None, a["job_uid"]))
        self.assertEqual(len(self.linked_events()), 1)
        own = self.ingest({"source": "greenhouse", "jobs": [self.gh]})
        self.assertEqual((own[0]["job_uid"], own[0]["duplicate_of"]), (b["job_uid"], a["job_uid"]))
        # a later listing whose fingerprint matches the closed row is a duplicate of the survivor
        fp = self.ingest({"source": "naukri", "jobs": [item(company="Kestrel Labs")]})
        self.assertEqual((fp[0]["outcome"], fp[0]["reason_code"], fp[0]["duplicate_of"]),
                         ("duplicate", "fingerprint", a["job_uid"]))
        self.assertEqual(self.job(fp[0]["job_uid"])["duplicate_of"], a["id"])

    def test_new_keys_go_to_the_survivor(self):
        a, b = self.two_jobs()
        li = dict(self.li, apply_url=self.GH_URL,
                  redirect_urls=["https://job-boards.greenhouse.io/kestrellabs/jobs/4012399"])
        res = self.ingest({"source": "linkedin", "jobs": [li]})
        self.assertEqual((res[0]["outcome"], res[0]["job_uid"], res[0]["reason_code"]),
                         ("alias_added", a["job_uid"], "shared_key"))
        owner = self.conn.execute("SELECT job_id FROM job_keys WHERE key = 'ats:greenhouse:4012399'").fetchone()
        self.assertEqual(owner[0], a["id"])

    def test_a_new_row_becomes_duplicate(self):
        a, b = self.two_jobs(gh_via="api", fetch_jd_later=True)
        self.assertEqual(b["status"], "new")
        self.relink()
        b2 = self.job(b["job_uid"])
        self.assertEqual((b2["status"], b2["status_reason"], b2["duplicate_of"]), ("duplicate", "shared_key", a["id"]))

    def test_an_evaluating_row_loses_its_claim(self):
        from jobhunter import evaluate
        a, b = self.two_jobs()
        with db.tx(self.conn):
            evaluate.claim(self.conn, 5, "C20260927T050000ZAAAA", profile=self.deps.profile, config=self.deps.cfg)
        self.assertEqual(self.job(b["job_uid"])["status"], "evaluating")
        self.relink()
        b2 = self.job(b["job_uid"])
        self.assertEqual((b2["status"], b2["claimed_by"], b2["duplicate_of"]), ("closed", None, a["id"]))
        self.assertEqual(self.job(a["job_uid"])["status"], "evaluating")

    def test_the_row_with_an_application_survives(self):
        from jobhunter import jobstate
        a, b = self.two_jobs()
        with db.tx(self.conn):
            for st in ("evaluating", "eligible", "apply_queued", "applying", "applied"):
                jobstate.set_job_status(self.conn, b["id"], st, "test", "test")
        res = self.relink()
        self.assertEqual((res[0]["job_uid"], res[0]["reason_code"], res[0]["duplicate_of"]),
                         (b["job_uid"], "shared_key", b["job_uid"]))
        a2, b2 = self.job(a["job_uid"]), self.job(b["job_uid"])
        self.assertEqual((a2["status"], a2["duplicate_of"]), ("closed", b["id"]))
        self.assertEqual((b2["status"], b2["duplicate_of"]), ("applied", None))

    def test_an_excluded_row_cannot_be_requeued(self):
        from jobhunter import evaluate, jobstate
        a, b = self.two_jobs()
        with db.tx(self.conn):
            jobstate.set_job_status(self.conn, b["id"], "excluded", "excluded_company", "test")
        self.relink()
        b2 = self.job(b["job_uid"])
        self.assertEqual((b2["status"], b2["duplicate_of"]), ("excluded", a["id"]))
        with db.tx(self.conn):
            d = self.assertDenied("E_DUP_JOB", evaluate.requeue, self.conn, job_uid=b["job_uid"],
                                  profile=self.deps.profile, config=self.deps.cfg)
        self.assertEqual(d.data["duplicate_of"], a["job_uid"])


if __name__ == "__main__":
    import unittest
    unittest.main()


# ---------------------------------------------------------------- the API lane (sources fetch)
from unittest import mock  # noqa: E402

from jobhunter import sources  # noqa: E402
from jobhunter.sources import greenhouse, himalayas, remotive  # noqa: E402
from tests.fakes.u2 import FakeClient, HttpError, NotModified, RateLimited  # noqa: E402

GH_LIST = greenhouse.list_url("kestrelcommerce")


def only(cfg, *ids):
    for k in cfg["sources"]["api"]:
        cfg["sources"]["api"][k] = k in ids
    return cfg


class TestApiLane(IngestCase):
    def setUp(self):
        super().setUp()
        only(self.deps.cfg, "greenhouse")
        with open(os.path.join(paths.private_dir(), "targets.csv"), "w") as fh:
            fh.write("company,ats,tenant\n# a comment line\nKestrel Commerce,greenhouse,kestrelcommerce\n"
                     "Bad Row,nosuchats,x\n")

    def gh_client(self, **extra):
        routes = {GH_LIST: fixture_json("greenhouse_list.json"),
                  GH_LIST + "/4012345": fixture_json("greenhouse_detail.json")}
        routes.update(extra)
        return FakeClient(routes)

    def run_fetch(self, client, **kw):
        return sources.run_fetch(self.conn, client=client, **kw)

    def test_fetch_ingest_and_detail(self):
        client = self.gh_client()
        s = self.run_fetch(client)
        self.assertEqual((s["fetched"], s["new"], s["prefilter_rejected"], s["eval_queued"], s["jd_fetched"]),
                         (3, 1, 2, 1, 1), s)
        self.assertEqual(s["errors"], [])
        self.assertEqual([c[1] for c in client.calls], [GH_LIST, GH_LIST + "/4012345"])
        row = self.conn.execute("SELECT j.status, j.apply_url, t.jd_text FROM jobs j JOIN job_texts t ON "
                                "t.job_id = j.id WHERE j.canonical_key = 'ats:greenhouse:4012345'").fetchone()
        self.assertEqual(row["status"], "eval_queued")
        self.assertIn("forecasting", row["jd_text"])
        tenant = self.conn.execute("SELECT company_id, source, validated_at FROM ats_tenants WHERE ats = "
                                   "'greenhouse' AND tenant = 'kestrelcommerce'").fetchone()
        self.assertEqual(tenant["source"], "targets_file")
        self.assertIsNotNone(tenant["company_id"])
        self.assertIsNotNone(tenant["validated_at"])
        st = self.conn.execute("SELECT next_fetch_at, last_status, etag FROM sources_state WHERE source = "
                               "'greenhouse' AND tenant = 'kestrelcommerce'").fetchone()
        self.assertEqual(st["last_status"], 200)
        self.assertEqual(st["etag"], 'W/"etag-1"')
        self.assertEqual(st["next_fetch_at"], "2026-09-27T17:00:00Z")
        n = self.conn.execute("SELECT SUM(n) FROM counters WHERE platform = 'api:greenhouse'").fetchone()[0]
        self.assertEqual(n, 2)
        # nothing is due on the next run
        client2 = self.gh_client()
        s2 = self.run_fetch(client2)
        self.assertEqual((client2.calls, s2["sources"]), ([], 0))
        # twelve hours later the board is due again; everything is a duplicate
        self.clock.advance(hours=12, minutes=1)
        s3 = self.run_fetch(self.gh_client())
        self.assertEqual((s3["duplicates"], s3["new"]), (3, 0))

    def test_not_modified_and_not_found(self):
        s = self.run_fetch(self.gh_client(**{GH_LIST: NotModified(304, GH_LIST)}))
        self.assertEqual((s["not_modified"], s["fetched"]), (1, 0))
        self.clock.advance(hours=13)
        s = self.run_fetch(FakeClient({}))
        self.assertEqual(s["errors"][0]["error"], "not_found")
        active = self.conn.execute("SELECT active FROM ats_tenants WHERE tenant = 'kestrelcommerce'").fetchone()[0]
        self.assertEqual(active, 0)

    def test_rate_limit_trips_the_source_breaker(self):
        s = self.run_fetch(FakeClient({GH_LIST: RateLimited(429, GH_LIST)}))
        self.assertEqual(s["errors"][0]["error"], "rate_limited")
        br = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'api:greenhouse'").fetchone()
        self.assertEqual((br["state"], br["reason_code"]), ("open", "http_429"))
        self.clock.advance(minutes=5)
        client = self.gh_client()
        self.run_fetch(client, source="greenhouse")
        self.assertEqual(client.calls, [])

    def test_three_errors_trip_the_breaker(self):
        for i in range(3):
            s = self.run_fetch(FakeClient({GH_LIST: HttpError(503, GH_LIST)}), source="greenhouse")
            self.assertEqual(s["errors"][0]["error"], "HttpError")
        br = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'api:greenhouse'").fetchone()
        self.assertEqual((br["state"], br["reason_code"]), ("open", "consecutive_errors"))

    def test_clock_skew_trips_global(self):
        client = self.gh_client()
        client.first_date = "Sun, 27 Sep 2026 06:00:00 GMT"      # one hour past the test clock
        with self.assertRaises(Exception) as cm:
            self.run_fetch(client)
        self.assertEqual(getattr(cm.exception, "code", None), "E_CLOCK_SKEW")
        br = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'global'").fetchone()
        self.assertEqual((br["state"], br["reason_code"]), ("open", "clock_skew"))
        self.assertDenied("E_BREAKER_OPEN", self.run_fetch, self.gh_client())

    def test_unconfirmed_profile_fetches_nothing(self):
        self.deps.profile = dict(self.deps.profile, confirmed=False)
        client = self.gh_client()
        s = self.run_fetch(client)
        self.assertEqual((s["skipped"], client.calls), ("profile_unconfirmed", []))

    def test_paused(self):
        with open(paths.paused_file(), "w") as fh:
            fh.write("x")
        self.assertDenied("E_PAUSED", self.run_fetch, self.gh_client())
        self.assertDenied("E_USAGE", sources.run_fetch, self.conn, lane="browser")

    def test_remotive_daily_cap_and_harvest(self):
        only(self.deps.cfg, "remotive", "himalayas", "lever")
        self.deps.cfg["sources"]["remotive_max_calls_day"] = 1
        routes = {remotive.API: fixture_json("remotive_list.json"),
                  himalayas.SEARCH % "India": fixture_json("himalayas_list.json")}
        s = self.run_fetch(FakeClient(routes))
        self.assertEqual(s["sources"], 2, s)
        # the Lever board behind a Himalayas listing becomes a polled tenant (companies.resolve records it)
        t = self.conn.execute("SELECT ats, tenant, active FROM ats_tenants WHERE ats = 'lever'").fetchall()
        self.assertEqual([tuple(r) for r in t], [("lever", "tidemark", 1)])
        self.assertEqual(sources._tenants(self.conn, "lever"), ["tidemark"])
        self.clock.advance(hours=7)
        client = FakeClient(routes)
        self.run_fetch(client, source="remotive")
        self.assertEqual(client.calls, [])                        # the 24-hour cap is used up

    def test_workday_tenant_forms(self):
        wd = {"source_url": "https://kestrelfreight.wd5.myworkdayjobs.com/External/job/Pune/Analyst_JR-9",
              "company": "Kestrel Freight", "title": "Data Analyst", "location": "Bengaluru", "jd_text": "x"}
        self.ingest({"source": "linkedin_jobs", "jobs": [wd]})
        rows = sorted(tuple(r) for r in self.conn.execute("SELECT tenant, source FROM ats_tenants WHERE ats = "
                                                          "'workday'"))
        self.assertIn(("kestrelfreight.wd5/External", "harvest"), rows)
        self.assertEqual(sources._tenants(self.conn, "workday"), ["kestrelfreight.wd5/External"])

    def test_cli_fetch_list_and_add_tenant(self):
        client = self.gh_client()

        def run(argv):
            out = io.StringIO()
            with mock.patch.object(sources, "make_client", lambda cfg: client):
                rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
            return rc, json.loads(out.getvalue())

        rc, env = run(["sources", "fetch", "--lane", "api"])
        self.assertEqual((rc, env["data"]["eval_queued"]), (0, 1), env)
        rc, env = run(["sources", "list"])
        gh = [r for r in env["data"]["sources"] if r["id"] == "greenhouse"][0]
        self.assertEqual((gh["enabled"], gh["due"], gh["tenants"], gh["last_status"]), (True, False, 1, 200))
        self.assertIn("naukri", [r["id"] for r in env["data"]["sources"] if r["lane"] == "browser"])
        rc, env = run(["sources", "add-tenant", "--ats", "greenhouse", "--tenant", "kestrelcommerce"])
        self.assertEqual((rc, env["data"]["jobs_seen"]), (0, 3), env)
        rc, env = run(["sources", "add-tenant", "--ats", "greenhouse", "--tenant", "nosuchboard"])
        self.assertEqual((rc, env["code"]), (10, "E_VALIDATION"))
        rc, env = run(["sources", "add-tenant", "--ats", "greenhouse", "--tenant", "bad token!"])
        self.assertEqual(env["code"], "E_VALIDATION")
        client.routes = {GH_LIST: HttpError(500, GH_LIST)}
        self.clock.advance(hours=13)
        rc, env = run(["sources", "fetch", "--lane", "api"])
        self.assertEqual((rc, env["code"]), (12, "E_NETWORK"))
