"""U2: the deterministic pre-filter (design 6.2). Pure function tests with a fictional confirmed profile."""
from __future__ import annotations

import copy
import unittest

import tests  # noqa: F401
from jobhunter import prefilter
from tests.fakes.u2 import config, profile_fixture
from tests.helpers import FakeClock


def job(**kw):
    base = {"title": "Data Analyst", "company": "Kestrel Commerce", "location": "Bengaluru", "work_mode": "hybrid",
            "remote_scope": None, "employment_type": "full_time", "posted_at": "2026-09-25",
            "years": {"min": None, "max": None}, "salary": None, "jd_text": "SQL and Python for forecasting."}
    base.update(kw)
    return base


class TestPrefilter(unittest.TestCase):
    def setUp(self):
        FakeClock("2026-09-27T05:00:00Z")
        self.addCleanup(FakeClock.stop)
        self.prof = profile_fixture()
        self.cfg = config()

    def check(self, j, prof=None, cfg=None):
        return prefilter.check(j, self.prof if prof is None else prof, self.cfg if cfg is None else cfg)

    def set_field(self, name, value, source="user_confirmed"):
        p = copy.deepcopy(self.prof)
        p["fields"][name] = {"value": value, "source": source}
        return p

    def test_good_job_passes(self):
        self.assertEqual(self.check(job()), (True, None))

    def test_profile_shapes(self):
        flat = prefilter.profile_values({"experience_years": 3, "locations": {"cities": ["bengaluru"]}})
        self.assertEqual(flat["experience_years"], 3)
        vals = prefilter.profile_values(self.prof)
        self.assertNotIn("notice_period_days", vals)       # inferred fields are ignored by every gate
        self.assertEqual(vals["experience_years"], 3.0)
        self.assertEqual(prefilter.profile_values(None), {})

    def test_stale(self):
        self.assertEqual(self.check(job(posted_at="2026-08-01")), (False, "stale"))
        self.assertEqual(self.check(job(posted_at="2026-09-01")), (True, None))
        self.assertEqual(self.check(job(can_apply=False)), (False, "stale"))
        cfg = config()
        cfg["sources"]["max_age_days"] = 10
        self.assertEqual(self.check(job(posted_at="2026-09-10"), cfg=cfg), (False, "stale"))

    def test_location_city_and_mode(self):
        self.assertEqual(self.check(job(location="Pune", work_mode="onsite")), (False, "location"))
        self.assertEqual(self.check(job(location="Pune", work_mode="hybrid")), (False, "location"))
        self.assertEqual(self.check(job(location="Bengaluru", work_mode="onsite")), (False, "location"))
        self.assertEqual(self.check(job(location="Bangalore, Karnataka, India", work_mode="hybrid")), (True, None))
        self.assertEqual(self.check(job(location="Mumbai / Bengaluru", work_mode="unknown")), (True, None))
        self.assertEqual(self.check(job(location="Mumbai", work_mode="unknown")), (False, "location"))
        self.assertEqual(self.check(job(location=None, work_mode="unknown")), (True, None))
        self.assertEqual(self.check(job(location="India", work_mode="hybrid")), (True, None))   # no city named
        p = copy.deepcopy(self.prof)
        p["fields"]["locations"]["value"]["relocate"] = True
        self.assertEqual(self.check(job(location="Pune", work_mode="hybrid"), prof=p), (True, None))

    def test_remote_scope(self):
        self.assertEqual(self.check(job(location="Remote", work_mode="remote", remote_scope="Worldwide")),
                         (True, None))
        self.assertEqual(self.check(job(location="Remote (US only)", work_mode="unknown")), (False, "location"))
        self.assertEqual(self.check(job(location="Remote", work_mode="remote", remote_scope="APAC")), (True, None))
        self.assertEqual(self.check(job(location="Remote", work_mode="remote", remote_scope="United States")),
                         (False, "location"))
        self.assertEqual(self.check(job(location="Remote", work_mode="remote",
                                        jd_text="Candidates must be located in the United States.")),
                         (False, "location"))
        ok, code, why = prefilter.explain(job(location="Remote", work_mode="remote", remote_scope="EMEA"), self.prof,
                                          self.cfg)
        self.assertEqual(code, "location")
        self.assertIn("restricted", why)
        p = self.set_field("locations", {"cities": ["bengaluru"], "work_modes": ["hybrid"]})
        self.assertEqual(self.check(job(location="Remote", work_mode="remote"), prof=p), (False, "location"))

    def test_years(self):
        self.assertEqual(self.check(job(years={"min": 7, "max": None})), (False, "years_required"))
        self.assertEqual(self.check(job(years={"min": 6, "max": 9})), (True, None))   # 6 <= 3 + 3
        self.assertEqual(self.check(job(years={"min": 0, "max": 1})), (False, "years_required"))
        self.assertEqual(self.check(job(jd_text="We need 8+ years of experience in analytics.")),
                         (False, "years_required"))
        self.assertEqual(self.check(job(jd_text="8+ years overall, 2+ years with Tableau.")), (True, None))
        self.assertEqual(self.check(job(jd_text="2\u20134 years of SQL")), (True, None))
        self.assertEqual(prefilter.years_required(prefilter.job_view(job(jd_text="2 to 4 yrs, 3-5 years"))), (2, 5))
        self.assertEqual(self.check(job(jd_text="Founded 100+ years ago.")), (True, None))

    def test_seniority_and_role_family(self):
        self.assertEqual(self.check(job(title="Director of Analytics")), (False, "seniority_title"))
        self.assertEqual(self.check(job(title="Analytics Intern")), (False, "seniority_title"))
        self.assertEqual(self.check(job(title="Junior Data Analyst")), (False, "seniority_title"))
        self.assertEqual(self.check(job(title="Senior Data Analyst")), (True, None))
        self.assertEqual(self.check(job(title="Principal Data Analyst")), (False, "seniority_title"))
        self.assertEqual(self.check(job(title="Backend Engineer")), (False, "role_family"))
        self.assertEqual(self.check(job(title="Sales Analyst")), (False, "role_family"))
        self.assertEqual(self.check(job(title="Business Analyst")), (True, None))
        p = copy.deepcopy(self.prof)
        del p["fields"]["role_families"]
        del p["fields"]["seniority"]
        self.assertEqual(self.check(job(title="Backend Engineer"), prof=p), (True, None))

    def test_employment_type(self):
        self.assertEqual(self.check(job(employment_type="part_time")), (False, "employment_type"))
        self.assertEqual(self.check(job(employment_type="contract")), (True, None))
        p = self.set_field("employment_types", ["full_time"])
        self.assertEqual(self.check(job(employment_type="contract"), prof=p), (False, "employment_type"))

    def test_comp_below_floor(self):
        low = {"min": 600000, "max": 900000, "currency": "INR", "period": "year"}
        self.assertEqual(self.check(job(salary=low)), (False, "comp_below_floor"))
        monthly = {"min": 100000, "max": 150000, "currency": "INR", "period": "month"}   # 1.8M a year
        self.assertEqual(self.check(job(salary=monthly)), (True, None))
        usd = {"min": 10000, "max": 20000, "currency": "USD", "period": "year"}          # other currency: no rule
        self.assertEqual(self.check(job(salary=usd)), (True, None))
        nocur = {"min": 1, "max": 2, "currency": None, "period": "year"}
        self.assertEqual(self.check(job(salary=nocur)), (True, None))

    def test_language_and_authorization(self):
        self.assertEqual(self.check(job(jd_text="Fluent German is required.")), (False, "language_or_authorization"))
        self.assertEqual(self.check(job(jd_text="Fluency in Hindi helps.")), (True, None))
        self.assertEqual(self.check(job(jd_text="You must be authorized to work in the United States. We do not "
                                                "sponsor visas.")), (False, "language_or_authorization"))
        self.assertEqual(self.check(job(jd_text="You must be authorized to work in India.")), (True, None))

    def test_agency_only_when_the_profile_says_so(self):
        j = job(jd_text="Hiring for a leading MNC client in Bengaluru.")
        self.assertEqual(self.check(j), (True, None))
        p = self.set_field(prefilter.AGENCY_SKIP_FIELD, True)
        self.assertEqual(self.check(j, prof=p), (False, "agency_unnamed"))

    def test_unconfirmed_profile_rejects_nothing_but_stale(self):
        self.assertEqual(self.check(job(title="Backend Engineer", location="Pune"), prof={}), (True, None))
        self.assertEqual(self.check(job(posted_at="2026-01-01"), prof={}), (False, "stale"))

    def test_sentences(self):
        ok, code, why = prefilter.explain(job(years={"min": 7, "max": None}), self.prof, self.cfg)
        self.assertEqual((ok, code), (False, "years_required"))
        self.assertEqual(why, "Needs 7 or more years; you have 3")
        for code in prefilter.REASON_CODES + prefilter.EXCLUSION_CODES:
            self.assertTrue(prefilter.reason_sentence(code))


if __name__ == "__main__":
    unittest.main()
