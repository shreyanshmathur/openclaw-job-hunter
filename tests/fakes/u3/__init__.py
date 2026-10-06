"""U3 test fakes: profile facts (U4 profile.facts), the reviewer turn (qc.agent_turn) and the one-shot cron run
under it (U1 ocrun.qc_turn), no worker spawn, reviewer hashes as `install render-workspaces` (U7) would write
them, agent calls with the guard's identity proofs (agent_call), and a fictional outreach scenario.

    from tests.fakes.u3 import QCTestCase

Everything is fictional (Kestrel Commerce, Alex Rivera, example.com) and lives in the temp home.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys

import tests  # noqa: F401
from jobhunter import canon, db, paths
from jobhunter import qc as qcpkg
from tests import helpers
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


# The verdict-file line of an F-QC turn (jobhunter.qc.verdict_file_line).
VERDICT_LINE_RE = re.compile(r"write your complete answer .*? to the file (\S+) with the write tool")


class FakeQcTurn:
    """ocrun.qc_turn stand-in (CLI-ROUTE-DESIGN 6.3.2): (session_key, message_file, timeout_s) -> {ok, text, raw}.

    answer(session_key, message_file, timeout_s) -> str is what the reviewer answers. run_text False plays V13
    failing (the run finishes but its record carries no text). When the message ends with the verdict-file line
    and write_file is True, the answer goes to that file and the reply is DONE, as the reviewer does in F-QC mode.
    ok False is a failed run (gateway not reachable). run_cap N plays OpenClaw 2026.9.8 (D13): a run-record text
    longer than N characters keeps its first N and gets cut_mark (U+2026 by default; "" for a cut without one)."""

    def __init__(self, answer, run_text: bool = True, write_file: bool = True, ok: bool = True,
                 run_cap: int | None = None, cut_mark: str = "\u2026"):
        self.answer = answer
        self.run_text = run_text
        self.write_file = write_file
        self.ok = ok
        self.run_cap = run_cap
        self.cut_mark = cut_mark
        self.calls = []

    def __call__(self, session_key, message_file, timeout_s):
        with open(message_file, "r", encoding="utf-8") as fh:
            message = fh.read()
        m = VERDICT_LINE_RE.search(message)
        self.calls.append({"session_key": session_key, "file": message_file, "message": message,
                           "verdict_file": m.group(1) if m else None, "timeout_s": timeout_s})
        if not self.ok:
            return {"ok": False, "text": None, "raw": "", "error": "gateway not reachable"}
        text = self.answer(session_key, message_file, timeout_s)
        if m and self.write_file:
            with open(m.group(1), "w", encoding="utf-8") as fh:
                fh.write(text)
            text = "DONE"
        if self.run_cap is not None and len(text) > self.run_cap:
            text = text[:self.run_cap] + self.cut_mark
        return {"ok": True, "text": text if self.run_text else "", "raw": json.dumps({"status": "ok"})}


def reviewer_answer(mode: str = "pass"):
    """An `answer` for FakeQcTurn: the FakeReviewer reply text for the packet."""
    rev = FakeReviewer(mode)
    return lambda session_key, message_file, timeout_s: rev("jobhunter-qc", session_key, message_file,
                                                            timeout_s)["text"]


def agent_call(home, agent: str, argv: list) -> tuple:
    """(argv, env) of one call by a jobhunter agent as the guard makes it (CLI-ROUTE-DESIGN 5): a fresh argv
    proof in front of the command and a fresh env proof, both for one session. tests.helpers.agent_argv and
    agent_env (U1) when they are there; else the same proofs minted with jobhunter.auth (test key in the temp
    home); before proof version 2, the older guard environment."""
    from jobhunter import auth
    session = "agent:%s:test" % agent
    if hasattr(helpers, "agent_env") and hasattr(helpers, "agent_argv"):
        return (list(helpers.agent_argv(paths.root(), agent, list(argv), session=session)),
                dict(helpers.agent_env(paths.root(), agent, session=session)))
    if hasattr(auth, "argv_proof") and hasattr(auth, "env_proof"):
        auth.create_guard_key()
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": agent, "JH_SESSION_KEY": session,
               "JH_RUN_ID": "run-u3-test", "JH_AGENT_PROOF": auth.env_proof(agent, session_key=session)}
        return ["--agent-proof", auth.argv_proof(agent, list(argv), session_key=session)] + list(argv), env
    return list(argv), {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": agent}


class _IsolatedFlags:
    """sys.flags as `python -I` sets them (the guard always starts jh.py that way); other flags are real."""
    isolated = 1
    ignore_environment = 1
    no_user_site = 1

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def isolated_flags():
    real = sys.flags
    sys.flags = _IsolatedFlags(real)
    try:
        yield
    finally:
        sys.flags = real


def agent_cli(home, agent: str, argv: list, modules: list) -> tuple:
    """(exit code, envelope) of one in-process jh.py call by `agent` (agent_call), under the flags of python -I."""
    from jobhunter import cli
    argv, env = agent_call(home, agent, argv)
    out = io.StringIO()
    with isolated_flags():
        rc = cli.main(argv, env=env, stdin=io.StringIO(""), stdout=out, modules=modules)
    return rc, json.loads(out.getvalue())


_CHILD = """
import io, json, sys
spec = json.loads(sys.stdin.read())
sys.path[:0] = [spec["scripts"]]
from jobhunter import canon, paths
paths.use_test_home(spec["root"])
canon.set_test_clock(spec["now"])
from jobhunter import cli
from jobhunter.commands import drafts, qc
out = io.StringIO()
rc = cli.main(spec["argv"], env=spec["env"], stdin=io.StringIO(""), stdout=out, modules=[drafts, qc])
sys.stdout.write(json.dumps({"rc": rc, "out": out.getvalue(), "isolated": sys.flags.isolated}))
"""


def agent_cli_child(home, agent: str, argv: list, isolated: bool = True) -> tuple:
    """(exit code, envelope, child's sys.flags.isolated) of one jh.py call by `agent` (agent_call) in a real child
    interpreter, `python -I` as the guard starts it (isolated False: plain python), on this test home and clock."""
    import subprocess
    argv, env = agent_call(home, agent, argv)
    spec = {"scripts": paths.SCRIPTS_DIR, "root": paths.root(), "now": canon.now(), "argv": argv, "env": env}
    cmd = [sys.executable] + (["-I"] if isolated else []) + ["-c", _CHILD]
    proc = subprocess.run(cmd, input=json.dumps(spec).encode("utf-8"), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=paths.root(), env={"PATH": "/usr/bin:/bin"}, timeout=120)
    if proc.returncode != 0:
        raise AssertionError("child failed: %s" % proc.stderr.decode("utf-8", "replace")[-2000:])
    res = json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])
    return res["rc"], json.loads(res["out"]), res["isolated"]


def set_cli_route(**values) -> None:
    """Merge values into private/home.json cli_route (what install writes)."""
    h = paths.home()
    route = dict(h.get("cli_route") or {})
    route.update(values)
    h["cli_route"] = route
    with open(paths.home_file(), "w", encoding="utf-8") as fh:
        json.dump(h, fh, indent=1, sort_keys=True)


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
