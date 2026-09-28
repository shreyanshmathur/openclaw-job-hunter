"""sheets_labels.py and sheets/Code.gs must describe the same sheet (design 7.3)."""
from __future__ import annotations

import json
import os
import re
import unittest

import tests  # noqa: F401
from jobhunter import paths
from jobhunter import sheets_labels as L

CODE_GS = os.path.join(paths.REPO, "sheets", "Code.gs")
MANIFEST = os.path.join(paths.REPO, "sheets", "appsscript.json")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def parse_code_gs(src: str) -> dict:
    """{'tabs': [(key, title, kind, cols)], 'groups': {...}, 'outcomes': [...], 'schema_version': n}."""
    outcomes = re.findall(r"'([^']+)'", re.search(r"var OUTCOMES = \[(.*?)\];", src, re.S).group(1))
    thread_outcomes = re.findall(r"'([^']+)'", re.search(r"var THREAD_OUTCOMES = \[(.*?)\];", src, re.S).group(1))
    named = {"OUTCOMES": tuple(outcomes), "THREAD_OUTCOMES": tuple(thread_outcomes)}
    tabs_src = re.search(r"var TABS = \[(.*?)\n\];", src, re.S).group(1)
    starts = [m.start() for m in re.finditer(r"\{ key: '", tabs_src)]
    tabs = []
    for i, st in enumerate(starts):
        chunk = tabs_src[st:starts[i + 1] if i + 1 < len(starts) else len(tabs_src)]
        key, title, kind = re.match(r"\{ key: '(\w+)', title: '([^']+)', kind: '(\w+)'", chunk).groups()
        cols = []
        for m in re.finditer(r"col\('(\w+)', '([^']+)', '(\w+)', (\d+)(, \{(.*?)\})?\)", chunk, re.S):
            opts = m.group(6) or ""
            editable = "editable: true" in opts
            choices = None
            cm = re.search(r"choices: (\[[^\]]*\]|THREAD_OUTCOMES|OUTCOMES)", opts)
            if cm:
                choices = named.get(cm.group(1)) or tuple(re.findall(r"'([^']+)'", cm.group(1)))
            cols.append((m.group(1), m.group(2), m.group(3), editable, choices))
        tabs.append((key, title, kind, cols))
    groups = {}
    gsrc = re.search(r"var STATUS_GROUPS = \{(.*?)\n\};", src, re.S).group(1)
    for m in re.finditer(r"(\w+):\s*\[(.*?)\]", gsrc, re.S):
        groups[m.group(1)] = tuple(re.findall(r"'([^']+)'", m.group(2)))
    schema = int(re.search(r"var SCHEMA_VERSION = (\d+);", src).group(1))
    return {"tabs": tabs, "groups": groups, "outcomes": outcomes, "thread_outcomes": thread_outcomes,
            "schema_version": schema}


class TestCodeGsMatchesLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(CODE_GS)
        cls.gs = parse_code_gs(cls.src)

    def test_tab_order_titles_and_kinds(self):
        self.assertEqual([t[0] for t in self.gs["tabs"]], list(L.TAB_ORDER))
        for key, title, kind, _cols in self.gs["tabs"]:
            self.assertEqual(title, L.TAB_TITLES[key])
            self.assertEqual(kind, "table" if key in L.TABLE_TABS else "render")

    def test_columns_identical(self):
        for key, _title, kind, cols in self.gs["tabs"]:
            if kind != "table":
                continue
            with self.subTest(tab=key):
                self.assertEqual(cols, [tuple(c) for c in L.columns(key)])
                self.assertEqual(cols[-1][0], "id")

    def test_status_groups_identical(self):
        self.assertEqual(self.gs["groups"], dict(L.STATUS_GROUPS))
        seen = [x for g in L.STATUS_GROUPS.values() for x in g]
        self.assertEqual(len(seen), len(set(seen)), "a label is in two color groups")

    def test_outcomes_and_schema(self):
        self.assertEqual(tuple(self.gs["outcomes"]), L.OUTCOME_CHOICES)
        self.assertEqual(tuple(self.gs["thread_outcomes"]), L.THREAD_OUTCOME_CHOICES)

    def test_followups_outcome_has_its_own_list(self):
        # threads.outcome has no 'withdrawn' (a Withdrawn edit would end refused:E_VALIDATION) but has 'referred'
        cols = {key: c for key, _t, _k, cs in self.gs["tabs"] for c in cs if c[0] == "outcome"}
        self.assertEqual(cols["followups"][4], L.THREAD_OUTCOME_CHOICES)
        self.assertEqual(cols["applications"][4], L.OUTCOME_CHOICES)
        self.assertNotIn("Withdrawn", L.THREAD_OUTCOME_CHOICES)
        self.assertIn("Referred", L.THREAD_OUTCOME_CHOICES)
        self.assertNotIn("Referred", L.OUTCOME_CHOICES)
        self.assertEqual(L.editable_columns("followups")["outcome"], L.THREAD_OUTCOME_CHOICES)
        self.assertEqual(L.editable_columns("applications")["outcome"], L.OUTCOME_CHOICES)
        self.assertEqual(self.gs["schema_version"], L.SCHEMA_VERSION)

    def test_every_status_label_has_a_color(self):
        maps = [L.DRAFT_STATUS, L.JOB_STATUS, L.VERDICT, L.ACTION_STATUS_OUTREACH, L.ACTION_STATUS_APPLICATION,
                L.REPLY_SHORT, L.REPLY_DETAIL]
        extra = ["Passed", "Failed QC", "Not run", "Open", "Resolved", "Stopped", "Warning", "No reply", "Bounced",
                 "Invite pending", "Pending", "Sent", "Approved", "Dropped", "Rewritten", "Skipped", "Expired"]
        for label in [v for m in maps for v in m.values()] + extra:
            self.assertIsNotNone(L.status_group(label), label)

    def test_outcome_maps_round_trip(self):
        for label in L.OUTCOME_CHOICES + L.THREAD_OUTCOME_CHOICES:
            self.assertEqual(L.OUTCOME_LABEL[L.OUTCOME_CODE[label]], label)
        for label in L.HUMAN_CALL_CODE:
            self.assertEqual(L.HUMAN_CALL_LABEL[L.HUMAN_CALL_CODE[label]], label)

    def test_code_gs_safety_features_present(self):
        for needle in ("checkSecret_(req.secret)", "LockService.getDocumentLock()", "schema_mismatch",
                       "setWarningOnly(true)", "requireValueInList", "setFrozenRows(1)", "createFilter()",
                       "setLinkUrl(", "addDeveloperMetadata(MD_COL", "function onEdit(e)", "appendRow(",
                       "deleteRows(", "function doPost(e)", "SPARKLINE", "setSpreadsheetTimeZone"):
            self.assertIn(needle, self.src, needle)
        # the secret is compared in constant time and never read from the URL
        self.assertNotIn("e.parameter", self.src)
        self.assertNotIn("HYPERLINK", self.src)

    def test_manifest_least_privilege(self):
        m = json.loads(_read(MANIFEST))
        self.assertEqual(sorted(m["oauthScopes"]), [
            "https://www.googleapis.com/auth/script.container.ui",
            "https://www.googleapis.com/auth/spreadsheets.currentonly"])
        self.assertEqual(m["webapp"], {"executeAs": "USER_DEPLOYING", "access": "ANYONE_ANONYMOUS"})
        self.assertEqual(m["runtimeVersion"], "V8")

    def test_files_are_ascii_without_dashes(self):
        for path in (CODE_GS, MANIFEST, os.path.join(paths.PKG_DIR, "sheets_labels.py")):
            with open(path, "rb") as fh:
                raw = fh.read()
            self.assertTrue(all(b < 128 for b in raw), path)


class TestLabelFunctions(unittest.TestCase):
    def test_person_name_styles(self):
        self.assertEqual(L.person_name("Meera Nair"), "Meera N.")
        self.assertEqual(L.person_name("Alex van Rivera", "Alex"), "Alex R.")
        self.assertEqual(L.person_name("Alex Rivera", style="full"), "Alex Rivera")
        self.assertEqual(L.person_name("Alex Rivera", style="first"), "Alex")
        self.assertEqual(L.person_name("Careers"), "Careers")
        self.assertEqual(L.person_name(None), "")

    def test_prefilter_sentences(self):
        self.assertEqual(L.prefilter_sentence("years_required", 5, None, 2), "Needs 5 or more years; you have 2")
        self.assertEqual(L.prefilter_sentence("years_required", 5, None, None), "Needs 5 or more years")
        self.assertEqual(L.prefilter_sentence("location"), L.PREFILTER_REASON["location"])
        self.assertEqual(L.prefilter_sentence("odd_code"), "Odd code")
        self.assertEqual(L.prefilter_sentence(None, fallback="Given text"), "Given text")

    def test_gates_sentences(self):
        text = L.gates_sentences(json.dumps(["years_gap", {"gate": "must_have_missing", "jd_quote": "CPA license"}]))
        self.assertIn("Years of experience do not match", text)
        self.assertIn('Missing a must-have requirement: "CPA license"', text)
        self.assertEqual(L.gates_sentences("[]"), "")
        self.assertIn("Pay is below", L.gates_sentences({"comp_below_floor": True, "role_closed": False}))

    def test_scopes_and_todo(self):
        self.assertEqual(L.scope_label("site:naukri"), "Site: Naukri")
        self.assertEqual(L.scope_label("global"), "Everything")
        self.assertEqual(L.scope_label("api:greenhouse"), "Job API: Company site (Greenhouse)")
        self.assertIn("breaker reset linkedin", L.todo_sentence("linkedin", "captcha"))
        self.assertEqual(L.todo_sentence("api:remotive", "rate_limited"), L.TODO_AUTO)
        self.assertIn("mail connect", L.todo_sentence("gmail", "smtp_auth"))
        self.assertIn("after the waiting time", L.todo_sentence("linkedin", "captcha"))
        no_wait = L.todo_sentence("gmail", "gmail_security", waiting=False)
        self.assertIn("breaker reset gmail", no_wait)
        self.assertNotIn("waiting time", no_wait)

    def test_every_detect_and_policy_reason_code_has_a_sentence(self):
        # The Alerts tab's "What happened" and `status` read REASON_SENTENCE by the breaker's reason_code; a code
        # that is missing shows as "Gmail security" or "Ats blocked" instead of a sentence.
        from jobhunter import breakers as B
        codes = set(B.POLICIES)
        ddir = os.path.join(os.path.dirname(os.path.abspath(L.__file__)), "detect")

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("reason_code"), str):
                    codes.add(o["reason_code"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        for name in sorted(os.listdir(ddir)):
            if name.endswith(".json"):
                with open(os.path.join(ddir, name), encoding="utf-8") as fh:
                    walk(json.load(fh))
        # codes tripped from code paths outside POLICIES (replies.py, sources/__init__.py)
        codes |= {"bounces_24h", "consecutive_errors", "http_429"}
        self.assertTrue({"ats_blocked", "site_challenge", "gmail_security", "li_challenge"} <= codes)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertIn(code, L.REASON_SENTENCE)
                s = L.reason_sentence(code)
                self.assertEqual(s, L.REASON_SENTENCE[code])
                self.assertNotIn("_", s)
                self.assertTrue(s.isascii())
                self.assertNotIn(code.replace("_", " ").capitalize(), s)

    def test_real_breaker_codes_read_as_sentences(self):
        self.assertEqual(L.reason_sentence("li_security_email"), "A security email from LinkedIn arrived in your inbox")
        s = L.reason_sentence("gmail_security", "Google security email: Critical security alert")
        self.assertTrue(s.startswith("Google asked to verify it is you or sent a security alert. "))
        self.assertNotIn("Gmail security", s)
        self.assertEqual(L.reason_sentence("ats_blocked"),
                         "A job application site blocked the agent or showed a bot check")
        self.assertEqual(L.reason_sentence("http_503"), "The site answered with an error (HTTP 503)")
        self.assertEqual(L.reason_sentence("http_429"), L.REASON_SENTENCE["http_429"])
        # only job APIs store http_999 (sources: RateLimited covers 429 and 999); LinkedIn uses li_http_429
        self.assertEqual(L.reason_sentence("http_999"), "The site refused the request (code 999)")
        self.assertNotIn("LinkedIn", L.reason_sentence("http_999"))
        self.assertEqual(L.reason_sentence("odd_code"), "Odd code")
        self.assertEqual(L.reason_sentence(None), "The agent stopped")

    def test_real_breaker_codes_get_specific_todo(self):
        self.assertIn("mail connect", L.todo_sentence("gmail", "gmail_auth_failed"))
        self.assertIn("browser login", L.todo_sentence("linkedin", "li_logged_out"))
        self.assertIn("7 days", L.todo_sentence("linkedin", "li_restricted"))
        self.assertIn("different", L.reason_sentence("gmail_identity_mismatch"))
        self.assertIn("own account", L.todo_sentence("linkedin", "li_identity_mismatch"))
        self.assertIn("security email", L.todo_sentence("linkedin", "li_security_email"))
        self.assertIn("breaker reset linkedin.invites", L.todo_sentence("linkedin.invites", "li_invite_limit"))
        for code in L.TODO_BY_REASON:
            with self.subTest(code=code):
                for scope in ("site:naukri", "linkedin", "ats", "site:acme"):
                    text = L.todo_sentence(scope, code)
                    self.assertNotIn("{scope}", text)
                    self.assertNotIn("{site}", text)
                    self.assertNotIn("browser login and", text)
                    self.assertTrue(text.isascii())

    def test_pause_of_one_area_names_the_area_to_resume(self):
        # `./jobhunter resume` without an area means all and only clears state/PAUSED, not pause:<area>
        self.assertEqual(L.todo_sentence("pause:gmail", "paused"),
                         "Run ./jobhunter resume gmail when you want the agent to continue there.")
        self.assertIn("./jobhunter resume site:naukri ", L.todo_sentence("pause:site:naukri", "paused"))
        self.assertIn("./jobhunter resume linkedin ", L.todo_sentence("pause:linkedin", None, True))
        # the global pause (state/PAUSED, shown by status) keeps the plain command: its default area is all
        self.assertIn("./jobhunter resume when", L.TODO_BY_REASON["paused"])

    def test_login_advice_names_a_site_the_wrapper_knows(self):
        self.assertIn("./jobhunter browser login linkedin and", L.todo_sentence("linkedin", "li_logged_out"))
        self.assertIn("./jobhunter browser login linkedin and", L.todo_sentence("linkedin.invites", "login_wall"))
        self.assertIn("./jobhunter browser login gmail and", L.todo_sentence("gmail.cold", "logged_out"))
        self.assertIn("./jobhunter browser login naukri and", L.todo_sentence("site:naukri", "login_wall"))
        for scope in ("ats", "site:acme", "global"):
            with self.subTest(scope=scope):
                text = L.todo_sentence(scope, "logged_out")
                self.assertNotIn("browser login", text)
                self.assertIn("breaker reset %s" % scope, text)
        self.assertEqual((L.login_site("site:iimjobs"), L.login_site("linkedin.search"), L.login_site("api:yc")),
                         ("iimjobs", "linkedin", None))

    def test_expired_session_names_the_login_fix(self):
        # gmail_logged_out and site_logged_out (change request item 4): the reason reads as a sentence and the
        # todo names the re-import or login command, the consent command, the check and the reset, with no wait
        self.assertIn("signed out of Gmail", L.reason_sentence("gmail_logged_out"))
        self.assertIn("signed the agent's browser out", L.reason_sentence("site_logged_out"))
        for code in ("gmail_logged_out", "site_logged_out"):
            self.assertNotIn("verify", L.reason_sentence(code).lower())
        for scope, code, site in (("gmail", "gmail_logged_out", "gmail"), ("site:naukri", "site_logged_out", "naukri"),
                                  ("site:yc", "site_logged_out", "yc")):
            for waiting in (True, False):
                with self.subTest(scope=scope, waiting=waiting):
                    text = L.todo_sentence(scope, code, True, waiting=waiting)
                    self.assertIn("./jobhunter browser import ", text)
                    self.assertIn("./jobhunter browser login %s and" % site, text)
                    self.assertIn("./jobhunter browser consent %s " % site, text)
                    self.assertIn("./jobhunter browser check %s and" % site, text)
                    self.assertTrue(text.endswith("./jobhunter breaker reset %s." % scope), text)
                    self.assertNotIn("waiting time", text)
                    self.assertNotIn("{", text)
                    self.assertTrue(text.isascii())
        # a scope the wrapper has no login page for gets the by-hand advice, not a command that would fail
        for scope in ("site:acme", "ats"):
            with self.subTest(scope=scope):
                text = L.todo_sentence(scope, "site_logged_out", waiting=False)
                self.assertEqual(text, L.TODO_LOGIN_OTHER.format(scope=scope))
                self.assertNotIn("browser import", text)
        # the automatic job API scopes still retry by themselves
        self.assertEqual(L.todo_sentence("api:yc", "site_logged_out", True), L.TODO_AUTO)

    def test_every_email_finder_stop_has_a_sentence(self):
        # enrich.budget.trip(conn, scope, "<code>", ...) calls in the email finder (U10, optional)
        edir = os.path.join(os.path.dirname(os.path.abspath(L.__file__)), "enrich")
        if not os.path.isdir(edir):
            self.skipTest("email finder not installed")
        codes = set()
        for name in sorted(os.listdir(edir)):
            if name.endswith(".py"):
                with open(os.path.join(edir, name), encoding="utf-8") as fh:
                    codes |= set(re.findall(r"\btrip\(\s*conn,\s*[^,]+,\s*\"([a-z0-9_]+)\"", fh.read()))
        self.assertIn("auth_failed", codes)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertIn(code, L.REASON_SENTENCE)
        self.assertEqual(L.todo_sentence("enrich:tomba", "auth_failed"), L.TODO_ENRICH_KEY.format(provider="tomba"))

    def test_login_sites_match_the_wrapper(self):
        # `./jobhunter browser login <site>` opens a login page for a site named in open_login_page, or for any
        # consent site through `install login-probe --site <site> --url`
        with open(os.path.join(os.path.dirname(os.path.abspath(L.__file__)), "..", "..", "jobhunter"),
                  encoding="utf-8") as fh:
            wrapper = fh.read()
        start = "open_login_page() {" if "open_login_page() {" in wrapper else "cmd_browser() {"
        body = wrapper[wrapper.index(start):]
        body = body[:body.index("\n}\n")]
        from jobhunter import identity
        try:
            from jobhunter import install
        except ImportError:
            install = None
        for site in L.BROWSER_LOGIN_SITES:
            with self.subTest(site=site):
                if re.search(r"(^|[\s|])%s([|)])" % site, body):
                    continue
                self.assertIn("login-probe --site", body)
                self.assertIn(site, identity.CONSENT_SITES)
                if install is not None and hasattr(install, "CONSENT_SITES"):
                    self.assertIn(site, install.CONSENT_SITES)
        # every consent site has a login command, and a revoked consent points at the consent command
        self.assertEqual(set(L.BROWSER_LOGIN_SITES), set(identity.CONSENT_SITES))
        self.assertIn("./jobhunter browser consent naukri", L.todo_sentence("site:naukri", "consent_revoked",
                                                                            waiting=False))
        self.assertIn("./jobhunter browser consent gmail", L.todo_sentence("gmail", "consent_revoked"))

    def test_scope_labels_match_the_breaker_area_labels(self):
        # one breaker, one name: the chat alert and `breaker status` use breakers.area_label
        from jobhunter import breakers as B
        for scope, label in B.AREA_LABEL.items():
            with self.subTest(scope=scope):
                self.assertEqual(L.scope_label(scope), label)
        self.assertEqual(L.scope_label("ats"), "Job forms")

    def test_cooldown_until(self):
        t = "2026-10-01T10:30:00Z"
        self.assertIsNone(L.cooldown_until(t, t, True))                        # reset only, no cooldown
        self.assertIsNone(L.cooldown_until("2026-10-01T10:00:00Z", t, True))
        self.assertIsNone(L.cooldown_until(None, t, True))
        self.assertEqual(L.cooldown_until("2026-10-02T10:30:00Z", t, True), "2026-10-02T10:30:00Z")
        self.assertEqual(L.cooldown_until(t, t, False), t)                      # auto: the time it retries

    def test_source_labels(self):
        self.assertEqual(L.source_label("greenhouse"), "Company site (Greenhouse)")
        self.assertEqual(L.source_label("linkedin_post"), "LinkedIn post")
        self.assertEqual(L.source_label("naukri"), "Naukri")
        self.assertEqual(L.source_label("new_board"), "New Board")

    def test_every_job_api_scope_has_a_formatted_name(self):
        from jobhunter import sources
        self.assertEqual(L.API_SOURCE_IDS, {sp.id: sp.job_source for sp in sources.SPECS if sp.id != sp.job_source})
        for sp in sources.SPECS:
            with self.subTest(source=sp.id):
                label = L.scope_label("api:" + sp.id)
                self.assertEqual(label, "Job API: " + L.source_label(sp.job_source))
                self.assertTrue(sp.job_source in L.BOARD_NAMES or sp.job_source in L.ATS_NAMES, sp.job_source)
        self.assertEqual(L.scope_label("api:hn_whoishiring"), "Job API: Hacker News jobs")
        self.assertEqual(L.scope_label("api:serpapi_google_jobs"), "Job API: Google Jobs (SerpApi)")
        self.assertEqual(L.scope_label("api:weworkremotely"), "Job API: We Work Remotely")
        self.assertEqual(L.scope_label("api:workingnomads"), "Job API: Working Nomads")
        self.assertEqual(L.source_label("weworkremotely"), "We Work Remotely")

    def test_safe_cell_and_clip(self):
        self.assertEqual(L.safe_cell("=1+1"), "'=1+1")
        self.assertEqual(L.safe_cell("@x"), "'@x")
        self.assertEqual(L.safe_cell("ok"), "ok")
        self.assertEqual(L.clip("abcdef", 5), "ab...")
        self.assertEqual(L.first_line("\n\n  Hello there\nsecond"), "Hello there")

    def test_all_label_text_is_ascii_and_dash_free(self):
        values = []
        for name in dir(L):
            v = getattr(L, name)
            if isinstance(v, dict):
                values += [x for x in v.values() if isinstance(x, str)]
        for s in values:
            self.assertTrue(s.isascii(), s)
            self.assertNotIn("\x20-\x20", s)


if __name__ == "__main__":
    unittest.main()
