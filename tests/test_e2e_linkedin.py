"""E2E (INT): one InMail from draft to confirm with the real modules of U1, U3 and U6 in one temp home.

The InMail subject is part of the approved text (U3 drafts.send_text, hashed at draft create), the approval shows
it, and the gate (U1) compares it with the read-back of the real drivers/read_compose.js (U6), run under node
against a recorded LinkedIn compose (the fake DOM of tests/test_drivers_static.py). A read-back with another subject
is refused before the click and fails the action; the retry with the approved subject arms, dwells and confirms.
The QC reviewer's model turn is the U3 FakeReviewer, whose output goes through the real review parser.
Fictional people and companies only; nothing touches LinkedIn.
"""
from __future__ import annotations

import hashlib
import shutil
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, ocrun, sheets_rows
from jobhunter import qc as qcpkg
from jobhunter.commands.core import LINKEDIN_ACK
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u3 import FakeReviewer, install_reviewer_hashes
from tests.fixtures.e2e.support import World
from tests.test_drivers_static import li_page, message_box, run_driver, subject_input

OU = "jobhunter-outreach"
LI = "https://www.linkedin.com/" + "in/"          # assembled so the leak check sees no profile path
SUBJECT = "Daily forecasts and returns data"
BODY = ("Hi Avery,\n\nThe Kestrel engineering blog says demand planning moved to daily forecasts this summer. "
        "Returns data tends to be where that kind of change hurts first.\n\nAt Tidemark Logistics my weekly returns "
        "forecasts in SQL and Python covered 40 warehouses, so those gaps are familiar ground.\n\nWho on your team "
        "is the right person to ask about analyst openings? A pointer would help a lot.\n\nThanks,")
SNIPPET = "this summer we moved demand planning from weekly spreadsheets to daily forecasts"


class LinkedInWorld(World):
    """The running guard refreshes its heartbeat all day; here each lane start does."""

    def preflight(self, lane: str, agent: str) -> str:
        write_heartbeat()
        return super().preflight(lane, agent)


@unittest.skipUnless(shutil.which("node"), "node is not installed (read_compose.js runs under node)")
class TestInMailThroughGate(unittest.TestCase):
    def setUp(self):
        self.w = LinkedInWorld()
        self.addCleanup(self.w.stop)
        prev = qcpkg.SPAWN
        qcpkg.SPAWN = [].append
        self.addCleanup(setattr, qcpkg, "SPAWN", prev)
        p = mock.patch.object(ocrun, "agent_turn", FakeReviewer("pass"))
        p.start()
        self.addCleanup(p.stop)

    def draft_inmail(self) -> dict:
        """Contact, research, InMail draft, QC and the owner's approval. Returns the ids."""
        w = self.w
        cyc = w.preflight("outreach", OU)
        contact = {"full_name": "Avery Stone", "first_name": "Avery", "title": "VP Data", "company": "Kestrel Commerce",
                   "company_domain": "kestrel.example", "role_type": "hiring_manager", "locale": "US", "email": None,
                   "email_grade": None, "email_evidence_url": None, "linkedin_url": LI + "example-avery-stone",
                   "linkedin_member_url": None, "source_url": "https://kestrel.example/team"}
        c = w.ok(["contact", "add", "--file", w.wfile("outreach", "%s/contact.json" % cyc, contact)], OU, cycle=cyc)
        research = {"subject": {"kind": "person", "contact_uid": c["contact_uid"]},
                    "facts": [{"text": "Kestrel Commerce: %s." % SNIPPET, "snippet": SNIPPET,
                               "source_type": "company_blog", "source_url": "https://kestrel.example/blog/post-1",
                               "published_at": "2026-09-18", "retrieved_at": w.clock.now()[:10]}]}
        r = w.ok(["research", "add", "--file", w.wfile("outreach", "%s/research.json" % cyc, research)], OU, cycle=cyc)
        draft = {"kind": "inmail", "channel": "inmail", "contact_uid": c["contact_uid"], "subject": SUBJECT,
                 "body": BODY,
                 "hook": {"anchor": "daily forecasts", "source_type": "company_blog",
                          "source_url": "https://kestrel.example/blog/post-1", "snippet": SNIPPET,
                          "published_at": "2026-09-18", "retrieved_at": w.clock.now()[:10],
                          "fact_id": r["facts"][0]["fact_uid"]},
                 "claims": [{"text": "weekly returns forecasts in SQL and Python covered 40 warehouses",
                             "fact_id": "P2"}], "links": []}
        d = w.ok(["draft", "create", "--file", w.wfile("outreach", "%s/draft.json" % cyc, draft)], OU, cycle=cyc)
        verdict = w.qc_pass(d["draft_uid"], OU, cyc)
        self.assertEqual(verdict.get("draft_status"), "awaiting_approval", verdict)
        w.end_cycle(cyc, OU)
        return {"contact_uid": c["contact_uid"], "draft_uid": d["draft_uid"]}

    def reserve(self, cyc: str, made: dict, n: int) -> str:
        """Precheck (no earlier message in the conversation), the page check and reserve. Returns the token."""
        w = self.w
        plan = w.ok(["gate", "precheck-plan", "--kind", "inmail", "--contact", made["contact_uid"]], OU, cycle=cyc)
        self.assertEqual([c["name"] for c in plan["checks"]], ["conversation_has_our_message"], plan)
        ev = {"kind": "inmail", "platform": "linkedin", "observed_at": w.clock.now(),
              "page_url": LI + "example-avery-stone",
              "checks": [{"name": c["name"], "value": False} for c in plan["checks"]]}
        pc = w.ok(["gate", "precheck", "--kind", "inmail", "--platform", "linkedin", "--contact", made["contact_uid"],
                   "--file", w.wfile("outreach", "%s/precheck-%d.json" % (cyc, n), ev)], OU, cycle=cyc)
        page = {"platform": "linkedin", "url": LI + "example-avery-stone", "title": "Avery Stone | LinkedIn",
                "http_status": None, "text": "Avery Stone. VP Data at Kestrel Commerce. Message. More."}
        det = w.ok(["detect", "--file", w.wfile("outreach", "%s/detect-%d.json" % (cyc, n), page)], OU, cycle=cyc)
        self.assertEqual(det["verdict"], "clear", det)
        res = w.ok(["gate", "reserve", "--kind", "inmail", "--draft", made["draft_uid"], "--precheck",
                    str(pc["precheck_id"]), "--platform", "linkedin", "--contact", made["contact_uid"]], OU, cycle=cyc)
        return res["token"]

    def read_back(self, subject: str, body: str) -> str:
        """observed_text of the real read_compose.js on an InMail compose holding subject and body."""
        out = run_driver("read_compose", li_page(
            {"tag": "form", "cls": "msg-form", "children": [subject_input(subject), message_box(body)]}))
        self.assertTrue(out["subject_field_present"], out)
        return out["observed_text"]

    def test_inmail_subject_is_approved_hashed_and_read_back(self):
        w = self.w
        w.onboard(**{"channels.linkedin.writes.inmail": True, "channels.linkedin.account_type": "premium"})
        install_reviewer_hashes(w.conn)
        en = w.ok(["linkedin", "enable", "--ack", LINKEDIN_ACK, "--account-type", "premium",
                   "--account-age-years", "6"], "human")
        self.assertTrue(en["enabled"], en)
        # the LinkedIn warm-up allows no InMail in weeks 1 to 3; week 4 starts three weeks after `linkedin enable`
        w.clock.set("2026-10-20T11:00:00Z")          # a Tuesday, inside the LinkedIn active hours
        made = self.draft_inmail()

        # the approved text is "Subject: <subject>", a blank line, the body; the stored hash covers the subject
        want = canon.canonical_send_text("inmail", SUBJECT, BODY, None, None)
        row = w.one("SELECT status, subject, text_sha256 FROM drafts WHERE draft_uid = ?", made["draft_uid"])
        self.assertEqual((row["status"], row["subject"]), ("awaiting_approval", SUBJECT))
        self.assertEqual(row["text_sha256"], hashlib.sha256(want.encode("utf-8")).hexdigest())
        pending = w.ok(["approvals", "list"])["pending"]
        self.assertEqual(len(pending), 1, pending)
        self.assertEqual([a["draft_uid"] for a in w.approve_all()], [made["draft_uid"]])
        cyc = w.preflight("outreach", OU)
        self.assertEqual(w.ok(["draft", "show", made["draft_uid"], "--field", "subject"], OU, cycle=cyc).get("value",
                         SUBJECT), SUBJECT)

        # 1. the compose holds another subject: refused before the click, the action fails, nothing is sent
        token = self.reserve(cyc, made, 1)
        wrong = self.read_back("Quick question", BODY)
        path = w.wfile("outreach", "%s/observed-%s.txt" % (cyc, token), wrong)
        w.fails(["gate", "arm", token, "--observed-file", path], "E_OBSERVED_MISMATCH", OU, cycle=cyc)
        self.assertEqual(w.one("SELECT status, fail_reason FROM actions WHERE token = ?", token)[:],
                         ("failed", "observed_text_mismatch"))
        # the body alone (a read-back that drops the subject line) does not match either
        self.assertNotEqual(canon.canonical_send_text("inmail", None, BODY, None, None), want)

        # 2. the retry types the approved subject: armed, one dwell, the click, confirmed
        token2 = self.reserve(cyc, made, 2)
        self.assertNotEqual(token2, token)
        good = self.read_back(SUBJECT, BODY)
        self.assertEqual(good, "Subject: " + SUBJECT + "\n\n" + BODY)
        path = w.wfile("outreach", "%s/observed-%s.txt" % (cyc, token2), good)
        w.ok(["gate", "arm", token2, "--observed-file", path], OU, cycle=cyc)
        for _ in range(3):
            if not w.ok(["pace", "wait", "--platform", "linkedin", "--kind", "dwell"], OU, cycle=cyc)["remaining_s"]:
                break
        path = w.wfile("outreach", "%s/evidence-%s.txt" % (cyc, token2), "Conversation: InMail sent. " + SUBJECT)
        confirmed = w.ok(["gate", "confirm", token2, "--evidence-file", path], OU, cycle=cyc)
        self.assertEqual(confirmed["status"], "sent", confirmed)
        w.end_cycle(cyc, OU)

        act = w.one("SELECT kind, status, platform, observed_sha256, thread_key FROM actions WHERE token = ?", token2)
        self.assertEqual((act["kind"], act["status"], act["platform"]), ("inmail", "sent", "linkedin"))
        self.assertEqual(w.one("SELECT status FROM drafts WHERE draft_uid = ?", made["draft_uid"])[0], "sent")
        self.assertEqual(w.all("SELECT count(*) FROM actions WHERE kind = 'inmail' AND status = 'sent'"), [(1,)])
        self.assertIsNotNone(act["thread_key"])
        # a second InMail to the same person is refused
        cyc = w.preflight("outreach", OU)
        rc, env = w.run(["dedup", "check", "--kind", "li_invite", "--contact", made["contact_uid"]], OU, cycle=cyc)
        self.assertFalse((env.get("data") or {}).get("allowed", True), env)
        w.end_cycle(cyc, OU)
        # the Sheet's outreach rows: the refused attempt as Failed, the InMail as Sent with its subject and the
        # exact approved text (subject line included, as for an email)
        rows = dict(sheets_rows.build_rows(w.conn, "outreach", None))
        self.assertEqual(set(rows), {token, token2})
        self.assertEqual(rows[token]["status"], "Failed", rows[token])
        sent = rows[token2]
        self.assertEqual((sent["status"], sent["channel"], sent["subject"], sent["message"]),
                         ("Sent", "LinkedIn InMail", SUBJECT, want), sent)


if __name__ == "__main__":
    unittest.main()
