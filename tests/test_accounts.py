"""U1: ATS accounts (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.8, 2.9, 1.2): tenant keys, one account per tenant, the
password policy, the terms-checkbox table, `accounts forget`, and the capability consent rules (an ats_accounts
grant made with another sender address is inactive). Fictional hosts and addresses only."""
from __future__ import annotations

import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import accounts, canon, db, identity, paths, secretstore
from jobhunter.errors import Denied
from jobhunter.secretstore import Secret
from tests.fakes.u1.fake_browser_steps import OWNER, grant_caps, write_owner_config
from tests.helpers import HomeTestCase


class TestTenantsAndHosts(unittest.TestCase):
    def test_tenant_keys(self):
        t = accounts.tenant_key
        self.assertEqual(t("workday", "https://kestrel.wd5.myworkdayjobs.com/en-US/External/job/x"), "kestrel.wd5")
        self.assertEqual(t("icims", "https://careers-kestrel.icims.com/jobs/1/login"), "careers-kestrel.icims.com")
        self.assertEqual(t("successfactors", "https://career5.successfactors.eu/careers?company=kestrelcom"),
                         "career5.successfactors.eu|company=kestrelcom")
        self.assertEqual(t("taleo", "https://kestrel.taleo.net/careersection/2/jobdetail.ftl"), "kestrel.taleo.net")
        self.assertEqual(t("oracle_hcm", "https://kestrel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
                                         "CX_1/job/1"), "kestrel.fa.us2.oraclecloud.com|site=cx_1")
        self.assertEqual(t("greenhouse", "https://my.greenhouse.io/"), "global")
        self.assertEqual(t("smartrecruiters", "https://jobs.smartrecruiters.com/Kestrel/1"), "global")
        self.assertEqual(t(None, "https://careers.kestrel.example/apply"), "careers.kestrel.example")
        for plat in ("lever", "ashby", "jobvite"):
            with self.assertRaises(Denied) as cm:
                t(plat, "https://jobs.lever.co/kestrel/1")
            self.assertEqual(cm.exception.data["reason"], "no_accounts_on_platform")
        with self.assertRaises(Denied) as cm:
            t("workday", "https://example.com/x")
        self.assertEqual(cm.exception.data["reason"], "tenant_unknown")

    def test_hosts_follow_the_guard_file(self):
        c = accounts.classify_host
        self.assertEqual(c("kestrel.wd5.myworkdayjobs.com")["platform"], "workday")
        self.assertEqual(c("kestrel.fa.us2.oraclecloud.com")["platform"], "oracle_hcm")
        self.assertIsNone(c("www.oraclecloud.com")["platform"])
        self.assertTrue(c("accounts.google.com")["never"])
        self.assertTrue(c("login.microsoftonline.com")["never"])
        self.assertTrue(c("www.linkedin.com", "https://www.linkedin.com/oauth/v2/authorization")["never"])


class TestPasswords(unittest.TestCase):
    def test_policy(self):
        for _ in range(30):
            pw = accounts.generate_password(20, OWNER).reveal()
            self.assertEqual(len(pw), 20)
            self.assertTrue(accounts.password_ok(pw, 20, OWNER))
            self.assertFalse(set(pw) & accounts.PW_FORBIDDEN)
            alt = accounts.generate_password(16, OWNER, alternative=True).reveal()
            self.assertEqual(alt.count("-"), 1)
            self.assertTrue(all(ch.isalnum() or ch == "-" for ch in alt))
        self.assertEqual(len(accounts.generate_password(8, OWNER).reveal()), 16)     # floor 16
        self.assertFalse(accounts.password_ok("aaaBBB111!!!xyzQ", 16))                 # three in a row
        self.assertFalse(accounts.password_ok("Ab1!Ab1!sam.leeQ", 16, OWNER))          # holds the local part
        self.assertFalse(accounts.password_ok("Ab1!Ab1!Ab1 $Ab1", 16))                 # forbidden characters

    def test_repr_hides_it(self):
        s = accounts.generate_password(20, OWNER)
        self.assertNotIn(s.reveal(), repr(s) + str(s))


class TestTerms(unittest.TestCase):
    TABLE = [
        ("I have read and agree to the Candidate Privacy Statement", "standard_terms"),
        ("I accept the Terms of Use", "standard_terms"),
        ("Yes, I consent to the processing of my data under the privacy policy", "standard_terms"),
        ("Send me job alerts and marketing emails", "unusual"),
        ("Join our talent community", "unusual"),
        ("I consent to a background check", "unusual"),
        ("Text me updates by SMS", "unusual"),
        ("I agree to the privacy policy and to share my profile with third party partners", "unusual"),
        ("Remember me", "other"),
    ]

    def test_classification(self):
        for label, want in self.TABLE:
            self.assertEqual(accounts.classify_checkbox(label), want, label)

    def test_plan(self):
        boxes = [{"label": self.TABLE[0][0], "required": True}, {"label": "Remember me", "required": False},
                 {"label": self.TABLE[3][0], "required": False}, {"label": self.TABLE[3][0], "required": True},
                 {"label": "Remember me", "required": True}]
        acts = [p["action"] for p in accounts.terms_plan(boxes)]
        self.assertEqual(acts, ["ticked", "left_unticked", "left_unticked", "refused", "refused"])


class TestLedgerAndConsent(HomeTestCase):
    def setUp(self):
        super().setUp()
        write_owner_config()
        secretstore.use_test_backend({})
        self.addCleanup(secretstore.use_test_backend, None)

    def add(self, host, tenant, status="active", uid="NAAAAAAA"):
        ts = canon.now()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO ats_accounts (account_uid, platform, site, host, tenant, email, store, "
                              "secret_ref, status, created_at, updated_at) VALUES (?, 'workday', 'workday', ?, ?, ?, "
                              "'keychain', 'x', ?, ?, ?)", (uid, host, tenant, OWNER, status, ts, ts))

    def test_one_account_per_tenant(self):
        self.add("kestrel.wd5.myworkdayjobs.com", "kestrel.wd5")
        with self.assertRaises(Denied) as cm:
            self.add("kestrel.wd5.myworkdayjobs.com", "kestrel.wd5", uid="NBBBBBBB")
        self.assertEqual(cm.exception.code, "E_ALREADY_DONE")
        self.add("kestrel.wd5.myworkdayjobs.com", "kestrel.wd5", status="forgotten", uid="NCCCCCCC")

    def test_forget(self):
        self.add("kestrel.wd5.myworkdayjobs.com", "kestrel.wd5")
        secretstore.put("kestrel.wd5.myworkdayjobs.com", OWNER, Secret("Kx7#fixture#Pw2q"))
        with db.tx(self.conn):
            res = accounts.forget(self.conn, "myworkdayjobs.com", "human:cli")
        self.assertEqual(res["forgotten"], ["NAAAAAAA"])
        self.assertTrue(res["keychain_deleted"])
        self.assertIn("still exists", res["reminder"])
        self.assertIsNone(secretstore.get("kestrel.wd5.myworkdayjobs.com", OWNER))
        self.assertEqual(self.conn.execute("SELECT status FROM ats_accounts").fetchone()[0], "forgotten")
        with self.assertRaises(Denied):
            with db.tx(self.conn):
                accounts.forget(self.conn, "myworkdayjobs.com", "human:cli")

    def test_capability_rows(self):
        self.assertFalse(identity.capability_active("ats_accounts", "workday"))
        grant_caps(self.conn)
        self.assertTrue(identity.capability_active("ats_accounts", "workday"))
        self.assertTrue(identity.capability_active("email_codes", "workday"))
        self.assertFalse(identity.capability_active("email_codes", "icims"))
        # the sites object and every reader of it are unchanged by the capabilities object
        with open(paths.consent_file(), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertIn("gmail", doc["sites"])
        self.assertTrue(identity.consent_active("gmail"))
        # a changed sender address makes the account consent inactive
        write_owner_config(**{"owner.gmail_address": "alex.rivera@example.com"})
        self.assertFalse(identity.capability_active("ats_accounts", "workday"))
        self.assertTrue(identity.capability_active("email_codes", "workday"))

    def test_host_sites_and_revoke(self):
        grant_caps(self.conn, sites=("host:careers.kestrel.example",), caps=("email_codes",))
        self.assertTrue(identity.capability_active("email_codes", "host:careers.kestrel.example"))
        self.assertTrue(identity.capability_active("email_codes", "host:apply.careers.kestrel.example"))
        self.assertFalse(identity.capability_active("email_codes", "host:kestrel.example"))
        with self.assertRaises(Denied):
            identity.valid_capability_site("host:accounts.google.com")
        with self.assertRaises(Denied):
            identity.valid_capability_site("host:boards.greenhouse.io")
        grant_caps(self.conn)
        with db.tx(self.conn):
            res = identity.revoke_capability(self.conn, None, ["workday"])
        self.assertEqual(sorted(r["capability"] for r in res["revoked"]), ["ats_accounts", "email_codes"])
        self.assertEqual(res["breakers_tripped"], ["ats:workday"])
        grant_caps(self.conn)
        row = self.conn.execute("SELECT state FROM breakers WHERE scope = 'ats:workday'").fetchone()
        self.assertEqual(row[0], "closed")

    def test_unsafe_file_gives_no_capability(self):
        grant_caps(self.conn)
        os.chmod(paths.consent_file(), 0o666)
        self.assertFalse(identity.capability_active("email_codes", "workday"))
        os.chmod(paths.consent_file(), 0o600)
        self.assertTrue(identity.capability_active("email_codes", "workday"))

    def test_require_capability_and_prerequisite(self):
        with self.assertRaises(Denied) as cm:
            identity.require_capability(self.conn, "email_codes", "workday")
        self.assertEqual((cm.exception.code, cm.exception.data["capability"]), ("E_CONSENT_MISSING", "email_codes"))
        grant_caps(self.conn)
        identity.require_capability(self.conn, "email_codes", "workday")
        with db.tx(self.conn):
            identity.revoke_consent(self.conn, ["gmail"])
        with self.assertRaises(Denied) as cm:
            identity.require_capability(self.conn, "email_codes", "workday")
        self.assertEqual(cm.exception.data["reason"], "gmail_not_allowed")


if __name__ == "__main__":
    unittest.main()
