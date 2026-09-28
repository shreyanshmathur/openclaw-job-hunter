"""E2E (INT): API discovery from a Recruitee board through the real U2 source, the U1 job keys and dedup.

`sources fetch --lane api --source recruitee` reads the offers list from a fixture (U2 FakeClient behind
`sources.make_client`; nothing reaches the network). Each offer is keyed ats:recruitee:<company>:<offer id>
(design 2.2, 2.4.1) even when its careers_url is on a custom careers domain, and an offer without a numeric id is
skipped instead of failing the batch. The same job named again by the scout (the recruitee.com URL with tracking
parameters, the custom-domain URL) is refused as a duplicate, and a second fetch adds nothing.

A job API that answers 429 trips only its own `api:<source>` breaker (code `http_429`; 5xx answers count toward
`consecutive_errors`): the source is skipped while it is open, no chat alert goes out for it, the Alerts row reads a
sentence with the "Warning" severity and asks nothing of the person, and the breaker closes by itself at
`auto_close_at` (housekeeping), after which the fetch goes through again.
Fictional companies only.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import sheets_labels, sheets_rows, sources
from jobhunter.sources import recruitee
from jobhunter.sources.http import HttpError, RateLimited
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u2 import FakeClient
from tests.fixtures.e2e.support import World, e2e_fixture

TENANT = "birchwood"


class SourcesWorld(World):
    def preflight(self, lane: str, agent: str) -> str:
        write_heartbeat()
        return super().preflight(lane, agent)


class TestRecruiteeDiscovery(unittest.TestCase):
    def setUp(self):
        self.w = SourcesWorld()
        self.addCleanup(self.w.stop)

    def fetch(self) -> dict:
        client = FakeClient({recruitee.list_url(TENANT): e2e_fixture("recruitee_list.json")})
        with mock.patch.object(sources, "make_client", lambda cfg: client):
            out = self.w.ok(["sources", "fetch", "--lane", "api", "--source", "recruitee"])
        self.assertEqual([c[1] for c in client.calls], [recruitee.list_url(TENANT)])
        return out

    def test_recruitee_offers_keyed_by_offer_id_and_deduplicated(self):
        w = self.w
        w.onboard()
        with open(os.path.join(w.home.dir, "private", "targets.csv"), "w", encoding="utf-8") as fh:
            fh.write("company,ats,tenant\nBirchwood Health,recruitee,%s\n" % TENANT)

        s = self.fetch()
        self.assertEqual(s["fetched"], 2, s)                          # the offer without a numeric id is skipped
        self.assertEqual(s["new"], 2, s)
        keys = {r[0]: r[1] for r in w.all("SELECT canonical_key, source_url FROM jobs")}
        self.assertEqual(keys, {
            "ats:recruitee:birchwood:2210001": "https://birchwood.recruitee.com/o/data-analyst-returns",
            "ats:recruitee:birchwood:2210002": "https://birchwood.recruitee.com/l/en/o/business-analyst"})
        job_uid = w.one("SELECT job_uid FROM jobs WHERE canonical_key = 'ats:recruitee:birchwood:2210001'")[0]
        n_jobs = w.one("SELECT count(*) FROM jobs")[0]

        # the scout meets the same job by the recruitee.com URL with tracking parameters, then by the custom careers
        # domain URL (no offer id there: the company, title and place fingerprint)
        base = {"company": "Birchwood Health", "company_domain": "birchwood.example", "title": "Data Analyst, Returns",
                "location": "Remote, US", "work_mode": "remote", "remote_scope": "US", "employment_type": "full_time",
                "posted_at": "2026-09-27", "years": {"min": 2, "max": 4},
                "salary": {"min": 65000, "max": 80000, "currency": "USD", "period": "year"},
                "apply_route_hint": "ats_form",
                "jd_text": "Birchwood Health needs a Data Analyst for returns forecasting. 2 to 4 years of SQL and "
                           "Python. Remote in the US.", "native_ids": {}}
        ingest = {"source": "wellfound", "discovered_via": "browser", "jobs": [
            dict(base, source_url="https://wellfound.com/jobs/2210101-data-analyst-returns",
                 apply_url="https://birchwood.recruitee.com/o/data-analyst-returns?utm_source=wellfound"),
            dict(base, source_url="https://careers.birchwood.example/o/data-analyst-returns", apply_url=None)]}
        res = w.add_jobs(ingest)
        self.assertEqual(len(res), 2, res)
        for r in res:
            self.assertIn(r["outcome"], ("alias_added", "duplicate"), res)
            self.assertEqual(r["duplicate_of"], job_uid, res)
        self.assertEqual(w.one("SELECT count(*) FROM jobs WHERE status NOT IN ('duplicate', 'prefilter_rejected')")[0],
                         n_jobs)

        # a second fetch of the same board adds nothing and keeps the keys
        again = self.fetch()
        self.assertEqual(again["new"], 0, again)
        self.assertEqual(w.one("SELECT count(*) FROM jobs WHERE canonical_key LIKE 'ats:recruitee:%'")[0], 2)


class TestApiStops(unittest.TestCase):
    """The stops a job API can cause, from `sources fetch` through the real breakers to the Sheet's Alerts rows."""

    def setUp(self):
        self.w = SourcesWorld()
        self.addCleanup(self.w.stop)
        self.w.onboard()
        with open(os.path.join(self.w.home.dir, "private", "targets.csv"), "w", encoding="utf-8") as fh:
            fh.write("company,ats,tenant\nBirchwood Health,recruitee,%s\n" % TENANT)
        self.url = recruitee.list_url(TENANT)

    def fetch(self, answer, code: str = "OK") -> tuple:
        """`sources fetch` of the one Recruitee board; code is the envelope code it must end with (E_NETWORK when
        every source it tried failed)."""
        client = FakeClient({self.url: answer})
        with mock.patch.object(sources, "make_client", lambda cfg: client):
            rc, env = self.w.run(["sources", "fetch", "--lane", "api", "--source", "recruitee"])
        self.assertEqual(env.get("code"), code, env)
        return env.get("data") or {}, client

    def alert_rows(self) -> list:
        return [row for _k, row in sheets_rows.build_rows(self.w.conn, "alerts", None)]

    def check_warning_row(self, row: dict, code: str) -> None:
        self.assertEqual(row["area"], sheets_labels.scope_label("api:recruitee"), row)
        self.assertEqual(row["area"], "Job API: Company site (Recruitee)", row)
        self.assertEqual(row["severity"], "Warning", row)
        head = row["what"].split(". ")[0]
        self.assertEqual(head, sheets_labels.reason_sentence(code), row)
        self.assertNotIn("_", head, row)
        self.assertNotIn(code, row["what"], row)

    def test_rate_limit_trips_only_the_api_breaker_and_closes_by_itself(self):
        w = self.w
        s, client = self.fetch(RateLimited(429, self.url), "E_NETWORK")
        self.assertEqual((s["new"], [e["error"] for e in s["errors"]]), (0, ["rate_limited"]), s)
        b = w.one("SELECT state, reason_code, requires_human, auto_close_at, tripped_at FROM breakers "
                  "WHERE scope = 'api:recruitee'")
        self.assertEqual((b["state"], b["reason_code"], b["requires_human"]), ("open", "http_429", 0))
        self.assertGreater(b["auto_close_at"], b["tripped_at"])
        self.assertEqual(w.all("SELECT scope FROM breakers WHERE state = 'open'"), [("api:recruitee",)])
        # an API stop is not a chat alert: nothing waits for the person
        self.assertEqual(w.all("SELECT dedupe_key FROM notifications WHERE dedupe_key LIKE 'breaker:%'"), [])
        rows = self.alert_rows()
        self.assertEqual(len(rows), 1, rows)
        self.check_warning_row(rows[0], "http_429")
        self.assertEqual(rows[0]["what"].split(". ")[0], "The site answered with too many requests", rows)
        self.assertEqual((rows[0]["todo"], rows[0]["status"]), (sheets_labels.TODO_AUTO, "Open"), rows)

        # while it is open the source is skipped: no request reaches the API
        w.clock.advance(minutes=30)
        s, client = self.fetch(e2e_fixture("recruitee_list.json"))
        self.assertEqual((client.calls, s["new"]), ([], 0), s)

        # after auto_close_at the nightly housekeeping closes it; the next fetch goes through
        w.clock.set(b["auto_close_at"])
        w.clock.advance(minutes=1)
        w.ok(["housekeeping", "--only", "breakers"])
        self.assertEqual(w.one("SELECT state FROM breakers WHERE scope = 'api:recruitee'")[0], "closed")
        s, client = self.fetch(e2e_fixture("recruitee_list.json"))
        self.assertEqual(([c[1] for c in client.calls], s["new"]), ([self.url], 2), s)
        rows = self.alert_rows()
        self.assertEqual([(r["status"], r["todo"]) for r in rows], [("Resolved", "Nothing, this is resolved.")], rows)
        self.assertTrue(rows[0]["resolved"], rows)

    def test_repeated_server_errors_trip_consecutive_errors(self):
        w = self.w
        for i in range(3):
            s, client = self.fetch(HttpError(503, self.url), "E_NETWORK")
            self.assertEqual([c[1] for c in client.calls], [self.url], (i, s))
            self.assertEqual([e["error"] for e in s["errors"]], ["HttpError"], s)
            state = w.one("SELECT state FROM breakers WHERE scope = 'api:recruitee'")
            self.assertEqual(state[0] if state else None, "open" if i == 2 else None, (i, s))
            w.clock.advance(hours=12)
        b = w.one("SELECT reason_code, requires_human FROM breakers WHERE scope = 'api:recruitee'")
        self.assertEqual(tuple(b), ("consecutive_errors", 0))
        self.assertEqual(w.all("SELECT dedupe_key FROM notifications WHERE dedupe_key LIKE 'breaker:%'"), [])
        rows = self.alert_rows()
        self.assertEqual(len(rows), 1, rows)
        self.check_warning_row(rows[0], "consecutive_errors")
        self.assertIn("503", rows[0]["what"], rows)


if __name__ == "__main__":
    unittest.main()
