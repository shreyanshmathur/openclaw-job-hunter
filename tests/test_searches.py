"""U2: browser searches for the scout (generate, private/searches.json, due, hand-out, page budgets)."""
from __future__ import annotations

import copy
import io
import json
import os
import urllib.parse

import tests  # noqa: F401
from jobhunter import cli, db, paths, searches
from tests.fakes.u2 import config, install, profile_fixture
from tests.helpers import HomeTestCase, raw_meta

CYCLE = "C20260927T050000ZAAAA"


class TestGenerate(HomeTestCase):
    def test_generate_from_profile(self):
        prof = profile_fixture()
        items = searches.generate(prof, config())
        sites = sorted({s["site"] for s in items})
        self.assertEqual(sites, ["naukri", "wellfound"])       # naukri from the profile, wellfound from config
        naukri = [s for s in items if s["site"] == "naukri"]
        self.assertEqual(len(naukri), 4)                        # 2 titles x (bengaluru + remote)
        urls = {s["url"] for s in naukri}
        self.assertIn("https://www.naukri.com/data-analyst-jobs-in-bengaluru?jobAge=3", urls)
        self.assertIn("https://www.naukri.com/business-analyst-jobs?wfhType=2&jobAge=3", urls)
        for s in items:
            self.assertTrue(s["url"].startswith("https://"))
            s["url"].encode("ascii")
            self.assertEqual(s["search_id"], searches.search_id(s["site"], s["query"],
                                                                None if s["remote"] else s["location"], s["remote"]))
        ids = [s["search_id"] for s in items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_linkedin_needs_the_switch(self):
        prof = copy.deepcopy(profile_fixture())
        cfg = config()
        cfg["boards"]["sites"]["linkedin_jobs"]["discover"] = "browser"
        self.assertNotIn("linkedin_jobs", searches.enabled_sites(prof, cfg))
        prof["fields"]["linkedin"]["value"]["enabled"] = True
        self.assertIn("linkedin_jobs", searches.enabled_sites(prof, cfg))
        url = searches.build_url("linkedin_jobs", "Data Analyst", "bengaluru", False, "IN")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual((q["keywords"], q["location"], q["f_TPR"]), (["Data Analyst"], ["bengaluru"], ["r86400"]))
        posts = searches.build_url("linkedin_posts", "Data Analyst", None, True, "IN")
        self.assertIn("keywords=hiring%20Data%20Analyst%20remote", posts)
        cfg["boards"]["sites"]["naukri"] = {"discover": "api"}
        self.assertNotIn("naukri", searches.enabled_sites(prof, cfg))

    def test_country_hosts(self):
        self.assertTrue(searches.build_url("indeed", "Data Analyst", "bengaluru", False, "IN")
                        .startswith("https://in.indeed.com/jobs?q=Data%20Analyst&l=bengaluru"))
        self.assertTrue(searches.build_url("glassdoor", "Data Analyst", None, True, None)
                        .startswith("https://www.glassdoor.com/Job/jobs.htm?"))
        self.assertIsNone(searches.build_url("unknown_site", "x", None, False, None))


class TestDue(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.deps = install(self)
        items = searches.generate(self.deps.profile, self.deps.cfg)
        self.written = searches.write_file(items, "pv-test-1")

    def due(self, cycle=None):
        with db.tx(self.conn):
            return searches.due(self.conn, cycle)

    def test_file_merge_keeps_person_edits(self):
        self.assertTrue(os.path.exists(searches.searches_file()))
        self.assertEqual(oct(os.stat(searches.searches_file()).st_mode & 0o777), "0o600")
        with open(searches.searches_file()) as fh:
            data = json.load(fh)
        data["searches"][0]["enabled"] = False
        with open(searches.searches_file(), "w") as fh:
            json.dump(data, fh)
        res = searches.write_file(searches.generate(self.deps.profile, self.deps.cfg), "pv-test-1")
        self.assertEqual(res["added"], 0)
        self.assertFalse(searches.load_file()[0]["enabled"])
        res = searches.write_file(searches.generate(self.deps.profile, self.deps.cfg), "pv-test-1", overwrite=True)
        self.assertTrue(searches.load_file()[0]["enabled"])

    def test_due_budgets_and_hand_out(self):
        due = self.due()
        by_site: dict = {}
        for s in due:
            by_site.setdefault(s["site"], []).append(s)
        # naukri: 20 pages a day over at most 3 scout cycles = 6 per cycle, 3 searches of 2 pages
        self.assertEqual([s["page_budget"] for s in by_site["naukri"]], [2, 2, 2])
        # wellfound: 10 pages a day = 3 per cycle, 3 searches of 1 page
        self.assertEqual([s["page_budget"] for s in by_site["wellfound"]], [1, 1, 1])
        self.assertEqual(searches.due_count(self.conn), len(due))
        with db.tx(self.conn):
            searches.mark_handed_out(self.conn, due, CYCLE)
        again = self.due(CYCLE)                         # the same cycle asks again: same list
        self.assertEqual([s["search_id"] for s in again], [s["search_id"] for s in due])
        rest = self.due("C20260927T050500ZBBBB")        # another cycle gets the searches not handed out yet
        self.assertEqual(len(rest), 2)
        self.assertFalse({s["search_id"] for s in rest} & {s["search_id"] for s in due})
        with db.tx(self.conn):
            searches.mark_handed_out(self.conn, rest, "C20260927T050500ZBBBB")
        self.assertEqual(self.due("C20260927T051000ZCCCC"), [])
        self.clock.advance(hours=25)
        self.assertEqual(len(self.due("C20260928T060000ZDDDD")), len(due))

    def test_breakers_pause_and_linkedin_switch(self):
        from jobhunter import breakers
        with db.tx(self.conn):
            breakers.trip(self.conn, "site:naukri", "captcha", "test")
        self.assertEqual({s["site"] for s in self.due()}, {"wellfound"})
        with open(paths.paused_file(), "w") as fh:
            fh.write("paused")
        self.assertEqual(self.due(), [])
        os.remove(paths.paused_file())
        with db.tx(self.conn):
            breakers.trip(self.conn, "global", "clock_skew", "test")
        self.assertEqual(self.due(), [])

    def test_linkedin_sites_need_the_database_switch(self):
        prof = copy.deepcopy(self.deps.profile)
        prof["fields"]["linkedin"]["value"]["enabled"] = True
        prof["fields"]["sites_enabled"]["value"] = ["linkedin_jobs"]
        self.deps.profile = prof
        searches.write_file(searches.generate(prof, self.deps.cfg), "pv-test-1", overwrite=True)
        self.assertEqual({s["site"] for s in self.due()}, {"wellfound"})
        with db.tx(self.conn):
            raw_meta(self.conn, "channel_linkedin_enabled", "1")
        self.assertEqual({s["site"] for s in self.due()}, {"linkedin_jobs", "wellfound"})

    def test_cli_searches_due_and_generate(self):
        out = io.StringIO()
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-scout"}
        rc = cli.main(["--cycle", CYCLE, "searches", "due"], env=env, stdin=io.StringIO(""), stdout=out)
        res = json.loads(out.getvalue())
        self.assertEqual(rc, 0, res)
        self.assertEqual(len(res["data"]["searches"]), 6)
        state = self.conn.execute("SELECT count(*) FROM sources_state WHERE source = 'search' AND etag = ?",
                                  (CYCLE,)).fetchone()[0]
        self.assertEqual(state, 6)
        out = io.StringIO()
        rc = cli.main(["searches", "generate", "--overwrite"], env={}, stdin=io.StringIO(""), stdout=out)
        res = json.loads(out.getvalue())
        self.assertEqual((rc, res["data"]["searches"]), (0, 8), res)
        out = io.StringIO()
        rc = cli.main(["searches", "generate"], env=env, stdin=io.StringIO(""), stdout=out)
        self.assertNotEqual(rc, 0)       # the scout cannot rewrite searches


if __name__ == "__main__":
    import unittest
    unittest.main()
