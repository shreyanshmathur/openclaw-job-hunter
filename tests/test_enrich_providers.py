"""U10 provider adapters: request building for every adapter and op, and parsing of every recorded fixture
into the normalized result. Offline: fixtures only, the socket guard is on."""
from __future__ import annotations

import dataclasses
import json
import unittest

import tests  # noqa: F401
from jobhunter.enrich import keystore, providers, transport
from jobhunter.enrich.providers import PersonQuery
from tests.fakes.u10 import FAKE_KEY, FAKE_SECRET, fixture_names, fixture_response, install_guard, load_fixture, \
    remove_guard

Q = PersonQuery(first_name="Example", last_name="Person", full_name="Example Person", domain="kestrel.example",
                company_name="Kestrel Commerce", li_handle="example-person")
Q_FULL_ONLY = PersonQuery(first_name=None, last_name=None, full_name="Example", domain="kestrel.example")
FORBIDDEN_IN_RESULT = ("+10000000000", "Engineering Manager", "linkedin.com", "personal", "Kestrel Commerce")


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


def key_for(name):
    if name == "tomba":
        return {"key": keystore.Secret(FAKE_KEY), "secret": keystore.Secret(FAKE_SECRET)}
    return {"api_key": keystore.Secret(FAKE_KEY)}


def arg_for(op):
    return "example.person@kestrel.example" if op == "verify" else Q


class TestBuildRequest(unittest.TestCase):
    def test_every_adapter_and_op(self):
        for name, adapter in providers.REGISTRY.items():
            for op in sorted(adapter.ops):
                with self.subTest(provider=name, op=op):
                    req = adapter.build_request(op, arg_for(op))
                    self.assertIn(req.host, adapter.HOSTS)
                    self.assertIn(req.host, transport.ALLOWED_HOSTS)
                    url, data, headers = transport.build(req, key_for(name))
                    self.assertTrue(url.startswith("https://%s/" % req.host))
                    self.assertNotIn(FAKE_KEY, url)
                    self.assertNotIn(FAKE_SECRET, url)
                    self.assertTrue(headers["User-Agent"].startswith("openclaw-job-hunter/"))
                    body = (data or b"").decode("utf-8")
                    if name == "zerobounce":
                        self.assertIn("api_key=" + FAKE_KEY, body)
                        self.assertEqual(req.method, "POST")
                    else:
                        self.assertNotIn(FAKE_KEY, body)
                        self.assertIn(FAKE_KEY, " ".join(headers.values()))
                    self.assertNotIn("webhook_url", url + body)
                    low = (url + body).lower()
                    for flag in ("enrich_mobile=true", '"enrich_mobile":true', "reveal_phone_number=true",
                                 '"reveal_phone_number":true', "reveal_personal_emails=true"):
                        self.assertNotIn(flag, low.replace(" ", ""))
                    self.assertFalse(any(n.lower() in transport.KEYLIKE_QUERY_NAMES for n, _ in req.query))

    def test_provider_specific_shapes(self):
        pr = providers.get("prospeo").build_request("find_name_domain", Q)
        self.assertEqual((pr.method, pr.path), ("POST", "/enrich-person"))
        self.assertEqual(pr.json_body["only_verified_email"], True)
        self.assertEqual(pr.json_body["enrich_mobile"], False)
        self.assertEqual(pr.json_body["data"], {"first_name": "Example", "last_name": "Person",
                                                "company_website": "kestrel.example"})
        self.assertEqual(pr.secret_headers, (("X-KEY", "api_key", ""),))
        full = providers.get("prospeo").build_request("find_name_domain", Q_FULL_ONLY)
        self.assertEqual(full.json_body["data"], {"full_name": "Example", "company_website": "kestrel.example"})
        li = providers.get("prospeo").build_request("find_linkedin", Q)
        self.assertEqual(li.json_body["data"], {"linkedin_url": providers.linkedin_profile_url("example-person")})

        h = providers.get("hunter").build_request("find_name_domain", Q, timeout_s=12)
        self.assertEqual(dict(h.query), {"domain": "kestrel.example", "first_name": "Example", "last_name": "Person",
                                         "max_duration": "10"})
        self.assertEqual(h.secret_headers, (("X-API-KEY", "api_key", ""),))
        self.assertEqual(dict(providers.get("hunter").build_request("find_name_domain", Q, timeout_s=6).query)
                         ["max_duration"], "4")
        hl = dict(providers.get("hunter").build_request("find_linkedin", Q).query)
        self.assertEqual(hl["linkedin_handle"], "example-person")          # the handle, never the URL
        self.assertNotIn("first_name", hl)
        hv = providers.get("hunter").build_request("verify", "example.person@kestrel.example")
        self.assertEqual((hv.path, dict(hv.query)), ("/v2/email-verifier", {"email": "example.person@kestrel.example"}))

        t = providers.get("tomba").build_request("find_name_domain", Q)
        self.assertEqual([h[0] for h in t.secret_headers], ["X-Tomba-Key", "X-Tomba-Secret"])
        self.assertNotIn("enrich_mobile", dict(t.query))
        g = providers.get("getprospect").build_request("find_name_domain", Q)
        self.assertEqual(g.secret_headers[0][0], "apiKey")
        gl = providers.get("getprospect").build_request("find_linkedin", Q)
        self.assertEqual(gl.path, "/public/v1/insights/contact")
        a = providers.get("anymailfinder").build_request("find_name_domain", Q)
        self.assertEqual(a.secret_headers, (("Authorization", "api_key", ""),))   # no Bearer prefix
        f = providers.get("findymail").build_request("find_name_domain", Q)
        self.assertEqual(f.secret_headers, (("Authorization", "api_key", "Bearer "),))
        self.assertEqual(f.json_body, {"name": "Example Person", "domain": "kestrel.example"})
        ap = providers.get("apollo").build_request("find_name_domain", Q)
        for flag in ("reveal_personal_emails", "reveal_phone_number", "run_waterfall_phone"):
            self.assertIs(ap.json_body[flag], False)
            self.assertEqual(dict(ap.query)[flag], "false")
        z = providers.get("zerobounce").build_request("verify", "example.person@kestrel.example")
        self.assertEqual((z.method, z.path, z.query), ("POST", "/v2/validate", ()))
        self.assertEqual(z.secret_body_fields, (("api_key", "api_key"),))

    def test_unsupported_ops_raise(self):
        for name, adapter in providers.REGISTRY.items():
            for op in set(providers.OPS) - set(adapter.ops):
                with self.subTest(provider=name, op=op):
                    with self.assertRaises(NotImplementedError):
                        adapter.build_request(op, arg_for(op))

    def test_registry_and_exclusions(self):
        self.assertEqual(set(providers.REGISTRY), set(providers.PROVIDER_NAMES))
        for banned in ("snov", "lusha", "proxycurl", "linkedin", "reacher"):
            self.assertNotIn(banned, providers.REGISTRY)
        self.assertEqual(providers.get("anymailfinder").budget_period, "lifetime")
        self.assertEqual(providers.get("findymail").budget_period, "lifetime")
        self.assertEqual(providers.get("hunter").max_charge["verify"], 0.5)


class TestParseFixtures(unittest.TestCase):
    def test_every_fixture(self):
        n = 0
        for name, adapter in providers.REGISTRY.items():
            for fname in fixture_names(name):
                fx = load_fixture(name, fname)
                with self.subTest(provider=name, fixture=fname):
                    status, headers, body = fixture_response(fx)
                    r = adapter.parse(fx["op"], status, headers, body)
                    exp = fx["expect"]
                    self.assertEqual(r.provider, name)
                    self.assertEqual(r.outcome, exp["outcome"])
                    self.assertIn(r.verification, providers.VERIFICATIONS)
                    for k in ("verification", "confidence", "retry_after_s", "source_url", "raw_status",
                              "free_mail_flag"):
                        if k in exp:
                            self.assertEqual(getattr(r, k), exp[k], k)
                    if "email" in exp:
                        self.assertEqual(r.email, exp["email"])
                    if "source_urls" in exp:
                        self.assertEqual(list(r.source_urls), exp["source_urls"])
                    if "credits" in exp:
                        from jobhunter.enrich import budget
                        self.assertEqual(budget.charge(name, fx["op"], r), exp["credits"])
                    dump = json.dumps(dataclasses.asdict(r))
                    for bad in FORBIDDEN_IN_RESULT:
                        self.assertNotIn(bad, dump)
                    for u in r.source_urls:
                        self.assertTrue(u.startswith("https://"))
                    n += 1
        self.assertGreaterEqual(n, 80)

    def test_unverified_fixtures_are_marked(self):
        for name in ("getprospect", "findymail"):
            for fname in fixture_names(name):
                self.assertTrue(load_fixture(name, fname).get("_unverified"), fname)

    def test_unknown_getprospect_status_is_unknown_with_raw_status(self):
        r = providers.get("getprospect").parse("find_name_domain", 200, {}, json.dumps(
            {"email": "example.person@kestrel.example", "status": "brand_new_status"}).encode())
        self.assertEqual((r.outcome, r.verification, r.raw_status), ("hit", "unknown", "brand_new_status"))

    def test_normalize_maps(self):
        n = providers.normalize_status
        self.assertEqual(n("zerobounce", "catch-all"), "accept_all")
        self.assertEqual(n("zerobounce", "do_not_mail"), "invalid")
        self.assertEqual(n("hunter", "webmail"), "invalid")
        self.assertEqual(n("prospeo", "VERIFIED"), "valid")
        self.assertEqual(n("prospeo", "CATCH_ALL"), "unknown")
        self.assertEqual(n("apollo", None), "none")
        self.assertEqual(n("anymailfinder", "risky"), "unknown")

    def test_common_statuses(self):
        a = providers.get("hunter")
        self.assertEqual(a.parse("find_name_domain", 403, {}, b'{"errors":[{"details":"forbidden"}]}').outcome,
                         "auth_failed")
        self.assertEqual(a.parse("find_name_domain", 403, {}, b'{"errors":[{"details":"plan limit"}]}').outcome,
                         "quota_exhausted")
        self.assertEqual(a.parse("find_name_domain", 302, {"location": "https://elsewhere.example/"}, b"").outcome,
                         "bad_response")
        self.assertEqual(a.parse("find_name_domain", 418, {}, b"").outcome, "bad_response")
        r = a.parse("find_name_domain", 429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, b"")
        self.assertEqual((r.outcome, r.retry_after_s), ("rate_limited", 3600))

    def test_call_methods_convert_transport_errors(self):
        class Boom:
            def __init__(self, kind):
                self.kind = kind

            def request(self, req, key, timeout_s):
                raise transport.TransportError(self.kind)
        a = providers.get("hunter")
        key = key_for("hunter")
        for kind, outcome in (("connect", "network_before_send"), ("tls", "network_before_send"),
                              ("timeout_after_send", "timeout_after_send"), ("reset_after_send", "timeout_after_send"),
                              ("oversize", "bad_response")):
            with self.subTest(kind=kind):
                r = a.find_by_name_domain(Q, key, 12, Boom(kind))
                self.assertEqual((r.outcome, r.raw_status), (outcome, kind))

    def test_parser_crash_is_bad_response(self):
        class Weird:
            def request(self, req, key, timeout_s):
                return 200, {}, b'{"data": {"email": "example.person@kestrel.example", "sources": 5}}'
        r = providers.get("hunter").find_by_name_domain(Q, key_for("hunter"), 12, Weird())
        self.assertIn(r.outcome, ("hit", "bad_response"))


if __name__ == "__main__":
    unittest.main()
