"""U2: API source parsers against saved fictional fixtures (no network), plus the HTTP helpers."""
from __future__ import annotations

import datetime as dt
import unittest

import tests  # noqa: F401
from jobhunter import canon, jobs
from jobhunter.sources import (ashby, bamboohr, greenhouse, hn, himalayas, http, jobicy, lever, recruitee, remoteok,
                               remotive, serpapi, smartrecruiters, workable, workday, workingnomads, wwr, yc)
from tests.fakes.u2 import FakeClient, fixture_json, fixture_text
from tests.helpers import FakeClock


class ItemContract(unittest.TestCase):
    """Every parsed listing must pass the 12.1 validator and give job keys through U1's keys.job_key."""

    def setUp(self):
        self.clock = FakeClock("2026-09-27T05:00:00Z")
        self.addCleanup(FakeClock.stop)

    def check_items(self, items, source):
        self.assertTrue(items)
        _, clean = jobs.validate_payload({"source": source, "jobs": items}, "api")
        for it in clean:
            keys = jobs.derive_keys(it, source)
            self.assertTrue(keys, it["source_url"])
        return clean


class TestAtsParsers(ItemContract):
    def test_greenhouse_list_and_detail(self):
        items = greenhouse.parse_list(fixture_json("greenhouse_list.json"), "kestrelcommerce")
        self.assertEqual(len(items), 3)   # the row with a non-numeric id is skipped
        first = items[0]
        self.assertEqual(first["source_url"], "https://job-boards.greenhouse.io/kestrelcommerce/jobs/4012345")
        self.assertEqual(first["apply_url"], "https://kestrel.example/careers?gh_jid=4012345")
        self.assertEqual(first["company"], "Kestrel Commerce")
        self.assertEqual(first["posted_at"], "2026-09-22")
        self.assertIsNone(first["jd_text"])
        self.assertEqual(first["tenant"], {"ats": "greenhouse", "tenant": "kestrelcommerce"})
        self.check_items(items, "greenhouse")
        keys = jobs.derive_keys(first, "greenhouse")
        self.assertEqual(keys[0], "ats:greenhouse:4012345")
        self.assertEqual(greenhouse.detail_url(first),
                         "https://boards-api.greenhouse.io/v1/boards/kestrelcommerce/jobs/4012345")
        det = greenhouse.parse_detail(fixture_json("greenhouse_detail.json"))
        self.assertIn("2-4 years of experience with SQL and Python for forecasting", det["jd_text"])
        self.assertNotIn("<li>", det["jd_text"])
        self.assertNotIn("&lt;", det["jd_text"])

    def test_lever(self):
        items = lever.parse_list(fixture_json("lever_list.json"), "tidemark")
        self.assertEqual(len(items), 2)
        a = items[0]
        self.assertEqual(a["work_mode"], "hybrid")
        self.assertEqual(a["employment_type"], "full_time")
        self.assertEqual(a["posted_at"], "2026-09-24")
        self.assertEqual(a["salary"], {"min": 1800000.0, "max": 2600000.0, "currency": "INR", "period": "year"})
        self.assertIn("3+ years with SQL", a["jd_text"])
        self.assertEqual(items[1]["employment_type"], "internship")
        self.assertEqual(items[1]["work_mode"], "onsite")
        self.check_items(items, "lever")
        self.assertEqual(jobs.derive_keys(a, "lever")[0], "ats:lever:5d3c1a2b-0000-4000-8000-000000000001")
        self.assertEqual(lever.list_url("eu:tidemark"), "https://api.eu.lever.co/v0/postings/tidemark?mode=json")

    def test_ashby_skips_unlisted(self):
        items = ashby.parse_list(fixture_json("ashby_list.json"), "larkspur")
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["work_mode"], "remote")
        self.assertEqual(it["salary"]["max"], 1200000.0)
        self.assertEqual(it["salary"]["period"], "year")
        self.check_items(items, "ashby")

    def test_smartrecruiters(self):
        items = smartrecruiters.parse_list(fixture_json("smartrecruiters_list.json"), "HarborFinch")
        self.assertEqual(items[0]["source_url"], "https://jobs.smartrecruiters.com/HarborFinch/744000012345678")
        self.assertEqual(items[0]["work_mode"], "hybrid")
        self.check_items(items, "smartrecruiters")
        self.assertEqual(smartrecruiters.detail_url(items[0]),
                         "https://api.smartrecruiters.com/v1/companies/HarborFinch/postings/744000012345678")
        det = smartrecruiters.parse_detail(fixture_json("smartrecruiters_detail.json"))
        self.assertIn("Analyse patient flow with SQL.", det["jd_text"])

    def test_workable(self):
        items = workable.parse_list(fixture_json("workable_list.json"), "quillon")
        self.assertEqual(items[0]["company"], "Quillon Labs")
        self.assertEqual(items[0]["work_mode"], "remote")
        self.assertEqual(items[0]["source_url"], "https://apply.workable.com/quillon/j/A1B2C3D4E5/")
        self.check_items(items, "workable")

    def test_recruitee(self):
        items = recruitee.parse_list(fixture_json("recruitee_list.json"), "birchwood")
        it = items[0]
        self.assertEqual(it["work_mode"], "hybrid")
        self.assertEqual(it["salary"]["min"], 1600000.0)
        self.assertEqual(it["posted_at"], "2026-09-19")
        self.check_items(items, "recruitee")
        # 2.2 / 2.4.1: the offer id is not in the /o/<slug> URL but still gives the global ATS key
        self.assertEqual(it["source_url"], "https://birchwood.recruitee.com/o/data-analyst")
        self.assertEqual(it["native_ids"], {"ats_job_id": "1987654"})
        keys = jobs.derive_keys(it, "recruitee")
        self.assertEqual(keys[0], "ats:recruitee:birchwood:1987654")
        self.assertTrue(any(k.startswith("url:") for k in keys))

    def test_recruitee_offer_shapes(self):
        """A custom-domain careers_url falls back to the recruitee.com offer URL so the ATS key holds; a
        localized /l/<lang>/o/<slug> careers_url is kept; rows without a numeric id or a plain slug are skipped."""
        data = {"offers": [
            {"id": 2001, "slug": "ml-engineer", "title": "ML Engineer",
             "careers_url": "https://careers.birchwood.example/o/ml-engineer"},
            {"id": "2002", "slug": "bi-analyst", "title": "BI Analyst",
             "careers_url": "https://birchwood.recruitee.com/l/en/o/bi-analyst"},
            {"id": "abc", "slug": "bad-id", "title": "Bad Id"},
            {"id": True, "slug": "bool-id", "title": "Bool Id"},
            {"id": 2003, "slug": "a/b", "title": "Bad Slug"},
            {"id": 2004, "title": "No Slug"},
        ]}
        items = recruitee.parse_list(data, "birchwood")
        self.assertEqual([i["title"] for i in items], ["ML Engineer", "BI Analyst"])
        self.assertEqual(items[0]["source_url"], "https://birchwood.recruitee.com/o/ml-engineer")
        self.assertEqual(items[1]["source_url"], "https://birchwood.recruitee.com/l/en/o/bi-analyst")
        clean = self.check_items(items, "recruitee")
        self.assertEqual([jobs.derive_keys(i, "recruitee")[0] for i in clean],
                         ["ats:recruitee:birchwood:2001", "ats:recruitee:birchwood:2002"])

    def test_bamboohr(self):
        items = bamboohr.parse_list(fixture_json("bamboohr_list.json"), "example")
        self.assertEqual(items[0]["work_mode"], "hybrid")
        self.assertEqual(items[1]["employment_type"], "part_time")
        self.check_items(items, "bamboohr")
        self.assertEqual(bamboohr.detail_url(items[0]), "https://example.bamboohr.com/careers/41/detail")
        det = bamboohr.parse_detail(fixture_json("bamboohr_detail.json"))
        self.assertIn("2 to 5 years", det["jd_text"])
        self.assertEqual(det["posted_at"], "2026-09-18")

    def test_workday(self):
        items = workday.parse_list(fixture_json("workday_list.json"), "kestrelfreight.wd5/External")
        self.assertEqual(items[0]["source_url"],
                         "https://kestrelfreight.wd5.myworkdayjobs.com/External/job/Bengaluru/Data-Analyst-II_JR-004512")
        self.assertEqual(items[0]["posted_at"], "2026-09-24")
        self.assertEqual(items[0]["apply_route_hint"], "human")
        self.check_items(items, "workday")
        self.assertEqual(jobs.derive_keys(items[0], "workday")[0], "ats:workday:kestrelfreight:JR-004512")
        self.assertEqual(workday.detail_url(items[0]), "https://kestrelfreight.wd5.myworkdayjobs.com/wday/cxs/"
                         "kestrelfreight/External/job/Bengaluru/Data-Analyst-II_JR-004512")
        det = workday.parse_detail(fixture_json("workday_detail.json"))
        self.assertTrue(det["can_apply"])
        self.assertEqual(det["work_mode"], "hybrid")
        with self.assertRaises(ValueError):
            workday.split_tenant("kestrelfreight")

    def test_workday_fetch_posts_one_search_per_title(self):
        client = FakeClient({"https://kestrelfreight.wd5.myworkdayjobs.com/wday/cxs/kestrelfreight/External/jobs":
                             fixture_json("workday_list.json")})
        items = workday.fetch_list(client, "kestrelfreight.wd5/External", {"queries": ["Data Analyst",
                                                                                         "Business Analyst"]})
        self.assertEqual(len(items), 2)     # the second search returns the same postings: deduplicated
        self.assertEqual([c[0] for c in client.calls], ["POST", "POST"])
        self.assertEqual(client.calls[0][2]["searchText"], "Data Analyst")


class TestBoardParsers(ItemContract):
    def test_yc_page(self):
        items = yc.parse_page(fixture_text("yc_page.html"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source_url"], "https://www.workatastartup.com/jobs/66123")
        self.assertEqual(items[0]["years"], {"min": 3, "max": None})
        self.assertEqual(items[0]["company"], "Wrenfield")
        self.check_items(items, "yc")
        self.assertEqual(jobs.derive_keys(items[0], "yc")[0], "board:yc:66123")
        with self.assertRaises(ValueError):
            yc.parse_page("<html>no data</html>")

    def test_himalayas(self):
        items = himalayas.parse_list(fixture_json("himalayas_list.json"))
        self.assertEqual(items[0]["remote_scope"], "India, Singapore")
        self.assertEqual(items[0]["posted_at"], "2026-09-24")
        self.check_items(items, "himalayas")
        keys = jobs.derive_keys(items[0], "himalayas")
        self.assertEqual(keys[0], "ats:lever:5d3c1a2b-0000-4000-8000-000000000001")   # ATS alias ranks first
        self.assertIn("board:himalayas:data-analyst-apac", keys)

    def test_remoteok_skips_legal_row(self):
        items = remoteok.parse_list(fixture_json("remoteok_list.json"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_url"],
                         "https://remoteok.com/remote-jobs/remote-data-analyst-harbor-finch-1100231")
        self.assertEqual(items[0]["salary"]["currency"], "USD")
        self.check_items(items, "remoteok")

    def test_remotive(self):
        items = remotive.parse_list(fixture_json("remotive_list.json"))
        self.assertEqual(items[0]["remote_scope"], "APAC")
        self.assertEqual(items[0]["employment_type"], "full_time")
        self.check_items(items, "remotive")

    def test_wwr_rss(self):
        items = wwr.parse_rss(fixture_text("wwr_feed.rss"))
        self.assertEqual(items[0]["company"], "Quillon Labs")
        self.assertEqual(items[0]["title"], "Data Analyst")
        self.assertEqual(items[0]["posted_at"], "2026-09-23")
        self.check_items(items, "weworkremotely")
        with self.assertRaises(ValueError):
            wwr.parse_rss('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss></rss>')

    def test_workingnomads(self):
        items = workingnomads.parse_list(fixture_json("workingnomads_list.json"))
        self.assertEqual(items[0]["remote_scope"], "Europe")
        self.check_items(items, "workingnomads")

    def test_hn(self):
        self.assertEqual(hn.parse_story_search(fixture_json("hn_search.json")), "45100000")
        items = hn.parse_thread(fixture_json("hn_items.json"))
        self.assertEqual(len(items), 2)
        a = items[0]
        self.assertEqual(a["company"], "Wrenfield")
        self.assertEqual(a["title"], "Data Analyst")
        self.assertEqual(a["apply_email"], "jobs@wrenfield.example")
        self.assertEqual(a["apply_route_hint"], "email")
        self.assertEqual(a["work_mode"], "remote")
        self.assertEqual(a["source_url"], "https://news.ycombinator.com/item?id=45100001")
        self.check_items(items, "hn")

    def test_hn_fetch_uses_latest_hiring_story(self):
        client = FakeClient({hn.SEARCH: fixture_json("hn_search.json"),
                             hn.ITEM % "45100000": fixture_json("hn_items.json")})
        self.assertEqual(len(hn.fetch_list(client, "", {})), 2)

    def test_jobicy(self):
        items = jobicy.parse_list(fixture_json("jobicy_list.json"))
        self.assertEqual(items[0]["remote_scope"], "APAC")
        self.check_items(items, "jobicy")

    def test_serpapi_prefers_ats_link_and_needs_key(self):
        items = serpapi.parse_list(fixture_json("serpapi_list.json"))
        self.assertEqual(items[0]["source_url"], "https://job-boards.greenhouse.io/birchwoodhealth/jobs/5550001")
        self.assertEqual(items[0]["redirect_urls"], ["https://www.linkedin.com/jobs/view/4100200300"])
        self.assertEqual(items[0]["posted_at"], "2026-09-24")
        self.check_items(items, "serpapi")
        keys = jobs.derive_keys(items[0], "serpapi")
        self.assertEqual(keys[:2], ["ats:greenhouse:5550001", "board:linkedin:4100200300"])
        self.assertEqual(serpapi.fetch_list(FakeClient(), "", {"queries": ["Data Analyst"]}), [])


class TestHttpHelpers(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(http.html_to_text("<p>One&nbsp;two</p><ul><li>A</li><li>B</li></ul><script>x()</script>"),
                         "One two\n\nA\n\nB")
        self.assertEqual(http.html_to_text("&lt;p&gt;Hi&lt;/p&gt;", unescape_first=True), "Hi")
        self.assertEqual(http.html_to_text(None), "")

    def test_to_date(self):
        now = dt.datetime(2026, 9, 27, 5, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(http.to_date("2026-09-20T10:00:00Z"), "2026-09-20")
        self.assertEqual(http.to_date(1790208000), "2026-09-24")
        self.assertEqual(http.to_date(1790208000000), "2026-09-24")
        self.assertEqual(http.to_date("Wed, 23 Sep 2026 10:00:00 +0000"), "2026-09-23")
        self.assertEqual(http.to_date("Posted 3 Days Ago", now), "2026-09-24")
        self.assertEqual(http.to_date("Posted Yesterday", now), "2026-09-26")
        self.assertEqual(http.to_date("5 hours ago", now), "2026-09-27")
        self.assertIsNone(http.to_date("sometime"))

    def test_date_skew(self):
        now = canon.parse_ts("2026-09-27T05:00:00Z")
        self.assertEqual(http.date_skew_seconds("Sun, 27 Sep 2026 05:20:00 GMT", now), 1200)
        self.assertIsNone(http.date_skew_seconds(None, now))

    def test_client_refuses_plain_http(self):
        c = http.HttpClient("openclaw-job-hunter/test", sleep=lambda s: None)
        with self.assertRaises(http.HttpError):
            c.get_json("http://example.com/api")

    def test_throttle_per_host(self):
        slept = []
        t = [100.0]
        c = http.HttpClient("openclaw-job-hunter/test", min_interval_ms=1000, sleep=slept.append, clock=lambda: t[0])
        c._throttle("a.example")
        t[0] += 0.25
        c._throttle("a.example")
        c._throttle("b.example")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 0.75)

    def test_emails_and_salary(self):
        self.assertEqual(http.emails_in("Write to Jobs@Kestrel.example. or hr@kestrel.example"),
                         ["jobs@kestrel.example", "hr@kestrel.example"])
        self.assertIsNone(http.salary(None, None, "INR", "year"))
        self.assertEqual(http.salary("10", 20, "inr", "per-month-salary"),
                         {"min": 10.0, "max": 20.0, "currency": "INR", "period": "month"})


if __name__ == "__main__":
    unittest.main()
