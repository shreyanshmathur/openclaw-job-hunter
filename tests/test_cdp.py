"""U6: the stdlib CDP client (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.2) against the fake CDP server: websocket framing
(client masking, fragmented replies, ping before a reply), loopback only, a foreign websocket URL refused, a
missing tab or port as E_ROUTE_UNAVAILABLE, the budget, and errors that never carry a value."""
from __future__ import annotations

import json
import time
import unittest

import tests  # noqa: F401
from jobhunter import cdp, paths
from jobhunter.errors import Denied
from jobhunter.secretstore import Secret
from tests.fakes.u6.fake_cdp import FakeCdp
from tests.helpers import HomeTestCase


class TestCdp(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.f = FakeCdp().start()
        self.addCleanup(self.f.stop)
        h = paths.home()
        h["browser_cdp"] = {"port": self.f.port}
        with open(paths.home_file(), "w", encoding="utf-8") as fh:
            json.dump(h, fh)
        self.tab = self.f.add_tab("wd_signin")

    def test_port_from_home_and_targets(self):
        self.assertEqual(cdp.port(), self.f.port)
        self.assertEqual([t["id"] for t in cdp.list_targets()], [self.tab])
        self.assertTrue(cdp.available())

    def test_framing(self):
        for frag, ping in ((False, False), (True, False), (False, True), (True, True)):
            self.f.fragment, self.f.ping_first = frag, ping
            with cdp.connect(self.tab) as s:
                self.assertTrue(s.page_text()["text"].startswith("Sign In"))
        self.assertIn("ws:pong", self.f.methods())

    def test_unknown_script_is_an_error_without_text(self):
        with cdp.connect(self.tab) as s:
            with self.assertRaises(cdp.CdpError) as cm:
                s.evaluate("() => document.cookie")
        self.assertEqual(str(cm.exception), "cdp Runtime.evaluate failed (exception)")

    def test_secret_goes_only_into_the_frame(self):
        with cdp.connect(self.tab) as s:
            s.click_at(100, 140)
            s.insert_text(Secret("Kx7#fixture#Pw2q"))
        self.assertIn({"method": "Input.insertText", "len": 16}, self.f.log)
        self.assertNotIn("Kx7#fixture#Pw2q", json.dumps(self.f.log))

    def test_foreign_websocket_url_is_refused(self):
        self.f.foreign_ws = True
        self.assertEqual(cdp.list_targets(), [])
        with self.assertRaises(Denied) as cm:
            cdp.connect(self.tab)
        self.assertEqual((cm.exception.code, cm.exception.data["reason"]), ("E_ROUTE_UNAVAILABLE", "tab_not_found"))

    def test_missing_tab_and_port(self):
        with self.assertRaises(Denied) as cm:
            cdp.connect("NOSUCHTAB")
        self.assertEqual(cm.exception.code, "E_ROUTE_UNAVAILABLE")
        self.f.stop()
        with self.assertRaises(Denied) as cm:
            cdp.list_targets()
        self.assertEqual((cm.exception.code, cm.exception.data["reason"]), ("E_ROUTE_UNAVAILABLE", "cdp_unreachable"))
        self.assertFalse(cdp.available())

    def test_no_port_recorded(self):
        h = paths.home()
        h.pop("browser_cdp", None)
        with open(paths.home_file(), "w", encoding="utf-8") as fh:
            json.dump(h, fh)
        with self.assertRaises(Denied) as cm:
            cdp.port()
        self.assertEqual(cm.exception.data["reason"], "cdp_unknown")

    def test_budget(self):
        s = cdp.connect(self.tab, budget_s=0.2)
        time.sleep(0.3)
        with self.assertRaises(cdp.CdpError) as cm:
            s.page_text()
        self.assertEqual(cm.exception.code, "budget")
        s.close()

    def test_new_and_close_tab(self):
        tid = cdp.new_tab("https://mail.google.com/mail/u/0/#inbox")
        self.assertIn(tid, self.f.tabs)
        self.assertTrue(cdp.close_tab(tid))
        self.assertNotIn(tid, self.f.tabs)
        self.assertFalse(cdp.close_tab("../etc"))

    def test_masking_is_required_by_the_server(self):
        # the fake refuses unmasked client frames, so every call above proves the client masks
        with cdp.connect(self.tab) as s:
            s.press("Enter")
        self.assertIn("Input.dispatchKeyEvent", self.f.methods())


if __name__ == "__main__":
    unittest.main()
