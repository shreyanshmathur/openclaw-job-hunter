"""U10 stop conditions (section 10): each provider failure has its effect (breaker, exhaustion, back-off)
and the global stops refuse before anything else."""
from __future__ import annotations

import os
import unittest

import tests  # noqa: F401
from jobhunter import canon, db, paths
from jobhunter.enrich import budget
from jobhunter.enrich.providers import EnrichResult
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class StopCase(EnrichTestCase):
    enrich_block = {"enabled": True, "chain": ["hunter"], "verifiers": []}

    _n = 0

    def one(self, fixture_or_raise, provider="hunter"):
        StopCase._n += 1     # a new person each time (the same person is never looked up twice)
        t = self.make_target(full_name="Example Person%s" % ("abcdefghij"[StopCase._n % 10] * 2))
        self.fake.add(provider, fixture_or_raise)
        try:
            return self.run_find(t["contact_id"])
        except Exception as exc:  # noqa: BLE001 - callers inspect the Denied
            return exc

    def notes(self):
        return [r["text"] for r in self.conn.execute("SELECT text FROM notifications ORDER BY id")]


class TestProviderFailures(StopCase):
    def test_401_trips_auth_breaker_until_connect(self):
        self.one("auth_failed")
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["state"], b["reason_code"], b["requires_human"]), ("open", "auth_failed", 1))
        self.assertTrue(any("./jobhunter enrich connect hunter" in n for n in self.notes()))
        self.assertEqual(budget.can_reserve(self.conn, "hunter", "find_name_domain")[1], "skipped_breaker")
        self.clock.advance(days=3)
        self.assertEqual(budget.can_reserve(self.conn, "hunter", "find_name_domain")[1], "skipped_breaker")
        with db.tx(self.conn):
            budget.mark_key_set(self.conn, "hunter")
        self.assertEqual(self.breaker("enrich:hunter")["state"], "closed")

    def test_402_and_insufficient_credits_exhaust(self):
        self.one("quota")
        st = budget.state_row(self.conn, "hunter")
        self.assertEqual(st["exhausted_until"], canon.ts_add(self.requests()[0]["started_at"], days=31)[:10] +
                         st["exhausted_until"][10:])
        self.assertIsNone(self.breaker("enrich:hunter"))
        self.assertEqual(budget.can_reserve(self.conn, "hunter", "find_name_domain")[1], "skipped_budget")
        self.settings({"enabled": True, "chain": ["prospeo"], "verifiers": []})
        self.one("quota", provider="prospeo")
        self.assertIsNotNone(budget.state_row(self.conn, "prospeo")["exhausted_until"])

    def test_lifetime_quota_is_permanent(self):
        with db.tx(self.conn):
            budget.provider_error(self.conn, "findymail", EnrichResult(provider="findymail", outcome="quota_exhausted"))
        self.assertEqual(budget.state_row(self.conn, "findymail")["exhausted_until"], budget.FOREVER)

    def test_429_backoff_levels(self):
        self.one("rate_limited_retry_after")
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["reason_code"], b["requires_human"]), ("rate_limited", 0))
        self.assertEqual(canon.seconds_between(canon.now(), b["auto_close_at"]), 3600)   # max(2 s, 1 h)
        waits = []
        for _ in range(3):
            self.clock.set(b["auto_close_at"])
            self.clock.advance(seconds=1)
            with db.tx(self.conn):
                budget.provider_error(self.conn, "hunter", EnrichResult(provider="hunter", outcome="rate_limited"))
            b = self.breaker("enrich:hunter")
            waits.append(canon.seconds_between(canon.now(), b["auto_close_at"]))
        self.assertEqual(waits, [6 * 3600, 24 * 3600, 24 * 3600])
        with db.tx(self.conn):
            budget.provider_error(self.conn, "hunter", EnrichResult(provider="hunter", outcome="rate_limited",
                                                                    retry_after_s=3 * 86400))
        self.assertEqual(canon.seconds_between(canon.now(), self.breaker("enrich:hunter")["auto_close_at"]), 3 * 86400)

    def test_auto_close_breaker_counts_as_closed(self):
        with db.tx(self.conn):
            budget.provider_error(self.conn, "hunter", EnrichResult(provider="hunter", outcome="rate_limited"))
        self.assertIsNotNone(budget.breaker_row(self.conn, "enrich:hunter"))
        self.clock.advance(hours=1, seconds=1)
        self.assertIsNone(budget.breaker_row(self.conn, "enrich:hunter"))

    def test_three_errors_in_a_row(self):
        for outcome in ("server_error", "timeout_after_send"):
            with db.tx(self.conn):
                budget.provider_error(self.conn, "hunter", EnrichResult(provider="hunter", outcome=outcome))
        self.assertIsNone(self.breaker("enrich:hunter"))
        with db.tx(self.conn):
            budget.provider_error(self.conn, "hunter", EnrichResult(provider="hunter", outcome="network_before_send"))
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["reason_code"], b["requires_human"]), ("provider_errors", 0))
        self.assertEqual(canon.seconds_between(canon.now(), b["auto_close_at"]), 6 * 3600)

    def test_success_resets_error_count(self):
        for outcome in ("server_error", "server_error", "miss", "server_error"):
            with db.tx(self.conn):
                budget.provider_error(self.conn, "hunter", EnrichResult(provider="hunter", outcome=outcome))
        self.assertIsNone(self.breaker("enrich:hunter"))

    def test_two_bad_responses_in_24h(self):
        self.one("bad_schema")
        self.assertIsNone(self.breaker("enrich:hunter"))
        self.clock.advance(seconds=10)
        self.one("bad_schema")
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["reason_code"], b["requires_human"]), ("schema_changed", 1))
        self.assertEqual([c["credits_charged"] for c in self.calls()], [1.0, 1.0])     # fail closed

    def test_tls_failure(self):
        self.one("raise:tls")
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["reason_code"], b["requires_human"]), ("tls", 1))
        self.assertEqual(self.calls()[0]["credits_charged"], 0.0)

    def test_timeout_after_send_charged_at_max(self):
        out = self.one("raise:timeout_after_send")
        self.assertEqual(self.calls()[0]["credits_charged"], 1.0)
        self.assertEqual(out["data"]["status"], "not_found")
        self.assertEqual(self.requests()[0]["reason"], "provider_errors")

    def test_unexpected_phone(self):
        self.settings({"enabled": True, "chain": ["prospeo"], "verifiers": []})
        self.one("phone_present", provider="prospeo")
        b = self.breaker("enrich:prospeo")
        self.assertEqual((b["reason_code"], b["requires_human"]), ("unexpected_phone", 1))
        c = self.calls()[0]
        self.assertEqual((c["outcome"], c["credits_charged"], c["email"]), ("unexpected_phone", 11.0, None))
        dump = " ".join(str(v) for v in c.values())
        self.assertNotIn("10000000000", dump)


class TestKeyStoreFailures(StopCase):
    def test_refused_key_file_skips_and_alerts(self):
        from jobhunter.enrich import keystore
        from tests.fakes.u10 import FAKE_KEY
        keystore.use_test_backend(None)
        keystore.set_backend("file")
        keystore.store("hunter", {"api_key": FAKE_KEY})
        os.chmod(keystore.key_file(), 0o644)
        t = self.make_target()
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
        self.assertEqual(d.data["providers_tried"], [{"provider": "hunter", "outcome": "skipped_no_key"}])
        row = self.conn.execute("SELECT priority, text FROM notifications WHERE dedupe_key LIKE 'enrich_keystore:%'"
                                ).fetchone()
        self.assertEqual(row["priority"], "high")
        self.assertNotIn(FAKE_KEY, row["text"])


class TestGlobalStops(StopCase):
    def test_disabled(self):
        self.settings({"enabled": False})
        t = self.make_target()
        self.assertDenied("E_CHANNEL_DISABLED", self.run_find, t["contact_id"])

    def test_email_outreach_disabled(self):
        from tests.fakes.u1 import write_config
        write_config({"channels.email_outreach.enabled": False})
        t = self.make_target()
        self.assertDenied("E_CHANNEL_DISABLED", self.run_find, t["contact_id"])

    def test_paused_and_global(self):
        t = self.make_target()
        with open(paths.paused_file(), "w") as fh:
            fh.write("{}")
        self.assertDenied("E_PAUSED", self.run_find, t["contact_id"])
        os.unlink(paths.paused_file())
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO breakers (scope, state, reason_code, updated_at) VALUES "
                              "('global', 'open', 'test', ?)", (canon.now(),))
        self.assertDenied("E_BREAKER_OPEN", self.run_find, t["contact_id"])

    def test_scoped_breakers(self):
        t = self.make_target()
        for scope in ("pause:enrich", "enrich", "gmail", "gmail.cold"):
            with self.subTest(scope=scope):
                with db.tx(self.conn):
                    self.conn.execute("DELETE FROM breakers")
                    self.conn.execute("INSERT INTO breakers (scope, state, reason_code, updated_at) VALUES "
                                      "(?, 'open', 'test', ?)", (scope, canon.now()))
                d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
                self.assertEqual(d.data["scope"], scope)
        self.assertEqual(self.fake.requests, [])


class TestRealBreakers(StopCase):
    """The finder's stops are ordinary U1 breakers (INTEGRATION PATCH LIST U1-12): pause --scope enrich,
    breaker reset, housekeeping's auto close, and a milder trip never weakens an open stop."""

    def test_pause_enrich(self):
        from jobhunter import breakers
        t = self.make_target()
        with db.tx(self.conn):
            breakers.pause(self.conn, "enrich", "test", by="human")
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
        self.assertEqual(d.data["scope"], "pause:enrich")
        with db.tx(self.conn):
            breakers.unpause(self.conn, "enrich")
        self.fake.add("hunter", "hit_valid")                 # this case's chain is hunter only
        self.assertEqual(self.run_find(t["contact_id"])["code"], "OK")

    def test_trip_goes_through_breakers(self):
        from jobhunter import breakers
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:hunter", "bounce_strikes", "2 hard bounces on hunter addresses in 30 days")
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["state"], b["requires_human"]), ("open", 1))
        self.assertIsNotNone(b["evidence_path"])
        self.assertTrue(any(n.startswith("Stopped Email finder: Hunter") for n in self.notes()))
        with db.tx(self.conn):
            breakers.reset(self.conn, "enrich:hunter", "checked the addresses", "human")
        self.assertEqual(self.breaker("enrich:hunter")["state"], "closed")
        self.assertIsNone(budget.breaker_row(self.conn, "enrich:hunter"))

    def test_milder_trip_keeps_the_owner_stop(self):
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:hunter", "auth_failed", "HTTP 401")
            budget.trip(self.conn, "enrich:hunter", "rate_limited", "HTTP 429", requires_human=False,
                        auto_close_at=canon.ts_add(canon.now(), seconds=60))
        b = self.breaker("enrich:hunter")
        self.assertEqual((b["requires_human"], b["auto_close_at"]), (1, None))
        self.clock.advance(minutes=5)
        self.assertIsNotNone(budget.breaker_row(self.conn, "enrich:hunter"))

    def test_auto_close_by_housekeeping(self):
        from jobhunter import breakers
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:tomba", "rate_limited", "HTTP 429", requires_human=False,
                        auto_close_at=canon.ts_add(canon.now(), seconds=60))
        self.assertEqual(self.notes(), [])                  # a stop that ends by itself does not alert
        self.clock.advance(minutes=2)
        with db.tx(self.conn):
            self.assertEqual(breakers.close_expired(self.conn), ["enrich:tomba"])
        self.assertEqual(self.breaker("enrich:tomba")["state"], "closed")


if __name__ == "__main__":
    unittest.main()
