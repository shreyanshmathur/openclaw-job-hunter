"""U10 send-time rules for provider addresses (11.3): recipient mismatch, stale result, bounce-strike
breaker, share of cold sends (first allowed, second blocked at 0.5); the SQL trigger refuses the same
address cases without the hook; other contacts and kinds pass untouched. TestRealGateReserve runs the real
U1 gate.reserve and hooks.on_reserve_address (only the U3 presend and U6 on_confirm hooks are faked), including
the hook's fail-closed path when the finder package cannot be imported."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, db, gate, hooks
from jobhunter.enrich import budget, gatecheck
from tests.fakes.u1 import TUESDAY_NOON, World, patch_hooks, write_config, write_heartbeat
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard
from tests.helpers import insert_action, insert_company, insert_cycle

E = "example.person@kestrel.example"


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class TestGatecheck(EnrichTestCase):
    def check(self, t, recipient=None, kind="cold_email"):
        return gatecheck.check(self.conn, kind=kind, contact_id=t["contact_id"],
                               recipient=recipient if recipient is not None else t["email"], reserved_at=canon.now())

    def send(self, t, email=None):
        with db.tx(self.conn):
            insert_action(self.conn, kind="cold_email", status="sent", contact_id=t["contact_id"],
                          company_id=t["company_id"], recipient=email or t["email"])
        self.clock.advance(minutes=5)

    def test_first_provider_send_allowed(self):
        t = self.provider_contact(E)
        self.check(t)
        self.check(t, recipient=E.upper())

    def test_recipient_mismatch(self):
        t = self.provider_contact(E)
        self.assertDenied("E_ADDRESS_GRADE", self.check, t, recipient="someone.else@kestrel.example")
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET email = 'other.person@kestrel.example' WHERE id = ?",
                              (t["call_id"],))
        self.assertDenied("E_ADDRESS_GRADE", self.check, t)

    def test_bounced_or_purged(self):
        t = self.provider_contact(E)
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET bounced_at = ? WHERE id = ?", (canon.now(), t["call_id"]))
        self.assertDenied("E_ADDRESS_GRADE", self.check, t)

    def test_stale_result(self):
        t = self.provider_contact(E)
        self.clock.advance(days=181)
        d = self.assertDenied("E_ADDRESS_GRADE", self.check, t)
        self.assertEqual(d.data["reason"], "stale_result")

    def test_bounce_strike_breaker(self):
        t = self.provider_contact(E)
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:prospeo", "bounce_strikes", "test")
        self.assertDenied("E_ADDRESS_GRADE", self.check, t)
        with db.tx(self.conn):
            budget.close(self.conn, "enrich:prospeo")
            budget.trip(self.conn, "enrich", "bounce_strikes", "test")
        self.assertDenied("E_ADDRESS_GRADE", self.check, t)
        with db.tx(self.conn):
            budget.close(self.conn, "enrich")
            budget.trip(self.conn, "enrich:prospeo", "auth_failed", "test")     # other reasons do not block sends
        self.check(t)

    def test_share_rule(self):
        a = self.provider_contact("example.alpha@kestrel.example", provider="prospeo")
        self.check(a)
        self.send(a)
        other = insert_company(self.conn, name="Other Example", domain="other.example")
        b = self.provider_contact("example.beta@other.example", provider="hunter", company_id=other)
        d = self.assertDenied("E_CEILING", self.check, b)                # N=2, P=2 > max(1, floor(1.0))
        self.assertEqual(d.data["reason"], "provider_share")
        self.assertTrue(0 < d.retry_after <= 7 * 86400)
        for i in range(2):                                              # two ordinary sends: N=4, P=2 <= 2
            c = insert_company(self.conn, name="Plain %d" % i, domain="plain%d.example" % i)
            p = self.make_target(full_name="Plain Person%d" % i, company_id=c, email="plain.p%d@plain%d.example" % (i, i))
            self.send(p, email="plain.p%d@plain%d.example" % (i, i))
        self.check(b)
        self.clock.advance(days=8)                                      # the window rolls
        self.check(b)

    def test_other_contacts_and_kinds_pass(self):
        t = self.make_target(email="published.person@kestrel.example")
        gatecheck.check(self.conn, kind="cold_email", contact_id=t["contact_id"], recipient="anything@kestrel.example",
                        reserved_at=canon.now())
        p = self.provider_contact(E)
        gatecheck.check(self.conn, kind="li_invite", contact_id=p["contact_id"], recipient="example-person",
                        reserved_at=canon.now())
        gatecheck.check(self.conn, kind="cold_email", contact_id=None, recipient=None, reserved_at=canon.now())

    def test_hook_entry_point(self):
        t = self.provider_contact(E)
        ctx = {"kind": "cold_email", "contact_id": t["contact_id"], "recipient": E, "company_id": t["company_id"],
               "reserved_at": canon.now()}
        gatecheck.on_reserve_address(self.conn, ctx)
        ctx["recipient"] = "someone.else@kestrel.example"
        self.assertDenied("E_ADDRESS_GRADE", gatecheck.on_reserve_address, self.conn, ctx)

    def test_sql_trigger_without_the_hook(self):
        t = self.provider_contact(E)
        self.assertDenied("E_ADDRESS_GRADE", insert_action, self.conn, kind="cold_email", contact_id=t["contact_id"],
                          company_id=t["company_id"], recipient="someone.else@kestrel.example")
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich", "bounce_strikes", "test")
        self.assertDenied("E_ADDRESS_GRADE", insert_action, self.conn, kind="cold_email", contact_id=t["contact_id"],
                          company_id=t["company_id"], recipient=E)


class TestRealGateReserve(EnrichTestCase):
    """gate.reserve (U1) -> hooks.on_reserve_address (U1) -> gatecheck.on_reserve_address (U10)."""
    start_ts = TUESDAY_NOON
    E = "alex.rivera@kestrel.example"

    def setUp(self):
        super().setUp()
        write_config({"gmail.route": "app_password"})     # the mailer route: code sends, gate.reserve checks
        write_heartbeat()
        p = patch_hooks()           # presend (U3) and on_confirm (U6) only; on_reserve_address stays real
        p.start()
        self.addCleanup(p.stop)
        with db.tx(self.conn):
            self.w = World(self.conn)
            insert_cycle(self.conn, "outreach")
        self.pa = self.provider_address(self.w.person, self.w.company, self.E)

    def reserve(self):
        d = self.w.draft("cold_email", contact_id=self.w.person)
        with db.tx(self.conn):
            pc = self.w.precheck("cold_email", "gmail", contact_id=self.w.person)
        with db.tx(self.conn):
            return gate.reserve(self.conn, route="mailer", agent_id="system:mailer", platform="gmail",
                                kind="cold_email", draft_id=d, precheck_id=pc["precheck_id"])

    def test_provider_address_is_reserved(self):
        r = self.reserve()
        row = self.conn.execute("SELECT kind, status, recipient FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual(tuple(row), ("cold_email", "reserved", self.E))

    def test_hook_is_called_with_the_reserve_context(self):
        seen = []
        real = gatecheck.on_reserve_address

        def spy(conn, ctx):
            seen.append(dict(ctx))
            return real(conn, ctx)
        with mock.patch.object(gatecheck, "on_reserve_address", spy):
            self.reserve()
        self.assertEqual(len(seen), 1)
        self.assertEqual({k: seen[0][k] for k in ("kind", "contact_id", "recipient", "company_id")},
                         {"kind": "cold_email", "contact_id": self.w.person, "recipient": self.E,
                          "company_id": self.w.company})
        self.assertTrue(seen[0]["reserved_at"])

    def test_bounce_strike_breaker_refuses_the_reserve(self):
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:prospeo", "bounce_strikes", "test")
        d = self.assertDenied("E_ADDRESS_GRADE", self.reserve)
        self.assertEqual(d.data["reason"], "bounce_strikes")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions").fetchone()[0], 0)   # rolled back

    def test_stale_result_refuses_the_reserve(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET finished_at = ?, started_at = ? WHERE id = ?",
                              (canon.ts_add(canon.now(), days=-200), canon.ts_add(canon.now(), days=-200),
                               self.pa["call_id"]))
        d = self.assertDenied("E_ADDRESS_GRADE", self.reserve)
        self.assertEqual(d.data["reason"], "stale_result")

    def test_bounced_call_refuses_the_reserve(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET bounced_at = ? WHERE id = ?", (canon.now(), self.pa["call_id"]))
        self.assertDenied("E_ADDRESS_GRADE", self.reserve)

    def test_hook_fails_closed_without_the_finder(self):
        """U1 hooks.on_reserve_address: when jobhunter.enrich.gatecheck cannot be imported, a provider-found
        address is refused and any other address passes (ENRICH-SPEC 11.3, last paragraph)."""
        with mock.patch.dict(sys.modules, {"jobhunter.enrich.gatecheck": None}):
            self.assertDenied("E_ADDRESS_GRADE", self.reserve)
            ctx = {"kind": "cold_email", "contact_id": self.w.person, "recipient": self.E,
                   "company_id": self.w.company, "reserved_at": canon.now()}
            self.assertDenied("E_ADDRESS_GRADE", hooks.on_reserve_address, self.conn, ctx)
            with db.tx(self.conn):
                other = self.w.contact("Example Published", "example.published@kestrel.example", "example-published")
            hooks.on_reserve_address(self.conn, dict(ctx, contact_id=other,
                                                     recipient="example.published@kestrel.example"))
        hooks.on_reserve_address(self.conn, ctx)        # the finder is back: its rules pass this address

    def test_published_address_does_not_touch_the_finder(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET email_source = 'published', email_enrich_call_id = NULL, "
                              "email_grade = 'A' WHERE id = ?", (self.w.person,))
            budget.trip(self.conn, "enrich", "bounce_strikes", "test")
        self.reserve()


if __name__ == "__main__":
    unittest.main()
