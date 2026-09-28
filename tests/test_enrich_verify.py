"""U10 grading: every row of the 5.1 checks and the 5.2 grade table (free-mail, other domain, role address,
name mismatch, agreement, verifier paths, X)."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import db
from jobhunter.enrich import verify
from jobhunter.enrich.providers import EnrichResult
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard

DOMS = {"kestrel.example"}
FREE = {"freemail.example", "mail.example"}
HOST = ("hosting.example",)
E = "example.person@kestrel.example"


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


def pre(email, **kw):
    kw.setdefault("company_domains", DOMS)
    kw.setdefault("freemail", FREE)
    kw.setdefault("hosting", HOST)
    return verify.precheck_address(email, **kw)


def r(verification="valid", confidence=None, outcome="hit", email=E, provider="hunter"):
    return EnrichResult(email=email, verification=verification, confidence=confidence, outcome=outcome,
                        provider=provider)


def g(cand, verifier=None, agreement=False, pattern=False, name_ok=True, minc=90):
    return verify.grade(cand, verifier=verifier, agreement=agreement, pattern_match=pattern, name_ok=name_ok,
                        min_confidence=minc)


class TestPrecheck(unittest.TestCase):
    def test_rows(self):
        self.assertIsNone(pre(E))
        self.assertIsNone(pre("example.person@mail.kestrel.example"))            # subdomain of the company
        self.assertEqual(pre("not an address"), "syntax")
        self.assertEqual(pre("a@b"), "syntax")
        self.assertEqual(pre("x" * 65 + "@kestrel.example"), "syntax")
        self.assertEqual(pre("example.person@freemail.example"), "free_mail")
        self.assertEqual(pre("example.person@mail.example"), "free_mail")
        self.assertEqual(pre("example.person@site.hosting.example"), "free_mail")
        self.assertEqual(pre(E, free_mail_flag=True), "free_mail")
        self.assertEqual(pre(E, raw_status="webmail"), "free_mail")
        self.assertEqual(pre("example.person@other.example"), "domain_mismatch")
        for local in ("careers", "jobs", "hr", "info", "no-reply", "talent", "hello+x"):
            self.assertEqual(pre(local + "@kestrel.example"), "role_address", local)

    def test_name_plausibility(self):
        np = verify.name_plausible
        self.assertTrue(np("example.person", "Example", "Person"))
        self.assertTrue(np("eperson", "Example", "Person"))
        self.assertTrue(np("examplep", "Example", "Person"))
        self.assertTrue(np("person", "Example", "Person"))
        self.assertTrue(np("exa", "Exa", "Li"))
        self.assertTrue(np("li.w", "Wen", "Li"))
        self.assertTrue(np("jose.nunez", "Jos\u00e9", "N\u00fa\u00f1ez"))      # NFKD folding
        self.assertFalse(np("someone.else", "Example", "Person"))
        self.assertFalse(np("ab", "Ab", "C"))
        self.assertFalse(np("", "Example", "Person"))


class TestGradeTable(unittest.TestCase):
    def test_pattern_is_b(self):
        self.assertEqual(g(r("unknown"), pattern=True), ("B", "pattern_evidence"))

    def test_verified_by_finder(self):
        self.assertEqual(g(r("valid", 94)), ("B", "verified_by_finder"))
        self.assertEqual(g(r("valid", None)), ("B", "verified_by_finder"))     # no score given (Prospeo)
        self.assertEqual(g(r("valid", 90)), ("B", "verified_by_finder"))

    def test_agreement(self):
        self.assertEqual(g(r("valid", 70), agreement=True), ("B", "finder_agreement"))
        self.assertEqual(g(r("valid", 94), agreement=True, name_ok=False), ("B", "finder_agreement"))

    def test_verifier_valid(self):
        for v in ("accept_all", "unknown", "none"):
            self.assertEqual(g(r(v), verifier=r("valid", provider="zerobounce")), ("B", "verified_by_verifier"))
        self.assertEqual(g(r("unknown"), verifier=r("valid"), name_ok=False), ("C", "name_mismatch"))

    def test_low_confidence_and_name(self):
        self.assertEqual(g(r("valid", 70)), ("C", "low_confidence"))
        self.assertEqual(g(r("valid", 95), name_ok=False), ("C", "name_mismatch"))
        self.assertEqual(g(r("valid", 70), minc=65), ("B", "verified_by_finder"))

    def test_accept_all_unknown(self):
        self.assertEqual(g(r("accept_all")), ("C", "accept_all_no_evidence"))
        self.assertEqual(g(r("unknown")), ("C", "unknown_no_evidence"))
        self.assertEqual(g(r("none")), ("C", "unknown_no_evidence"))
        self.assertEqual(g(r("unknown"), verifier=r("accept_all")), ("C", "accept_all_no_evidence"))
        self.assertEqual(g(r("accept_all"), verifier=r("unknown")), ("C", "unknown_no_evidence"))

    def test_invalid_is_x_and_wins(self):
        self.assertEqual(g(r("invalid")), ("X", "invalid"))
        self.assertEqual(g(r("accept_all"), verifier=r("invalid")), ("X", "invalid"))
        self.assertEqual(g(r("valid", 99), pattern=True, verifier=r("invalid")), ("X", "invalid"))
        self.assertEqual(g(r("unknown", outcome="invalid")), ("X", "invalid"))

    def test_needs_verifier(self):
        self.assertTrue(verify.needs_verifier(r("accept_all"), "C", True))
        self.assertFalse(verify.needs_verifier(r("accept_all"), "C", False))
        self.assertFalse(verify.needs_verifier(r("valid", 70), "C", True))
        self.assertFalse(verify.needs_verifier(r("accept_all"), "B", True))


class TestDatabaseReads(EnrichTestCase):
    def test_company_domains_and_suppression(self):
        t = self.make_target()
        self.assertEqual(verify.company_domains(self.conn, t["company_id"]), {"kestrel.example"})
        self.assertIsNone(verify.suppression(self.conn, E, t["company_id"]))
        from jobhunter import exclusions
        with db.tx(self.conn):
            exclusions.add(self.conn, "email", E, "asked not to be contacted", source="human")
        self.assertEqual(verify.suppression(self.conn, E, t["company_id"]), "excluded")
        self.assertIsNone(verify.suppression(self.conn, "someone.else@kestrel.example", t["company_id"]))
        with db.tx(self.conn):
            db.meta_set(self.conn, "no_guess:kestrel.example", "1", "system")
        self.assertEqual(verify.suppression(self.conn, "someone.else@kestrel.example", t["company_id"]),
                         "no_guess_domain")

    def test_lookup_hook(self):
        self.assertIsNone(verify.lookup(self.conn, E))
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        got = verify.lookup(self.conn, E.upper())
        self.assertEqual((got["grade"], got["provider"], got["bounced"]), ("B", "prospeo", False))


class TestRealVerifyAddress(EnrichTestCase):
    """U6 emailcheck.verify_address reads the finder's result through verify.lookup (ENRICH-SPEC U6-2)."""

    def found(self):
        t = self.make_target()
        self.fake.add("prospeo", "hit_valid")
        self.run_find(t["contact_id"])
        return t

    def check(self, grade, evidence_url=None):
        from jobhunter import emailcheck
        with db.tx(self.conn):
            return emailcheck.verify_address(self.conn, E, grade, evidence_url, mx_hosts=["mx.kestrel.example"])

    def test_agent_cannot_raise_a_provider_grade(self):
        t = self.found()
        res = self.check("A")
        self.assertEqual((res["grade"], res["email_source"], res["allowed"]), ("B", "provider", True))
        self.assertEqual(res["provider_result"], {"grade": "B", "provider": "prospeo", "bounced": False})
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email_grade"], c["email_source"]), ("B", "provider"))

    def test_bounced_provider_result_is_never_usable(self):
        t = self.found()
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET bounced_at = ?", (self.clock.now(),))
        res = self.check("B", "https://kestrel.example/team")
        self.assertFalse(res["allowed"])
        self.assertIn("provider_invalid", res["reasons"])
        self.assertEqual(self.contact(t["contact_id"])["email_source"], "provider")


if __name__ == "__main__":
    unittest.main()
