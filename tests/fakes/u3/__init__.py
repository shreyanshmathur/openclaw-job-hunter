"""U3 test fakes: profile facts (U4 profile.facts), the reviewer turn (U1 ocrun.agent_turn), no worker spawn,
reviewer hashes as `install render-workspaces` (U7) would write them, and a fictional outreach scenario.

    from tests.fakes.u3 import QCTestCase

Everything is fictional (Kestrel Commerce, Alex Rivera, example.com) and lives in the temp home.
"""
from __future__ import annotations

import json
import os
import re

import tests  # noqa: F401
from jobhunter import canon, db, paths
from jobhunter import qc as qcpkg
from tests.helpers import HomeTestCase, insert_company, insert_contact, insert_job

PROFILE = {
    "P1": "Built the return-to-origin (RTO) risk model for COD orders at Tidemark Logistics; RTO fell from 18% to 13% "
          "over two quarters.",
    "P2": "4 years of analytics experience at Tidemark Logistics.",
    "P3": "Wrote a one-page note on pincode-level feature selection, published at https://example.com/rto-notes",
}
SNIPPET = "pincode-level models beat our city-level RTO model last quarter"

GOOD_BODY = ("Hi Alex,\n\nYour post on 18 September said pincode-level models beat Kestrel's city-level RTO model. "
             "We saw the same pattern at Tidemark Logistics.\n\nI built Tidemark's return-risk model for COD orders, "
             "and RTO fell from 18% to 13% over two quarters. That is close to the returns forecasting work in your "
             "Data Analyst opening.\n\nWould a 15 minute call next week be useful? If someone else owns this hire, a "
             "name is plenty.\n\nThanks,")


def fake_profile_facts(facts: dict | None = None):
    data = dict(PROFILE if facts is None else facts)
    return lambda: dict(data)


class FakeReviewer:
    """agent_turn stand-in. mode: pass | fail | soft_fail | untruthful | garbage | wrong_nonce | error | timeout.
    It reads the packet the worker wrote (nonce and draft sha) and answers like the reviewer would."""

    def __init__(self, mode: str = "pass", modes: list | None = None):
        self.mode = mode
        self.modes = list(modes or [])
        self.calls = []

    def verdict(self, nonce: str, sha: str, mode: str) -> dict:
        gates = {"truthful": True, "hook_verified": True, "swap_test": True, "no_ai_voice": True, "safe": True}
        scores = {"specificity": 5, "value": 4, "human_voice": 5, "clarity": 5, "cta": 5, "tone_fit": 4,
                  "channel_fit": 5}
        claims = [{"quote": "RTO fell from 18% to 13%", "fact_id": "P1", "supported": True, "note": ""}]
        verdict = "pass"
        issues = []
        if mode == "fail":
            scores["specificity"] = 2
            gates["swap_test"] = False
            verdict = "fail"
            issues = [{"severity": "major", "quote": "Would a 15 minute call", "problem": "generic", "fix": "tie it"}]
        elif mode == "soft_fail":
            gates["no_ai_voice"] = False
            verdict = "fail"
            issues = [{"severity": "minor", "quote": "a name is plenty", "problem": "stock phrase", "fix": "reword"}]
        elif mode == "untruthful":
            claims[0]["supported"] = False     # the model still says pass: code must fail it
        w = round(0.25 * scores["specificity"] + 0.2 * scores["value"] + 0.2 * scores["human_voice"] +
                  0.1 * scores["clarity"] + 0.1 * scores["cta"] + 0.1 * scores["tone_fit"] +
                  0.05 * scores["channel_fit"], 2)
        return {"nonce": nonce, "draft_sha256": sha, "verdict": verdict, "gates": gates, "scores": scores,
                "weighted_score": w, "claims": claims,
                "hook": {"quote": "pincode-level models", "fact_id": "R1", "accurate": True, "note": ""},
                "ai_tells": [], "issues": issues, "rewrite_brief": "" if verdict == "pass" else "be specific",
                "confidence": 0.8}

    def __call__(self, agent, session_key, message_file, timeout_s):
        mode = self.modes.pop(0) if self.modes else self.mode
        self.calls.append({"agent": agent, "session_key": session_key, "file": message_file, "mode": mode})
        with open(message_file, "r", encoding="utf-8") as fh:
            packet = fh.read()
        nonce = re.search(r"<nonce>([0-9a-f]+)</nonce>", packet).group(1)
        sha = re.search(r"<draft_sha256>([0-9a-f]+)</draft_sha256>", packet).group(1)
        if mode == "error":
            return {"ok": False, "error": "gateway not reachable", "text": "", "raw": ""}
        if mode == "timeout":
            return {"ok": False, "error": "timeout after %ss" % timeout_s, "timeout": True, "text": "", "raw": ""}
        if mode == "garbage":
            return {"ok": True, "text": "I think it is fine. {not json", "raw": ""}
        if mode == "wrong_nonce":
            return {"ok": True, "text": json.dumps(self.verdict("0" * 16, sha, "pass")), "raw": ""}
        body = json.dumps(self.verdict(nonce, sha, mode), indent=1)
        return {"ok": True, "text": "Here is my review.\n```json\n%s\n```\n" % body, "raw": ""}


def install_reviewer_hashes(conn) -> None:
    """What `install render-workspaces` records: sha256 of prompts/reviewer.md and of WS/qc/AGENTS.md."""
    from jobhunter.qc import review
    path = review.agents_md_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("fictional rendered QC agent instructions for tests\n")
    with db.tx(conn):
        db.meta_set(conn, "reviewer_prompt_sha256", canon.sha256_file(qcpkg.REVIEWER_PROMPT_FILE), "install")
        db.meta_set(conn, "qc_agents_md_sha256", canon.sha256_file(path), "install")


class QCTestCase(HomeTestCase):
    """HomeTestCase plus the U3 fakes and one fictional target: Kestrel Commerce, Alex Rivera, a job, a fact."""
    reviewer_mode = "pass"

    def setUp(self):
        super().setUp()
        self.spawned = []
        qcpkg.PROFILE_FACTS = fake_profile_facts()
        self.reviewer = FakeReviewer(self.reviewer_mode)
        qcpkg.AGENT_TURN = self.reviewer
        qcpkg.SPAWN = self.spawned.append
        install_reviewer_hashes(self.conn)
        with db.tx(self.conn):
            self.company_id = insert_company(self.conn, name="Kestrel Commerce", domain="kestrel.example")
            self.contact_id = insert_contact(self.conn, company_id=self.company_id, full_name="Alex Rivera",
                                             email="alex.rivera@kestrel.example")
            self.job_id = insert_job(self.conn, company_id=self.company_id, title="Data Analyst", status="apply_queued")
            self.fact_uid = canon.new_uid("R")
            self.conn.execute(
                "INSERT INTO research_facts (fact_uid, subject_kind, subject_id, text, snippet, source_type, source_url,"
                " published_at, retrieved_at, created_at) VALUES (?, 'person', ?, ?, ?, 'linkedin_post', ?, ?, ?, ?)",
                (self.fact_uid, self.contact_id, "LinkedIn post by Alex Rivera, 2026-09-18: " + SNIPPET, SNIPPET,
                 "https://www.linkedin.com/posts/example-person-activity-1", "2026-09-18", "2026-09-26", canon.now()))
        self.contact_uid = self.conn.execute("SELECT contact_uid FROM contacts WHERE id = ?",
                                             (self.contact_id,)).fetchone()[0]
        self.job_uid = self.conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (self.job_id,)).fetchone()[0]

    def tearDown(self):
        qcpkg.PROFILE_FACTS = None
        qcpkg.AGENT_TURN = None
        qcpkg.SPAWN = None
        super().tearDown()

    # ------------------------------------------------------------ helpers
    def draft_file(self, **over) -> dict:
        d = {"kind": "cold_email", "channel": "email_cold", "contact_uid": self.contact_uid,
             "subject": "Pincode-level RTO models", "body": GOOD_BODY,
             "hook": {"anchor": "pincode-level models", "source_type": "linkedin_post",
                      "source_url": "https://www.linkedin.com/posts/example-person-activity-1", "snippet": SNIPPET,
                      "published_at": "2026-09-18", "retrieved_at": "2026-09-26", "fact_id": self.fact_uid},
             "claims": [{"text": "RTO fell from 18% to 13% over two quarters", "fact_id": "P1"}], "links": []}
        d.update(over)
        return d

    def create(self, **over) -> dict:
        from jobhunter import drafts
        from jobhunter.auth import Caller as _FallbackCaller
        with db.tx(self.conn):
            return drafts.create_draft(self.conn, self.draft_file(**over), None,
                                       _FallbackCaller("agent", "jobhunter-outreach"))

    def row(self, draft_uid: str):
        return self.conn.execute("SELECT * FROM drafts WHERE draft_uid = ?", (draft_uid,)).fetchone()

    def start_review(self, draft_uid: str) -> dict:
        from jobhunter.qc import review
        with db.tx(self.conn):
            return review.start(self.conn, draft_uid)

    def run_review(self, draft_uid: str) -> dict:
        from jobhunter.qc import worker
        job = self.start_review(draft_uid)
        return worker.run_job(job["qjob_uid"])

    def set_auto(self) -> None:
        with db.tx(self.conn):
            db.meta_set(self.conn, "approval_mode", "auto", "human")

    def write_config(self, cfg: dict) -> None:
        with open(os.path.join(paths.private_dir(), "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
