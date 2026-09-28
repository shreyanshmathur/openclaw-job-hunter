"""The browser consent helpers behind ./jobhunter browser consent|forget|check (U7): private/consent.json as the
installer writes it, every site at No until the owner answers, Chrome profiles by display name (work profiles
flagged, never picked silently), the read-only login check verdicts, and the wrapper routes of the email finder
commands (PIN for the owner actions)."""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest

import tests  # noqa: F401
from jobhunter import auth, cli, install as ins, paths
from jobhunter.commands import install as install_cmds
from jobhunter.errors import Denied
from tests.helpers import HomeTestCase

PIN = "135790"


class ConsentCase(HomeTestCase):
    browser_consent = False            # the state of a new install: no private/consent.json

    def run_cli(self, *argv, stdin: str = "", env: dict | None = None):
        out = io.StringIO()
        rc = cli.main(list(argv), env=env or {}, stdin=io.StringIO(stdin), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def read(self) -> dict:
        with open(ins.consent_path()) as fh:
            return json.load(fh)


class TestDefaultNo(ConsentCase):
    def test_no_file_means_no_site(self):
        self.assertFalse(os.path.exists(ins.consent_path()))
        for site in ins.CONSENT_SITES:
            self.assertFalse(ins.consent_active(site), site)
        rows = ins.consent_sites({})
        self.assertEqual([r["site"] for r in rows][:8], list(ins.CONSENT_ALWAYS_ASK))
        self.assertEqual({r["status"] for r in rows}, {"none"})           # never asked: off
        self.assertEqual(ins.remaining_imports(), {"remaining_imports": [], "remaining_manual": []})

    def test_enabled_boards_are_asked_too(self):
        cfg = {"boards": {"sites": {"cutshort": {"discover": "browser"}, "hirist": {"discover": "api"}}}}
        names = [r["site"] for r in ins.consent_sites(cfg)]
        self.assertIn("cutshort", names)
        self.assertNotIn("hirist", names)
        self.assertIn("iimjobs", [r["site"] for r in ins.consent_sites({}, browsed_sites=["iimjobs"])])
        self.assertEqual([r["site"] for r in ins.consent_sites({}, only=["naukri"])], ["naukri"])
        with self.assertRaises(Denied):
            ins.consent_sites({}, only=["example-board"])

    def test_human_lines_of_consent_sites(self):
        rc, text = self.run_cli("--human", "install", "consent-sites", "--site", "gmail", "--site", "naukri")
        self.assertEqual(rc, 0, text)
        lines = [l.split("\t") for l in text.strip().split("\n")]
        self.assertEqual(lines, [["gmail", "none", "google.com,mail.google.com,accounts.google.com", "Gmail"],
                                 ["naukri", "none", "naukri.com", "Naukri"]])


class TestRecordAndRevoke(ConsentCase):
    def test_grant_decline_revoke(self):
        res = ins.record_consent(["gmail,naukri"], ["linkedin"], method="chrome_import", chrome_profile="Profile 1",
                                 chrome_profile_name="Personal")
        self.assertEqual((res["granted"], res["declined"]), (["gmail", "naukri"], ["linkedin"]))
        self.assertEqual(os.stat(ins.consent_path()).st_mode & 0o777, 0o600)
        doc = self.read()
        g = doc["sites"]["gmail"]
        self.assertEqual((g["status"], g["method"], g["chrome_profile"], g["chrome_profile_name"]),
                         ("granted", "chrome_import", "Profile 1", "Personal"))
        self.assertTrue(g["granted_at"])
        self.assertEqual(doc["sites"]["linkedin"]["status"], "declined")
        self.assertTrue(ins.consent_active("gmail"))
        self.assertFalse(ins.consent_active("linkedin"))
        self.assertEqual(ins.remaining_imports()["remaining_imports"],
                         [{"chrome_profile": "Profile 1",
                           "domains": ["google.com", "mail.google.com", "accounts.google.com", "naukri.com"]}])
        # a later No makes the earlier grant inactive
        ins.record_consent(None, ["naukri"], method="manual_login")
        self.assertFalse(ins.consent_active("naukri"))
        out = ins.revoke_consent(["gmail"])
        self.assertEqual(out["revoked"], ["gmail"])
        self.assertEqual(self.read()["sites"]["gmail"]["status"], "revoked")
        self.assertTrue(self.read()["sites"]["gmail"]["revoked_at"])
        self.assertEqual(ins.active_consents(), {})
        self.assertEqual(ins.revoke_consent(everything=True)["revoked"], [])

    def test_manual_logins_are_listed_apart(self):
        ins.record_consent(["wellfound"], None, method="manual_login")
        ins.record_consent(["gmail"], None, method="chrome_import", chrome_profile="Default")
        res = ins.revoke_consent(["gmail"])
        self.assertEqual(res["remaining_imports"], [])
        self.assertEqual(res["remaining_manual"], ["wellfound"])

    def test_bad_input_is_refused(self):
        with self.assertRaises(Denied):
            ins.record_consent(["gmail"], ["gmail"], method="manual_login")        # both at once
        with self.assertRaises(Denied):
            ins.record_consent(None, None, method="manual_login")                # nothing named
        with self.assertRaises(Denied):
            ins.record_consent(["gmail"], None, method="chrome_import", chrome_profile="../../etc")
        with self.assertRaises(Denied):
            ins.record_consent(["gmail"], None, method="copy_everything")
        with self.assertRaises(Denied):
            ins.record_consent(["facebook"], None, method="manual_login")
        with self.assertRaises(Denied):
            ins.revoke_consent(None)
        self.assertFalse(os.path.exists(ins.consent_path()))

    def test_unsafe_files_give_no_consent(self):
        ins.record_consent(["gmail"], None, method="manual_login")
        self.assertTrue(ins.consent_active("gmail"))
        os.chmod(ins.consent_path(), 0o666)                                 # others may write it
        self.assertFalse(ins.consent_active("gmail"))
        os.chmod(ins.consent_path(), 0o600)
        self.assertTrue(ins.consent_active("gmail"))
        real = ins.consent_path() + ".real"
        os.rename(ins.consent_path(), real)
        os.symlink(real, ins.consent_path())                                # a link
        self.assertFalse(ins.consent_active("gmail"))
        os.unlink(ins.consent_path())
        with open(ins.consent_path(), "w") as fh:
            fh.write("{not json")
        os.chmod(ins.consent_path(), 0o600)
        self.assertFalse(ins.consent_active("gmail"))
        with open(ins.consent_path(), "w") as fh:
            json.dump({"sites": {"gmail": {"status": "allowed", "granted_at": "x"}}}, fh)
        self.assertFalse(ins.consent_active("gmail"))                       # unknown status
        # the rule of the core and the guard: granted, a granted_at, no revoked_at, under its own name
        for row in ({"status": "granted"}, {"status": "granted", "granted_at": "t", "revoked_at": "t"},
                    {"status": "granted", "granted_at": "t", "site": "linkedin"}):
            with open(ins.consent_path(), "w") as fh:
                json.dump({"sites": {"gmail": row}}, fh)
            self.assertFalse(ins.consent_active("gmail"), row)
            self.assertNotEqual(ins.consent_sites({}, only=["gmail"])[0]["status"], "granted", row)


class TestConsentCommands(ConsentCase):
    def test_record_needs_the_owner_pin(self):
        rc, env = self.run_cli("install", "consent-record", "--allow", "gmail", "--method", "manual_login")
        self.assertNotEqual(rc, 0)
        self.assertEqual(env["code"], "E_HUMAN_ONLY")
        rc, env = self.run_cli("install", "consent-record", "--allow", "gmail", "--method", "manual_login",
                               env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-applier"})
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(ins.consent_path()))
        auth.set_pin(None, PIN)
        rc, env = self.run_cli("--pin-stdin", "install", "consent-record", "--allow", "gmail", "--decline",
                               "linkedin", "--method", "manual_login", stdin=PIN + "\n")
        self.assertEqual(rc, 0, env)
        self.assertEqual((env["data"]["granted"], env["data"]["declined"]), (["gmail"], ["linkedin"]))
        self.assertTrue(ins.consent_active("gmail"))

    def test_revoke_and_imports_lines(self):
        ins.record_consent(["gmail", "naukri"], None, method="chrome_import", chrome_profile="Default")
        ins.record_consent(["wellfound"], None, method="manual_login")
        rc, text = self.run_cli("--human", "install", "consent-imports")
        self.assertEqual(text.strip().split("\n"), [
            "import\tgoogle.com,mail.google.com,accounts.google.com,naukri.com\tDefault", "manual\twellfound"])
        rc, text = self.run_cli("--human", "install", "consent-revoke", "--site", "naukri")
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.strip().split("\n"), [
            "revoked\tnaukri", "import\tgoogle.com,mail.google.com,accounts.google.com\tDefault", "manual\twellfound"])
        rc, env = self.run_cli("install", "consent-revoke", "--site", "naukri")
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))
        rc, env = self.run_cli("install", "consent-revoke")
        self.assertNotEqual(rc, 0)


LOCAL_STATE = {"profile": {
    "profiles_order": ["Default", "Profile 2", "Profile 1"],
    "info_cache": {
        "Default": {"name": "Personal", "user_name": "alex.rivera@example.com", "hosted_domain": "NO_HOSTED_DOMAIN"},
        "Profile 1": {"name": "Alex", "user_name": "alex@kestrel.example", "hosted_domain": "kestrel.example"},
        "Profile 2": {"name": "Office stuff", "user_name": ""},
        "Profile 3": {"name": "Side project", "is_managed": True},
        "Guest Profile": {"name": "Guest"},
        "System Profile": {"name": "System"}}}}


class TestChromeProfiles(ConsentCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp(prefix="jh-chrome-")
        self.path = os.path.join(self.d, "Local State")
        with open(self.path, "w") as fh:
            json.dump(LOCAL_STATE, fh)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)
        super().tearDown()

    def test_names_order_and_work_flags(self):
        profs = ins.chrome_profiles(self.path)
        self.assertEqual([(p["dir"], p["name"], p["work"]) for p in profs],
                         [("Default", "Personal", False), ("Profile 2", "Office stuff", True),
                          ("Profile 1", "Alex", True), ("Profile 3", "Side project", True)])
        self.assertIn("managed by kestrel.example", profs[2]["work_reason"])

    def test_cli_list_and_pick(self):
        rc, text = self.run_cli("--human", "install", "chrome-profiles", "--local-state", self.path)
        self.assertEqual(rc, 0, text)
        self.assertIn("1. Personal", text)
        self.assertIn("WORK OR SCHOOL PROFILE", text)
        rc, text = self.run_cli("--human", "install", "chrome-profiles", "--local-state", self.path, "--pick", "3")
        self.assertEqual(text.strip().split("\t"), ["Profile 1", "1", "Alex"])
        rc, env = self.run_cli("install", "chrome-profiles", "--local-state", self.path, "--pick", "9")
        self.assertNotEqual(rc, 0)
        rc, env = self.run_cli("install", "chrome-profiles", "--local-state", os.path.join(self.d, "missing"))
        self.assertEqual(env["code"], "E_NOT_FOUND")

    def test_default_paths(self):
        self.assertTrue(ins.chrome_local_state_path("/h", "darwin").endswith(
            "Library/Application Support/Google/Chrome/Local State"))
        self.assertEqual(ins.chrome_local_state_path("/h", "linux"), "/h/.config/google-chrome/Local State")


def probe(url, **kw):
    d = {"jh_login_probe": 1, "url": url, "title": "t", "account_email": None, "password_field": False,
         "captcha": False, "challenge_text": False}
    d.update(kw)
    return d


class TestLoginVerdict(unittest.TestCase):
    def test_gmail(self):
        inbox = "https://mail.google.com/mail/u/0/#inbox"
        self.assertEqual(ins.login_verdict("gmail", probe(inbox, account_email="eve@example.com"),
                                           "eve@example.com")[0], "ok")
        v, text = ins.login_verdict("gmail", probe(inbox, account_email="other@example.com"), "eve@example.com")
        self.assertEqual(v, "mismatch")
        self.assertIn("other@example.com", text)
        self.assertEqual(ins.login_verdict("gmail", probe("https://accounts.google.com/v3/signin/identifier",
                                                          password_field=True), "eve@example.com")[0], "logged_out")
        self.assertEqual(ins.login_verdict("gmail", probe(inbox, challenge_text=True), "eve@example.com")[0],
                         "checkpoint")
        self.assertEqual(ins.login_verdict("gmail", probe(inbox), "eve@example.com")[0], "unknown")

    def test_linkedin_and_boards(self):
        self.assertEqual(ins.login_verdict("linkedin", probe("https://www.linkedin.com/feed/"))[0], "ok")
        self.assertEqual(ins.login_verdict("linkedin", probe("https://www.linkedin.com/checkpoint/challenge/x"))[0],
                         "checkpoint")
        self.assertEqual(ins.login_verdict("linkedin", probe("https://www.linkedin.com/authwall?x=1"))[0],
                         "logged_out")
        self.assertEqual(ins.login_verdict("naukri", probe("https://www.naukri.com/mnjuser/homepage"))[0], "ok")
        self.assertEqual(ins.login_verdict("naukri", probe("https://www.naukri.com/nlogin/login"))[0], "logged_out")
        self.assertEqual(ins.login_verdict("naukri", probe("https://www.naukri.com/x", captcha=True))[0], "checkpoint")
        self.assertEqual(ins.login_verdict("naukri", None)[0], "unknown")

    def test_probe_is_found_in_cli_output(self):
        self.assertEqual(ins.find_probe({"ok": True, "result": {"value": probe("u")}})["url"], "u")
        self.assertEqual(ins.find_probe({"result": json.dumps(probe("v"))})["url"], "v")
        self.assertIsNone(ins.find_probe({"result": "nothing"}))

    def test_probe_function_only_reads(self):
        js = ins.LOGIN_PROBE_JS
        for never in (".click(", ".submit(", "value =", "localStorage", "fetch(", "XMLHttpRequest", "location ="):
            self.assertNotIn(never, js)


class TestEmailFinderRoutes(unittest.TestCase):
    """./jobhunter enrich ...: the owner actions need the PIN (acl.json human_only), the rest run as system."""

    def setUp(self):
        parser = cli.build_parser()
        self.commands = {k: v.get_default("_jh_callers") for k, v in cli.registered_commands(parser).items()}
        with open(paths.ACL_FILE) as fh:
            self.human_only = json.load(fh)["human_only"]
        if "enrich connect" not in self.commands:
            self.skipTest("the email finder commands (U10) are not installed")

    def route(self, *words):
        return ins.wrapper_route(list(words), self.commands, self.human_only)

    def test_routes(self):
        for words in (("enrich", "connect", "hunter"), ("enrich", "disconnect", "--all"), ("enrich", "retry", "P1"),
                      ("enrich", "test", "hunter"), ("enrich", "find", "--contact", "P1", "--include-reserve")):
            self.assertEqual(self.route(*words)[0], "pin", words)
        self.assertEqual(self.route("enrich", "find", "--contact", "P1"), ("open", "enrich find"))
        self.assertEqual(self.route("enrich", "budget"), ("open", "enrich budget"))



class TestGmailDisclosure(unittest.TestCase):
    """Allowing Gmail hands the agent profile the whole Google account session (google.com cookies). The consent
    question and docs/PRIVACY.md say so in plain words, say that the guard lets the agents use only
    mail.google.com, say how to take it back, and the answer stays No unless the owner types y."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def text(self, *rel) -> str:
        with open(os.path.join(self.REPO, *rel), encoding="ascii") as fh:
            return fh.read()

    def gmail_block(self) -> str:
        wrapper = self.text("jobhunter")
        start = wrapper.index('      gmail) say "Gmail: ')
        return wrapper[start:wrapper.index(";;", start)]

    def test_the_question_says_what_is_copied_what_the_guard_allows_and_how_to_revoke(self):
        block = " ".join(line.strip()[len('say "'):].rstrip('"').strip()
                         for line in self.gmail_block().replace("gmail) ", "").split("\n"))
        for words in ("your Google account session", "google.com cookies are copied",
                      "other Google services", "The guard lets the agents use only mail.google.com",
                      "blocks every other Google service", "./jobhunter browser forget gmail",
                      "Your devices"):
            self.assertIn(words, block)
        wrapper = self.text("jobhunter")
        intro = wrapper[wrapper.index("consent_intro() {"):wrapper.index("\n}\n", wrapper.index("consent_intro() {"))]
        self.assertIn("Gmail is the exception: its login is your whole Google account session", intro)
        self.assertIn("Every question defaults to No", intro)
        # the disclosure is printed before the question, and only a typed y means Yes
        consent = wrapper[wrapper.index("browser_consent() {"):]
        self.assertLess(consent.index("your Google account session"),
                        consent.index("printf 'Allow the agent to use your %s login? [y/N] '"))
        self.assertIn('      y|Y|yes|YES|Yes) grant="${grant:+$grant,}$site"', consent)

    def test_privacy_doc_states_it_plainly(self):
        doc = " ".join(self.text("docs", "PRIVACY.md").split())
        for words in ("Gmail means your whole Google account session",
                      "allowing Gmail copies your Google account session cookies",
                      "`google.com`, `mail.google.com` and `accounts.google.com`",
                      "The guard therefore lets the agents use only `mail.google.com`",
                      "Gmail's own settings pages are blocked for every agent",
                      "The answer still defaults to No",
                      "**Taking Gmail back:** run `./jobhunter browser forget gmail` (PIN)",
                      "every Google cookie is cleared from the `jobhunter` profile",
                      "https://myaccount.google.com/device-activity",
                      "this signs your Chrome out of Google too"):
            self.assertIn(words, doc)

    def test_the_disclosure_matches_the_import_filter_and_the_guard(self):
        doc = self.text("docs", "PRIVACY.md")
        self.assertEqual(ins.CONSENT_SITES["gmail"][1], ("google.com", "mail.google.com", "accounts.google.com"))
        for dom in ins.CONSENT_SITES["gmail"][1]:
            self.assertIn("`%s`" % dom, doc)
        hosts = json.loads(self.text("openclaw", "guard-hosts.json"))
        self.assertEqual(hosts["platforms"]["gmail"]["hosts"], ["mail.google.com"])
        self.assertEqual(hosts["platforms"]["gmail"]["consent"], "gmail")
        never = [re.compile(p, re.I) for p in hosts["never"]["url_patterns"]]

        def blocked(url):
            return any(p.search(url) for p in never)
        for url in ("https://drive.google.com/drive/my-drive", "https://docs.google.com/document/d/x/edit",
                    "https://myaccount.google.com/security", "https://www.google.com/search?q=x",
                    "https://photos.google.com/", "https://google.com/", "https://pay.google.com/",
                    "https://mail.google.com/mail/u/0/#settings/general"):
            self.assertTrue(blocked(url), url)
        for url in ("https://mail.google.com/mail/u/0/#inbox", "https://mail.google.com/mail/u/0/#sent"):
            self.assertFalse(blocked(url), url)


if __name__ == "__main__":
    unittest.main()
