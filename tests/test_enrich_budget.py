"""U10 budgets: rolling 31 days, 24 hours, lifetime and request caps; reserve at the maximum then settle;
after-send failures charged at the maximum; fail closed without meta rows; a race for the last credit has
exactly one winner. Also the settings clamp (file can only tighten)."""
from __future__ import annotations

import json
import os
import threading
import unittest

import tests  # noqa: F401
from jobhunter import canon, db
from jobhunter.enrich import budget, settings
from jobhunter.enrich.providers import EnrichResult
from jobhunter.errors import Denied
from tests.fakes.u10 import FIXTURES, EnrichTestCase, install_guard, remove_guard

SMALL = {"enabled": True, "providers": {
    "hunter": {"budget_31d": 3, "day_credits": 2, "day_requests": 3, "min_interval_s": 1},
    "anymailfinder": {"enabled": True, "budget_31d": 5, "budget_lifetime": 2, "day_credits": 1, "day_requests": 2,
                      "min_interval_s": 1}}}


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class BudgetCase(EnrichTestCase):
    enrich_block = SMALL

    def setUp(self):
        super().setUp()
        with db.tx(self.conn):   # reserve providers start at budget 0; only `config raise` lifts them
            db.meta_set(self.conn, "raise:enrich.providers.anymailfinder.budget_31d", "5", "human")
            db.meta_set(self.conn, "raise:enrich.providers.anymailfinder.budget_lifetime", "2", "human")
        self.write_meta()

    def request(self) -> int:
        ts = canon.now()
        with db.tx(self.conn):
            return self.conn.execute("INSERT INTO enrich_requests (request_uid, status, created_by, started_at, "
                                     "created_at, updated_at) VALUES (?, 'found', 'system', ?, ?, ?)",
                                     (canon.new_uid("E"), ts, ts, ts)).lastrowid

    def reserve(self, provider="hunter", op="find_name_domain", settle=None) -> int:
        cid = budget.try_reserve(self.conn, provider, op, self.request(), None)
        if settle is not None:
            budget.settle(self.conn, cid, EnrichResult(provider=provider, **settle))
        self.clock.advance(seconds=5)
        return cid

    def credits(self, cid) -> float:
        return self.conn.execute("SELECT credits_charged FROM enrich_calls WHERE id = ?", (cid,)).fetchone()[0]


class TestReserveSettle(BudgetCase):
    def test_reserve_at_max_then_settle(self):
        cid = budget.try_reserve(self.conn, "hunter", "find_name_domain", self.request(), "C20260927T050000ZAAAA")
        row = self.conn.execute("SELECT * FROM enrich_calls WHERE id = ?", (cid,)).fetchone()
        self.assertEqual((row["outcome"], row["credits_charged"], row["cycle_id"]),
                         ("inflight", 1.0, "C20260927T050000ZAAAA"))
        budget.settle(self.conn, cid, EnrichResult(provider="hunter", outcome="miss"))
        self.assertEqual(self.credits(cid), 0.0)
        st = budget.state_row(self.conn, "hunter")
        self.assertIsNotNone(st["next_call_at"])

    def test_charges_by_outcome(self):
        cases = [({"outcome": "hit", "email": "example.person@kestrel.example", "verification": "valid",
                   "credits_charged": 1.0}, 1.0),
                 ({"outcome": "timeout_after_send"}, 1.0), ({"outcome": "server_error"}, 1.0),
                 ({"outcome": "bad_response"}, 1.0), ({"outcome": "network_before_send"}, 0.0),
                 ({"outcome": "auth_failed"}, 0.0)]
        for settle, want in cases:
            with self.subTest(settle=settle["outcome"]):
                self.conn.execute("DELETE FROM breakers")
                self.conn.execute("DELETE FROM enrich_provider_state")
                self.conn.execute("DELETE FROM enrich_calls")
                cid = self.reserve(settle=settle)
                self.assertEqual(self.credits(cid), want)

    def test_hunter_verify_half_credit(self):
        cid = budget.try_reserve(self.conn, "hunter", "verify", self.request(), None)
        self.assertEqual(self.credits(cid), 0.5)
        budget.settle(self.conn, cid, EnrichResult(provider="hunter", outcome="in_progress", credits_charged=0.5))
        budget.settle(self.conn, cid, EnrichResult(provider="hunter", outcome="hit", verification="valid",
                                                   credits_charged=0.5))
        self.assertEqual(self.credits(cid), 0.5)

    def test_day_credits_and_requests(self):
        self.reserve(settle={"outcome": "hit", "email": "a.b@kestrel.example", "credits_charged": 1.0})
        self.reserve(settle={"outcome": "hit", "email": "c.d@kestrel.example", "credits_charged": 1.0})
        ok, reason, retry = budget.can_reserve(self.conn, "hunter", "find_name_domain")
        self.assertEqual((ok, reason), (False, "skipped_budget"))
        self.assertTrue(retry and 0 < retry <= 86400 + 1)
        self.assertDenied("E_CEILING", budget.try_reserve, self.conn, "hunter", "find_name_domain", self.request(), None)
        self.clock.advance(days=1, seconds=1)
        self.reserve(settle={"outcome": "miss"})
        self.reserve(settle={"outcome": "miss"})
        self.reserve(settle={"outcome": "miss"})
        ok, reason, _ = budget.can_reserve(self.conn, "hunter", "find_name_domain")
        self.assertEqual((ok, reason), (False, "skipped_budget"))      # 3 requests in 24 h, misses included

    def test_rolling_31_days(self):
        for day in range(3):
            self.reserve(settle={"outcome": "hit", "email": "p%d.q@kestrel.example" % day, "credits_charged": 1.0})
            self.clock.advance(days=1, seconds=1)
        ok, reason, retry = budget.can_reserve(self.conn, "hunter", "find_name_domain")
        self.assertEqual((ok, reason), (False, "skipped_budget"))
        self.assertGreater(retry, 25 * 86400)
        self.clock.advance(days=29)
        self.assertTrue(budget.can_reserve(self.conn, "hunter", "find_name_domain")[0])

    def test_lifetime(self):
        self.reserve("anymailfinder", settle={"outcome": "hit", "email": "a.b@kestrel.example",
                                              "verification": "valid", "credits_charged": 1.0})
        self.clock.advance(days=40)
        self.reserve("anymailfinder", settle={"outcome": "hit", "email": "c.d@kestrel.example",
                                              "verification": "valid", "credits_charged": 1.0})
        self.clock.advance(days=40)
        ok, reason, _ = budget.can_reserve(self.conn, "anymailfinder", "find_name_domain")
        self.assertEqual((ok, reason), (False, "skipped_budget"))

    def test_pacing(self):
        budget.try_reserve(self.conn, "hunter", "find_name_domain", self.request(), None)
        ok, reason, retry = budget.can_reserve(self.conn, "hunter", "find_name_domain")
        self.assertEqual((ok, reason, retry), (False, "skipped_rate", 2))   # 1 asked; the floor keeps 2

    def test_disabled_and_breaker(self):
        self.assertEqual(budget.can_reserve(self.conn, "apollo", "find_name_domain")[1], "skipped_disabled")
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:hunter", "auth_failed", "test")
        self.assertEqual(budget.can_reserve(self.conn, "hunter", "find_name_domain")[1], "skipped_breaker")
        with db.tx(self.conn):
            self.assertTrue(budget.mark_key_set(self.conn, "hunter"))
        self.assertTrue(budget.can_reserve(self.conn, "hunter", "find_name_domain")[0])

    def test_reported_remaining(self):
        cid = budget.try_reserve(self.conn, "hunter", "find_name_domain", self.request(), None)
        budget.settle(self.conn, cid, EnrichResult(provider="hunter", outcome="miss", reported_remaining=0.0))
        self.clock.advance(seconds=5)
        self.assertEqual(budget.can_reserve(self.conn, "hunter", "find_name_domain")[1], "skipped_budget")


class TestFailClosed(BudgetCase):
    def test_missing_meta_rows_mean_no_calls(self):
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM meta WHERE key LIKE 'enrich_%'")
        self.assertEqual(budget.can_reserve(self.conn, "hunter", "find_name_domain")[1], "skipped_budget")
        self.assertDenied("E_CEILING", budget.try_reserve, self.conn, "hunter", "find_name_domain", self.request(), None)
        # even when the Python check is bypassed, the trigger refuses
        ts = canon.now()
        rid = self.request()
        self.assertDenied("E_CEILING", self.conn.execute,
                          "INSERT INTO enrich_calls (request_id, provider, op, outcome, started_at, credits_charged, "
                          "created_at, updated_at) VALUES (?, 'hunter', 'find_name_domain', 'inflight', ?, 1, ?, ?)",
                          (rid, ts, ts, ts))

    def test_disabled_writes_zero_meta(self):
        rows = settings.meta_rows(settings.clamp({"enabled": False}))
        self.assertTrue(all(v == "0" for k, v in rows.items() if k.startswith("enrich_budget_31d:")))
        rows = settings.meta_rows(settings.clamp({"enabled": True}))
        self.assertEqual(rows["enrich_budget_31d:prospeo"], "90")
        self.assertEqual(rows["enrich_budget_lifetime:hunter"], str(settings.LIFETIME_UNLIMITED))
        self.assertEqual(rows["enrich_budget_lifetime:anymailfinder"], "0")
        self.assertEqual(rows["enrich_budget_31d:apollo"], "0")

    def test_race_for_last_credit(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE meta SET value = '1' WHERE key = 'enrich_day_credits:hunter'")
        rids = [self.request(), self.request()]
        results = []
        barrier = threading.Barrier(2)

        def worker(rid):
            c = db.connect()
            try:
                barrier.wait()
                results.append(("ok", budget.try_reserve(c, "hunter", "find_name_domain", rid, None)))
            except Denied as d:
                results.append((d.code, None))
            finally:
                c.close()
        ts = [threading.Thread(target=worker, args=(r,)) for r in rids]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)
        self.assertEqual(sorted(r[0] for r in results), ["E_CEILING", "ok"])
        n = self.conn.execute("SELECT count(*) FROM enrich_calls WHERE provider = 'hunter'").fetchone()[0]
        self.assertEqual(n, 1)


class TestSettingsClamp(unittest.TestCase):
    def test_file_can_only_tighten(self):
        with open(os.path.join(FIXTURES, "config", "snippets.json")) as fh:
            snip = json.load(fh)
        s = settings.clamp(snip["loosened_file"])
        self.assertEqual(s["max_finders_per_person"], 2)          # 9 asked, default 2 (raise needs config raise)
        self.assertEqual(s["min_confidence"], 90)                 # 10 asked, floor stays at the default
        self.assertEqual(s["timeout_s"], 20)                      # bounded [5, 20]
        self.assertEqual(s["providers"]["prospeo"]["budget_31d"], 90.0)
        self.assertEqual(s["providers"]["prospeo"]["min_interval_s"], 5)
        self.assertEqual(s["providers"]["tomba"]["day_requests"], 2)
        self.assertEqual(s["chain"], ["prospeo", "hunter"])       # unknown and duplicate items dropped
        lower = settings.clamp({"max_finders_per_person": 1, "min_confidence": 95,
                                "providers": {"hunter": {"budget_31d": 10}}})
        self.assertEqual((lower["max_finders_per_person"], lower["min_confidence"]), (1, 95))
        self.assertEqual(lower["providers"]["hunter"]["budget_31d"], 10.0)
        self.assertFalse(settings.clamp(None)["enabled"])

    def test_raise_up_to_hard_max_only(self):
        meta = {"raise:enrich.max_finders_per_person": "3", "raise:enrich.providers.prospeo.budget_31d": "500",
                "raise:enrich.min_confidence": "70"}
        s = settings.clamp({"max_finders_per_person": 4, "providers": {"prospeo": {"budget_31d": 500}},
                            "min_confidence": 70}, meta)
        self.assertEqual(s["max_finders_per_person"], 3)
        self.assertEqual(s["providers"]["prospeo"]["budget_31d"], 100.0)   # free-tier hard maximum
        self.assertEqual(s["min_confidence"], 80)                           # hard minimum


class TestRealConfigApply(EnrichTestCase):
    """U1 config.load / config.apply carry the enrich block and write the budget meta rows the triggers read
    (INTEGRATION PATCH LIST U1-3..U1-5): the same rows as settings.meta_rows, 0 when off."""

    def meta(self) -> dict:
        return {r[0]: r[1] for r in self.conn.execute("SELECT key, value FROM meta WHERE key LIKE 'enrich_%'")}

    def apply(self, overrides: dict) -> dict:
        from jobhunter import config
        from tests.fakes.u1 import write_config
        write_config(overrides)
        with db.tx(self.conn):
            config.apply(self.conn)
        return self.meta()

    def test_apply_writes_the_budget_rows(self):
        settings.use_test_overrides(None)          # read the real private/config.json through config.load
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM meta WHERE key LIKE 'enrich_%'")
        meta = self.apply({"enrich.enabled": True, "enrich.providers.hunter.enabled": False,
                           "enrich.providers.prospeo.budget_31d": 500})
        s = settings.load(self.conn)
        self.assertTrue(s["enabled"])
        self.assertEqual(meta, settings.meta_rows(s))
        self.assertEqual(meta["enrich_budget_31d:prospeo"], "90")        # the file cannot raise a limit
        self.assertEqual(meta["enrich_budget_31d:hunter"], "0")          # provider off: no calls
        meta = self.apply({"enrich.enabled": False})
        self.assertEqual({k: v for k, v in meta.items() if k.startswith(("enrich_budget", "enrich_day"))
                          and v != "0"}, {})                              # finder off: every budget is 0


if __name__ == "__main__":
    unittest.main()
