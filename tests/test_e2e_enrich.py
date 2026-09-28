"""E2E (INT): the email finder (U10) inside the real outreach flow. Only the network is faked: provider answers come
from U10's recorded fixtures (tests/fixtures/enrich, FakeTransport at the `enrich find` command's transport seam),
DNS from U10's FakeDns under the real emailcheck.mx_for_domain, and keys from the in-memory key store. Everything
else is the real code of U1, U2, U3, U4, U6 and U10, called through the CLI as the lanes and the owner call it.

A hiring manager named on an eligible posting has no published address. `enrich find` asks the chain; the
provider address is checked and graded by code (verify), written by contacts.set_address (email_source provider,
the call id, MX), drafted, QC'd, approved and sent on the web_ui route, where `gate reserve` runs
hooks.on_reserve_address (U10 gatecheck). A bounce on it marks the address invalid, the provider call bounced
and the domain no-guessing, and the person is never looked up again. The refusals: a grade C result is never
written or sendable, an excluded address is refused by the chain and by the gate, an address grade the owner no
longer allows is refused at reserve, and a spent budget means no provider call at all.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import emailcheck, paths
from jobhunter.commands import enrich as enrich_cmd
from jobhunter.enrich import gatecheck, keystore
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u3 import install_reviewer_hashes
from tests.fakes.u10 import ALL_KEYS, FakeDns, FakeTransport, MonoClock, install_guard, remove_guard
from tests.fixtures.e2e.support import OU, OutreachSteps, World, e2e_fixture, install_qc_fakes

ADDRESS = "example.person@kestrel.example"          # what the U10 provider fixtures return for Example Person
POSTING = "https://wellfound.com/jobs/1000001-data-analyst"


def exclude(address: str, reason: str) -> None:
    """The owner adds a row to private/exclusions.csv (section 9); the next preflight imports it."""
    path = os.path.join(paths.private_dir(), "exclusions.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("type,value,reason\n")
        fh.write("email,%s,%s\n" % (address, reason))


def setUpModule():
    install_guard()          # no socket may connect while these tests run (U10's module guard)


def tearDownModule():
    remove_guard()


class EnrichWorld(World, OutreachSteps):
    pass


class E2EEnrichBase(unittest.TestCase):
    enrich = {"enabled": True}

    def setUp(self):
        self.w = EnrichWorld()
        self.addCleanup(self.w.stop)
        install_qc_fakes(self)
        keystore.use_test_backend({p: dict(v) for p, v in ALL_KEYS.items()})
        self.addCleanup(keystore.use_test_backend, None)
        self.dns = FakeDns()
        emailcheck.clear_mx_cache()
        p = mock.patch.object(emailcheck, "lookup_mx", self.dns.lookup_mx)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(emailcheck.clear_mx_cache)
        self.fake = FakeTransport(MonoClock())
        for name in ("_TRANSPORT", "_CLOCK"):
            self.addCleanup(setattr, enrich_cmd, name, getattr(enrich_cmd, name))
        enrich_cmd._TRANSPORT, enrich_cmd._CLOCK = self.fake, self.fake.clock
        over = {"enrich." + k: v for k, v in self.enrich.items()}
        self.w.onboard(**over)
        install_reviewer_hashes(self.w.conn)
        self.w.add_jobs(e2e_fixture("ingest_greenhouse.json"))
        self.w.evaluate_all()
        self.assertEqual(self.w.one("SELECT status FROM jobs")[0], "eligible")

    def later(self, minutes: int) -> None:
        self.w.clock.advance(minutes=minutes)
        write_heartbeat()

    def hiring_manager(self, cyc: str, full_name: str = "Example Person") -> dict:
        first = full_name.split()[0]
        slug = "-".join(full_name.lower().split())
        slug = slug if slug.startswith("example") else "example-" + slug
        c = self.w.add_contact(cyc, full_name=full_name, first_name=first, company="Kestrel Commerce",
                               company_domain="kestrel.example", source_url=POSTING,
                               linkedin_url="https://www.linkedin.com/in/" + slug)
        self.assertIsNone(c.get("email"))
        return c

    def find(self, cyc: str, contact_uid: str):
        return self.w.run(["enrich", "find", "--contact", contact_uid], OU, cycle=cyc)

    def contact(self, uid: str):
        return self.w.one("SELECT * FROM contacts WHERE contact_uid = ?", uid)

    def provider_draft(self) -> dict:
        """Outreach cycle: the target, `enrich find` (Prospeo finds a verified address), a cold email to it, QC;
        then the owner approves it."""
        w = self.w
        self.fake.add("prospeo", "hit_valid")
        cyc = w.preflight("outreach", OU)
        c = self.hiring_manager(cyc)
        rc, env = self.find(cyc, c["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        found = env["data"]
        self.assertEqual((found["status"], found["grade"]), ("found", "B"), found)
        row = self.contact(c["contact_uid"])
        self.assertEqual((row["email"], row["email_grade"], row["email_source"], row["email_mx_ok"]),
                         (ADDRESS, "B", "provider", 1))
        call = w.one("SELECT provider, outcome, email, verification, bounced_at FROM enrich_calls WHERE id = ?",
                     row["email_enrich_call_id"])
        self.assertEqual(tuple(call), ("prospeo", "hit", ADDRESS, "valid", None))
        self.assertEqual([r["provider"] for r in self.fake.requests], ["prospeo"])
        d = w.cold_email_draft(cyc, c["contact_uid"], "Example", "Kestrel Commerce")
        self.assertEqual(w.one("SELECT recipient, send_route FROM drafts WHERE draft_uid = ?", d)[:],
                         (ADDRESS, "browser"))
        w.end_cycle(cyc, OU)
        self.assertEqual([a["draft_uid"] for a in w.approve_all()], [d])
        return {"contact_uid": c["contact_uid"], "draft_uid": d, "call_id": row["email_enrich_call_id"]}


class TestProviderAddressFlow(E2EEnrichBase):
    def test_found_graded_written_sent_then_a_bounce_suppresses_it(self):
        w = self.w
        made = self.provider_draft()
        # the web route: reserve runs hooks.on_reserve_address (U10) on the provider address; then send and confirm
        self.later(30)
        cyc = w.preflight("outreach", OU)
        shown = w.ok(["enrich", "show", made["contact_uid"]], OU, cycle=cyc)
        self.assertEqual((shown["bounced"], shown["sendable"]), (False, True), shown)
        with mock.patch.object(gatecheck, "check", wraps=gatecheck.check) as spy:
            rc, env = w.web_reserve(cyc, made["draft_uid"], made["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        self.assertEqual(spy.call_count, 1)
        token = env["data"]["token"]
        self.assertEqual(w.web_send(cyc, token, made["draft_uid"])["confirm"]["status"], "sent")
        w.end_cycle(cyc, OU)
        self.assertEqual(w.one("SELECT status, recipient FROM actions WHERE token = ?", token)[:], ("sent", ADDRESS))

        # the delivery failure, read in Gmail by the replies lane (web route) and recorded on the thread
        self.later(60)
        cyc = w.preflight("replies", OU)
        pending = w.ok(["reply", "pending"], OU, cycle=cyc)
        bc = pending.get("bounce_check") or {}
        self.assertIn({"thread_key": "em:" + token, "recipient": ADDRESS}, bc.get("threads", []), pending)
        rec = {"inbound_id": None, "thread_key": "em:" + token, "class": "bounce",
               "summary": "Address not found: the message to this address was not delivered.",
               "received_at": w.clock.now(), "msg_ref": "gm:18c2f0000000f001"}
        rr = w.ok(["reply", "record", "--file", w.wfile("outreach", "%s/bounce.json" % cyc, rec)], OU, cycle=cyc)
        w.end_cycle(cyc, OU)
        self.assertTrue(rr["effects"], rr)
        row = self.contact(made["contact_uid"])
        self.assertEqual(row["email_invalid"], 1)
        self.assertEqual(w.one("SELECT state FROM threads WHERE thread_key = ?", "em:" + token)[0], "bounced")
        self.assertIsNotNone(w.one("SELECT bounced_at FROM enrich_calls WHERE id = ?", made["call_id"])[0],
                             "hooks.on_bounce did not reach the provider call (U10 feedback)")
        self.assertTrue(emailcheck.guess_blocked(w.conn, "kestrel.example"), "a B bounce blocks guessing the domain")

        # suppressed: no second lookup for the person (no provider call), no new email to the address
        self.later(60)
        cyc = w.preflight("outreach", OU)
        before = len(self.fake.requests)
        rc, env = self.find(cyc, made["contact_uid"])
        self.assertEqual((rc, env["code"]), (3, "E_DUP_PERSON"), env)
        self.assertEqual(len(self.fake.requests), before)
        shown = w.ok(["enrich", "show", made["contact_uid"]], OU, cycle=cyc)
        self.assertEqual((shown["bounced"], shown["sendable"], shown["retry_available"]), (True, False, False), shown)
        self.assertNotIn(ADDRESS, json.dumps(shown))
        w.end_cycle(cyc, OU)

    def test_gate_refuses_a_provider_address_the_owner_no_longer_allows(self):
        w = self.w
        made = self.provider_draft()
        # the owner narrows the allowed grades to published addresses only (a lowering needs no PIN)
        w.write_config(**{"enrich.enabled": True, "gmail.address_grades_allowed": ["A"]})
        self.later(30)
        cyc = w.preflight("outreach", OU)
        rc, env = w.web_reserve(cyc, made["draft_uid"], made["contact_uid"])
        self.assertEqual((rc, env["code"]), (7, "E_ADDRESS_GRADE"), env)
        self.assertEqual(w.one("SELECT count(*) FROM actions")[0], 0)
        w.end_cycle(cyc, OU)

    def test_gate_refuses_a_provider_address_excluded_after_approval(self):
        w = self.w
        made = self.provider_draft()
        self.later(30)
        cyc = w.preflight("outreach", OU)
        w.gmail_detect(cyc)
        pc = w.web_precheck(cyc, made["contact_uid"])
        # the owner excludes the address while the cycle runs (after the precheck)
        exclude(ADDRESS, "met at a meetup")
        w.ok(["exclusions", "import"], "human")
        rc, env = w.run(["gate", "reserve", "--kind", "cold_email", "--draft", made["draft_uid"], "--precheck",
                         str(pc["precheck_id"]), "--platform", "gmail", "--contact", made["contact_uid"]], OU, cycle=cyc)
        self.assertEqual((rc, env["code"]), (7, "E_EXCLUDED"), env)
        self.assertEqual(w.one("SELECT count(*) FROM actions")[0], 0)
        self.assertEqual(self.contact(made["contact_uid"])["do_not_contact"], 1)
        # the next cycle drops the target at the precheck plan already
        w.end_cycle(cyc, OU)
        self.later(30)
        cyc = w.preflight("outreach", OU)
        w.fails(["gate", "precheck-plan", "--kind", "cold_email", "--contact", made["contact_uid"]], "E_EXCLUDED", OU,
                cycle=cyc)
        w.end_cycle(cyc, OU)


class TestProviderAddressRefused(E2EEnrichBase):
    def test_grade_c_is_never_written_or_sendable(self):
        w = self.w
        # Prospeo cannot verify the mailbox; the verifier says the domain accepts everything: grade C
        self.fake.add("prospeo", "hit_unknown").add("zerobounce", "verify_catch_all")
        cyc = w.preflight("outreach", OU)
        c = self.hiring_manager(cyc)
        rc, env = self.find(cyc, c["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        data = env["data"]
        self.assertEqual((data["status"], data["grade"], data["sendable"], data["email"]), ("found", "C", False, None),
                         data)
        row = self.contact(c["contact_uid"])
        self.assertEqual((row["email"], row["email_grade"], row["email_source"]), (None, None, None))
        self.assertEqual([r["provider"] for r in self.fake.requests], ["prospeo", "zerobounce"])
        # no address, no cold email: draft create refuses
        draft = {"kind": "cold_email", "channel": "email_cold", "contact_uid": c["contact_uid"],
                 "subject": "Pincode-level RTO models", "body": "Hi Example,\n\nA short note.\n\nThanks,",
                 "hook": None, "claims": [], "links": []}
        w.fails(["draft", "create", "--file", w.wfile("outreach", "%s/draft-c.json" % cyc, draft)], "E_VALIDATION", OU,
                cycle=cyc)
        self.assertEqual(w.one("SELECT count(*) FROM drafts WHERE contact_id = ?", row["id"])[0], 0)
        w.end_cycle(cyc, OU)

    def test_excluded_address_found_by_a_provider_is_not_sendable(self):
        w = self.w
        exclude(ADDRESS, "contacted in August")
        # Prospeo returns the excluded address: code rejects it (5.1 suppression) and asks the next finder
        self.fake.add("prospeo", "hit_valid").add("hunter", "miss")
        cyc = w.preflight("outreach", OU)
        c = self.hiring_manager(cyc)
        rc, env = self.find(cyc, c["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        data = env["data"]
        self.assertEqual((data["status"], data["sendable"], data["email"]), ("not_found", False, None), data)
        self.assertEqual([p["provider"] for p in data["providers_tried"]], ["prospeo", "hunter"])
        self.assertEqual([r["provider"] for r in self.fake.requests], ["prospeo", "hunter"])
        row = self.contact(c["contact_uid"])
        self.assertEqual((row["email"], row["email_source"]), (None, None))
        call = w.one("SELECT outcome, reject_reason FROM enrich_calls WHERE provider = 'prospeo'")
        self.assertEqual(call["reject_reason"], "excluded", tuple(call))
        w.end_cycle(cyc, OU)


class TestBudgetExhausted(E2EEnrichBase):
    enrich = {"enabled": True, "chain": ["prospeo"], "verifiers": [],
              "providers.prospeo.budget_31d": 1, "providers.prospeo.day_credits": 1}

    def test_no_provider_call_once_the_budget_is_spent(self):
        w = self.w
        self.fake.add("prospeo", "hit_valid")
        cyc = w.preflight("outreach", OU)
        first = self.hiring_manager(cyc)
        second = self.hiring_manager(cyc, "Riley Stone")
        rc, env = self.find(cyc, first["contact_uid"])
        self.assertEqual((rc, env["code"], env["data"]["grade"]), (0, "OK", "B"), env)
        self.later(10)                                   # past Prospeo's minimum interval and next_call_at
        rc, env = self.find(cyc, second["contact_uid"])
        self.assertEqual((rc, env["code"]), (4, "E_ENRICH_UNAVAILABLE"), env)
        self.assertIn("skipped_budget", json.dumps(env["data"]), env)
        self.assertEqual(len(self.fake.requests), 1, "a spent budget must not reach the provider")
        self.assertIsNone(self.contact(second["contact_uid"])["email"])
        b = w.ok(["enrich", "budget"])
        self.assertIn("prospeo", json.dumps(b))
        w.end_cycle(cyc, OU)


if __name__ == "__main__":
    unittest.main()
