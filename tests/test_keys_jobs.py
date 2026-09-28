"""Job keys (design 2.4.1): ATS global ids, board ids, post ids, canonical URLs; invented keys refused."""
from __future__ import annotations

import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import keys
from jobhunter.errors import Denied

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "core", "keys_jobs.json")


class TestJobKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIX, encoding="utf-8") as fh:
            cls.fx = json.load(fh)

    def test_fixture_groups_share_one_key(self):
        for group in self.fx["same_key"]:
            for item in group["inputs"]:
                with self.subTest(group=group["name"], url=item["url"]):
                    key, aliases = keys.job_key(item["url"], item.get("native_ids") or {}, item.get("source"))
                    self.assertEqual(key, group["key"])
                    self.assertTrue(all(a.startswith(("board:", "post:", "url:")) for a in aliases))

    def test_different_jobs_differ(self):
        for a, b in self.fx["different"]:
            self.assertNotEqual(keys.job_key(a, {}, None)[0], keys.job_key(b, {}, None)[0])

    def test_invented_keys_refused(self):
        for item in self.fx["invented"]:
            with self.subTest(url=item["url"]):
                with self.assertRaises(Denied) as cm:
                    keys.job_key(item["url"], item["native_ids"], item["source"])
                self.assertEqual(cm.exception.code, "E_INVENTED_KEY")
        with self.assertRaises(Denied) as cm:
            keys.job_key("https://example.com/job", {"other_id": "1"}, None)
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        with self.assertRaises(Denied):
            keys.job_key("ftp://example.com/job", {}, None)

    def test_recruitee_offer_id_from_the_api(self):
        """2.4.1: <company>.recruitee.com/o/<slug> with the offer id from the API; the id is not in the URL."""
        url = "https://kestrel.recruitee.com/o/data-analyst"
        self.assertEqual(keys.job_key(url, {"ats_job_id": "412345"}, "recruitee")[0], "ats:recruitee:kestrel:412345")
        self.assertEqual(keys.job_key("https://kestrel.recruitee.com/l/en/o/data-analyst", {"ats_job_id": 412345},
                                      "recruitee")[0], "ats:recruitee:kestrel:412345")
        for native, source, u in (({"ats_job_id": "412345"}, "linkedin", url),          # not the API lane
                                  ({"ats_job_id": "412345"}, None, url),
                                  ({"ats_job_id": "abc"}, "recruitee", url),             # not an offer id
                                  ({"ats_job_id": "412345"}, "recruitee", "https://kestrel.example/o/data-analyst"),
                                  ({"ats_job_id": "412345"}, "recruitee", "https://kestrel.recruitee.com/api/offers")):
            with self.subTest(native=native, source=source, url=u):
                with self.assertRaises(Denied) as cm:
                    keys.job_key(u, native, source)
                self.assertEqual(cm.exception.code, "E_INVENTED_KEY")
        # without the id the careers URL key is used, as before
        self.assertTrue(keys.job_key(url, {}, "recruitee")[0].startswith("url:"))

    def test_canonical_url(self):
        c = keys.canonical_url
        self.assertEqual(c("HTTPS://WWW.Kestrel.Example/Careers/Data/apply/?utm_source=x&b=2&a=1&trk=abc#top"),
                         "https://kestrel.example/Careers/Data?a=1&b=2")
        self.assertEqual(c("https://m.kestrel.example/jobs/1/application"), "https://kestrel.example/jobs/1")
        self.assertEqual(c("https://kestrel.example/jobs/1?refId=9&gh_src=1&lever-origin=x&from=feed"),
                         "https://kestrel.example/jobs/1")
        a, _ = keys.job_key("https://kestrel.example/jobs/1?utm_medium=x", {}, None)
        b, _ = keys.job_key("https://www.kestrel.example/jobs/1/", {}, None)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("url:"))

    def test_titles_cities_fingerprint(self):
        self.assertEqual(keys.norm_title("Sr. Data Analyst (Remote) - R12345"), "senior data analyst")
        self.assertEqual(keys.norm_title("Data Analyst & BI Mgr"), "data analyst and bi manager")
        self.assertEqual(keys.norm_city("Bangalore, Karnataka, India"), "bengaluru")
        self.assertEqual(keys.norm_city("Bombay / Remote"), "mumbai")
        self.assertEqual(keys.norm_city("Trivandrum or Kochi"), "thiruvananthapuram")
        for canon_name, aliases in keys._CODE_CITY_ALIASES.items():     # renames kept in code, not the JSON
            for a in aliases:
                self.assertEqual(keys.norm_city(a.title() + ", India"), canon_name)
        self.assertIsNone(keys.norm_city(""))
        self.assertEqual(keys.role_key("Senior Data Analyst, Returns"), "analyst data returns senior")
        self.assertAlmostEqual(keys.jaccard("analyst data", "analyst data senior"), 2 / 3.0)
        self.assertEqual(keys.fingerprint("KAAAAAAA", "Sr Data Analyst", "Bangalore"),
                         keys.fingerprint("KAAAAAAA", "Senior Data Analyst", "Bengaluru"))
        self.assertNotEqual(keys.fingerprint("KAAAAAAA", "Data Analyst", "Pune"),
                            keys.fingerprint("KBBBBBBB", "Data Analyst", "Pune"))


if __name__ == "__main__":
    unittest.main()
