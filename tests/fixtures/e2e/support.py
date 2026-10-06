"""Shared driver for the end-to-end tests (INT, tests/test_e2e_*.py). Not a test module.

Everything runs the real modules of every unit in a temp home (tests.helpers.TempHome): the `jh` CLI is called
in process through cli.main with the caller class a real call would have: system, the owner with --pin-stdin, or
a jobhunter agent exactly as the guard runs it (CLI-ROUTE-DESIGN 5: `--agent-proof <T>` in front of the
arguments and a JH_AGENT_PROOF env proof, both single use and for one session, under the flags of `python -I`;
tests.helpers.agent_argv, agent_env and as_isolated). Only three things are replaced, each at the seam the owning
unit provides for tests: the QC reviewer's one-shot cron run (ocrun.qc_turn, tests/fakes/u3 FakeQcTurn answering
with a FakeReviewer, so the real qc.agent_turn and review parser run), the detached QC worker spawn (qc.SPAWN)
and, where a test says so, the mail transport (U9 fake SMTP and IMAP servers) and the openclaw binary (a fake
script in this folder). Fictional people and companies only.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
from unittest import mock

import tests  # noqa: F401  (puts scripts/ on sys.path)
from jobhunter import auth, canon, cli, db, pacing, paths
from tests import helpers
from tests.helpers import TempHome

REPO = paths.REPO
E2E = os.path.dirname(os.path.abspath(__file__))
PROFILE_FIX = os.path.join(REPO, "tests", "fixtures", "profile")
PIN = "482915"
AP = "jobhunter-applier"
START = "2026-09-29T09:00:00Z"          # a Tuesday; the temp home runs in UTC
OWNER_CHAT = "+15555550100"          # fictional 555-01xx chat number; not the shipped example number

# U4 onboarding: the answers the interview needs before the profile counts as confirmed
REQUIRED_ANSWERS = [
    ("Q1", "Data analytics: Data Analyst, Business Analyst\nOperations analytics: Operations Analyst"),
    ("Q2", "intern, director"),
    ("Q3", "associate to senior"),
    ("Q4", "3"),
    ("Q5", "USD per year"),
    ("Q6", "60k"),
    ("Q7", "72000"),
    ("Q8", "yes"),
    ("Q10", "30 days"),
    ("Q11", "Springfield, remote"),
    ("Q12", "remote, hybrid"),
    ("Q14", "US: citizen; other: needs_sponsorship"),
]


def load_json(*parts):
    with open(os.path.join(*parts), "r", encoding="utf-8") as fh:
        return json.load(fh)


def e2e_fixture(name: str):
    return load_json(E2E, name)


class CliError(AssertionError):
    pass


# ---------------------------------------------------------------- jh.py as OpenClaw's exec tool starts it
# The child plays `<PY> -I <REPO>/scripts/jh.py <args>`: a python -I interpreter (unless isolated=False) whose
# whole environment is the one given, running jh.py's cli.main against the current test home and test clock.
# It reports every caller classification (class and agent id, or the refusal code) so a test can prove that no
# agent call was ever classified `system`.
_EXEC_CHILD = r"""
import io, json, sys
spec = json.loads(sys.stdin.read())
sys.path.insert(0, spec["scripts"])
from jobhunter import auth, canon, paths
paths.use_test_home(spec["root"])
canon.set_test_clock(spec["now"])
seen = []
markers = []
_real = auth.classify
def _classify(*a, **k):
    try:
        c = _real(*a, **k)
    except BaseException as exc:
        seen.append({"refused": getattr(exc, "code", type(exc).__name__)})
        raise
    seen.append({"class": c.cls, "agent_id": c.agent_id})
    markers.append(list((getattr(c, "detail", None) or {}).get("markers") or []))
    return c
auth.classify = _classify
from jobhunter import cli
out = io.StringIO()
rc = cli.main(spec["argv"], stdin=io.StringIO(""), stdout=out)
sys.stdout.write(json.dumps({"rc": rc, "out": out.getvalue(), "isolated": sys.flags.isolated, "classify": seen,
                            "markers": markers}))
"""
CHILD_PATH = "/usr/bin:/bin"


def run_jh_child(argv, env: dict, cwd: str | None = None, isolated: bool = True, timeout: int = 120) -> dict:
    """One jh.py call in a child interpreter: {rc, envelope, isolated, classify, markers} (classify: one
    {class, agent_id} or {refused: code} per classification; markers: the harness marker names of each accepted
    one). env is the child's whole environment (PATH added when missing); cwd defaults to the install root."""
    child_env = {str(k): str(v) for k, v in (env or {}).items()}
    child_env.setdefault("PATH", CHILD_PATH)
    spec = {"scripts": os.path.join(REPO, "scripts"), "root": paths.root(), "now": canon.now(),
            "argv": [str(t) for t in argv]}
    cmd = [sys.executable] + (["-I"] if isolated else []) + ["-c", _EXEC_CHILD]
    if cwd:
        os.makedirs(cwd, exist_ok=True)
    p = subprocess.run(cmd, input=json.dumps(spec), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=child_env, cwd=cwd or paths.root(), universal_newlines=True, timeout=timeout)
    try:
        res = json.loads(p.stdout)
    except ValueError:
        raise AssertionError("jh child failed (exit %s): %s %s" % (p.returncode, p.stdout[-2000:], p.stderr[-2000:]))
    text = (res.get("out") or "").strip()
    try:
        envelope = json.loads(text.splitlines()[-1]) if text else {}
    except ValueError:
        envelope = {"raw": text[-2000:]}
    return {"rc": res["rc"], "envelope": envelope, "isolated": res["isolated"], "classify": res["classify"],
            "markers": res["markers"]}


def split_rewritten(command: str, carriers=("argv", "env")) -> list:
    """The jh.py arguments of a guard-rewritten exec command, `<PY> -I <REPO>/scripts/jh.py [--agent-proof <T>]
    <args>` (the proof pair only with the argv carrier). AssertionError for any other shape."""
    toks = shlex.split(command)
    h = paths.home()
    want = [h["python"], "-I", os.path.join(REPO, "scripts", "jh.py")]
    if toks[:3] != want:
        raise AssertionError("not a guard-rewritten jh.py command: %s" % command)
    if "argv" in carriers and (len(toks) < 5 or toks[3] != "--agent-proof"):
        raise AssertionError("the guard did not insert the argv proof: %s" % command)
    if "argv" not in carriers and any(t.startswith("--agent-p") for t in toks):
        raise AssertionError("an argv proof without the argv carrier: %s" % command)
    return toks[3:]


def exec_tool_env(guard_env: dict | None) -> dict:
    """The environment OpenClaw's exec tool gives the command: its own marker plus what the guard's
    resolve_exec_env hook returned for this exec."""
    env = {"OPENCLAW_SHELL": "exec"}
    env.update(guard_env or {})
    return env


class World:
    """A temp home plus the calls a lane, the mailer or the owner makes."""

    def __init__(self, start: str = START, consent: bool = True):
        # consent=False: a new install, private/consent.json does not exist and no browser site is allowed
        self.home = TempHome(clock=start, consent=consent).start()
        self.conn = self.home.conn
        self.clock = self.home.clock
        self.calls = []
        self.slept = []
        # `pace wait` sleeps for real (up to 50 s); here the sleep moves the fake clock instead
        real_wait = pacing.pace_wait

        def pace_wait(conn, platform, kind, max_s=pacing.MAX_SLEEP, agent_id="system", sleep=None):
            return real_wait(conn, platform, kind, max_s, agent_id, sleep=self._sleep)
        self._pace_patch = mock.patch.object(pacing, "pace_wait", pace_wait)
        self._pace_patch.start()

    def _sleep(self, seconds) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds=seconds)

    def stop(self) -> None:
        self._pace_patch.stop()
        self.home.stop()

    # ------------------------------------------------------------ CLI
    def run(self, argv, who: str = "system", stdin_text: str = "", cycle: str | None = None):
        """(rc, envelope) of one jh.py call. who: system | human (PIN on stdin) | jobhunter-<lane> (agent: both
        proof carriers for the session agent:<id>:test, as the guard gives them, under python -I flags)."""
        argv = list(argv)
        if cycle:
            argv = ["--cycle", cycle] + argv
        env = {}
        call = argv
        isolated = False
        if who.startswith("jobhunter-"):
            env = helpers.agent_env(paths.root(), who)
            call = helpers.agent_argv(paths.root(), who, argv)
            isolated = True
        elif who == "human":
            argv = ["--pin-stdin"] + argv
            call = argv
            stdin_text = PIN + "\n" + stdin_text
        out = io.StringIO()
        with (helpers.as_isolated() if isolated else contextlib.nullcontext()):
            rc = cli.main(call, env=env, stdin=io.StringIO(stdin_text), stdout=out)
        text = out.getvalue().strip()
        try:
            envelope = json.loads(text.splitlines()[-1]) if text else {}
        except ValueError:
            envelope = {"raw": text[-2000:]}
        self.calls.append((who, argv, rc, envelope.get("code")))
        return rc, envelope

    def ok(self, argv, who: str = "system", stdin_text: str = "", cycle: str | None = None) -> dict:
        """data of a call that must succeed (exit 0, code OK)."""
        rc, env = self.run(argv, who, stdin_text, cycle)
        if rc != 0 or env.get("code") != "OK":
            raise CliError("%s %s -> rc=%s %s: %s" % (who, " ".join(argv), rc, env.get("code"),
                                                      json.dumps(env)[:1500]))
        return env.get("data") or {}

    def fails(self, argv, code: str, who: str = "system", cycle: str | None = None) -> dict:
        """envelope of a call that must fail with `code`."""
        rc, env = self.run(argv, who, cycle=cycle)
        if env.get("code") != code or rc == 0:
            raise CliError("%s %s: expected %s, got rc=%s %s" % (who, " ".join(argv), code, rc,
                                                                json.dumps(env)[:1500]))
        return env

    def wfile(self, role: str, name: str, obj) -> str:
        """A work file the agent of `role` writes into its workspace."""
        text = obj if isinstance(obj, str) else json.dumps(obj, indent=1)
        return self.home.write_agent_file(role, name, text)

    # ------------------------------------------------------------ install-time state
    def write_config(self, **over) -> dict:
        """private/config.json from config.example.json with a fictional owner, wide-open windows, no desktop
        notifications. over: dotted paths, e.g. {"owner.first_name": ""}."""
        cfg = load_json(REPO, "config.example.json")
        cfg["timezone"] = "UTC"
        cfg["owner"].update({"first_name": "Sam", "last_name": "Lee", "gmail_address": "sam.lee@example.com",
                             "signature": {"full_name": "Sam Lee", "phone": "", "links": ["https://example.com/sam"]}})
        cfg["owner"]["notify"]["desktop"] = False
        cfg["gmail"].update({"active_days": [1, 2, 3, 4, 5, 6, 7], "sender_window": ["00:00", "23:59"],
                             "recipient_window": ["00:00", "23:59"]})
        for path, value in over.items():
            node = cfg
            parts = path.split(".")
            for p in parts[:-1]:
                node = node[p]
            node[parts[-1]] = value
        with open(os.path.join(self.home.dir, "private", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        self.cfg = cfg
        return cfg

    def set_home(self, **fields) -> None:
        """Merge fields into private/home.json (for example oc_bin)."""
        hf = paths.home_file()
        with open(hf, "r", encoding="utf-8") as fh:
            h = json.load(fh)
        h.update(fields)
        with open(hf, "w", encoding="utf-8") as fh:
            json.dump(h, fh, indent=1, sort_keys=True)

    def install_fake_openclaw(self) -> str:
        """Copy tests/fixtures/e2e/fake-openclaw into the temp home and record it as oc_bin. Returns the log
        path (one JSON line per accepted call)."""
        dst = os.path.join(self.home.dir, "bin", "openclaw")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(E2E, "fake-openclaw"), dst)
        os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.set_home(oc_bin=dst, oc_profile="jhtest")
        return os.path.join(self.home.dir, "bin", "openclaw-calls.jsonl")

    def onboard(self, **config_over) -> None:
        """U4 onboarding from its fixtures: PIN, config, salary and inference records, the interview answers,
        the base resume marked reviewed and built."""
        from jobhunter import profile as P
        from jobhunter import resume as R
        from tests.fakes.u1 import write_heartbeat
        auth.set_pin(None, PIN)
        self.write_config(**config_over)
        with db.tx(self.conn):
            P.record_salary(self.conn, load_json(PROFILE_FIX, "salary.json"))
        with db.tx(self.conn):
            P.record_inference(self.conn, load_json(PROFILE_FIX, "inference.json"))
        for qid, value in REQUIRED_ANSWERS:
            with db.tx(self.conn):
                P.answer(self.conn, qid, value)
        st = self.ok(["profile", "status"])
        if not st.get("confirmed"):
            raise CliError("profile not confirmed after onboarding: %s" % json.dumps(st)[:800])
        write_heartbeat()
        R.mark_reviewed(R.load_base())
        self.ok(["resume", "base-build"])

    # ------------------------------------------------------------ lanes
    def preflight(self, lane: str, agent: str) -> str:
        pf = self.ok(["preflight", "--lane", lane], agent)
        if not pf.get("go") or not pf.get("cycle_id"):
            raise CliError("preflight %s: %s" % (lane, json.dumps(pf)[:800]))
        return pf["cycle_id"]

    def end_cycle(self, cycle: str, agent: str) -> None:
        self.ok(["cycle", "end", "--cycle", cycle], agent)

    def add_jobs(self, ingest: dict) -> list:
        cyc = self.preflight("scout", "jobhunter-scout")
        path = self.wfile("scout", "%s/ingest.json" % cyc, ingest)
        res = self.ok(["job", "add", "--file", path], "jobhunter-scout", cycle=cyc)
        self.end_cycle(cyc, "jobhunter-scout")
        return res["results"]

    def evaluate_all(self) -> list:
        """One evaluator cycle: a scorecard that passes every gate for each packet."""
        from jobhunter import profile as P
        cyc = self.preflight("evaluator", "jobhunter-evaluator")
        packets = self.ok(["eval", "next"], "jobhunter-evaluator", cycle=cyc)["packets"]
        facts = P.facts()
        fid = sorted(facts)[0]
        out = []
        for pk in packets:
            card = {"job_uid": pk["job_uid"],
                    "gates": {"must_have_missing": [], "years_gap": False, "location_incompatible": False,
                              "comp_below_floor": False, "role_family_excluded": False,
                              "requires_account_creation": False, "role_closed": False},
                    "criteria": {"role_family": {"score": 5, "evidence": "Title matches family Data analytics"},
                                 "skills": {"score": 4, "matches": [{"jd_quote": "SQL and Python for returns forecasting",
                                                                    "fact_id": fid}], "gaps": []},
                                 "seniority": {"score": 4, "evidence": "2 to 4 years; profile has 3"},
                                 "domain": {"score": 4, "evidence": "returns forecasting"},
                                 "location": {"score": 5, "evidence": "remote"},
                                 "compensation": {"score": 4, "evidence": "65000 to 80000 USD"},
                                 "company": {"score": 3, "evidence": "unknown"}},
                    "reason_text": "SQL and Python forecasting match.", "must_have_quotes": [],
                    "model": "anthropic/claude-sonnet-5"}
            path = self.wfile("evaluator", "%s/card-%s.json" % (cyc, pk["job_uid"]), card)
            out.append(self.ok(["eval", "record", "--job", pk["job_uid"], "--file", path], "jobhunter-evaluator",
                               cycle=cyc))
        self.end_cycle(cyc, "jobhunter-evaluator")
        return out

    def qc_pass(self, draft_uid: str, agent: str, cycle: str) -> dict:
        """qc review start, the worker drains the queue (fake reviewer turn), qc review wait."""
        q = self.ok(["qc", "review", "start", "--draft", draft_uid], agent, cycle=cycle)
        self.ok(["qc", "worker", "--drain", "--max-seconds", "250"])
        return self.ok(["qc", "review", "wait", "--job", q["qjob_uid"], "--max", "1"], agent, cycle=cycle)

    def approve_all(self) -> list:
        pending = self.ok(["approvals", "list"])["pending"]
        return [self.ok(["approve", it["code"]], "human") for it in pending]

    # ------------------------------------------------------------ database
    def one(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list:
        return [tuple(r) for r in self.conn.execute(sql, args).fetchall()]

    def write_lock_free(self) -> bool:
        """True when another connection can take the write lock right now (BEGIN IMMEDIATE, 100 ms)."""
        other = sqlite3.connect(paths.db_path(), timeout=0.1, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            other.close()


OU = "jobhunter-outreach"
HOOK_SNIP = "pincode-level models beat our city-level RTO model last quarter"
# two different cold emails (the presend lint refuses a text too similar to one sent in the last 30 days):
# (body, the claim it makes from profile fact P1)
COLD_BODIES = [
    ("Hi {first},\n\nYour post on 18 September said pincode-level models beat the city-level RTO model at "
     "{company}. We saw the same pattern at Tidemark Logistics.\n\nI built the COD return-risk model at Tidemark, "
     "and RTO fell from 18% to 13% in two quarters.\n\nWould a 15 minute call next week be useful? If someone else "
     "owns this, a name is plenty.\n\nThanks,", "RTO fell from 18% to 13% in two quarters"),
    ("Hi {first},\n\nIn September you wrote that {company} found pincode-level models more accurate than a "
     "city-level view of returns. Delivery-area features did the same for me.\n\nMy return-risk model for cash on "
     "delivery orders at Tidemark Logistics cut RTO from 18% to 13% over two quarters.\n\nCould we talk for a "
     "quarter of an hour next week? A pointer to the right person helps just as much.\n\nBest,",
     "cut RTO from 18% to 13% over two quarters"),
    ("Hello {first},\n\nA recent post from {company} made the case for pincode-level models. That matches "
     "what I found in practice.\n\nAt Tidemark Logistics I owned the model that scored COD orders for return "
     "risk. RTO went from 18% to 13% within two quarters of launch.\n\nShall I walk you through the features in "
     "15 minutes next week? Happy to be pointed to a colleague too.\n\nRegards,",
     "RTO went from 18% to 13% within two quarters"),
]
COLD_SUBJECT = "Pincode-level RTO models"


def install_reviewer(test, reviewer=None):
    """The reviewer's one-shot cron run (U1 ocrun.qc_turn, CLI-ROUTE-DESIGN 6.3.2) replaced by U3's FakeQcTurn
    answering with `reviewer` (a tests.fakes.u3 FakeReviewer, default mode pass), so the real qc.agent_turn, its
    reply handling and the review parser run. Returns the FakeReviewer (its .calls name the agent jobhunter-qc)."""
    from jobhunter import ocrun
    from tests.fakes.u3 import FakeQcTurn, FakeReviewer
    rev = reviewer if reviewer is not None else FakeReviewer("pass")
    turn = FakeQcTurn(lambda session_key, message_file, timeout_s:
                      rev("jobhunter-qc", session_key, message_file, timeout_s)["text"])
    p = mock.patch.object(ocrun, "qc_turn", turn)
    p.start()
    test.addCleanup(p.stop)
    return rev


def install_qc_fakes(test, reviewer=None):
    """The two QC seams every e2e test replaces (U3): no detached worker spawn, the fake reviewer run
    (install_reviewer). Returns the FakeReviewer."""
    from jobhunter import qc as qcpkg
    prev = qcpkg.SPAWN
    qcpkg.SPAWN = [].append
    test.addCleanup(setattr, qcpkg, "SPAWN", prev)
    return install_reviewer(test, reviewer)


class OutreachSteps:
    """The outreach lane's calls (design 1.3.5 and 6.1) and the web_ui email route (skill jobhunter-gmail-web),
    made through the real CLI as the outreach agent. Mixed into a World."""

    # ------------------------------------------------------------ owner consent (private/consent.json)
    def grant(self, *sites: str, method: str = "manual_login") -> dict:
        argv = ["browser", "consent", "grant", "--method", method]
        for s in sites:
            argv += ["--site", s]
        return self.ok(argv, "human")

    def revoke(self, *sites: str) -> dict:
        argv = ["browser", "consent", "revoke"]
        for s in sites:
            argv += ["--site", s]
        return self.ok(argv, "human")

    # ------------------------------------------------------------ research, contact, draft
    def add_contact(self, cyc: str, **fields) -> dict:
        """contact add (12.8). fields: full_name, first_name, company, company_domain, email, ... (defaults: a
        hiring manager with no address)."""
        c = {"full_name": None, "first_name": None, "title": "Head of Analytics", "company": None,
             "company_domain": None, "role_type": "hiring_manager", "locale": "US", "email": None, "email_grade": None,
             "email_evidence_url": None, "linkedin_url": None, "linkedin_member_url": None, "source_url": None}
        c.update(fields)
        path = self.wfile("outreach", "%s/contact-%d.json" % (cyc, len(self.calls)), c)
        return self.ok(["contact", "add", "--file", path], OU, cycle=cyc)

    def verify_email(self, cyc: str, address: str, grade: str = "A", evidence: str = "") -> dict:
        from jobhunter import emailcheck
        ev = self.wfile("outreach", "%s/email-evidence-%d.txt" % (cyc, len(self.calls)), evidence or "Team page")
        with mock.patch.object(emailcheck, "lookup_mx", return_value=["mx1." + address.split("@")[1]]):
            return self.ok(["email", "verify", "--address", address, "--grade", grade, "--evidence-file", ev], OU,
                           cycle=cyc)

    def cold_email_draft(self, cyc: str, contact_uid: str, first: str, company: str, n: int = 1) -> str:
        """research add (a dated post of the person) and draft create of a cold email hooked on it, then QC.
        Returns the draft uid (awaiting approval)."""
        post = "https://www.linkedin.com/posts/example-person-activity-%d" % n
        research = {"subject": {"kind": "person", "contact_uid": contact_uid},
                    "facts": [{"text": "Post: pincode-level models beat the city-level RTO model last quarter.",
                               "snippet": HOOK_SNIP, "source_type": "linkedin_post", "source_url": post,
                               "published_at": "2026-09-18", "retrieved_at": "2026-09-28"}]}
        r = self.ok(["research", "add", "--file", self.wfile("outreach", "%s/research-%d.json" % (cyc, n), research)],
                    OU, cycle=cyc)
        draft = {"kind": "cold_email", "channel": "email_cold", "contact_uid": contact_uid, "subject": COLD_SUBJECT,
                 "body": COLD_BODIES[(n - 1) % len(COLD_BODIES)][0].format(first=first, company=company),
                 "hook": {"anchor": "pincode-level models", "source_type": "linkedin_post", "source_url": post,
                          "snippet": HOOK_SNIP, "published_at": "2026-09-18", "retrieved_at": "2026-09-28",
                          "fact_id": r["facts"][0]["fact_uid"]},
                 "claims": [{"text": COLD_BODIES[(n - 1) % len(COLD_BODIES)][1], "fact_id": "P1"}], "links": []}
        d = self.ok(["draft", "create", "--file", self.wfile("outreach", "%s/draft-%d.json" % (cyc, n), draft)], OU,
                    cycle=cyc)
        verdict = self.qc_pass(d["draft_uid"], OU, cyc)
        if verdict.get("draft_status") != "awaiting_approval":
            raise CliError("cold email did not reach approval: %s" % json.dumps(verdict)[:800])
        return d["draft_uid"]

    # ------------------------------------------------------------ web_ui send (skill jobhunter-gmail-web)
    def gmail_detect(self, cyc: str) -> dict:
        det = {"platform": "gmail", "url": "https://mail.google.com/mail/u/0/#inbox", "title": "Inbox - Gmail",
               "http_status": None, "text": "Compose Inbox Starred Sent Drafts"}
        return self.ok(["detect", "--file", self.wfile("outreach", "%s/detect-%d.json" % (cyc, len(self.calls)),
                                                        det)], OU, cycle=cyc)

    def web_precheck(self, cyc: str, contact_uid: str, counts: dict | None = None) -> dict:
        """gate precheck-plan and gate precheck of a cold email with the Sent, Outbox and Scheduled counts the
        read_gmail_list.js driver reported (default: none)."""
        plan = self.ok(["gate", "precheck-plan", "--kind", "cold_email", "--contact", contact_uid], OU, cycle=cyc)
        counts = counts or {}
        ev = {"kind": "cold_email", "platform": "gmail", "observed_at": self.clock.now(),
              "checks": [{"name": c["name"], "value": counts.get(c["name"], 0)} for c in plan["checks"]]}
        path = self.wfile("outreach", "%s/precheck-%d.json" % (cyc, len(self.calls)), ev)
        return self.ok(["gate", "precheck", "--kind", "cold_email", "--platform", "gmail", "--contact", contact_uid,
                        "--file", path], OU, cycle=cyc)

    def web_reserve(self, cyc: str, draft_uid: str, contact_uid: str):
        """(rc, envelope) of the detect, precheck, pace wait and gate reserve of an approved cold email."""
        self.gmail_detect(cyc)
        pc = self.web_precheck(cyc, contact_uid)
        for _ in range(5):
            if not self.ok(["pace", "wait", "--platform", "gmail", "--kind", "write"], OU, cycle=cyc)["remaining_s"]:
                break
        return self.run(["gate", "reserve", "--kind", "cold_email", "--draft", draft_uid, "--precheck",
                         str(pc["precheck_id"]), "--platform", "gmail", "--contact", contact_uid], OU, cycle=cyc)

    @staticmethod
    def with_to(text: str, recipient: str) -> str:
        """An email text ("Subject: ...", blank line, body) as the web read-backs give it (12.7): the header
        lines with a `To: <recipient>` line after the Subject. A text whose header already names To, Cc or Bcc
        is returned as it is, so a test can hand in a read-back with other recipients."""
        head, _sep, body = text.partition("\n\n")
        if any(line.split(":", 1)[0].strip().lower() in ("to", "cc", "bcc") for line in head.split("\n")):
            return text
        return "%s\nTo: %s\n\n%s" % (head, recipient, body)

    def web_send(self, cyc: str, token: str, draft_uid: str, observed: str | None = None,
                 readback: str | None = None) -> dict:
        """Type the approved text (draft show --field send_text), read it back (the observed_text of
        read_compose.js: Subject and To lines, blank line, body), gate arm, dwell, one Send, Sent-folder read-back
        (the readback_text of read_gmail_message.js, default: the approved text to the reserved recipient), then
        `gate confirm --evidence-file --platform-ref-file --observed-file` as skill jobhunter-gmail-web section 3
        step 4 runs it. An observed or readback text without a To, Cc or Bcc line gets the reserved recipient's
        To line. With a readback given, "confirm" is the whole envelope plus "rc" (a refusal is exit 6)."""
        text = self.ok(["draft", "show", draft_uid, "--field", "send_text"], OU, cycle=cyc)["value"]
        rcpt = self.one("SELECT recipient FROM actions WHERE token = ?", token)[0]
        observed = self.with_to(text if observed is None else observed, rcpt)
        readback = None if readback is None else self.with_to(readback, rcpt)
        path = self.wfile("outreach", "%s/observed-%s.txt" % (cyc, token), observed)
        armed = self.ok(["gate", "arm", token, "--observed-file", path], OU, cycle=cyc)
        for _ in range(5):
            if not self.ok(["pace", "wait", "--platform", "gmail", "--kind", "dwell"], OU, cycle=cyc)["remaining_s"]:
                break
        ev = self.wfile("outreach", "%s/evidence-%s.txt" % (cyc, token),
                        "Toast: Message sent\nSent row: %s\n\n%s" % (COLD_SUBJECT, text))
        ref = self.wfile("outreach", "%s/ref-%s.json" % (cyc, token),
                         {"url": "https://mail.google.com/mail/u/0/#sent/18c2f0000000c%03d" % len(self.calls)})
        rb = self.wfile("outreach", "%s/readback-%s.txt" % (cyc, token),
                        self.with_to(text, rcpt) if readback is None else readback)
        argv = ["gate", "confirm", token, "--evidence-file", ev, "--platform-ref-file", ref, "--observed-file", rb]
        if readback is None:
            confirmed = self.ok(argv, OU, cycle=cyc)
        else:
            rc, env = self.run(argv, OU, cycle=cyc)
            confirmed = dict(env, rc=rc)
        return {"text": text, "armed": armed, "confirm": confirmed}


class ApplyWorld(World):
    """World plus the applier steps of design 1.3.4."""

    def claim(self, cycle: str, job_uid: str) -> dict:
        items = self.ok(["apply", "next"], AP, cycle=cycle)["items"]
        mine = [it for it in items if it["job_uid"] == job_uid]
        if not mine:
            raise AssertionError("apply next did not claim %s: %s" % (job_uid, json.dumps(items)[:800]))
        return mine[0]

    def build_resume(self, cycle: str, job_uid: str) -> dict:
        self.ok(["resume", "plan", "--job", job_uid], AP, cycle=cycle)
        tailor = load_json(PROFILE_FIX, "tailor_light.json")
        tailor["job_uid"] = job_uid
        path = self.wfile("applier", "%s/tailor-%s.json" % (cycle, job_uid), tailor)
        built = self.ok(["resume", "build", "--job", job_uid, "--file", path], AP, cycle=cycle)
        verdict = self.qc_pass(built["draft_uid"], AP, cycle)
        if verdict.get("draft_status") != "qc_passed":
            raise AssertionError("resume draft did not pass QC: %s" % json.dumps(verdict)[:800])
        return built

    def answer(self, cycle: str, job_uid: str, label: str):
        q = {"job_uid": job_uid, "label": label, "field_type": "text", "choices": []}
        path = self.wfile("applier", "%s/q-%d.json" % (cycle, len(self.calls)), q)
        return self.run(["answers", "get", "--file", path], AP, cycle=cycle)

    def package(self, cycle: str, job_uid: str, variant_uid: str, fields: list) -> str:
        pkg = {"kind": "application_package", "channel": "application_package", "job_uid": job_uid,
               "payload": {"job_uid": job_uid, "resume_variant_uid": variant_uid, "fields": fields,
                           "cover_note_draft_uid": None},
               "hook": None, "claims": [], "links": []}
        path = self.wfile("applier", "%s/pkg-%s.json" % (cycle, job_uid), pkg)
        created = self.ok(["draft", "create", "--file", path], AP, cycle=cycle)
        verdict = self.qc_pass(created["draft_uid"], AP, cycle)
        if verdict.get("draft_status") != "awaiting_approval":
            raise AssertionError("package did not reach approval: %s" % json.dumps(verdict)[:800])
        return created["draft_uid"]

    def submit(self, cycle: str, job_uid: str, pkg_uid: str, variant_uid: str, *, platform: str,
               detect_platform: str, page_url: str, fields: list) -> dict:
        """Steps 3.1 and 3.6 to 3.10 of 1.3.4 for an approved package. Returns what the steps produced."""
        plan = self.ok(["gate", "precheck-plan", "--kind", "application", "--job", job_uid], AP, cycle=cycle)
        ev = {"kind": "application", "platform": platform, "observed_at": self.clock.now(), "page_url": page_url,
              "checks": [{"name": c["name"], "value": False} for c in plan["checks"]]}
        path = self.wfile("applier", "%s/precheck-%s.json" % (cycle, job_uid), ev)
        pc = self.ok(["gate", "precheck", "--kind", "application", "--platform", platform, "--job", job_uid,
                      "--file", path], AP, cycle=cycle)
        det = {"platform": detect_platform, "url": page_url, "title": "Data Analyst at Kestrel Commerce",
               "http_status": None, "text": "Apply for this job. First name. Last name. Email. Resume."}
        path = self.wfile("applier", "%s/detect-%s.json" % (cycle, job_uid), det)
        detected = self.ok(["detect", "--file", path], AP, cycle=cycle)
        res = self.ok(["gate", "reserve", "--kind", "application", "--draft", pkg_uid, "--precheck",
                       str(pc["precheck_id"]), "--platform", platform, "--job", job_uid], AP, cycle=cycle)
        token = res["token"]
        staged = self.ok(["resume", "stage", "--variant", variant_uid, "--token", token], AP, cycle=cycle)
        obs = {"fields": [{"label": f["label"], "value": f["value"]} for f in fields],
               "resume_filename_visible": staged["filename"]}
        path = self.wfile("applier", "%s/observed-%s.json" % (cycle, token), obs)
        armed = self.ok(["gate", "arm", token, "--observed-file", path], AP, cycle=cycle)
        for _ in range(3):                         # dwell before the one click (the fake clock moves)
            if not self.ok(["pace", "wait", "--platform", platform, "--kind", "dwell"], AP, cycle=cycle)["remaining_s"]:
                break
        path = self.wfile("applier", "%s/evidence-%s.txt" % (cycle, token), "Page: Thank you for applying.")
        confirmed = self.ok(["gate", "confirm", token, "--evidence-file", path], AP, cycle=cycle)
        self.ok(["resume", "unstage", "--token", token], AP, cycle=cycle)
        return {"plan": plan, "precheck": pc, "detect": detected, "reserve": res, "staged": staged,
                "armed": armed, "confirm": confirmed, "token": token}
