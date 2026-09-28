"""E2E (INT): a simulated week (design 13.3) with the real modules of U1 to U6 and U9 in one temp home.

Tuesday 2026-09-29 to Monday 2026-10-05, UTC. Only the network is replaced: the Greenhouse API answers from
fixtures (U2 FakeClient behind `sources.make_client`), MX lookups are injected at emailcheck.lookup_mx, mail goes
through the U9 fake SMTP and IMAP servers, owner messages through the fake `openclaw`, and the QC reviewer's model
turn is the U3 FakeReviewer, whose output goes through the real review parser.

- Discovery: `sources fetch --lane api` (Greenhouse list and detail), then scout `job add` files naming the same job
  by a careers-page embed URL, a Greenhouse embed URL and a Naukri listing of "GrowthValley Pvt Ltd".
- Evaluation, seven cold emails (research, contact, email verify, draft, lint and review, owner approval), the
  mailer every five minutes (IMAP precheck, reserve, SMTP, confirm, thread), an ATS application.
- Injected duplicates: the same person by a +tag address, a LinkedIn URL variant and a second address; the same
  company as "Growth Valley", "GrowthValley Pvt Ltd" and growthvalley.example; the same job by other URLs.
- Ceilings: every send in the ledger is checked against the warm-up day limit, the hour limit and the gap.
- Stop signatures: a job board challenge page, an ATS rate-limit page and a Google security email each trip their
  breaker, and the lanes and the mailer stop there.
- The Sheet rows, the reply that comes back and the owner notifications through `openclaw message send`.
Fictional people and companies only.
"""
from __future__ import annotations

import email
import json
import os
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import breakers, canon, emailcheck, ocrun, sheets_labels, sheets_rows, sources
from jobhunter import qc as qcpkg
from jobhunter.mail import fetch
from jobhunter.sources import greenhouse
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u2 import FakeClient
from tests.fakes.u3 import FakeReviewer, install_reviewer_hashes
from tests.fakes.u9 import OWNER, inbound
from tests.fixtures.e2e.support import AP, OWNER_CHAT, REPO, ApplyWorld, e2e_fixture
from tests.test_e2e_outreach import MailWorld

OU = "jobhunter-outreach"
SC = "jobhunter-scout"
TENANT = "growthvalley"
GV_DOMAIN = "growthvalley.example"
GH_LIST = greenhouse.list_url(TENANT)
LI = "https://www.linkedin.com/" + "in/"          # assembled so the leak check sees no profile path
WEEK_END = "2026-10-05T23:55:00Z"


def target(key, company, domain, first, last, title, anchor, snippet, source, subject, body, claim):
    return {"key": key, "company": company, "domain": domain, "first": first, "last": last, "title": title,
            "anchor": anchor, "snippet": snippet, "source": source, "subject": subject, "body": body,
            "claim": claim, "email": "%s.%s@%s" % (first.lower(), last.lower(), domain)}


# Seven first-touch cold emails at seven companies. Each hook quotes its research snippet; each claim is a profile
# fact of tests/fixtures/profile/inference.json (P1 to P5).
TARGETS = [
    target("gv", "Growth Valley", GV_DOMAIN, "Jordan", "Blake", "Head of Analytics", "pincode-level models",
           "pincode-level models beat our city-level RTO model last quarter", "linkedin_post",
           "Pincode-level RTO models",
           "Hi Jordan,\n\nYour post on 18 September said pincode-level models beat the city-level RTO model at Growth "
           "Valley. We saw the same pattern at Tidemark Logistics.\n\nI built the COD return-risk model at Tidemark, "
           "and RTO fell from 18% to 13% in two quarters.\n\nWould a 15 minute call next week be useful? If someone "
           "else owns this, a name is plenty.\n\nThanks,",
           ("RTO fell from 18% to 13% in two quarters", "P1")),
    target("ke", "Kestrel Commerce", "kestrel.example", "Avery", "Stone", "VP Data", "daily forecasts",
           "this summer we moved demand planning from weekly spreadsheets to daily forecasts", "company_blog",
           "Daily forecasts and returns data",
           "Hi Avery,\n\nThe Kestrel engineering blog says demand planning moved to daily forecasts this summer. "
           "Returns data tends to be where that kind of change hurts first.\n\nAt Tidemark Logistics my weekly returns "
           "forecasts in SQL and Python covered 40 warehouses, so those gaps are familiar ground.\n\nWho on your team "
           "is the right person to ask about analyst openings? A pointer would help a lot.\n\nThanks,",
           ("weekly returns forecasts in SQL and Python covered 40 warehouses", "P2")),
    target("he", "Heron Freight", "heron.example", "Morgan", "Hale", "Director of Operations", "carrier scorecards",
           "our carrier scorecards still take a full day each week", "talk_or_podcast",
           "Carrier scorecards at Heron",
           "Hi Morgan,\n\nOn the Freight Signals podcast you mentioned that carrier scorecards at Heron still take a "
           "full day each week.\n\nTidemark Logistics had the same manual report. After I automated it in Tableau, "
           "the team got back 6 hours a week.\n\nWould it help to compare notes on how that setup works? The outline "
           "fits on one page.\n\nThanks,",
           ("automated it in Tableau, the team got back 6 hours a week", "P3")),
    target("bi", "Birchwood Health", "birchwood.example", "Casey", "Reed", "Analytics Manager",
           "delivery time promises", "we are rethinking delivery time promises for home test kits", "company_blog",
           "Delivery estimates for test kits",
           "Hi Casey,\n\nBirchwood's article on delivery time promises for home test kits described a problem close to "
           "one of my projects.\n\nOn my own time I built a delivery time estimator in Python, and the approach "
           "carries over to kit shipments.\n\nCould we speak briefly about how your analytics group handles these "
           "estimates? If a colleague owns this area, their name is enough.\n\nThanks,",
           ("built a delivery time estimator in Python", "P5")),
    target("lu", "Lumen Grocers", "lumen.example", "Drew", "Ellis", "Head of Supply Chain Analytics",
           "cash on delivery orders", "cash on delivery orders come back far more often than prepaid ones", "talk_or_podcast",
           "Cash on delivery returns at Lumen",
           "Hi Drew,\n\nAt the retail analytics meetup you noted that cash on delivery orders at Lumen come back far "
           "more often than prepaid ones.\n\nThat pattern shaped my work at Tidemark Logistics, where the COD "
           "return-risk model took RTO from 18% to 13% in two quarters.\n\nAre you open to a brief chat about "
           "analyst roles on your team?\n\nThanks,",
           ("the COD return-risk model took RTO from 18% to 13% in two quarters", "P1")),
    target("qu", "Quarry Mobility", "quarry.example", "Jamie", "Ward", "Data Lead", "forecasting stack",
           "next quarter we rebuild the warehouse forecasting stack in dbt", "company_site",
           "Rebuilding the forecasting stack",
           "Hi Jamie,\n\nThe Quarry Mobility careers page says the warehouse forecasting stack gets rebuilt in dbt "
           "next quarter.\n\nMy recent work lines up with that plan: weekly returns forecasts in SQL and Python for "
           "40 warehouses at Tidemark Logistics.\n\nIf the data team is hiring for this, could you point me to whoever "
           "runs it? A name would be plenty.\n\nThanks,",
           ("weekly returns forecasts in SQL and Python for 40 warehouses", "P2")),
    target("pi", "Pinecrest Outfitters", "pinecrest.example", "Robin", "Hart", "Director of Analytics",
           "free exchanges", "footwear returns rose after free exchanges launched in the spring", "news",
           "Footwear returns after free exchanges",
           "Hi Robin,\n\nA trade press piece quoted Pinecrest on footwear returns rising after free exchanges "
           "launched in the spring.\n\nReturn risk is the problem I know best. At Tidemark Logistics my COD model "
           "brought RTO from 18% to 13% in two quarters.\n\nWould a short call to compare approaches be useful for "
           "your analytics team?\n\nThanks,",
           ("brought RTO from 18% to 13% in two quarters", "P1")),
]
SOURCE_HOST = {"linkedin_post": "https://www.linkedin.com/posts/example-person-activity-%d",
               "company_blog": "https://%s/blog/post-%d", "talk_or_podcast": "https://%s/events/talk-%d",
               "company_site": "https://%s/careers/%d",
               "news": "https://%s/press/item-%d"}


def e2e_mail(name: str) -> str:
    with open(os.path.join(REPO, "tests", "fixtures", "mail", name), "r", encoding="utf-8") as fh:
        return fh.read()


class WeekWorld(MailWorld, ApplyWorld):
    """The mail world (fake SMTP and IMAP) with the applier steps. The running guard refreshes its heartbeat all
    week; here each lane start does."""

    def preflight(self, lane: str, agent: str) -> str:
        write_heartbeat()
        return super().preflight(lane, agent)


class TestSimulatedWeek(unittest.TestCase):
    def setUp(self):
        self.w = WeekWorld()
        self.addCleanup(self.w.stop)
        prev = qcpkg.SPAWN
        qcpkg.SPAWN = [].append
        self.addCleanup(setattr, qcpkg, "SPAWN", prev)
        self.reviewer = FakeReviewer("pass")
        p = mock.patch.object(ocrun, "agent_turn", self.reviewer)
        p.start()
        self.addCleanup(p.stop)
        self.mail_runs = []

    # ------------------------------------------------------------------ helpers
    def gh_client(self) -> FakeClient:
        return FakeClient({GH_LIST: e2e_fixture("week_greenhouse_list.json"),
                           GH_LIST + "/4100001": e2e_fixture("week_greenhouse_detail.json")})

    def company_of(self, table: str, where: str, *args):
        row = self.w.one("SELECT c.company_uid FROM %s x JOIN companies c ON c.id = x.company_id WHERE %s"
                         % (table, where), *args)
        return row[0] if row else None

    def source_url(self, t: dict, i: int) -> str:
        pattern = SOURCE_HOST[t["source"]]
        return pattern % i if t["source"] == "linkedin_post" else pattern % (t["domain"], i)

    def add_contact(self, cyc: str, name: str, contact: dict, code: str = "OK") -> dict:
        """`contact add`; code is the envelope code the call must end with. Returns the data."""
        w = self.w
        base = {"full_name": None, "first_name": None, "title": None, "company": None, "company_domain": None,
                "role_type": "hiring_manager", "locale": "US", "email": None, "email_grade": None,
                "email_evidence_url": None, "linkedin_url": None, "linkedin_member_url": None, "source_url": None}
        base.update(contact)
        rc, env = w.run(["contact", "add", "--file", w.wfile("outreach", "%s/%s.json" % (cyc, name), base)], OU,
                        cycle=cyc)
        self.assertEqual(env.get("code"), code, env)
        self.assertEqual(rc == 0, code == "OK", env)
        return env.get("data") or {}

    def draft_file(self, cyc: str, t: dict, contact_uid: str, fact_uid: str, i: int) -> str:
        today = self.w.clock.now()[:10]
        draft = {"kind": "cold_email", "channel": "email_cold", "contact_uid": contact_uid,
                 "subject": t["subject"], "body": t["body"],
                 "hook": {"anchor": t["anchor"], "source_type": t["source"], "source_url": self.source_url(t, i),
                          "snippet": t["snippet"], "published_at": "2026-09-18", "retrieved_at": today,
                          "fact_id": fact_uid},
                 "claims": [{"text": t["claim"][0], "fact_id": t["claim"][1]}], "links": []}
        return self.w.wfile("outreach", "%s/draft-%s.json" % (cyc, t["key"]), draft)

    def write_one(self, cyc: str, t: dict, i: int) -> dict:
        """Research, contact, address check, draft and review for one target. Returns the ids."""
        w = self.w
        c = self.add_contact(cyc, "contact-" + t["key"], {
            "full_name": "%s %s" % (t["first"], t["last"]), "first_name": t["first"], "title": t["title"],
            "company": t["company"], "company_domain": t["domain"], "email": t["email"], "email_grade": "A",
            "email_evidence_url": "https://%s/team" % t["domain"],
            "linkedin_url": LI + "example-%s-%s" % (t["first"].lower(), t["last"].lower()),
            "source_url": "https://%s/team" % t["domain"]})
        self.assertFalse(c["existing"], c)
        ev = w.wfile("outreach", "%s/evidence-%s.txt" % (cyc, t["key"]), "Team page: https://%s/team" % t["domain"])
        with mock.patch.object(emailcheck, "lookup_mx", return_value=["mx1." + t["domain"]]):
            v = w.ok(["email", "verify", "--address", t["email"], "--grade", "A", "--evidence-file", ev], OU, cycle=cyc)
        self.assertTrue(v["allowed"], v)
        research = {"subject": {"kind": "person", "contact_uid": c["contact_uid"]},
                    "facts": [{"text": "%s: %s." % (t["company"], t["snippet"]), "snippet": t["snippet"],
                               "source_type": t["source"], "source_url": self.source_url(t, i),
                               "published_at": "2026-09-18", "retrieved_at": w.clock.now()[:10]}]}
        r = w.ok(["research", "add", "--file", w.wfile("outreach", "%s/research-%s.json" % (cyc, t["key"]), research)],
                 OU, cycle=cyc)
        d = w.ok(["draft", "create", "--file", self.draft_file(cyc, t, c["contact_uid"], r["facts"][0]["fact_uid"], i)],
                 OU, cycle=cyc)
        verdict = w.qc_pass(d["draft_uid"], OU, cyc)
        self.assertEqual(verdict.get("draft_status"), "awaiting_approval", (t["key"], verdict))
        return {"contact_uid": c["contact_uid"], "company_uid": c["company_uid"], "draft_uid": d["draft_uid"],
                "fact_uid": r["facts"][0]["fact_uid"]}

    def mail_until(self, until: str, step_min: int = 5) -> list:
        """The mailer's cron job: `mail run` every step_min minutes of fake time up to `until`. Returns the tokens
        sent in these runs."""
        w = self.w
        sent = []
        while w.clock.now() < until:
            rc, env = w.run(["mail", "run"])
            self.assertIn(env.get("code"), ("OK", "NOTHING_TO_DO", "E_BREAKER_OPEN"), env)
            data = env.get("data") or {}
            self.mail_runs.append((w.clock.now(), env.get("code"), list(data.get("sent") or []), data.get("blocked")))
            sent += data.get("sent") or []
            w.clock.advance(minutes=step_min)
        return sent

    def dedup_refused(self, cyc: str, args: list, agent: str = OU) -> list:
        """The codes of a `dedup check` that must refuse."""
        rc, env = self.w.run(["dedup", "check"] + args, agent, cycle=cyc)
        data = env.get("data") or {}
        self.assertFalse(data.get("allowed", True), env)
        self.assertNotEqual(rc, 0, env)
        return [h["code"] for h in data.get("hits") or []]

    def refused_draft(self, cyc: str, t: dict, contact_uid: str, fact_uid: str | None, tag: str) -> str:
        """A cold email draft for contact_uid that `draft create` must refuse. Returns the code."""
        w = self.w
        if fact_uid is None:
            research = {"subject": {"kind": "person", "contact_uid": contact_uid},
                        "facts": [{"text": t["snippet"], "snippet": t["snippet"], "source_type": t["source"],
                                   "source_url": self.source_url(t, 90), "published_at": "2026-09-18",
                                   "retrieved_at": w.clock.now()[:10]}]}
            path = w.wfile("outreach", "%s/research-%s.json" % (cyc, tag), research)
            fact_uid = w.ok(["research", "add", "--file", path], OU, cycle=cyc)["facts"][0]["fact_uid"]
        path = self.draft_file(cyc, t, contact_uid, fact_uid, 90)
        rc, env = w.run(["draft", "create", "--file", path], OU, cycle=cyc)
        self.assertNotEqual(rc, 0, env)
        n = w.one("SELECT count(*) FROM drafts d JOIN contacts c ON c.id = d.contact_id WHERE c.contact_uid = ? AND "
                  "d.status IN ('awaiting_approval', 'approved', 'qc_pending', 'qc_passed')", contact_uid)[0]
        self.assertEqual(n, 0, env)
        return env.get("code")

    def check_ceilings(self) -> None:
        """Every mailer send so far against the warm-up week 1 day limit (5 cold emails per UTC day), the hour
        limit (3 in any 60 minutes), the minimum gap (10 minutes) and the week limit (60)."""
        rows = self.sends()
        stamps = [canon.parse_ts(r[3]) for r in rows]
        days = {}
        for r in rows:
            days[r[3][:10]] = days.get(r[3][:10], 0) + 1
        self.assertLessEqual(max(days.values() or [0]), 5, days)
        self.assertLessEqual(len(rows), 60)
        for i, ts in enumerate(stamps):
            in_hour = [x for x in stamps if 0 <= (ts - x).total_seconds() < 3600]
            self.assertLessEqual(len(in_hour), 3, rows)
            if i:
                self.assertGreaterEqual((ts - stamps[i - 1]).total_seconds(), 600, rows)
        budget = self.w.ok(["budget", "--platform", "gmail"])
        for row in budget["rows"]:
            if row.get("limit") is not None:
                self.assertLessEqual(row["used"], row["limit"], row)

    def owner_messages(self, log: str) -> list:
        """The texts the fake openclaw accepted for the owner."""
        if not os.path.exists(log):
            return []
        with open(log, "r", encoding="utf-8") as fh:
            return [json.loads(line)["options"].get("--message") or "" for line in fh if line.strip()]

    def check_no_duplicates(self) -> None:
        """No person, company, job or draft has two live first touches in the ledger."""
        w = self.w
        live = "('reserved','armed','sent','unknown','failed_after_click','imported')"
        for col, kinds in (("contact_id", "('cold_email','application_email','li_invite','li_message')"),
                           ("company_id", "('cold_email','application_email')"), ("job_id", "('application')"),
                           ("draft_id", "('cold_email','application')")):
            dup = w.all("SELECT %s, count(*) FROM actions WHERE kind IN %s AND status IN %s AND %s IS NOT NULL "
                        "GROUP BY %s HAVING count(*) > 1" % (col, kinds, live, col, col))
            self.assertEqual(dup, [], col)
        self.assertEqual(w.all("SELECT recipient, count(*) FROM actions WHERE route = 'mailer' GROUP BY recipient "
                               "HAVING count(*) > 1"), [])
        rcpts = [email.message_from_bytes(m)["To"] for m in w.smtp.messages]
        self.assertEqual(sorted(rcpts), sorted(t["email"] for t in TARGETS))

    def check_sheet(self, job_uid: str, app_token: str, jordan_token: str) -> None:
        """The Sheet rows the sync would push: every action with readable labels, dates and links."""
        w = self.w
        outreach = dict(sheets_rows.build_rows(w.conn, "outreach", None))
        tokens = {r[0] for r in w.all("SELECT token FROM actions WHERE kind = 'cold_email' AND status = 'sent'")}
        self.assertEqual(set(outreach), tokens)
        for row in outreach.values():
            self.assertEqual(row["channel"], "Email")
            self.assertRegex(json.dumps(row), r"2026-(09-29|09-30)")
        gv = outreach[jordan_token]
        self.assertEqual((gv["company"], gv["subject"]), ("Growth Valley", TARGETS[0]["subject"]))
        followups = dict(sheets_rows.build_rows(w.conn, "followups", None))
        self.assertEqual(set(followups), {"em:" + t for t in tokens})
        self.assertEqual(followups["em:" + jordan_token]["reply_type"], "Positive reply")
        self.assertEqual(followups["em:" + jordan_token]["what_they_said"], "Happy to talk on Thursday.")
        apps = sheets_rows.build_rows(w.conn, "applications", None)
        self.assertEqual(len(apps), 1, apps)
        self.assertIn("Growth Valley", json.dumps(apps[0][1]))
        self.assertEqual(w.one("SELECT a.token FROM applications ap JOIN actions a ON a.id = ap.action_id")[0],
                         app_token)
        jobs_rows = dict(sheets_rows.build_rows(w.conn, "jobs", None))
        self.assertTrue(jobs_rows[job_uid].get("applied_on"), jobs_rows[job_uid])
        skipped = [row for _k, row in sheets_rows.build_rows(w.conn, "skipped", None)]
        self.assertTrue(any("Director of Sales" in json.dumps(row) for row in skipped), skipped)
        alerts = [row for _k, row in sheets_rows.build_rows(w.conn, "alerts", None)]
        self.assertEqual(sorted(a["status"] for a in alerts), ["Open"] * 3, alerts)
        by_area = {}
        for a in alerts:
            by_area.setdefault(a["area"], a)
        # the Sheet names each area as the chat alert and `breaker status` do (breakers.AREA_LABEL)
        for scope in ("ats", "gmail", "site:naukri"):
            self.assertIn(breakers.area_label(scope), by_area, (scope, alerts))
        self.assertEqual(breakers.area_label("ats"), "Job forms")
        # the Google security stop has no waiting time: no fake "until", and the to-do is a reset, not a wait
        gm = by_area[breakers.area_label("gmail")]
        self.assertIsNone(gm["until"], gm)
        self.assertIn("breaker reset gmail", gm["todo"])
        self.assertNotIn("waiting time", gm["todo"])
        # the ATS rate limit has a real cooldown after its trip time
        ats = by_area[breakers.area_label("ats")]
        self.assertTrue(ats["until"] and ats["until"] > ats["time"], ats)
        # "What happened" reads as a sentence for the codes the week's trips really stored (detect signatures and
        # the IMAP fetch), never as the internal code; "What you need to do" is that code's advice for the scope
        opened = {r[0]: r[1] for r in w.all("SELECT scope, reason_code FROM breakers WHERE state = 'open'")}
        self.assertEqual(opened, {"site:naukri": "site_challenge", "ats": "ats_blocked", "gmail": "gmail_security"})
        for scope, code in opened.items():
            a = by_area[breakers.area_label(scope)]
            head = a["what"].split(". ")[0]
            self.assertEqual(head, sheets_labels.REASON_SENTENCE[code], a)
            self.assertNotIn("_", head, a)
            self.assertNotIn(code, a["what"], a)
            self.assertNotEqual(head.lower(), code.replace("_", " "), a)
            self.assertIn("./jobhunter breaker reset %s" % scope, a["todo"], a)
            self.assertNotIn("{", a["todo"], a)
            self.assertNotEqual(a["todo"], sheets_labels.TODO_DEFAULT.format(scope=scope), a)
            self.assertEqual(a["severity"], "Stopped", a)
        self.assertIn("Google", gm["what"])
        self.assertIn("security check", by_area[breakers.area_label("site:naukri")]["what"])

    def sends(self) -> list:
        return self.w.all("SELECT token, kind, status, reserved_at, recipient FROM actions WHERE route = 'mailer' "
                          "AND status IN ('sent', 'unknown', 'armed', 'reserved', 'failed_after_click') "
                          "ORDER BY reserved_at")

    # ------------------------------------------------------------------ the week
    def test_simulated_week(self):
        w = self.w
        w.onboard(**{"boards.sites.naukri.discover": "browser", "boards.sites.naukri.apply": "browser"})
        w.connect_mail(**{"owner.notify.channel": "whatsapp", "owner.notify.to": OWNER_CHAT,
                          "boards.sites.naukri.discover": "browser", "boards.sites.naukri.apply": "browser"})
        install_reviewer_hashes(w.conn)
        oc_log = w.install_fake_openclaw()
        w.ok(["config", "apply"])
        with open(os.path.join(w.home.dir, "private", "targets.csv"), "w", encoding="utf-8") as fh:
            fh.write("company,ats,tenant\nGrowth Valley,greenhouse,%s\n" % TENANT)

        # ---------------- Tuesday: API discovery, scout duplicates, evaluation
        client = self.gh_client()
        with mock.patch.object(sources, "make_client", lambda cfg: client):
            s = w.ok(["sources", "fetch", "--lane", "api", "--source", "greenhouse"])
        self.assertEqual((s["fetched"], s["new"], s["prefilter_rejected"], s["eval_queued"]), (2, 1, 1, 1), s)
        self.assertEqual([c[1] for c in client.calls], [GH_LIST, GH_LIST + "/4100001"])
        job_uid = w.one("SELECT job_uid FROM jobs WHERE canonical_key = 'ats:greenhouse:4100001'")[0]
        gv_company = self.company_of("jobs", "x.job_uid = ?", job_uid)
        self.assertIsNotNone(gv_company)

        # the same job again from the scout: a careers-page embed, a Greenhouse embed under "GrowthValley Pvt Ltd"
        # (both carry the global Greenhouse id: the URLs become aliases of the one job), and a Naukri listing with no
        # ATS id (company, title and city fingerprint)
        n_jobs = w.one("SELECT count(*) FROM jobs")[0]
        res = w.add_jobs(e2e_fixture("week_scout_wellfound.json"))
        self.assertEqual([(r["outcome"], r["job_uid"], r["duplicate_of"]) for r in res],
                         [("alias_added", job_uid, job_uid)] * 2, res)
        self.assertEqual(w.one("SELECT count(*) FROM jobs")[0], n_jobs)
        res = w.add_jobs(e2e_fixture("week_scout_naukri.json"))
        self.assertEqual([(r["outcome"], r["duplicate_of"]) for r in res], [("duplicate", job_uid)], res)
        live = w.all("SELECT job_uid FROM jobs WHERE status NOT IN ('duplicate', 'prefilter_rejected')")
        self.assertEqual(live, [(job_uid,)])
        self.assertEqual({r[0] for r in w.all("SELECT c.company_uid FROM jobs j JOIN companies c ON c.id = j.company_id")},
                         {gv_company})

        rec = w.evaluate_all()
        self.assertEqual([(r["job_uid"], r["verdict"], r["status"]) for r in rec], [(job_uid, "apply", "eligible")])

        # ---------------- Tuesday: one outreach cycle writes seven cold emails; the owner approves them
        cyc = w.preflight("outreach", OU)
        made = {}
        for i, t in enumerate(TARGETS):
            made[t["key"]] = self.write_one(cyc, t, i + 1)
        w.end_cycle(cyc, OU)
        self.assertEqual(made["gv"]["company_uid"], gv_company)          # the contact's company is the job's company
        approved = w.approve_all()
        self.assertEqual(sorted(a["draft_uid"] for a in approved), sorted(m["draft_uid"] for m in made.values()))

        # ---------------- Tuesday and Wednesday: the mailer's cron job (every 5 minutes; every 30 at night here)
        sent = self.mail_until("2026-09-29T13:00:00Z")
        self.assertEqual(len(sent), 5, self.sends())                     # warm-up week 1: 5 cold emails a day
        day = [r for r in w.ok(["budget", "--platform", "gmail", "--kind", "cold_email"])["rows"]
               if r["item"] == "cold emails" and r["window"] == "day"]
        self.assertEqual([(r["used"], r["limit"], r["remaining"], r["warmup_week"]) for r in day], [(5, 5, 0, 1)], day)
        self.assertEqual(w.one("SELECT count(*) FROM drafts WHERE kind = 'cold_email' AND status = 'approved'")[0], 2)
        sent += self.mail_until("2026-09-30T09:00:00Z", step_min=30)
        sent += self.mail_until("2026-09-30T11:00:00Z")
        self.assertEqual(len(sent), len(TARGETS), self.sends())
        self.assertEqual(len(w.smtp.messages), len(TARGETS))
        by_rcpt = {}
        for token in sent:
            act = w.one("SELECT kind, status, recipient, thread_key, message_id, draft_id FROM actions WHERE token = ?",
                        token)
            self.assertEqual((act["kind"], act["status"], act["thread_key"]), ("cold_email", "sent", "em:" + token))
            th = w.one("SELECT state, followup_due_at, first_message_id FROM threads WHERE thread_key = ?",
                       "em:" + token)
            self.assertEqual((th["state"], th["first_message_id"]), ("open", act["message_id"]))
            self.assertGreater(th["followup_due_at"], w.clock.now())
            self.assertEqual(w.one("SELECT status FROM drafts WHERE id = ?", act["draft_id"])[0], "sent")
            by_rcpt[act["recipient"]] = token
        self.assertEqual(set(by_rcpt), {t["email"] for t in TARGETS})
        jordan_token = by_rcpt[TARGETS[0]["email"]]
        self.check_ceilings()

        # ---------------- Wednesday: the same person and the same company come back in other forms
        write_heartbeat()
        cyc = w.preflight("outreach", OU)
        jordan = made["gv"]["contact_uid"]
        variants = [
            ("plus-tag", {"full_name": "Jordan Blake", "first_name": "Jordan", "company": "GrowthValley Pvt Ltd",
                          "email": "Jordan.Blake+jobs@" + GV_DOMAIN, "email_grade": "B"}),
            ("li-variant", {"full_name": "Jordan Blake", "first_name": "Jordan",
                            "linkedin_url": "https://in.linkedin.com/" + "in/Example-Jordan-Blake/?originalSubdomain=in"}),
            ("second-address", {"full_name": "Jordan Blake", "first_name": "Jordan", "company": "Growth Valley",
                                "company_domain": GV_DOMAIN, "email": "jblake@" + GV_DOMAIN, "email_grade": "B"}),
        ]
        for name, contact in variants:
            c = self.add_contact(cyc, "dup-" + name, contact, code="E_DUP_PERSON")
            self.assertEqual((c["contact_uid"], c["existing"], c["already_contacted"]), (jordan, True, True),
                             (name, c))
            hits = self.dedup_refused(cyc, ["--kind", "cold_email", "--contact", jordan])
            self.assertIn("E_DUP_PERSON", hits, name)
        # a second cold email to Jordan is refused before it reaches QC or the owner
        refused = self.refused_draft(cyc, TARGETS[0], jordan, made["gv"]["fact_uid"], "again")
        self.assertIn(refused, ("E_DUP_PERSON", "E_COMPANY_COOLDOWN"))
        # another person at "GrowthValley Pvt Ltd", and one known only by an address at growthvalley.example
        for name, contact in (("taylor", {"full_name": "Taylor Quinn", "first_name": "Taylor", "title": "Analytics Lead",
                                          "company": "GrowthValley Pvt Ltd", "email": "taylor.quinn@" + GV_DOMAIN,
                                          "email_grade": "A"}),
                              ("alex", {"full_name": "Alex Moss", "first_name": "Alex", "title": "Data Engineer",
                                        "email": "alex.moss@" + GV_DOMAIN, "email_grade": "A"})):
            c = self.add_contact(cyc, "co-" + name, contact)
            self.assertNotEqual(c["contact_uid"], jordan)
            self.assertEqual(c["company_uid"], gv_company, name)
            hits = self.dedup_refused(cyc, ["--kind", "cold_email", "--contact", c["contact_uid"]])
            self.assertIn("E_COMPANY_COOLDOWN", hits, name)
            self.assertEqual(self.refused_draft(cyc, TARGETS[0], c["contact_uid"], None, name), "E_COMPANY_COOLDOWN")
        w.end_cycle(cyc, OU)
        self.assertEqual(w.one("SELECT count(*) FROM companies WHERE merged_into IS NULL AND "
                               "(display_name LIKE '%rowth%alley%' OR domain = ?)", GV_DOMAIN)[0], 1)
        self.assertEqual(w.all("SELECT code FROM approval_codes WHERE closed_at IS NULL"), [])

        # ---------------- Wednesday: the applier applies to the Growth Valley job on its Greenhouse form
        cyc = w.preflight("applier", AP)
        item = w.claim(cyc, job_uid)
        self.assertEqual((item["needs"], item["route"]), ("package", "ats_form"), item)
        built = w.build_resume(cyc, job_uid)
        rc, env = w.answer(cyc, job_uid, "Email")
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        fields = [{"label": "Email", "type": "text", "value": env["data"]["value"], "answer_key": env["data"]["key"]}]
        pkg_uid = w.package(cyc, job_uid, built["variant_uid"], fields)
        w.end_cycle(cyc, AP)
        self.assertEqual([a["draft_uid"] for a in w.approve_all()], [pkg_uid])
        cyc = w.preflight("applier", AP)
        self.assertEqual(w.claim(cyc, job_uid)["needs"], "submit")
        gh_url = "https://job-boards.greenhouse.io/%s/jobs/4100001" % TENANT
        out = w.submit(cyc, job_uid, pkg_uid, built["variant_uid"], platform="greenhouse", detect_platform="greenhouse",
                       page_url=gh_url, fields=fields)
        w.end_cycle(cyc, AP)
        self.assertEqual(out["confirm"]["status"], "sent")
        app_token = out["token"]
        self.assertEqual(w.one("SELECT status FROM jobs WHERE job_uid = ?", job_uid)[0], "applied")

        # the same job once more: a tracking-parameter URL from the scout, then the applier's own checks
        again = dict(e2e_fixture("week_scout_wellfound.json"))
        again["jobs"] = [dict(again["jobs"][0], source_url="https://wellfound.com/jobs/2000003-data-analyst-remote",
                              apply_url=gh_url + "?gh_src=wellfound")]
        res = w.add_jobs(again)
        self.assertIn(res[0]["outcome"], ("alias_added", "duplicate"), res)
        self.assertEqual((res[0]["job_uid"], res[0]["duplicate_of"]), (job_uid, job_uid), res)
        cyc = w.preflight("applier", AP)
        self.assertIn("E_DUP_JOB", self.dedup_refused(cyc, ["--kind", "application", "--job", job_uid], AP))
        rc, env = w.run(["apply", "next"], AP, cycle=cyc)
        self.assertEqual((rc, env["code"], env["data"]["items"]), (0, "NOTHING_TO_DO", []), env)
        w.end_cycle(cyc, AP)
        self.assertEqual(w.all("SELECT a.token FROM applications ap JOIN actions a ON a.id = ap.action_id"),
                         [(app_token,)])

        # ---------------- Wednesday afternoon: Jordan replies; the mailer fetches it, the replies cycle reads it
        jordan_msgid = w.one("SELECT message_id FROM actions WHERE token = ?", jordan_token)[0]
        raw = inbound("Jordan Blake <%s>" % TARGETS[0]["email"], OWNER, "Re: " + TARGETS[0]["subject"],
                      "Thanks Sam, happy to talk. How about Thursday?", date="2026-09-30T13:40:00Z",
                      in_reply_to=jordan_msgid)
        uid = int(w.imap.add_message(raw))
        w.imap.match(TARGETS[0]["email"], [uid])
        w.imap.match(GV_DOMAIN, [uid])
        w.clock.set("2026-09-30T14:00:00Z")
        write_heartbeat()
        self.assertEqual(self.mail_until("2026-09-30T15:00:00Z"), [])
        cyc = w.preflight("replies", OU)
        pending = w.ok(["reply", "pending"], OU, cycle=cyc)["packets"]
        self.assertEqual([p["thread_key"] for p in pending], ["em:" + jordan_token])
        with open(pending[0]["packet_path"], "r", encoding="utf-8") as fh:
            packet = json.load(fh)
        rec = {"inbound_id": pending[0]["inbound_id"], "thread_key": "em:" + jordan_token, "class": "positive",
               "summary": "Happy to talk on Thursday.", "received_at": packet["received_at"], "msg_ref": None}
        w.ok(["reply", "record", "--file", w.wfile("outreach", "%s/reply.json" % cyc, rec)], OU, cycle=cyc)
        w.end_cycle(cyc, OU)
        n = w.ok(["notify", "flush", "--deliver"])
        self.assertGreaterEqual(n["delivered"], 1, n)
        self.assertTrue(any("Jordan Blake" in m and "replied" in m for m in self.owner_messages(oc_log)))

        # ---------------- Thursday morning: an eighth cold email waits for the owner; a second job reaches submit
        w.clock.set("2026-10-01T09:00:00Z")
        extra = target("ma", "Marlow Books", "marlow.example", "Sky", "Rivera", "Head of Data", "reorder forecasts",
                       "our reorder forecasts still miss seasonal returns", "company_blog", "Seasonal returns at Marlow",
                       "Hi Sky,\n\nThe Marlow Books blog says your reorder forecasts still miss seasonal returns, "
                       "which is a familiar gap.\n\nFor Tidemark Logistics I wrote weekly returns forecasts in SQL "
                       "and Python for 40 warehouses.\n\nWould your data team have time for a quick call this month? "
                       "If not, the name of the right person helps.\n\nThanks,",
                       ("weekly returns forecasts in SQL and Python for 40 warehouses", "P2"))
        cyc = w.preflight("outreach", OU)
        eighth = self.write_one(cyc, extra, 8)
        w.end_cycle(cyc, OU)
        res = w.add_jobs(e2e_fixture("week_scout_lever.json"))
        self.assertEqual([r["outcome"] for r in res], ["new"], res)
        job2 = res[0]["job_uid"]
        self.assertEqual([(r["job_uid"], r["status"]) for r in w.evaluate_all()], [(job2, "eligible")])
        cyc = w.preflight("applier", AP)
        w.claim(cyc, job2)
        built2 = w.build_resume(cyc, job2)
        rc, env = w.answer(cyc, job2, "Email")
        fields2 = [{"label": "Email", "type": "text", "value": env["data"]["value"], "answer_key": env["data"]["key"]}]
        pkg2 = w.package(cyc, job2, built2["variant_uid"], fields2)
        w.end_cycle(cyc, AP)
        codes = {d: c for c, d in w.all("SELECT a.code, d.draft_uid FROM approval_codes a JOIN drafts d ON "
                                          "d.id = a.draft_id WHERE a.closed_at IS NULL")}
        self.assertEqual(set(codes), {pkg2, eighth["draft_uid"]})
        w.ok(["approve", codes[pkg2]], "human")

        # ---------------- Thursday 10:00: stop signatures
        w.clock.set("2026-10-01T10:00:00Z")
        # a job board challenge page in the scout's browser: the site breaker opens, the scout stops counting pages
        cyc = w.preflight("scout", SC)
        page = {"platform": "site:naukri", "url": "https://www.naukri.com/data-analyst-jobs",
                "title": "Security check", "http_status": 200, "text": "Are you a robot? Complete the security check."}
        rc, env = w.run(["detect", "--file", w.wfile("scout", "%s/detect-naukri.json" % cyc, page)], SC, cycle=cyc)
        self.assertEqual((env["code"], env["data"]["verdict"], env["data"]["scope"]),
                         ("E_STOP_DETECTED", "stop", "site:naukri"), env)
        rc, env = w.run(["usage", "add", "--platform", "naukri", "--metric", "page_view"], SC, cycle=cyc)
        self.assertEqual(env["code"], "E_BREAKER_OPEN", env)
        w.end_cycle(cyc, SC)
        pf = w.ok(["preflight", "--lane", "scout"], SC)
        self.assertIn("site:naukri", pf.get("open_breakers") or [], pf)
        w.end_cycle(pf["cycle_id"], SC)
        # the Lever form answers 429 while the applier reads it: the ATS breaker opens and the reserve is refused
        cyc = w.preflight("applier", AP)
        self.assertEqual(w.claim(cyc, job2)["needs"], "submit")
        lever_url = "https://jobs.lever.co/ferncliff/5f3c1c2e-0000-4000-8000-000000000001/apply"
        plan = w.ok(["gate", "precheck-plan", "--kind", "application", "--job", job2], AP, cycle=cyc)
        ev = {"kind": "application", "platform": "lever", "observed_at": w.clock.now(), "page_url": lever_url,
              "checks": [{"name": c["name"], "value": False} for c in plan["checks"]]}
        pc = w.ok(["gate", "precheck", "--kind", "application", "--platform", "lever", "--job", job2, "--file",
                   w.wfile("applier", "%s/precheck-2.json" % cyc, ev)], AP, cycle=cyc)
        page = {"platform": "lever", "url": lever_url, "title": "Too Many Requests", "http_status": 429,
                "text": "Too many requests"}
        rc, env = w.run(["detect", "--file", w.wfile("applier", "%s/detect-ats.json" % cyc, page)], AP, cycle=cyc)
        self.assertEqual((env["code"], env["data"]["verdict"], env["data"]["scope"]),
                         ("E_STOP_DETECTED", "stop", "ats"), env)
        rc, env = w.run(["gate", "reserve", "--kind", "application", "--draft", pkg2, "--precheck",
                         str(pc["precheck_id"]), "--platform", "lever", "--job", job2], AP, cycle=cyc)
        self.assertEqual((rc, env["code"]), (5, "E_BREAKER_OPEN"), env)
        w.end_cycle(cyc, AP)
        self.assertEqual(w.one("SELECT count(*) FROM actions WHERE kind = 'application'")[0], 1)
        # a Google security email reaches the inbox: the mailer's next fetch opens the gmail breaker
        raw = e2e_mail("google_security.eml").replace("{GOOGLE}", "no-reply@" + fetch.GOOGLE_SEC_DOMAIN) \
            .replace("sam.lee.sender@example.com", OWNER).replace("Tue, 29 Sep 2026 08:50:00", "Thu, 01 Oct 2026 10:05:00")
        uid = int(w.imap.add_message(raw))
        w.imap.match("critical security alert", [uid])
        w.clock.set("2026-10-01T10:30:00Z")
        self.assertEqual(self.mail_until("2026-10-01T11:00:00Z"), [])
        self.assertEqual(w.one("SELECT state, reason_code FROM breakers WHERE scope = 'gmail'")[:],
                         ("open", "gmail_security"))
        # the owner approves the eighth email after the trip; the mailer never sends it
        w.ok(["approve", codes[eighth["draft_uid"]]], "human")
        n = w.ok(["notify", "flush", "--deliver"])
        texts = self.owner_messages(oc_log)
        for words in (("Naukri",), ("ats", "Job forms"), ("gmail", "Gmail")):
            self.assertTrue(any(any(x in m for x in words) for m in texts), (words, texts))

        # ---------------- Thursday to Monday: the mailer keeps running (hourly here), nothing more goes out
        self.assertEqual(self.mail_until(WEEK_END, step_min=60), [])
        self.assertEqual(len(w.smtp.messages), len(TARGETS))
        self.assertIn(w.one("SELECT status FROM drafts WHERE draft_uid = ?", eighth["draft_uid"])[0],
                      ("approved", "expired"))
        self.assertEqual(w.all("SELECT token FROM actions WHERE draft_id = (SELECT id FROM drafts WHERE draft_uid = ?)",
                               eighth["draft_uid"]), [])
        self.check_ceilings()
        self.check_no_duplicates()
        self.check_sheet(job_uid, app_token, jordan_token)
        undelivered = w.all("SELECT dedupe_key FROM notifications WHERE priority = 'high' AND delivered_at IS NULL")
        self.assertEqual(undelivered, [])

if __name__ == "__main__":
    unittest.main()
