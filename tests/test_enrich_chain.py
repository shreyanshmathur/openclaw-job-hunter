"""U10 chain (section 4): stop at the first B; max_finders_per_person; skip reasons; PENDING at the deadline
and resume; verify_pending resumes only the verifier; the pattern pre-step makes no call; an excluded
result is not sendable. Fake transport and fake monotonic clock; the socket guard is on."""
from __future__ import annotations

import json
import unittest

import tests  # noqa: F401
from jobhunter import db
from jobhunter.enrich import budget, cache, verify
from tests.fakes.u10 import FAKE_KEY, EnrichTestCase, fixture_variant, install_guard, remove_guard

E = "example.person@kestrel.example"
OTHER = "e.person@kestrel.example"
ALIAS = "example.person@kestrel-mail.example"


def hunter_hit(email: str = E, status: str = "valid") -> dict:
    """Hunter's recorded hit_valid answer with another fictional address or verification status."""
    def edit(fx):
        fx["body"]["data"]["email"] = email
        fx["body"]["data"]["verification"]["status"] = status
    return fixture_variant("hunter", "hit_valid", edit)


def tomba_hit(email: str = E, status: str = "valid") -> dict:
    def edit(fx):
        fx["body"]["data"]["email"] = email
        fx["body"]["data"]["verification"]["status"] = status
    return fixture_variant("tomba", "hit_valid", edit)


def prospeo_hit(email: str) -> dict:
    def edit(fx):
        fx["body"]["email"]["email"] = email
    return fixture_variant("prospeo", "hit_valid", edit)


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class ChainCase(EnrichTestCase):
    def providers_called(self):
        return [r["provider"] for r in self.fake.requests]


class TestHappyPaths(ChainCase):
    def test_stop_at_first_b(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        out = self.run_find(t["contact_id"])
        d = out["data"]
        self.assertEqual(out["code"], "OK")
        self.assertEqual((d["status"], d["grade"], d["sendable"], d["email"], d["provider"]),
                         ("found", "B", True, E, "prospeo"))
        self.assertEqual(d["providers_tried"], [{"provider": "prospeo", "outcome": "hit"}])
        self.assertEqual(d["credits_spent"], 1.0)
        self.assertEqual(out["next"], "Write the email draft for this contact.")
        self.assertEqual(self.providers_called(), ["prospeo"])
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_grade"], c["email_source"]), (E, "B", "provider"))
        self.assertIsNotNone(c["email_enrich_call_id"])
        self.assertEqual(c["email_mx_ok"], 1)
        req = self.requests()[0]
        self.assertEqual((req["status"], req["reason"], req["sendable"], req["next_step"]),
                         ("found", "verified_by_finder", 1, 3))
        self.assertNotIn(FAKE_KEY, json.dumps(out))

    def test_second_finder_and_evidence(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["provider"], d["confidence"], d["source_url"]), ("hunter", 94, "https://kestrel.example/team"))
        self.assertEqual(d["evidence_candidates"], ["https://kestrel.example/team", "https://blog.kestrel.example/post"])
        self.assertEqual([p["outcome"] for p in d["providers_tried"]], ["miss", "hit"])

    def test_max_finders_per_person(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "miss").add("tomba", "hit_valid")
        out = self.run_find(t["contact_id"])
        self.assertEqual((out["data"]["status"], out["data"]["sendable"]), ("not_found", False))
        self.assertEqual(self.providers_called(), ["prospeo", "hunter"])
        self.assertEqual(self.requests()[0]["reason"], "no_hit")
        self.assertIsNone(self.contact(t["contact_id"])["email"])

    def test_network_before_send_does_not_count(self):
        t = self.make_target()
        self.fake.add("prospeo", "raise:connect").add("hunter", "miss").add("tomba", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["provider"]), ("found", "tomba"))

    def test_skip_reasons(self):
        del self.keys["prospeo"]
        self.settings({"enabled": True, "providers": {"hunter": {"enabled": False}}})
        with db.tx(self.conn):
            budget.trip(self.conn, "enrich:tomba", "tls", "test")
        t = self.make_target()
        self.fake.add("getprospect", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        tried = {p["provider"]: p["outcome"] for p in d["providers_tried"]}
        self.assertEqual(tried, {"prospeo": "skipped_no_key", "hunter": "skipped_disabled",
                                 "tomba": "skipped_breaker", "getprospect": "hit"})

    def test_pattern_pre_step_makes_no_call(self):
        t = self.make_target()
        self.publish_pattern(t["company_id"])          # two published first.last addresses (real U6 rule)
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual(self.fake.requests, [])
        self.assertEqual((d["grade"], d["provider"], d["email"], d["sendable"]), ("B", "pattern", E, True))
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email_source"], c["email_evidence_url"]), ("pattern", "https://kestrel.example/team"))
        self.assertEqual(self.requests()[0]["result_source"], "pattern")
        self.assertEqual((c["email_grade"], c["email_mx_ok"], c["email_enrich_call_id"]), ("B", 1, None))

    def test_one_published_address_is_not_a_pattern(self):
        t = self.make_target()
        self.publish_pattern(t["company_id"], people=(("Jordan", "Sample"),))
        self.fake.add("prospeo", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["provider"], d["grade"]), ("prospeo", "B"))
        self.assertEqual(self.contact(t["contact_id"])["email_source"], "provider")

    def test_no_mx(self):
        self.dns.no_mx.add("kestrel.example")
        t = self.make_target()
        self.assertDenied("E_NO_MX", self.run_find, t["contact_id"])
        self.assertEqual(self.fake.requests, [])
        self.assertEqual((self.requests()[0]["status"], self.requests()[0]["reason"]), ("not_found", "no_mx"))

    def test_already_has_address(self):
        t = self.make_target(email="example.person@kestrel.example")
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET email_grade = 'A' WHERE id = ?", (t["contact_id"],))
        out = self.run_find(t["contact_id"])
        self.assertEqual(out["data"]["status"], "already_has_address")
        self.assertEqual(self.fake.requests, [])

    def test_excluded_after_set_address_is_not_sendable(self):
        """The person is excluded while the provider call is on the wire: the real U6 set_address applies the
        exclusion (do not contact) and the result is not sendable."""
        from jobhunter import exclusions
        t = self.make_target(li_slug="example-person")

        def exclude(_provider):
            with db.tx(self.conn):
                exclusions.add(self.conn, "linkedin", "https://www.linkedin.com/in/example-person", "test")
        self.fake.during.append(exclude)
        self.fake.add("prospeo", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["sendable"], d["email"]), ("found", False, None))
        self.assertEqual(self.requests()[0]["reason"], "excluded")
        self.assertEqual(self.contact(t["contact_id"])["do_not_contact"], 1)

    def test_set_address_refusal_is_not_sendable(self):
        """U6 set_address refuses a provider address that bounced before on another row: not sendable, the
        contact keeps no provider address."""
        t = self.make_target()
        other = self.make_target(full_name="Example Other", company_id=t["company_id"], email=E)
        with db.tx(self.conn):
            self.conn.execute("UPDATE contacts SET email_invalid = 1 WHERE id = ?", (other["contact_id"],))
        self.fake.add("prospeo", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["sendable"], d["email"]), ("found", False, None))
        self.assertEqual(self.requests()[0]["reason"], "e_address_grade")
        self.assertIsNone(self.contact(t["contact_id"])["email"])

    def test_found_address_of_another_row_merges(self):
        """The provider address already belongs to another row of the same person: U6 set_address merges the
        rows through people.resolve and the survivor carries the provider address."""
        t = self.make_target(li_slug="example-person")                 # the target's own key
        from tests.helpers import insert_contact
        with db.tx(self.conn):
            twin = insert_contact(self.conn, company_id=t["company_id"], full_name="Example Person", email=None)
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email', ?)",
                              ("email:" + E, twin, self.clock.now()))
        self.fake.add("prospeo", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["sendable"], d["email"]), ("found", True, E))
        rows = self.conn.execute("SELECT id, email, email_source, email_mx_ok, merged_into FROM contacts WHERE id IN "
                                 "(?, ?)", (t["contact_id"], twin)).fetchall()
        survivor = [r for r in rows if r["merged_into"] is None]
        self.assertEqual(len(survivor), 1)
        self.assertEqual((survivor[0]["email"], survivor[0]["email_source"], survivor[0]["email_mx_ok"]),
                         (E, "provider", 1))

    def test_email_only_returned_when_sendable_and_c_not_written(self):
        self.settings({"enabled": True, "chain": ["hunter"], "verifiers": []})
        t = self.make_target()
        self.fake.add("hunter", "hit_low_score")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["grade"], d["sendable"], d["email"]), ("found", "C", False, None))
        self.assertIsNone(self.contact(t["contact_id"])["email"])
        self.assertEqual(self.requests()[0]["reason"], "low_confidence")


class TestVerifier(ChainCase):
    def test_accept_all_then_verifier_valid(self):
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "hit_accept_all").add("zerobounce", "verify_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["grade"], d["sendable"], d["verification"]), ("B", True, "valid"))
        self.assertEqual(self.requests()[0]["reason"], "verified_by_verifier")
        zb = self.fake.calls("zerobounce")[0]
        self.assertNotIn(FAKE_KEY, zb["url"])
        self.assertIn(b"api_key=", zb["data"])

    def test_verifier_invalid_goes_to_next_finder(self):
        """Step 8: invalid -> X, then the next finder; it may find a different address, which can be B."""
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown").add("zerobounce", "verify_invalid") \
            .add("hunter", hunter_hit(OTHER))
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["provider"], d["grade"], d["sendable"], d["email"]), ("hunter", "B", True, OTHER))
        hints = [(c["provider"], c["op"], c["grade_hint"]) for c in self.calls()]
        self.assertEqual(hints, [("prospeo", "find_name_domain", "X"), ("zerobounce", "verify", None),
                                 ("hunter", "find_name_domain", "B")])
        self.assertEqual(verify.lookup(self.conn, E)["grade"], "X")
        self.assertEqual(verify.lookup(self.conn, OTHER)["grade"], "B")

    def test_verifier_catch_all_is_c(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown").add("zerobounce", "verify_catch_all")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["grade"], d["sendable"]), ("C", False))
        self.assertEqual(self.requests()[0]["reason"], "accept_all_no_evidence")
        self.assertEqual(self.providers_called(), ["prospeo", "zerobounce"])   # stop after the verifier

    def test_verify_pending_resume_runs_only_the_verifier(self):
        del self.keys["zerobounce"]
        self.settings({"enabled": True, "verifiers": ["zerobounce"]})
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["grade"], d["verify_pending"], d["sendable"]), ("C", True, False))
        self.keys["zerobounce"] = {"api_key": FAKE_KEY}
        self.fake.add("zerobounce", "verify_valid")
        self.clock.advance(minutes=5)
        d2 = self.run_find(t["contact_id"])["data"]
        self.assertEqual(self.providers_called(), ["prospeo", "zerobounce"])
        self.assertEqual((d2["grade"], d2["sendable"], d2["verify_pending"]), ("B", True, False))
        self.assertEqual(self.contact(t["contact_id"])["email"], E)
        self.assertEqual(len(self.requests()), 1)

    def test_hunter_202_repoll(self):
        self.settings({"enabled": True, "verifiers": ["hunter"], "chain": ["prospeo", "tomba"]})
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown").add("hunter", "verify_in_progress", "verify_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual(self.mono.slept, [10])
        self.assertEqual(d["grade"], "B")
        v = [c for c in self.calls() if c["op"] == "verify"][0]
        self.assertEqual((v["outcome"], v["credits_charged"]), ("hit", 0.5))


class TestInvalidIsFinal(ChainCase):
    """5.2: an address any call judged invalid (finder status, verifier invalid or spamtrap) stays X; a later
    finder that returns the same address as valid does not make it sendable."""

    def assert_not_sendable(self, t, d):
        self.assertFalse(d["sendable"])
        self.assertIsNone(d["email"])
        self.assertNotEqual(d["grade"], "B")
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_source"], c["email_grade"]), (None, None, None))
        self.assertEqual(verify.lookup(self.conn, E)["grade"], "X")
        self.assertTrue(verify.known_invalid(self.conn, E))

    def test_spamtrap_then_same_address_from_the_next_finder(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown").add("zerobounce", "verify_spamtrap").add("hunter", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assert_not_sendable(t, d)
        self.assertEqual(self.requests()[0]["status"], "not_found")
        rows = [(c["provider"], c["grade_hint"], c["reject_reason"]) for c in self.calls()]
        self.assertEqual(rows, [("prospeo", "X", None), ("zerobounce", None, None), ("hunter", "X", "known_invalid")])

    def test_finder_invalid_then_same_address_from_the_next_finder(self):
        self.settings({"enabled": True, "chain": ["hunter", "tomba"]})
        t = self.make_target()
        self.fake.add("hunter", hunter_hit(E, "invalid")).add("tomba", "hit_valid")
        d = self.run_find(t["contact_id"])["data"]
        self.assert_not_sendable(t, d)
        self.assertEqual([(c["provider"], c["grade_hint"]) for c in self.calls()], [("hunter", "X"), ("tomba", "X")])

    def test_invalid_after_a_c_candidate_drops_the_candidate(self):
        """Hunter's low score is a C candidate; Tomba then says the same address is invalid: no C fallback."""
        self.settings({"enabled": True, "chain": ["hunter", "tomba"]})
        t = self.make_target()
        self.fake.add("hunter", "hit_low_score").add("tomba", tomba_hit(E, "invalid"))
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["sendable"], d["email"]), ("not_found", False, None))
        self.assertEqual(verify.lookup(self.conn, E)["grade"], "X")

    def test_address_judged_invalid_elsewhere_is_x_for_a_new_lookup(self):
        """The retry or another lookup finds an address a verifier already rejected: X, next finder."""
        other = self.make_target(full_name="Example Human")
        self.fake.add("prospeo", "hit_unknown").add("zerobounce", "verify_invalid").add("hunter", "miss")
        self.run_find(other["contact_id"])
        self.clock.advance(minutes=5)
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid").add("hunter", hunter_hit(OTHER))
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["grade"], d["email"], d["provider"]), ("B", OTHER, "hunter"))

    def test_verify_pending_resume_of_an_address_judged_invalid_meanwhile(self):
        del self.keys["zerobounce"]
        self.settings({"enabled": True, "verifiers": ["zerobounce"]})
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["grade"], d["verify_pending"]), ("C", True))
        other = self.make_target(full_name="Example Human", company_id=t["company_id"])
        elsewhere = self.provider_address(other["contact_id"], t["company_id"], E, provider="hunter")
        with db.tx(self.conn):     # meanwhile another lookup's verifier rejected the same address
            self.conn.execute("UPDATE contacts SET email = NULL, email_grade = NULL, email_source = NULL, "
                              "email_enrich_call_id = NULL WHERE id = ?", (other["contact_id"],))
            budget.set_grade(self.conn, elsewhere["call_id"], "X")
        self.keys["zerobounce"] = {"api_key": FAKE_KEY}
        self.clock.advance(minutes=5)
        d2 = self.run_find(t["contact_id"])["data"]
        self.assertEqual(self.providers_called(), ["prospeo"])            # no verifier credit spent on it
        self.assertEqual((d2["sendable"], d2["email"], d2["verify_pending"]), (False, None, False))
        self.assertEqual(self.requests()[0]["status"], "not_found")
        self.assertEqual(self.calls()[0]["grade_hint"], "X")


class TestFoundAddressMx(ChainCase):
    """The MX pre-step checks the company domain; an address at another domain (a company alias, a
    subdomain) gets its own MX lookup before email_mx_ok is written."""

    def alias(self, t, domain="kestrel-mail.example"):
        from jobhunter import canon
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) "
                              "VALUES (?, ?, 'dom', 'careers_url', ?)", ("dom:" + domain, t["company_id"], canon.now()))

    def test_alias_domain_without_mx_is_not_sendable(self):
        t = self.make_target()
        self.alias(t)
        self.dns.no_mx.add("kestrel-mail.example")
        self.fake.add("prospeo", prospeo_hit(ALIAS))
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["grade"], d["sendable"], d["email"]), ("found", "B", False, None))
        self.assertEqual(self.requests()[0]["reason"], "no_mx")
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_mx_ok"]), (None, None))
        self.assertEqual(self.dns.calls, ["kestrel.example", "kestrel-mail.example"])

    def test_alias_domain_with_mx_is_checked_and_marked(self):
        t = self.make_target()
        self.alias(t)
        self.fake.add("prospeo", prospeo_hit(ALIAS))
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["sendable"], d["email"]), (True, ALIAS))
        self.assertEqual(self.contact(t["contact_id"])["email_mx_ok"], 1)
        self.assertEqual(self.dns.calls, ["kestrel.example", "kestrel-mail.example"])

    def test_alias_dns_failure_stores_the_address_without_the_mx_mark(self):
        t = self.make_target()
        self.alias(t)
        self.dns.failing.add("kestrel-mail.example")
        self.fake.add("prospeo", prospeo_hit(ALIAS))
        self.run_find(t["contact_id"])
        c = self.contact(t["contact_id"])
        self.assertEqual(c["email"], ALIAS)
        self.assertNotEqual(c["email_mx_ok"], 1)      # gate.reserve refuses it (E_NO_MX) until email verify

    def test_subdomain_gets_its_own_lookup(self):
        t = self.make_target()
        self.dns.no_mx.add("mail.kestrel.example")
        self.fake.add("prospeo", prospeo_hit("example.person@mail.kestrel.example"))
        d = self.run_find(t["contact_id"])["data"]
        self.assertFalse(d["sendable"])
        self.assertIn("mail.kestrel.example", self.dns.calls)

    def test_company_domain_is_looked_up_once(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.assertTrue(self.run_find(t["contact_id"])["data"]["sendable"])
        self.assertEqual(self.dns.calls, ["kestrel.example"])


class TestAgreementAndDeadline(ChainCase):
    def test_finder_agreement(self):
        self.settings({"enabled": True, "chain": ["hunter", "tomba"]})
        t = self.make_target()
        self.fake.add("hunter", "hit_low_score").add("tomba", "hit_accept_all")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["grade"], d["provider"]), ("B", "tomba"))
        self.assertEqual(self.requests()[0]["reason"], "finder_agreement")

    def test_pending_at_deadline_and_resume(self):
        self.fake.cost_s = 35.0
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("hunter", "hit_valid")
        out = self.run_find(t["contact_id"])
        self.assertEqual(out["code"], "PENDING")
        self.assertEqual(out["data"]["status"], "running")
        self.assertEqual(out["next"], "Call enrich find again for this contact.")
        self.assertEqual(self.providers_called(), ["prospeo"])
        uid = self.requests()[0]["request_uid"]
        self.assertFalse(self.conn.execute("SELECT 1 FROM locks WHERE name = ?", (cache.lock_name(uid),)).fetchone())
        out2 = self.run_find(t["contact_id"])
        self.assertEqual((out2["code"], out2["data"]["provider"]), ("OK", "hunter"))
        self.assertEqual(self.providers_called(), ["prospeo", "hunter"])
        self.assertEqual(out2["data"]["request_uid"], uid)

    def test_pending_before_the_verifier_resumes_at_the_verifier(self):
        self.fake.cost_s = 31.0
        t = self.make_target()
        self.fake.add("prospeo", "hit_unknown").add("zerobounce", "verify_valid")
        out = self.run_find(t["contact_id"])
        self.assertEqual(out["code"], "PENDING")
        self.assertEqual(self.requests()[0]["next_step"], 2)
        out2 = self.run_find(t["contact_id"])
        self.assertEqual((out2["code"], out2["data"]["grade"], out2["data"]["sendable"]), ("OK", "B", True))
        self.assertEqual(self.providers_called(), ["prospeo", "zerobounce"])

    def test_dns_failure_is_unavailable_not_a_lookup(self):
        self.dns.failing.add("kestrel.example")         # the real mx_for_domain turns this into E_ENRICH_UNAVAILABLE
        t = self.make_target()
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
        self.assertEqual(d.data["reason"], "mx_lookup_failed")
        self.assertEqual(self.requests()[0]["status"], "unavailable")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM enrich_request_keys").fetchone()[0], 0)

    def test_locked_by_another_process(self):
        from jobhunter import locks
        self.fake.cost_s = 35.0
        t = self.make_target()
        self.fake.add("prospeo", "miss")
        self.run_find(t["contact_id"])
        uid = self.requests()[0]["request_uid"]
        with db.tx(self.conn):
            locks.acquire(self.conn, cache.lock_name(uid), "enrich:other", 120)
        self.assertDenied("E_LOCKED", self.run_find, t["contact_id"])


class TestCallers(ChainCase):
    def test_include_reserve_is_human_only(self):
        t = self.make_target()
        self.assertDenied("E_HUMAN_ONLY", self.run_find, t["contact_id"], include_reserve=True)
        self.assertDenied("E_HUMAN_ONLY", self.run_find, t["contact_id"], caller="system", include_reserve=True)

    def test_human_reserve_chain_uses_long_timeout(self):
        with db.tx(self.conn):
            db.meta_set(self.conn, "raise:enrich.providers.anymailfinder.budget_31d", "5", "human")
            db.meta_set(self.conn, "raise:enrich.providers.anymailfinder.budget_lifetime", "5", "human")
        # `config raise` writes both the meta raise row and the file value (config.raise_)
        self.settings({"enabled": True, "chain": ["prospeo"],
                       "providers": {"anymailfinder": {"enabled": True, "budget_31d": 5, "budget_lifetime": 5}}})
        t = self.make_target()
        self.fake.add("prospeo", "miss").add("anymailfinder", "hit_valid")
        d = self.run_find(t["contact_id"], caller="human", include_reserve=True)["data"]
        self.assertEqual((d["provider"], d["grade"]), ("anymailfinder", "B"))
        self.assertEqual(self.fake.calls("anymailfinder")[0]["timeout_s"], 120.0)
        self.assertEqual(self.requests()[0]["created_by"], "human")

    def test_nothing_callable_is_unavailable_and_not_a_lookup(self):
        self.keys.clear()
        t = self.make_target()
        d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
        self.assertEqual(d.data["reason"], "no_provider")
        self.assertEqual({p["outcome"] for p in d.data["providers_tried"]}, {"skipped_no_key"})
        req = self.requests()[0]
        self.assertEqual(req["status"], "unavailable")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM enrich_request_keys").fetchone()[0], 0)
        self.keys["prospeo"] = {"api_key": FAKE_KEY}
        self.fake.add("prospeo", "hit_valid")
        self.assertEqual(self.run_find(t["contact_id"])["data"]["grade"], "B")

    def test_linkedin_identifier_op(self):
        self.settings({"enabled": True, "use_linkedin_identifier": True})
        t = self.make_target(full_name="Example", li_slug="example-person")
        self.fake.add("prospeo", "linkedin_hit")
        d = self.run_find(t["contact_id"])["data"]
        self.assertEqual(d["grade"], "B")                               # the first name is in the local part
        body = json.loads(self.fake.calls("prospeo")[0]["data"])
        self.assertEqual(list(body["data"]), ["linkedin_url"])


class TestMissingDependency(ChainCase):
    """A U1 or U6 function the finder needs is missing (a broken install): it fails closed."""

    def without(self, name):
        from unittest import mock
        from jobhunter.enrich import deps
        real = deps._real
        return mock.patch.object(deps, "_real", lambda n: None if n == name else real(n))

    def test_no_set_address_writes_nothing(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        with self.without("set_address"):
            d = self.run_find(t["contact_id"])["data"]
        self.assertEqual((d["status"], d["sendable"], d["email"]), ("found", False, None))
        self.assertEqual(self.requests()[0]["reason"], "set_address_missing")
        self.assertIsNone(self.contact(t["contact_id"])["email"])

    def test_no_mx_lookup_is_unavailable(self):
        t = self.make_target()
        with self.without("mx_for_domain"):
            d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
        self.assertEqual(d.data["reason"], "mx_unavailable")
        self.assertEqual(self.fake.requests, [])

    def test_no_guess_list_means_blocked(self):
        t = self.make_target()
        with self.without("guess_blocked"):
            d = self.assertDenied("E_ENRICH_UNAVAILABLE", self.run_find, t["contact_id"])
        self.assertEqual(d.data["reason"], "no_guess_domain")

    def test_selftest_names_the_missing_function(self):
        from jobhunter.enrich import selftest_checks
        with self.without("pattern_evidence"):
            res = {r["name"]: r for r in selftest_checks()}["enrich dependencies"]
        self.assertEqual((res["ok"], res["detail"]), (False, "missing: emailcheck.pattern_evidence"))


if __name__ == "__main__":
    unittest.main()
