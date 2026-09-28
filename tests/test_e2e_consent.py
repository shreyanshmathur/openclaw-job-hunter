"""E2E (INT): per-site browser consent (change request "use the existing Chrome logins, with consent") across the
real core (U1 identity, cycles, gate, breakers), the outreach lane (U3, U6) and the real jobhunter-guard runtime
(U8), on a new install where private/consent.json does not exist yet.

Without a consent row for a login site: `preflight` stops a lane whose login sites all lack consent and lists the
missing ones, `gate reserve`, `usage add` and `identity check` refuse that site (E_CONSENT_MISSING), and the guard
refuses every browser call to its hosts (G_NO_CONSENT). Only the owner (PIN) can grant: an agent's or a system
call to `browser consent grant` is refused, the guard does not even let an agent run it or write the file. After
the grant the same calls go through. A revoke trips the site's breaker (consent_revoked) and everything refuses
again; a revoke written outside the commands (the installer, a hand edit) is caught by the next preflight; a new
grant closes a breaker that consent alone held open.
"""
from __future__ import annotations

import json
import os
import unittest

import tests  # noqa: F401
from jobhunter import paths
from jobhunter.commands.core import LINKEDIN_ACK
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u3 import install_reviewer_hashes
from tests.fixtures.e2e.support import OU, OutreachSteps, World, install_qc_fakes
from tests.helpers import write_consent
from tests.test_e2e_guard_replay import GuardBridge, node_major

INBOX = "https://mail.google.com/mail/u/0/#inbox"
FEED = "https://www.linkedin.com/feed/"
NAUKRI = "https://www.naukri.com/mnjuser/homepage"
ATS = "https://boards.greenhouse.io/kestrelcommerce/jobs/4011"
SESSION = "agent:jobhunter-outreach:cron:e2e-consent"
BOARDS = {"boards.sites.naukri.discover": "browser", "boards.sites.naukri.apply": "browser"}


class ConsentWorld(World, OutreachSteps):
    pass


class ConsentBase(unittest.TestCase):
    def setUp(self):
        self.w = ConsentWorld(consent=False)
        self.addCleanup(self.w.stop)
        install_qc_fakes(self)
        self.w.onboard(**BOARDS)
        install_reviewer_hashes(self.w.conn)
        # the owner turns LinkedIn on (its own acknowledgement); the outreach lane then uses gmail and linkedin
        self.w.ok(["linkedin", "enable", "--ack", LINKEDIN_ACK, "--account-type", "free", "--account-age-years", "6"],
                  "human")

    def later(self, minutes: int) -> None:
        """Time passes; the guard keeps beating (its heartbeat file is what lets a lane start)."""
        self.w.clock.advance(minutes=minutes)
        write_heartbeat()

    def breaker(self, scope: str):
        row = self.w.one("SELECT state, reason_code FROM breakers WHERE scope = ?", scope)
        return tuple(row) if row is not None else None

    def preflight(self, lane: str, agent: str) -> tuple:
        rc, env = self.w.run(["preflight", "--lane", lane], agent)
        return rc, env.get("code"), env.get("data") or {}

    def approved_email(self, n: int, first: str, last: str, company: str, domain: str) -> dict:
        """A cold email to a new contact, approved by the owner, made in its own outreach cycle."""
        w = self.w
        cyc = w.preflight("outreach", OU)
        addr = "%s.%s@%s" % (first.lower(), last.lower(), domain)
        c = w.add_contact(cyc, full_name="%s %s" % (first, last), first_name=first, company=company,
                          company_domain=domain, email=addr, email_grade="A",
                          email_evidence_url="https://%s/team" % domain, source_url="https://%s/team" % domain)
        w.verify_email(cyc, addr, "A", "Team page: https://%s/team" % domain)
        d = w.cold_email_draft(cyc, c["contact_uid"], first, company, n=n)
        w.end_cycle(cyc, OU)
        self.assertEqual([a["draft_uid"] for a in w.approve_all()], [d])
        self.assertEqual(w.one("SELECT send_route FROM drafts WHERE draft_uid = ?", d)[0], "browser")
        return {"contact_uid": c["contact_uid"], "draft_uid": d, "address": addr}


class TestConsentGate(ConsentBase):
    def test_every_site_starts_at_no(self):
        w = self.w
        view = w.ok(["browser", "consent", "list"])
        self.assertEqual({r["state"] for r in view["sites"]}, {"not_granted"})
        self.assertFalse(os.path.exists(paths.consent_file()))
        # the default email route is the browser; with no consent the outreach lane has nothing it may use
        self.assertEqual(w.cfg["gmail"]["route"], "web_ui")
        rc, code, data = self.preflight("outreach", OU)
        self.assertEqual((rc, code, data["go"]), (7, "E_CONSENT_MISSING", False), data)
        self.assertEqual(data["consent"], {"allowed": [], "missing": ["gmail", "linkedin"]})
        rc, code, data = self.preflight("scout", "jobhunter-scout")
        self.assertEqual((code, data["consent"]["missing"]), ("E_CONSENT_MISSING", ["naukri"]), data)
        # the applier also has ATS forms, which need no login: it runs, but may use none of its login sites
        rc, code, data = self.preflight("applier", "jobhunter-applier")
        self.assertEqual((rc, code, data["go"]), (0, "OK", True), data)
        self.assertEqual(data["consent"], {"allowed": [], "missing": ["gmail", "naukri", "linkedin"]})
        self.assertNotIn("gmail", data["identity_required"])
        w.end_cycle(data["cycle_id"], "jobhunter-applier")
        for platform in ("gmail", "linkedin", "naukri"):
            w.fails(["usage", "add", "--platform", platform, "--metric", "page_view", "--n", "1"], "E_CONSENT_MISSING",
                    "jobhunter-applier")

    def test_only_the_owner_grants_and_a_revoke_refuses_again(self):
        w = self.w
        # an agent or a system job cannot give itself consent, and nothing was written
        w.fails(["browser", "consent", "grant", "--site", "gmail", "--method", "manual_login"], "E_CALLER_NOT_ALLOWED",
                OU)
        rc, env = w.run(["browser", "consent", "grant", "--site", "gmail", "--method", "manual_login"])
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"), env)
        self.assertFalse(os.path.exists(paths.consent_file()))

        # LinkedIn only: the lane runs, Gmail stays refused everywhere
        g = w.grant("linkedin")
        self.assertEqual(g["granted"], ["linkedin"])
        mode = os.stat(paths.consent_file()).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        first = self.approved_email(1, "Jordan", "Blake", "Kestrel Commerce", "kestrel.example")
        second = self.approved_email(2, "Casey", "Morgan", "Heron Freight", "heron.example")
        rc, code, data = self.preflight("outreach", OU)
        self.assertEqual((code, data["consent"]), ("OK", {"allowed": ["linkedin"], "missing": ["gmail"]}), data)
        self.assertEqual(data["identity_required"], ["linkedin"])
        cyc = data["cycle_id"]
        rc, env = w.web_reserve(cyc, first["draft_uid"], first["contact_uid"])
        self.assertEqual((rc, env["code"]), (7, "E_CONSENT_MISSING"), env)
        self.assertEqual(env["data"]["site"], "gmail")
        w.fails(["identity", "check", "--platform", "gmail", "--file",
                 w.wfile("outreach", "%s/identity.json" % cyc,
                         {"platform": "gmail", "observed": {"account_email": "sam.lee@example.com"}})],
                "E_CONSENT_MISSING", OU, cycle=cyc)
        self.assertEqual(w.one("SELECT count(*) FROM actions")[0], 0)

        # the owner allows Gmail: the same reserve goes through
        w.grant("gmail")
        rc, env = w.web_reserve(cyc, first["draft_uid"], first["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        sent = w.web_send(cyc, env["data"]["token"], first["draft_uid"])
        self.assertEqual(sent["confirm"]["status"], "sent")
        w.end_cycle(cyc, OU)

        # revoke: the gmail breaker trips (no waiting time), reserve refuses, the lane lists gmail as missing
        r = w.revoke("gmail")
        self.assertEqual((r["revoked"], r["breakers_tripped"]), (["gmail"], ["gmail"]))
        self.assertEqual(self.breaker("gmail"), ("open", "consent_revoked"))
        self.later(30)
        rc, code, data = self.preflight("outreach", OU)
        self.assertEqual((code, data["consent"]), ("OK", {"allowed": ["linkedin"], "missing": ["gmail"]}), data)
        self.assertNotIn("gmail", data["identity_required"])
        self.assertIn("gmail", data["open_breakers"])
        cyc = data["cycle_id"]
        rc, env = w.web_reserve(cyc, second["draft_uid"], second["contact_uid"])
        self.assertEqual((rc, env["code"]), (5, "E_BREAKER_OPEN"), env)
        w.end_cycle(cyc, OU)
        self.assertEqual(w.one("SELECT count(*) FROM actions")[0], 1)

        # allowed again: the breaker that consent alone held open closes, the second email can go
        g = w.grant("gmail")
        self.assertEqual(g["breakers_closed"], ["gmail"])
        self.assertEqual(self.breaker("gmail")[0], "closed")
        self.later(30)
        cyc = w.preflight("outreach", OU)
        rc, env = w.web_reserve(cyc, second["draft_uid"], second["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        w.ok(["gate", "fail", env["data"]["token"], "--reason", "not_attempted", "--evidence-file",
              w.wfile("outreach", "%s/why.txt" % cyc, "Cycle ended before compose.")], OU, cycle=cyc)
        w.end_cycle(cyc, OU)

        # both revoked: the outreach lane stops at preflight
        w.revoke("gmail", "linkedin")
        rc, code, data = self.preflight("outreach", OU)
        self.assertEqual((code, data["consent"]["missing"]), ("E_CONSENT_MISSING", ["gmail", "linkedin"]), data)
        self.assertEqual(self.breaker("linkedin"), ("open", "consent_revoked"))

    def test_revoke_outside_the_commands_is_caught_by_preflight(self):
        w = self.w
        w.grant("gmail", "linkedin")
        cyc = w.preflight("outreach", OU)
        w.end_cycle(cyc, OU)
        self.assertIsNone(self.breaker("gmail"))
        # the installer's consent step (U7) or a hand edit marks gmail revoked in the file itself
        write_consent(["linkedin"])
        with open(paths.consent_file(), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["sites"]["gmail"] = dict(doc["sites"]["linkedin"], site="gmail", status="revoked",
                                     revoked_at=w.clock.now())
        with open(paths.consent_file(), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.later(5)
        rc, code, data = self.preflight("outreach", OU)
        self.assertEqual((code, data["consent"]["missing"]), ("OK", ["gmail"]), data)
        self.assertEqual(self.breaker("gmail"), ("open", "consent_revoked"))
        w.end_cycle(data["cycle_id"], OU)
        # a consent file others can write counts as no consent at all
        os.chmod(paths.consent_file(), 0o666)
        rc, code, data = self.preflight("outreach", OU)
        self.assertEqual((code, data["consent"]["allowed"]), ("E_CONSENT_MISSING", []), data)
        os.chmod(paths.consent_file(), 0o600)
        # granted again by the owner: the breaker consent alone held open closes
        w.grant("gmail")
        self.assertEqual(self.breaker("gmail")[0], "closed")


@unittest.skipUnless(node_major() >= 24, "needs node 24 or later (the guard plugin runs TypeScript directly)")
class TestGuardConsent(ConsentBase):
    """The real jobhunter-guard runtime reads the same private/consent.json on every browser call."""

    def setUp(self):
        super().setUp()
        from jobhunter import auth, canon
        self.assertTrue(auth.create_guard_key())
        h = paths.home()
        self.g = GuardBridge({"repo": paths.REPO, "python": h["python"], "homeFile": paths.home_file(),
                              "publicReadonlyAgents": ["main"]}, canon.now())
        self.addCleanup(self.g.close)
        self.canon = canon

    def call(self, tool: str, params: dict, agent: str = OU) -> str:
        self.g.ask({"op": "clock", "now": self.canon.now()})
        return self.g.ask({"op": "call", "agent": agent, "session": SESSION, "tool": tool, "params": params})["outcome"]

    def nav(self, url: str, agent: str = OU) -> str:
        return self.call("browser", {"profile": "jobhunter", "action": "navigate", "targetUrl": url}, agent)

    def opened(self, url: str) -> None:
        """A navigation the guard allowed and whose page the browser then showed (the tab is on `url`)."""
        params = {"profile": "jobhunter", "action": "navigate", "targetUrl": url}
        self.assertEqual(self.call("browser", params), "allow")
        self.g.ask({"op": "result", "agent": OU, "session": SESSION, "tool": "browser", "params": params,
                    "result": {"content": [{"type": "text", "text": "- heading \"Inbox\" [level=1]"}],
                               "details": {"ok": True, "targetId": "t1", "url": url}}, "error": None})

    def test_guard_follows_grant_and_revoke(self):
        w = self.w
        self.assertEqual([self.nav(u) for u in (INBOX, FEED, NAUKRI)], ["G_NO_CONSENT"] * 3)
        self.assertEqual(self.nav(ATS, "jobhunter-applier"), "allow")            # ATS forms need no login
        # the agent cannot give itself consent: not by the command, not by writing the file; it does not even
        # read the list (preflight tells a lane its sites)
        h = paths.home()
        jh = "%s %s/scripts/jh.py " % (h["python"], paths.REPO)
        self.assertEqual(self.call("exec", {"command": jh + "browser consent grant --site gmail --method manual_login",
                                            "timeoutSeconds": 90}), "G_EXEC_ACL")
        self.assertEqual(self.call("exec", {"command": jh + "browser consent list", "timeoutSeconds": 90}), "G_EXEC_ACL")
        self.assertEqual(self.call("write", {"path": paths.consent_file(), "content": "{}"}), "G_PATH_DENIED")
        self.assertFalse(os.path.exists(paths.consent_file()))

        w.grant("gmail")
        self.assertEqual([self.nav(u) for u in (INBOX, FEED, NAUKRI)], ["allow", "G_NO_CONSENT", "G_NO_CONSENT"])
        w.grant("linkedin", "naukri")
        self.assertEqual([self.nav(u) for u in (INBOX, FEED, NAUKRI)], ["allow"] * 3)
        self.opened(INBOX)
        snapshot = {"profile": "jobhunter", "action": "snapshot"}
        self.assertEqual(self.call("browser", snapshot), "allow")
        w.revoke("gmail")
        self.assertEqual(self.breaker("gmail"), ("open", "consent_revoked"))
        # the tab still on Gmail: reading it is refused at once; LinkedIn is unaffected
        self.assertEqual(self.call("browser", snapshot), "G_NO_CONSENT")
        self.assertEqual([self.nav(u) for u in (INBOX, FEED)], ["G_NO_CONSENT", "allow"])
        # a consent file other users can write gives no site consent to the guard either
        os.chmod(paths.consent_file(), 0o666)
        self.assertEqual(self.nav(FEED), "G_NO_CONSENT")
        os.chmod(paths.consent_file(), 0o600)
        self.assertEqual(self.nav(FEED), "allow")

    def test_the_agent_can_close_its_tab_after_a_revoke_or_a_pause(self):
        """`browser consent revoke` removes the row and trips the site's breaker; closing the tab never touches
        the page, so the agent can still clean up. Listing tabs is not cleanup: a tab list carries every tab's
        title (a Gmail title shows the account address), so it is judged like a read of the tab's page and is
        refused with it. Only the owner's state/PAUSED stops a close too."""
        w = self.w
        close = {"profile": "jobhunter", "action": "close"}
        close_t1 = {"profile": "jobhunter", "action": "close", "targetId": "t1"}
        tabs = {"profile": "jobhunter", "action": "tabs"}
        snapshot = {"profile": "jobhunter", "action": "snapshot"}

        def cleanup() -> list:
            return [self.call("browser", p) for p in (close, close_t1)]

        w.grant("gmail")
        self.opened(INBOX)
        self.assertEqual(self.call("browser", tabs), "allow")
        w.revoke("gmail")
        self.assertEqual(self.breaker("gmail"), ("open", "consent_revoked"))
        self.assertEqual(cleanup(), ["allow"] * 2)
        # the tab still shows Gmail: its title in a tab list is page data, refused like a snapshot
        self.assertEqual([self.call("browser", p) for p in (tabs, snapshot)], ["G_NO_CONSENT"] * 2)
        self.assertEqual(self.nav(INBOX), "G_NO_CONSENT")
        # anything that touches the page is still refused
        self.assertEqual(self.call("browser", {"profile": "jobhunter", "action": "act", "kind": "click", "ref": "e5"}),
                         "G_NO_CONSENT")

        # consent back, then the owner pauses Gmail: the pause breaker refuses the page, not the cleanup
        w.grant("gmail")
        self.assertEqual(self.call("browser", snapshot), "allow")
        w.ok(["pause", "--scope", "gmail", "--reason", "owner break"], "human")
        self.assertEqual(self.breaker("pause:gmail")[0], "open")
        self.assertEqual([self.call("browser", p) for p in (snapshot, tabs)], ["G_BREAKER_OPEN"] * 2)
        self.assertEqual(self.nav(INBOX), "G_BREAKER_OPEN")
        self.assertEqual(cleanup(), ["allow"] * 2)
        w.ok(["unpause", "--scope", "gmail"], "human")
        self.assertEqual([self.call("browser", p) for p in (snapshot, tabs)], ["allow"] * 2)

        # the kill switch stops every browser call, cleanup too
        w.ok(["pause", "--reason", "owner away"], "human")
        self.assertTrue(os.path.exists(paths.paused_file()))
        self.assertEqual(cleanup(), ["G_BREAKER_OPEN"] * 2)
        self.assertEqual([self.call("browser", p) for p in (snapshot, tabs)], ["G_BREAKER_OPEN"] * 2)
        w.ok(["unpause"], "human")
        self.assertEqual(cleanup() + [self.call("browser", tabs)], ["allow"] * 3)


if __name__ == "__main__":
    unittest.main()
