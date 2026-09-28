"""U10 transport: host allowlist, no redirects, 256 KB cap, error kinds without URL, header or body text.
An injected opener stands in for the network; the socket guard is on."""
from __future__ import annotations

import io
import socket
import ssl
import unittest
import urllib.error
import urllib.request

import tests  # noqa: F401
from jobhunter.enrich import keystore, providers, transport
from jobhunter.enrich.providers import HttpRequest, PersonQuery
from tests.fakes.u10 import FAKE_KEY, install_guard, remove_guard

KEY = {"api_key": keystore.Secret(FAKE_KEY)}
Q = PersonQuery(first_name="Example", last_name="Person", full_name="Example Person", domain="kestrel.example")


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class FakeResp:
    def __init__(self, status=200, headers=None, body=b"{}", read_exc=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = body
        self.read_exc = read_exc
        self.closed = False

    def read(self, n=-1):
        if self.read_exc is not None:
            raise self.read_exc
        return self.body if n < 0 else self.body[:n]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, action):
        self.action = action
        self.seen = []

    def open(self, req, timeout=None):
        self.seen.append((req, timeout))
        return self.action(req)


def hunter_req():
    return providers.get("hunter").build_request("find_name_domain", Q)


class TestAllowlist(unittest.TestCase):
    def test_unknown_host_refused_before_any_socket(self):
        op = FakeOpener(lambda r: FakeResp())
        req = HttpRequest(method="GET", host="collector.example", path="/x", secret_headers=(("X-KEY", "api_key", ""),))
        with self.assertRaises(ValueError):
            transport.request(req, KEY, 5, opener=op)
        self.assertEqual(op.seen, [])

    def test_key_like_query_names_refused(self):
        req = HttpRequest(method="GET", host="api.hunter.io", path="/v2/email-finder", query=(("api_key", "x"),))
        with self.assertRaises(ValueError):
            transport.build(req, KEY)

    def test_bad_path_or_method(self):
        for req in (HttpRequest(method="GET", host="api.hunter.io", path="v2/x"),
                    HttpRequest(method="GET", host="api.hunter.io", path="/v2/x?api_key=1"),
                    HttpRequest(method="DELETE", host="api.hunter.io", path="/v2/x")):
            with self.assertRaises(ValueError):
                transport.build(req, KEY)

    def test_https_and_headers(self):
        op = FakeOpener(lambda r: FakeResp(body=b'{"data": {"email": null}}'))
        status, headers, body = transport.request(hunter_req(), KEY, 7, opener=op)
        req, timeout = op.seen[0]
        self.assertEqual(status, 200)
        self.assertTrue(req.full_url.startswith("https://api.hunter.io/v2/email-finder?"))
        self.assertNotIn(FAKE_KEY, req.full_url)
        self.assertEqual(req.get_header("X-api-key"), FAKE_KEY)
        self.assertEqual(timeout, 7.0)
        self.assertEqual(headers["content-type"], "application/json")

    def test_missing_secret_is_connect_error(self):
        with self.assertRaises(transport.TransportError) as cm:
            transport.build(hunter_req(), {})
        self.assertEqual(cm.exception.kind, "connect")


class TestRedirectsAndCap(unittest.TestCase):
    def test_redirect_handler_refuses(self):
        h = transport._NoRedirect()
        r = urllib.request.Request("https://api.hunter.io/v2/email-finder")
        self.assertIsNone(h.redirect_request(r, None, 302, "Found", {}, "https://elsewhere.example/"))
        opener = transport.default_opener()
        self.assertTrue(any(isinstance(x, transport._NoRedirect) for x in opener.handlers))

    def test_3xx_is_returned_and_parsed_as_bad_response(self):
        def act(req):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://elsewhere.example/"},
                                         io.BytesIO(b""))
        status, headers, body = transport.request(hunter_req(), KEY, 5, opener=FakeOpener(act))
        self.assertEqual(status, 302)
        self.assertEqual(providers.get("hunter").parse("find_name_domain", status, headers, body).outcome,
                         "bad_response")

    def test_4xx_body_is_returned(self):
        def act(req):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"errors": []}'))
        status, _h, body = transport.request(hunter_req(), KEY, 5, opener=FakeOpener(act))
        self.assertEqual((status, body), (401, b'{"errors": []}'))

    def test_body_cap(self):
        big = b"x" * (transport.MAX_BODY + 1)
        with self.assertRaises(transport.TransportError) as cm:
            transport.request(hunter_req(), KEY, 5, opener=FakeOpener(lambda r: FakeResp(body=big)))
        self.assertEqual(cm.exception.kind, "oversize")
        ok = b"x" * transport.MAX_BODY
        self.assertEqual(len(transport.request(hunter_req(), KEY, 5, opener=FakeOpener(lambda r: FakeResp(body=ok)))[2]),
                         transport.MAX_BODY)


class TestErrorKinds(unittest.TestCase):
    def kind(self, exc=None, read_exc=None):
        def act(req):
            if exc is not None:
                raise exc
            return FakeResp(read_exc=read_exc)
        with self.assertRaises(transport.TransportError) as cm:
            transport.request(hunter_req(), KEY, 5, opener=FakeOpener(act))
        e = cm.exception
        text = str(e) + repr(e)
        for bad in ("hunter.io", "https://", FAKE_KEY, "X-API-KEY", "Example", "kestrel"):
            self.assertNotIn(bad, text)
        self.assertIsNone(e.__cause__)
        return e.kind

    def test_kinds(self):
        self.assertEqual(self.kind(urllib.error.URLError(ConnectionRefusedError("refused"))), "connect")
        self.assertEqual(self.kind(urllib.error.URLError(socket.gaierror("no dns"))), "connect")
        self.assertEqual(self.kind(urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))), "tls")
        self.assertEqual(self.kind(ssl.SSLError("tls")), "tls")
        self.assertEqual(self.kind(socket.timeout("timed out")), "timeout_after_send")
        self.assertEqual(self.kind(ConnectionResetError("reset")), "reset_after_send")
        self.assertEqual(self.kind(read_exc=socket.timeout("slow body")), "timeout_after_send")
        self.assertEqual(self.kind(read_exc=ConnectionResetError("reset")), "reset_after_send")
        self.assertEqual(self.kind(RuntimeError("https://api.hunter.io/?x=" + FAKE_KEY)), "reset_after_send")

    def test_error_pickles_without_details(self):
        import pickle
        e = pickle.loads(pickle.dumps(transport.TransportError("tls")))
        self.assertEqual(e.kind, "tls")
        self.assertEqual(transport.TransportError("weird").kind, "reset_after_send")


if __name__ == "__main__":
    unittest.main()
