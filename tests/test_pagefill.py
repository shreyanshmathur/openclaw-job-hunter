"""U6: field finding and the fill checks of the code-owned steps (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.2) on every
recorded fixture page (tests/fixtures/browser/ats_account_pages.json) through the fake CDP server."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import accounts, cdp, pagefill
from jobhunter.errors import Denied
from jobhunter.secretstore import Secret
from tests.fakes.u6.fake_cdp import FakeCdp


class TestPagefill(unittest.TestCase):
    def setUp(self):
        self.f = FakeCdp().start()
        self.addCleanup(self.f.stop)
        cdp.set_test_port(self.f.port)
        self.addCleanup(cdp.set_test_port, None)

    def form(self, page):
        tab = self.f.add_tab(page)
        with cdp.connect(tab) as s:
            return tab, pagefill.read_form(s)

    def test_workday_account_form_by_automation_id(self):
        _t, form = self.form("wd_create")
        af = pagefill.find_account_form(form)
        self.assertEqual((af["email"]["aid"], [p["aid"] for p in af["passwords"]], af["button"]["name"]),
                         ("email", ["password", "verifyPassword"], "Create Account"))
        self.assertEqual([accounts.classify_checkbox(b["label"]) for b in af["checkboxes"]], ["standard_terms"])

    def test_label_based_account_form(self):
        _t, form = self.form("icims_account")
        af = pagefill.find_account_form(form)
        self.assertEqual([p["label"] for p in af["passwords"]], ["Password", "Confirm Password"])
        self.assertEqual(af["button"]["name"], "Register")
        plan = accounts.terms_plan(af["checkboxes"])
        self.assertEqual([p["action"] for p in plan], ["ticked", "left_unticked"])

    def test_no_account_form(self):
        _t, form = self.form("wd_posting")
        with self.assertRaises(Denied) as cm:
            pagefill.find_account_form(form)
        self.assertEqual(cm.exception.data["reason"], "form_not_recognized")

    def test_code_fields(self):
        for page, kind, n in (("wd_signin_code", "single", 1), ("gh_security_code", "single", 1),
                              ("oracle_verify", "single", 1), ("sf_otp", "split", 6)):
            _t, form = self.form(page)
            cf = pagefill.find_code_field(form)
            self.assertEqual((cf["kind"], len(cf["fields"])), (kind, n), page)
        _t, form = self.form("wd_app_form")
        self.assertIsNone(pagefill.find_code_field(form))

    def test_buttons_per_step(self):
        _t, form = self.form("gh_security_code")
        self.assertEqual(pagefill.find_button(form, "resubmit")["name"], "Submit application")
        _t, form = self.form("sf_otp")
        self.assertEqual(pagefill.find_button(form, "verify")["name"], "Submit Code")
        _t, form = self.form("wd_social")
        self.assertIsNone(pagefill.find_button(form, "signin"))     # "Sign in with Google" is never a sign-in button

    def test_fill_verifies_length_and_mask(self):
        tab, form = self.form("wd_signin")
        sf = pagefill.find_signin_form(form)
        with cdp.connect(tab) as s:
            pagefill.fill(s, sf["password"], Secret("Kx7#fixture#Pw2q"), masked=True)
            after = pagefill.field_by_index(pagefill.read_form(s), sf["password"]["i"])
        self.assertEqual((after["value_length"], after["masked"]), (16, True))

    def test_unmasked_password_is_cleared_and_refused(self):
        self.f.pages["wd_signin"]["fields"][1]["unmasked"] = True
        tab, form = self.form("wd_signin")
        sf = pagefill.find_signin_form(form)
        with cdp.connect(tab) as s:
            with self.assertRaises(Denied) as cm:
                pagefill.fill(s, sf["password"], Secret("Kx7#fixture#Pw2q"), masked=True)
            after = pagefill.field_by_index(pagefill.read_form(s), sf["password"]["i"])
        self.assertEqual(cm.exception.data["reason"], "fill_not_verified")
        self.assertEqual(after["value_length"], 0)

    def test_captcha_state(self):
        tab = self.f.add_tab("wd_captcha")
        with cdp.connect(tab) as s:
            self.assertTrue(pagefill.captcha_state(s)["visible"])
        tab = self.f.add_tab("wd_create")
        with cdp.connect(tab) as s:
            self.assertFalse(pagefill.captcha_state(s)["visible"])

    def test_scripts_hold_no_page_writes(self):
        for name, script in pagefill.SCRIPTS.items():
            self.assertNotIn("click(", script, name)
            self.assertNotIn(".value =", script, name)


if __name__ == "__main__":
    unittest.main()
