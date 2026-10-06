"""status, inbox, the dashboard and settings renders, and export (design 3.4, 7.3)."""
from __future__ import annotations

import csv
import io
import json
import os
import types
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import cli, export, paths
from jobhunter import status as S
from jobhunter.commands import report
from tests.fakes.u5 import config, seed
from tests.fakes.u5 import consent as fake_consent
from tests.fakes.u5 import enrich as fake_enrich
from tests.helpers import HomeTestCase

CFG = config()
STYLES = ("section", "good", "wait", "bad", "muted", "info")

# What the owner's own agent (`main`) looks like to jh.py (CLI-ROUTE-DESIGN 4.2, 5.4). The guard gives `main` no
# identity (resolve_exec_env returns nothing for non-jobhunter agents and it never gets an --agent-proof), so jh.py
# sees harness markers only. tests.helpers.agent_env/agent_argv mint proofs for jobhunter agents and do not apply.
MAIN_EXEC_ENVS = (
    {"OPENCLAW_SHELL": "1"},                          # OpenClaw's exec tool
    {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"},   # an id the agent set itself changes nothing
)
MAIN_CLI_ENV = {"OPENCLAW_MCP_TOKEN": "test-token"}   # Claude Code's own shell under an OpenClaw claude-cli run
REFUSED = ("E_GUARD_MISSING", "E_CALLER_NOT_ALLOWED")


def _run(argv, env, modules):
    out = io.StringIO()
    rc = cli.main(list(argv), env=dict(env), stdin=io.StringIO(""), stdout=out, modules=modules)
    return rc, out.getvalue()


class TestStatus(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.s = seed(self.conn, self.clock)

    def test_build_status(self):
        d = S.build_status(self.conn, CFG)
        self.assertEqual(d["state"], "Stopped: LinkedIn")
        self.assertEqual(d["install_id"], d["home_install_id"])
        self.assertEqual(d["db_path"], paths.db_path())
        self.assertEqual([b["scope"] for b in d["breakers"]], ["linkedin"])
        self.assertEqual(d["queues"]["approvals_waiting"], 1)
        self.assertEqual(d["queues"]["human_tasks"], 1)
        self.assertEqual(d["notifications"]["undelivered_high"], 1)
        self.assertEqual(d["last_cycles"]["scout"]["status"], "ok")
        self.assertFalse(d["guard"]["present"])
        self.assertFalse(d["sheet"]["connected"])
        self.assertEqual(d["approval_mode"], "human")
        usage = {(u["platform"], u["kind"]): u for u in d["usage"]}
        self.assertEqual(usage[("gmail", "cold_email")]["used"], 1)
        self.assertEqual(usage[("boards", "application")]["used"], 1)
        text = S.render_status_text(d, S.tzinfo(CFG))
        for needle in ("Job Hunter: Stopped: LinkedIn", "Safety plugin: not running", "What to do:",
                       "breaker reset linkedin", "1 approvals", "Google Sheet: not connected",
                       "1 important messages could not be delivered"):
            self.assertIn(needle, text)
        self.assertTrue(text.isascii())

    def test_paused_and_guard_heartbeat(self):
        open(paths.paused_file(), "w").close()
        hb = {"install_id": paths.home()["install_id"], "version": "2.0.0", "beat_at": self.clock.ago(minutes=3)}
        with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
            json.dump(hb, fh)
        d = S.build_status(self.conn, CFG)
        self.assertEqual(d["state"], "Paused")
        self.assertTrue(d["guard"]["fresh"])
        hb["beat_at"] = self.clock.ago(minutes=11)
        with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
            json.dump(hb, fh)
        self.assertFalse(S.guard_heartbeat()["fresh"])

    def test_inbox(self):
        d = S.build_inbox(self.conn, CFG)
        self.assertEqual(d["approvals"][0]["code"], "A7K2")
        self.assertEqual(d["approvals"][0]["to"], "Sam L. (Data Lead)")
        self.assertEqual(d["questions"][0]["question"], "Q3: notice period?")
        self.assertEqual(len(d["undelivered_high"]), 1)
        text = S.render_inbox_text(d, S.tzinfo(CFG))
        self.assertIn("A7K2  Cold email to Sam L. (Data Lead), Tidewater Labs", text)
        self.assertIn("/jh approve CODE", text)
        self.assertIn("Stopped: LinkedIn", text)

    def test_dashboard_payload(self):
        d = S.dashboard(self.conn, CFG)
        self.assertEqual(d["undelivered_high"], 1)
        self.assertEqual(d["activity"]["columns"], ["Today", "7 days", "30 days", "All time"])
        acts = {r[0]: r[1:] for r in d["activity"]["rows"]}
        self.assertEqual(acts["Applications"], [1, 1, 1, 1])
        self.assertEqual(acts["Jobs found"][3], 6)
        self.assertEqual(acts["Jobs found"][0], 5)   # one job was found 40 days ago
        self.assertEqual(dict(d["funnel"])["Applied"], 1)
        self.assertEqual([s["area"] for s in d["safety"]], ["LinkedIn"])
        self.assertIn("Approvals waiting", [a["item"] for a in d["attention"]])
        self.assertEqual(len(d["trend"]["series"][0]["values"]), 14)
        self.assertEqual(d["trend"]["series"][0]["values"][-1], 5)
        reasons = dict(d["skip_reasons"])
        self.assertEqual(reasons.get(S.L.PREFILTER_REASON["years_required"]), 1)

    def test_settings_rows(self):
        rows = S.settings_rows(self.conn, CFG)
        # [Setting, Value, Used today, What it means] plus an optional style for Code.gs
        self.assertTrue(all(len(r) in (4, 5) for r in rows))
        self.assertTrue(all(r[4] in STYLES for r in rows if len(r) == 5))
        self.assertTrue(all(isinstance(x, str) and x.isascii() for r in rows for x in r))
        names = [r[0] for r in rows]
        for needle in ("Approval mode", "Email route", "LinkedIn actions", "Safety plugin", "Time zone"):
            self.assertIn(needle, names)
        self.assertIn("Gmail: cold emails", names)

    def test_limits_today_use_each_kinds_own_day_cap(self):
        try:
            from jobhunter import ceilings
        except ImportError as exc:
            self.skipTest("ceilings missing: %s" % exc)
        own = {("gmail", "cold_email"): "cold emails", ("gmail", "followup_email"): "follow-ups",
               ("linkedin", "li_invite"): "li_invite", ("linkedin", "li_message"): "li_message",
               ("boards", "application"): "all applications"}
        usage = {(u["platform"], u["kind"]): u for u in S.limits_today(self.conn)}
        for (platform, kind), item in own.items():
            with self.subTest(kind=kind):
                rows = ceilings.budget(self.conn, platform, kind)["rows"]
                want = [r for r in rows if r["item"] == item and r["window"] in ("day", "utc_day")][0]
                self.assertEqual((usage[(platform, kind)]["used"], usage[(platform, kind)]["limit"]),
                                 (want["used"], want["limit"]))
        # not the shared platform totals, which come first in the budget rows
        gm = [r for r in ceilings.budget(self.conn, "gmail", "cold_email")["rows"] if r["item"] == "gmail total"][0]
        self.assertNotEqual(usage[("gmail", "cold_email")]["limit"], gm["limit"])

    def test_pick_day_row(self):
        rows = [{"item": "gmail total", "window": "day", "used": 3, "limit": 20, "remaining": 17},
                {"item": "gmail sends", "window": "hour", "used": 1, "limit": 3, "remaining": 2},
                {"item": "cold emails", "window": "day", "used": 2, "limit": 5, "remaining": 3},
                {"item": "cold emails", "window": "week", "used": 2, "limit": 60, "remaining": 58}]
        self.assertEqual(S.pick_day_row(rows, "cold_email")["item"], "cold emails")
        li = [{"item": "LinkedIn writes", "window": "day", "used": 1, "limit": 24},
              {"item": "LinkedIn actions", "window": "day", "used": 5, "limit": 100},
              {"item": "li_invite", "window": "cycle", "used": 0, "limit": 2},
              {"item": "li_invite", "window": "day", "used": 1, "limit": 3}]
        self.assertEqual(S.pick_day_row(li, "li_invite")["limit"], 3)
        # no own row: the day row with the least remaining
        self.assertEqual(S.pick_day_row(li[:3], "li_other")["item"], "LinkedIn writes")
        self.assertEqual(S.pick_day_row(li, "li_other")["item"], "li_invite")
        apps = [{"item": "all applications", "window": "day", "used": 1, "limit": 24},
                {"item": "unknown site boards", "window": "day", "used": 0, "limit": 0}]
        self.assertEqual(S.pick_day_row(apps, "application")["item"], "all applications")
        self.assertEqual(S.pick_day_row([{"item": "x", "window": "week"}], "cold_email")["item"], "x")
        self.assertIsNone(S.pick_day_row([], "cold_email"))
        with mock.patch.object(S, "_optional", return_value=mock.Mock(budget=lambda c, p, k: {"rows": rows})):
            usage = {(u["platform"], u["kind"]): u for u in S.limits_today(self.conn)}
        self.assertEqual((usage[("gmail", "cold_email")]["used"], usage[("gmail", "cold_email")]["limit"]), (2, 5))

    def test_control_skill_uses_the_installer_placeholders(self):
        path = os.path.join(paths.REPO, "shared-skills", "jobhunter-control", "SKILL.template.md")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("{PY}", src)
        self.assertNotIn("{REPO}", src)
        try:
            from jobhunter import install
        except ImportError as exc:
            self.skipTest("installer missing: %s" % exc)
        text = install.substitute(src, {"__REPO__": "/opt/example-repo", "__PY__": "/usr/bin/python3"})
        self.assertEqual(install.PLACEHOLDER_RE.findall(text), [])
        for cmd in ("status", "inbox"):
            self.assertIn("      /usr/bin/python3 /opt/example-repo/scripts/jh.py %s --human\n" % cmd, text)

    def test_count_activity_uses_local_days(self):
        tz = S.tzinfo(config(timezone="America/New_York"))
        start, end = S.day_bounds("2026-09-27", tz)
        self.assertEqual((start, end), ("2026-09-27T04:00:00Z", "2026-09-28T04:00:00Z"))
        self.assertEqual(S.local_date("2026-09-27T03:59:59Z", tz), "2026-09-26")
        self.assertEqual(S.fmt_local("2026-09-27T05:00:00Z", S.tzinfo(config(timezone="Asia/Kolkata"))),
                         "27 Sep 2026 10:30")
        self.assertEqual(S.parse_any("2026-09-27T05:18:03.000Z").isoformat(), "2026-09-27T05:18:03+00:00")

    def test_cli_status_and_inbox_are_read_only_and_public(self):
        with mock.patch.object(S, "load_config", return_value=CFG):
            for argv in (["status"], ["inbox"], ["--human", "status"], ["--human", "inbox"]):
                out = io.StringIO()
                rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out, modules=[report])
                self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("Waiting for your approval", out.getvalue())
            # a non-jobhunter agent (for example `main`) may run the public read-only commands, and only those
            for env in MAIN_EXEC_ENVS:
                self._assert_main_is_public_readonly(env)

    def test_main_on_the_claude_subscription_route_is_public_readonly(self):
        """`main` on claude-cli reaches jh.py through Claude Code's own shell: only OPENCLAW_MCP_TOKEN marks it
        (CLI-ROUTE-DESIGN 4.2, 5.4). It is an unproven agent, never `system`."""
        from jobhunter import auth
        if not hasattr(auth, "harness_markers"):
            self.skipTest("auth.harness_markers is not there yet (claude-cli route core, U1)")
        self._assert_main_is_public_readonly(dict(MAIN_CLI_ENV))

    def _assert_main_is_public_readonly(self, env):
        from jobhunter.commands import drafts as drafts_cmd
        from jobhunter.commands import limits as limits_cmd
        modules = [report, drafts_cmd, limits_cmd]
        with mock.patch.object(S, "load_config", return_value=CFG):
            for argv in (["status"], ["inbox"], ["--human", "status"]):
                rc, out = _run(argv, env, modules)
                self.assertEqual(rc, 0, (env, argv, out))
            # the control skill tells `main` that jh.py refuses everything else; it does, before any handler runs
            for argv in (["export"], ["pause"], ["approve", "A7K2"], ["skip", "A7K2"]):
                rc, out = _run(argv, env, modules)
                self.assertEqual((rc, json.loads(out)["code"] in REFUSED), (11, True), (env, argv, out))
        self.assertFalse(os.path.exists(paths.paused_file()))
        self.assertFalse(os.path.isdir(paths.exports_dir()) and os.listdir(paths.exports_dir()))

    def test_control_skill_says_what_the_plugin_and_jh_refuse(self):
        path = os.path.join(paths.REPO, "shared-skills", "jobhunter-control", "SKILL.template.md")
        with open(path, "rb") as fh:
            flat = " ".join(fh.read().decode("ascii").split())   # the dash rules run in test_repo_layout
        for needle in ("The safety plugin and jh.py refuse those commands for you",
                       "Never start, edit or message the Job Hunter agents",
                       "no cron, sessions, subagent or agent commands or tools for them",
                       "no openclaw cron, agent or sessions command that names them",
                       "--agent-proof, --grant, --pin-stdin and --home are refused",
                       "A refused call is final"):
            self.assertIn(needle, flat)
        self.assertNotIn("blocks those commands", flat)

    def test_export_writes_one_csv_per_tab(self):
        out = export.export_all(self.conn, None, CFG)
        self.assertEqual(out["out_dir"], paths.exports_dir())
        self.assertEqual(out["files"]["jobs"]["rows"], 3)
        with open(out["files"]["jobs"]["path"], newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], [c[1] for c in S.L.columns("jobs")])
        body = {r[-1]: r for r in rows[1:]}
        self.assertEqual(body["JAAAAAA2"][6], "https://job-boards.greenhouse.io/example/jobs/1")
        self.assertEqual(body["JAAAAAA2"][0], "27 Sep 2026 05:00")
        self.assertEqual(body["JAAAAAA6"][12], "27 Sep 2026")
        self.assertEqual(oct(os.stat(out["files"]["jobs"]["path"]).st_mode & 0o777), "0o600")
        for key in ("approvals", "skipped", "applications", "outreach", "followups", "qc", "daily", "alerts",
                    "settings", "dashboard"):
            self.assertTrue(os.path.exists(out["files"][key]["path"]), key)

    def test_export_neutralises_formulas(self):
        self.conn.execute("UPDATE jobs SET title = '=HYPERLINK(\"x\")', updated_at = ? WHERE id = ?",
                          (self.clock.ago(seconds=3), self.s.j_good))
        out = export.export_all(self.conn, os.path.join(self.home.dir, "exports", "custom"), CFG)
        with open(out["files"]["jobs"]["path"], newline="") as fh:
            roles = [r[2] for r in csv.reader(fh)]
        self.assertIn("'=HYPERLINK(\"x\")", roles)


def _no_consent_file():
    if os.path.lexists(paths.consent_file()):
        os.remove(paths.consent_file())


def _no_dashes(testcase, rows):
    for r in rows:
        for x in r:
            testcase.assertTrue(isinstance(x, str) and x.isascii(), r)
            testcase.assertNotIn(chr(0x2013), x)
            testcase.assertNotIn(chr(0x2014), x)


class TestBrowserConsentView(HomeTestCase):
    """Per-site browser consent (change request: Chrome logins with consent), read through U1's consent API
    and never written; while that API is not installed private/consent.json is read directly."""
    browser_consent = False     # a new install: no private/consent.json

    def setUp(self):
        super().setUp()
        _no_consent_file()
        seed(self.conn, self.clock)

    def test_fake_api_states_default_no_and_read_only(self):
        api = fake_consent.FakeConsentAPI(fake_consent.sample_rows())
        v = S.browser_consent(self.conn, api=api)
        self.assertEqual([x["site"] for x in v["sites"]], list(fake_consent.SITES))
        states = {x["site"]: x["state"] for x in v["sites"]}
        self.assertEqual(states["gmail"], "granted")
        self.assertEqual(states["linkedin"], "revoked")
        self.assertEqual(states["naukri"], "needs_you")        # the naukri.com row: its login expired
        for site in ("indeed", "glassdoor", "foundit", "instahyre", "wellfound"):
            self.assertEqual(states[site], "not_granted")     # never asked: No
        self.assertEqual(v["chrome_profile"], "Profile 1")
        self.assertEqual(api.calls, ["summary"])
        self.assertEqual(api.writes, [])
        self.assertEqual(S.consent_line(v), 'Browser sites allowed: Gmail (Chrome profile "Profile 1"); needs you: '
                                            'Naukri (./jobhunter browser consent); taken back: LinkedIn; every '
                                            'other site is off')

    def test_installed_api_in_status_text_and_sheet(self):
        api = fake_consent.FakeConsentAPI(fake_consent.sample_rows())
        name = fake_consent.install(api)
        self.addCleanup(fake_consent.uninstall)
        web = config(gmail__route="web_ui")
        with mock.patch.object(S, "CONSENT_APIS", ((name, ("summary",)),)):
            d = S.build_status(self.conn, web)
            rows = S.settings_rows(self.conn, web)
        self.assertEqual(api.writes, [])
        self.assertEqual(d["browser_sites"]["sites"][0]["state"], "granted")
        self.assertIn("Browser sites allowed: Gmail", S.render_status_text(d, S.tzinfo(web)))
        _no_dashes(self, rows)
        by = {r[0]: r for r in rows}
        self.assertEqual(by["Browser sites (your Chrome logins)"][4], "section")
        self.assertEqual(by["Chrome profile"][1], "Profile 1")
        self.assertEqual((by["Gmail"][1], by["Gmail"][4]), ("Allowed", "good"))
        self.assertIn("Allowed on 27 Sep 2026 09:00.", by["Gmail"][3])
        self.assertIn("./jobhunter browser forget gmail", by["Gmail"][3])
        self.assertEqual((by["LinkedIn"][1], by["LinkedIn"][4]), ("Revoked", "muted"))
        self.assertIn("You took this back on 28 Sep 2026 08:00.", by["LinkedIn"][3])
        self.assertEqual((by["Naukri"][1], by["Naukri"][4]), ("Needs you", "wait"))
        self.assertEqual((by["Indeed"][1], by["Indeed"][4]), ("Not allowed", "muted"))
        self.assertEqual((by["Email connected"][1], by["Email connected"][4]), ("Gmail allowed", "good"))
        # the consent block comes after the daily limits and before the email finder
        names = [r[0] for r in rows]
        self.assertLess(names.index("Daily limits (last 24 hours)"), names.index("Browser sites (your Chrome logins)"))

    def test_identity_style_api_is_read_without_calling_the_gate(self):
        api = fake_consent.IdentityLikeAPI(fake_consent.sample_rows())
        v = S.browser_consent(self.conn, api=api)
        self.assertEqual(api.writes, [])
        self.assertEqual({x["site"]: x["state"] for x in v["sites"]}["gmail"], "granted")

    def test_other_api_shapes(self):
        v = S.browser_consent(self.conn, api=fake_consent.LoadOnlyAPI(
            {"gmail": True, "linkedin.com": "declined", "chrome_profile": "Profile 3"}))
        states = {x["site"]: x["state"] for x in v["sites"]}
        self.assertEqual((states["gmail"], states["linkedin"], v["chrome_profile"]),
                         ("granted", "not_granted", "Profile 3"))
        v = S.browser_consent(self.conn, api=types.SimpleNamespace(sites=lambda: ["mail.google.com", "indeed"]))
        self.assertEqual([x["label"] for x in v["sites"] if x["state"] == "granted"], ["Gmail", "Indeed"])
        v = S.browser_consent(self.conn, api=types.SimpleNamespace(summary=lambda: 42))
        self.assertTrue(v["error"])

    def test_unreadable_api_is_unknown_never_allowed(self):
        api = fake_consent.FakeConsentAPI(fake_consent.sample_rows(), fail=True)
        v = S.browser_consent(self.conn, api=api)
        self.assertTrue(v["error"])
        self.assertIn("could not read which sites you allowed", S.consent_line(v))
        rows = S.settings_rows(self.conn, config(gmail__route="web_ui"), consent=v, finder=None)
        by = {r[0]: r for r in rows}
        self.assertEqual((by["Browser sites"][1], by["Browser sites"][4]), ("Unknown", "wait"))
        self.assertEqual(by["Email connected"][1], "Gmail unknown")
        self.assertNotIn("Allowed", [r[1] for r in rows])

    def test_consent_file_is_read_when_the_api_is_missing(self):
        fake_consent.write_consent_file(paths.consent_file())
        with open(paths.consent_file(), "rb") as fh:
            before = fh.read()
        web = config(gmail__route="web_ui")
        with mock.patch.object(S, "CONSENT_APIS", ()):
            v = S.browser_consent(self.conn)
            rows = S.settings_rows(self.conn, web, finder=None)
        with open(paths.consent_file(), "rb") as fh:
            self.assertEqual(fh.read(), before)                             # read only
        self.assertEqual({x["site"]: x["state"] for x in v["sites"]},
                         {"gmail": "granted", "linkedin": "not_granted", "naukri": "revoked", "indeed": "not_granted",
                          "glassdoor": "not_granted", "foundit": "not_granted", "instahyre": "granted",
                          "wellfound": "not_granted", "yc": "granted"})
        self.assertEqual(v["sites"][-1]["label"], "YC Work at a Startup")   # other allowed sites follow the list
        self.assertTrue(v["several_profiles"])                              # Personal and Side projects
        self.assertIsNone(v["chrome_profile"])
        _no_dashes(self, rows)
        by = {r[0]: r for r in rows}
        self.assertEqual(by["Chrome profile"][1], "Several (see each site)")
        self.assertIn('Copied from Chrome profile "Personal".', by["Gmail"][3])   # display names, not folders
        self.assertIn('Copied from Chrome profile "Side projects".', by["YC Work at a Startup"][3])
        self.assertIn("You logged in by hand", by["Instahyre"][3])
        self.assertEqual(by["LinkedIn"][1:2], ["Not allowed"])
        self.assertIn("You took this back on 28 Sep 2026 01:30.", by["Naukri"][3])
        self.assertEqual(S.consent_line(v), "Browser sites allowed: Gmail, Instahyre, YC Work at a Startup (from "
                                            "several Chrome profiles); taken back: Naukri; every other site is off")
        # one Chrome profile: named once at the top, not on every site
        doc = fake_consent.file_doc()
        doc["sites"]["yc"].update(chrome_profile="Profile 1", chrome_profile_name="Personal")
        fake_consent.write_consent_file(paths.consent_file(), doc)
        with mock.patch.object(S, "CONSENT_APIS", ()):
            v = S.browser_consent(self.conn)
        by = {r[0]: r for r in S._consent_rows(v, S.tzinfo(web))}
        self.assertEqual((v["chrome_profile"], v["several_profiles"]), ("Personal", False))
        self.assertEqual(by["Chrome profile"][1], "Personal")
        self.assertNotIn("Copied from", by["Gmail"][3])
        self.assertIn('(Chrome profile "Personal")', S.consent_line(v))

    def test_u1_consent_api_matches_the_gate(self):
        from jobhunter import identity
        if not hasattr(identity, "consent_summary"):
            self.skipTest("U1 consent API not installed")
        fake_consent.write_consent_file(paths.consent_file())
        with mock.patch.object(identity, "consent_summary", wraps=identity.consent_summary) as spy:
            v = S.browser_consent(self.conn)
            rows = S.settings_rows(self.conn, config(gmail__route="web_ui"), finder=None)
        self.assertTrue(spy.called)                                         # U1's API, not the direct read
        states = {x["site"]: x["state"] for x in v["sites"]}
        for site in identity.SITES:
            with self.subTest(site=site):
                self.assertEqual(states.get(site) == "granted", identity.consent_active(site))
        self.assertEqual((states["naukri"], states["linkedin"]), ("revoked", "not_granted"))
        by = {r[0]: r for r in rows}
        self.assertEqual((by["Gmail"][1], by["Email connected"][1]), ("Allowed", "Gmail allowed"))
        self.assertEqual(by["Naukri"][1], "Revoked")
        self.assertIn('Copied from Chrome profile "Profile 1".', by["Gmail"][3])    # two profiles: named per site
        _no_dashes(self, rows)
        # one profile: U1 names it once (display name) and the sites do not repeat its folder name
        doc = fake_consent.file_doc()
        doc["sites"]["yc"].update(chrome_profile="Profile 1", chrome_profile_name="Personal")
        fake_consent.write_consent_file(paths.consent_file(), doc)
        v = S.browser_consent(self.conn)
        by = {r[0]: r for r in S._consent_rows(v, S.tzinfo(CFG))}
        self.assertEqual(by["Chrome profile"][1], "Personal")
        self.assertNotIn("Copied from", by["Gmail"][3] + by["YC Work at a Startup"][3])

    def test_consent_file_fails_closed(self):
        path = paths.consent_file()
        with mock.patch.object(S, "CONSENT_APIS", ()):
            v = S.browser_consent(self.conn)            # no file yet: nothing is allowed
            self.assertFalse(v.get("error"))
            self.assertEqual({x["state"] for x in v["sites"]}, {"not_granted"})
            self.assertEqual(S.consent_line(v), "Browser sites allowed: none yet (./jobhunter browser consent)")
            for kind in ("group_writable", "bad_json", "no_sites", "symlink"):
                with self.subTest(kind=kind):
                    if os.path.lexists(path):
                        os.remove(path)
                    if kind == "group_writable":
                        fake_consent.write_consent_file(path, mode=0o620)
                    elif kind == "bad_json":
                        fake_consent.write_consent_file(path, raw="{not json")
                    elif kind == "no_sites":
                        fake_consent.write_consent_file(path, doc={"version": 1})
                    else:
                        os.symlink(fake_consent.write_consent_file(path + ".real"), path)
                    v = S.browser_consent(self.conn)
                    self.assertTrue(v["error"])
                    rows = S._consent_rows(v, S.tzinfo(CFG))
                    self.assertEqual(rows[-1][1], "Unknown")
                    self.assertNotIn("Allowed", [r[1] for r in rows])

    def test_real_modules_show_nothing_allowed_without_a_consent_file(self):
        v = S.browser_consent(self.conn)
        self.assertIsNotNone(v)
        self.assertFalse(any(x["state"] == "granted" for x in v.get("sites", [])))


class TestEmailFinderView(HomeTestCase):
    """The optional email finder's budget (jobhunter.enrich.budget.summary, U10) in status and the Sheet."""

    def setUp(self):
        super().setUp()
        seed(self.conn, self.clock)

    def test_summary_line_and_sheet_rows(self):
        v = S.email_finder(self.conn, CFG, summary_fn=lambda conn: fake_enrich.sample_summary())
        self.assertTrue(v["enabled"])
        # credits of services with a key that are turned on: Hunter 32.5 + Tomba 20 + GetProspect 0 + ZeroBounce 90
        self.assertEqual((v["credits_left"], v["budget_31d"], v["spent_31d"]), (142.5, 202.0, 59.5))
        self.assertEqual(v["stops"], ["Tomba"])
        self.assertEqual(S.email_finder_line(v), "Email finder: on; 0 addresses found in 31 days; 142.5 of 202 free "
                                                 "credits left (31 days); stopped: Tomba")
        rows = S.settings_rows(self.conn, CFG, finder=v, consent=None)
        _no_dashes(self, rows)
        by = {r[0]: r for r in rows}
        self.assertNotIn("Browser sites (your Chrome logins)", by)
        self.assertEqual(by["Email finder (optional)"][4], "section")
        self.assertEqual([by["Email finder"][i] for i in (1, 2, 4)], ["On", "3 lookups", "good"])
        self.assertIn("2 found addresses are not emailed yet", by["Addresses found (31 days)"][3])
        hunter = by["Email finder: Hunter"]
        self.assertEqual(hunter[1:3] + hunter[4:], ["32.5 of 45 credits left (31 days)", "1 of 3 credits", "good"])
        self.assertEqual(by["Email finder: Tomba"][4], "bad")
        self.assertIn("./jobhunter enrich connect tomba", by["Email finder: Tomba"][3])
        self.assertEqual(by["Email finder: GetProspect"][4], "wait")
        self.assertIn("skipped until 20 Oct 2026 00:00", by["Email finder: GetProspect"][3])
        self.assertIn("No key yet: ./jobhunter enrich connect prospeo.", by["Email finder: Prospeo"][3])
        self.assertNotIn("Email finder: Anymail Finder", by)    # off and never used
        self.assertEqual(by["Email finder: ZeroBounce"][1], "90 of 90 credits left (31 days)")

    def test_paused_and_stopped(self):
        raw = dict(fake_enrich.sample_summary(), paused=True)
        v = S.email_finder(self.conn, CFG, summary_fn=lambda conn: raw)
        self.assertTrue(S.email_finder_line(v).startswith("Email finder: paused by you;"))
        self.assertEqual(S._finder_rows(v, S.tzinfo(CFG))[1][4], "wait")
        raw = dict(fake_enrich.sample_summary(), breaker="provider_warning")
        v = S.email_finder(self.conn, CFG, summary_fn=lambda conn: raw)
        self.assertEqual(v["stops"], ["Email finder", "Tomba"])
        row = S._finder_rows(v, S.tzinfo(CFG))[1]
        self.assertEqual((row[1], row[4]), ("Stopped", "bad"))
        self.assertIn("breaker reset enrich", row[3])

    def test_off_and_unused_does_not_ask_the_key_store(self):
        fn = mock.Mock(side_effect=AssertionError("summary must not run"))
        with mock.patch.object(S, "_import_attr", return_value=fn):
            v = S.email_finder(self.conn, CFG)
        fn.assert_not_called()
        self.assertFalse(S.finder_active(v))
        self.assertEqual(S.email_finder_line(v), "Email finder: off (optional, see docs/EMAIL-FINDER.md)")
        self.assertEqual([r[:2] for r in S._finder_rows(v, S.tzinfo(CFG))],
                         [["Email finder (optional)", ""], ["Email finder", "Off"]])
        fn = mock.Mock(return_value=fake_enrich.sample_summary())
        with mock.patch.object(S, "_import_attr", return_value=fn):
            v = S.email_finder(self.conn, config(enrich__enabled=True))
        fn.assert_called_once_with(self.conn)
        self.assertTrue(S.finder_active(v))

    def test_missing_or_failing_budget_module_shows_nothing(self):
        with mock.patch.object(S, "_import_attr", return_value=None):
            self.assertIsNone(S.email_finder(self.conn, CFG))
            d = S.build_status(self.conn, CFG)
            rows = S.settings_rows(self.conn, CFG)
        self.assertNotIn("Email finder", S.render_status_text(d))
        self.assertNotIn("Email finder (optional)", [r[0] for r in rows])
        self.assertIsNone(S.email_finder(self.conn, CFG, summary_fn=mock.Mock(side_effect=RuntimeError("down"))))
        self.assertIsNone(S.email_finder(self.conn, CFG, summary_fn=lambda conn: "not a dict"))

    def test_real_budget_summary_when_importable(self):
        try:
            from jobhunter.enrich import budget, keystore  # noqa: F401
        except Exception as exc:  # the email finder is optional
            self.skipTest("email finder not installed: %s" % exc)
        with mock.patch.object(keystore, "status", return_value={"key": False, "backend": "test", "error": None}):
            v = S.email_finder(self.conn, config(enrich__enabled=True))
            d = S.build_status(self.conn, config(enrich__enabled=True))
        self.assertIsNotNone(v)
        self.assertTrue(v["providers"])
        self.assertTrue(all(p["name"] == S.L.enrich_provider_label(p["provider"]) for p in v["providers"]))
        self.assertIn("Email finder", S.render_status_text(d))

    def test_export_keeps_four_columns(self):
        with mock.patch.object(S, "email_finder", return_value=S.email_finder(
                self.conn, CFG, summary_fn=lambda conn: fake_enrich.sample_summary())):
            out = export.export_all(self.conn, None, CFG)
        with open(out["files"]["settings"]["path"], newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["Setting", "Value", "Used today", "What it means"])
        self.assertTrue(all(len(r) == 4 for r in rows))
        self.assertIn("Email finder: Hunter", [r[0] for r in rows])
        self.assertIn("Browser sites (your Chrome logins)", [r[0] for r in rows])


if __name__ == "__main__":
    unittest.main()
