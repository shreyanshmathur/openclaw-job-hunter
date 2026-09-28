"""U10 feedback hooks (10, 11.4, 9.5): bounce strikes per provider and across providers, also when another
breaker is already open on the scope (escalated to bounce_strikes, held until reset) and after a purge of an
earlier bounced address; opt-out purge."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import db, hooks
from jobhunter.enrich import feedback
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard
from tests.helpers import insert_action, insert_company


def canon_add(**kw) -> str:
    from jobhunter import canon
    return canon.ts_add(canon.now(), **kw)


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class TestBounce(EnrichTestCase):
    def bounce(self, t):
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", status="sent", contact_id=t["contact_id"],
                                company_id=t["company_id"], recipient=t["email"])
            action = self.conn.execute("SELECT * FROM actions WHERE id = ?", (aid,)).fetchone()
            return feedback.on_bounce(self.conn, action, None)

    def target(self, i, provider):
        comp = insert_company(self.conn, name="Company %d" % i, domain="c%d.example" % i)
        return self.provider_contact("example.p%d@c%d.example" % (i, i), provider=provider, company_id=comp)

    def test_per_provider_strikes(self):
        a, b = self.target(1, "prospeo"), self.target(2, "prospeo")
        self.assertEqual(self.bounce(a), [])
        self.assertIsNotNone(self.conn.execute("SELECT bounced_at FROM enrich_calls WHERE id = ?",
                                               (a["call_id"],)).fetchone()[0])
        self.assertEqual(self.bounce(b), ["enrich:prospeo"])
        br = self.breaker("enrich:prospeo")
        self.assertEqual((br["reason_code"], br["requires_human"]), ("bounce_strikes", 1))
        self.assertIsNone(self.breaker("enrich"))

    def test_across_providers(self):
        ts = [self.target(1, "prospeo"), self.target(2, "hunter"), self.target(3, "tomba")]
        self.assertEqual(self.bounce(ts[0]), [])
        self.assertEqual(self.bounce(ts[1]), [])
        self.assertEqual(self.bounce(ts[2]), ["enrich"])

    def test_old_bounces_do_not_count(self):
        a, b = self.target(1, "prospeo"), self.target(2, "prospeo")
        self.bounce(a)
        self.clock.advance(days=31)
        self.assertEqual(self.bounce(b), [])

    def gate_denies(self, t):
        from jobhunter import canon
        from jobhunter.enrich import gatecheck
        self.assertDenied("E_ADDRESS_GRADE", gatecheck.check, self.conn, kind="cold_email",
                          contact_id=t["contact_id"], recipient=t["email"], reserved_at=canon.now())
        self.assertDenied("E_ADDRESS_GRADE", insert_action, self.conn, kind="cold_email",
                          contact_id=t["contact_id"], company_id=t["company_id"], recipient=t["email"])

    def test_strikes_escalate_an_open_rate_limit_breaker(self):
        """A 429 back-off is open on the provider when the bounces come: the stop becomes bounce_strikes, waits
        for the owner, and outlives the back-off."""
        from jobhunter.enrich import budget
        a, b, c = self.target(1, "hunter"), self.target(2, "hunter"), self.target(3, "hunter")
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:hunter", "rate_limited", "HTTP 429", requires_human=False,
                        auto_close_at=canon_add(hours=1))
        self.assertEqual(self.bounce(a), [])
        self.assertEqual(self.bounce(b), ["enrich:hunter"])
        br = self.breaker("enrich:hunter")
        self.assertEqual((br["state"], br["reason_code"], br["requires_human"], br["auto_close_at"]),
                         ("open", "bounce_strikes", 1, None))
        self.gate_denies(c)
        self.clock.advance(hours=2)                      # the back-off would have ended by now
        from jobhunter import breakers
        with db.tx(self.conn):
            self.assertNotIn("enrich:hunter", breakers.close_expired(self.conn))
        self.assertIsNotNone(budget.breaker_row(self.conn, "enrich:hunter"))
        self.gate_denies(c)

    def test_strikes_escalate_an_open_auth_breaker_and_connect_does_not_clear_them(self):
        from jobhunter.enrich import budget
        a, b, c = self.target(1, "hunter"), self.target(2, "hunter"), self.target(3, "hunter")
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:hunter", "auth_failed", "HTTP 401")
        self.bounce(a)
        self.assertEqual(self.bounce(b), ["enrich:hunter"])
        self.assertEqual(self.breaker("enrich:hunter")["reason_code"], "bounce_strikes")
        with db.tx(self.conn):
            self.assertFalse(budget.mark_key_set(self.conn, "hunter"))    # `enrich connect` clears auth_failed only
        self.gate_denies(c)

    def test_a_later_provider_error_does_not_relabel_the_bounce_stop(self):
        from jobhunter.enrich import budget, providers
        a, b, c = self.target(1, "hunter"), self.target(2, "hunter"), self.target(3, "hunter")
        self.bounce(a)
        self.bounce(b)
        with db.tx(self.conn):          # a call that was already in flight settles with 429, then 401
            budget.provider_error(self.conn, "hunter", providers.EnrichResult(provider="hunter",
                                                                              outcome="rate_limited"))
            budget.provider_error(self.conn, "hunter", providers.EnrichResult(provider="hunter",
                                                                              outcome="auth_failed"))
        br = self.breaker("enrich:hunter")
        self.assertEqual((br["reason_code"], br["requires_human"], br["auto_close_at"]), ("bounce_strikes", 1, None))
        self.gate_denies(c)

    def test_escalates_the_all_provider_stop_too(self):
        from jobhunter.enrich import budget
        ts = [self.target(1, "prospeo"), self.target(2, "hunter"), self.target(3, "tomba")]
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich", "provider_errors", "test", requires_human=False,
                        auto_close_at=canon_add(hours=6))
        self.bounce(ts[0])
        self.bounce(ts[1])
        self.assertEqual(self.bounce(ts[2]), ["enrich"])
        self.assertEqual(self.breaker("enrich")["reason_code"], "bounce_strikes")

    def test_an_open_bounce_stop_is_not_tripped_again(self):
        a, b, c = self.target(1, "prospeo"), self.target(2, "prospeo"), self.target(3, "prospeo")
        self.bounce(a)
        self.assertEqual(self.bounce(b), ["enrich:prospeo"])
        with db.tx(self.conn):          # c was sent before the stop (the trigger refuses a new send now)
            self.assertEqual(feedback.on_bounce(self.conn, {"contact_id": c["contact_id"], "recipient": c["email"]}),
                             ["enrich"])      # the third strike opens the all-provider stop; the provider's stays
        self.assertEqual(self.conn.execute("SELECT count(*) FROM breaker_events WHERE scope = 'enrich:prospeo' "
                                           "AND event = 'trip'").fetchone()[0], 1)

    def test_strikes_survive_a_purge(self):
        """Bounce A, then A's data is purged (opt-out, complaint, forget); bounce B still makes 2 strikes."""
        a, b = self.target(1, "hunter"), self.target(2, "hunter")
        self.assertEqual(self.bounce(a), [])
        with db.tx(self.conn):
            feedback.on_optout(self.conn, a["contact_id"])
        self.assertIsNone(self.conn.execute("SELECT email FROM enrich_calls WHERE id = ?", (a["call_id"],))
                          .fetchone()[0])
        self.assertEqual(self.bounce(b), ["enrich:hunter"])

    def test_strikes_survive_forget_across_providers(self):
        from jobhunter.enrich import cache
        ts = [self.target(1, "prospeo"), self.target(2, "hunter"), self.target(3, "tomba")]
        self.bounce(ts[0])
        self.bounce(ts[1])
        with db.tx(self.conn):
            cache.forget(self.conn, ts[0]["contact_id"])
            cache.forget(self.conn, ts[1]["contact_id"])
        self.assertEqual(self.bounce(ts[2]), ["enrich"])

    def test_non_provider_bounce_is_ignored(self):
        t = self.make_target(email="published.person@kestrel.example")
        t["email"] = "published.person@kestrel.example"
        self.assertEqual(self.bounce(t), [])


class TestOptout(EnrichTestCase):
    def test_purge(self):
        t = self.provider_contact("example.person@kestrel.example")
        with db.tx(self.conn):
            n = feedback.on_optout(self.conn, t["contact_id"])
        self.assertEqual(n, 1)
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_grade"], c["email_source"], c["email_enrich_call_id"]),
                         (None, None, None, None))
        call = self.calls()[0]
        self.assertEqual((call["email"], call["email_domain"], call["source_url"], call["source_urls_json"]),
                         (None, None, None, "[]"))
        self.assertEqual((call["outcome"], call["credits_charged"]), ("hit", 1.0))    # budget history stays
        self.assertIsNotNone(call["purged_at"])


class TestRealHooks(EnrichTestCase):
    """U1 hooks.on_bounce and hooks.on_optout reach feedback.on_bounce and feedback.on_optout."""

    def test_bounce_hook_returns_the_tripped_scopes(self):
        scopes = []
        for i in (1, 2):
            comp = insert_company(self.conn, name="Company %d" % i, domain="c%d.example" % i)
            t = self.provider_contact("example.p%d@c%d.example" % (i, i), company_id=comp)
            with db.tx(self.conn):
                aid = insert_action(self.conn, kind="cold_email", status="sent", contact_id=t["contact_id"],
                                    company_id=t["company_id"], recipient=t["email"])
                action = self.conn.execute("SELECT * FROM actions WHERE id = ?", (aid,)).fetchone()
                scopes.append(hooks.on_bounce(self.conn, action, None))
        self.assertEqual(scopes, [[], ["enrich:prospeo"]])

    def test_optout_hook(self):
        t = self.provider_contact("example.person@kestrel.example")
        with db.tx(self.conn):
            self.assertEqual(hooks.on_optout(self.conn, t["contact_id"]), 1)
        self.assertIsNotNone(self.calls()[0]["purged_at"])


class TestRealReplies(EnrichTestCase):
    """U6 replies -> U1 hooks -> U10 feedback: a bounce of a provider address marks its call and counts toward
    the strike breakers; an opt-out purges the person's provider data (ENRICH-SPEC 11.4 and 9.5)."""

    def sent(self, t) -> str:
        from jobhunter import threads
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", status="sent", contact_id=t["contact_id"],
                                company_id=t["company_id"], recipient=t["email"])
            row = self.conn.execute("SELECT * FROM actions WHERE id = ?", (aid,)).fetchone()
            return threads.on_confirm(self.conn, row)["thread_key"]

    def target(self, i):
        comp = insert_company(self.conn, name="Company %d" % i, domain="c%d.example" % i)
        return self.provider_contact("example.p%d@c%d.example" % (i, i), company_id=comp)

    def test_bounce_path(self):
        from jobhunter import replies
        a, b = self.target(1), self.target(2)
        effects = []
        for i, t in enumerate((a, b)):
            key = self.sent(t)
            with db.tx(self.conn):
                effects.append(replies.record_code_class(self.conn, {"msg_ref": "gm:b%d" % i, "code_class": "bounce",
                                                                     "thread_key": key,
                                                                     "received_at": self.clock.now()})["effects"])
        self.assertNotIn("finder_bounce_strikes", effects[0])
        self.assertIn("finder_bounce_strikes", effects[1])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM enrich_calls WHERE bounced_at IS NOT NULL")
                         .fetchone()[0], 2)
        self.assertEqual(self.breaker("enrich:prospeo")["reason_code"], "bounce_strikes")

    def test_optout_path(self):
        from jobhunter import replies
        t = self.target(1)
        key = self.sent(t)
        thread = self.conn.execute("SELECT * FROM threads WHERE thread_key = ?", (key,)).fetchone()
        with db.tx(self.conn):
            effects = replies.apply_class(self.conn, thread, "opt_out", self.clock.now(), "asked to stop", None, None)
        self.assertIn("finder_data_purged", effects)
        call = self.calls()[0]
        self.assertEqual((call["email"], call["source_url"]), (None, None))
        self.assertIsNotNone(call["purged_at"])


if __name__ == "__main__":
    unittest.main()
