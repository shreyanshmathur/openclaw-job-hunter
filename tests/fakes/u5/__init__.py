"""U5 test fakes: a fictional seed dataset, fake edit handlers (U2, U3, U6 functions), a fake chat sender
and a fake desktop notifier. Everything here is fictional placeholder data."""
from __future__ import annotations

import json

import tests  # noqa: F401
from jobhunter import canon
from jobhunter.errors import Denied
from tests import helpers as H

TEST_CONFIG = {
    "timezone": "UTC",
    "sheets": {"enabled": True, "person_name_style": "first_last_initial", "skipped_tab_days": 30,
               "max_ops_per_post": 400, "store_message_text": True, "resync_overlap_minutes": 10},
    "owner": {"notify": {"channel": "whatsapp", "to": "test-owner", "desktop": True, "quiet_hours": ["22:00", "08:00"]}},
    "approval": {"mode": "human"},
    "gmail": {"route": "app_password"},
}


def config(**over) -> dict:
    cfg = json.loads(json.dumps(TEST_CONFIG))
    for dotted, value in over.items():
        cur = cfg
        parts = dotted.split("__")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return cfg


def _ins(conn, table: str, row: dict) -> int:
    cols = list(row)
    return conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join("?" for _ in cols)),
                        [row[c] for c in cols]).lastrowid


class Seed:
    """Ids of the rows created by seed()."""


def seed(conn, clock) -> Seed:
    """A small realistic ledger: two companies, three contacts, jobs in several states, an approval waiting,
    a sent cold email with a positive reply, an application, QC rows, one open and one resolved breaker,
    an undelivered high notification and a cycle."""
    s = Seed()
    now = clock.now()
    # UPDATEs stamp a value different from the insert time, or the t_*_touch trigger would stamp wall time
    upd = clock.ago(seconds=1)
    s.k1 = H.insert_company(conn, "Kestrel Commerce", "example.com", uid="KAAAAAA2")
    s.k2 = H.insert_company(conn, "Tidewater Labs", "example.org", uid="KAAAAAA3")
    s.p1 = H.insert_contact(conn, s.k1, "Alex Rivera", "alex.rivera@example.com", uid="PAAAAAA2",
                            linkedin_url="https://www.linkedin.com/in/example-person")
    conn.execute("UPDATE contacts SET title = 'Head of Analytics', updated_at = ? WHERE id = ?", (upd, s.p1))
    s.p2 = H.insert_contact(conn, s.k1, "Careers", "careers@example.com", role_type="role_inbox", uid="PAAAAAA3")
    s.p3 = H.insert_contact(conn, s.k2, "Sam Lee", "sam.lee@example.org", uid="PAAAAAA4")
    conn.execute("UPDATE contacts SET title = 'Data Lead', updated_at = ? WHERE id = ?", (upd, s.p3))

    # jobs
    s.j_good = H.insert_job(conn, s.k1, "Senior Data Analyst", "eligible", uid="JAAAAAA2",
                            url="https://job-boards.greenhouse.io/example/jobs/1")
    conn.execute("UPDATE jobs SET location_raw = 'Bengaluru', work_mode = 'hybrid', updated_at = ? WHERE id = ?",
                 (upd, s.j_good))
    _ins(conn, "evaluations", {"job_id": s.j_good, "stage": "llm", "score": 82, "verdict": "apply",
                               "reason_code": "fit", "reason_text": "Forecasting and SQL match your work.",
                               "gates_failed": "[]", "profile_version": "v1", "evaluated_at": now, "updated_at": now})
    s.j_skip = H.insert_job(conn, s.k2, "Analytics Manager", "prefilter_rejected", uid="JAAAAAA3", source="naukri")
    conn.execute("UPDATE jobs SET status_reason = 'years_required', years_min = 5, updated_at = ? WHERE id = ?",
                 (upd, s.j_skip))
    s.j_border = H.insert_job(conn, s.k2, "Data Analyst", "borderline", uid="JAAAAAA4", source="lever")
    _ins(conn, "evaluations", {"job_id": s.j_border, "stage": "llm", "score": 62, "verdict": "borderline",
                               "reason_code": "fit", "reason_text": "Good skills match; domain is new.",
                               "gates_failed": json.dumps(["years_gap", {"gate": "must_have_missing",
                                                                        "jd_quote": "dbt certification"}]),
                               "profile_version": "v1", "evaluated_at": now, "updated_at": now})
    s.j_new = H.insert_job(conn, s.k1, "Intern", "new", uid="JAAAAAA5")
    s.j_applied = H.insert_job(conn, s.k1, "Business Analyst", "applied", uid="JAAAAAA6",
                               url="https://job-boards.greenhouse.io/example/jobs/2")
    old = clock.ago(days=40)
    s.j_old = H.insert_job(conn, s.k2, "BI Developer", "prefilter_rejected", uid="JAAAAAA7")
    conn.execute("UPDATE jobs SET discovered_at = ?, status_reason = 'location', updated_at = ? WHERE id = ?",
                 (old, upd, s.j_old))

    # approval waiting (cold email to Sam Lee at Tidewater Labs)
    s.d_wait = H.insert_draft(conn, "cold_email", "awaiting_approval", company_id=s.k2, contact_id=s.p3,
                              subject="Returns forecasting at Tidewater", uid="DAAAAAA2",
                              body="Hi Sam,\n\nYour post about returns stood out.\n\nThanks,")
    conn.execute("UPDATE drafts SET expires_at = ?, updated_at = ? WHERE id = ?",
                 (canon.ts_add(now, hours=72), upd, s.d_wait))
    _ins(conn, "approval_codes", {"code": "A7K2", "draft_id": s.d_wait, "issued_at": now})
    for stage, passed, score in (("lint", 1, None), ("review", 1, 4.35)):
        _ins(conn, "qc_results", {"draft_id": s.d_wait, "attempt": 1, "stage": stage, "passed": passed,
                                  "blocks_json": "[]", "warns_json": json.dumps([["S-LEN", "long"]]),
                                  "review_json": json.dumps({"top_issue": "Opening could be shorter"}) if score else None,
                                  "weighted_score": score, "lowest_criterion": "brevity" if score else None,
                                  "text_sha256": "ab" * 32, "created_at": now})

    # sent cold email to Alex Rivera with a positive reply
    s.d_sent = H.insert_draft(conn, "cold_email", "sent", company_id=s.k1, contact_id=s.p1,
                              subject="Pincode-level RTO models", uid="DAAAAAA3")
    payload = {"hook": {"source_type": "linkedin_post", "snippet": "pincode-level models beat the city model",
                        "source_url": "https://www.linkedin.com/posts/example-person-activity-1",
                        "published_at": "2026-09-18"}}
    conn.execute("UPDATE drafts SET payload_json = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(payload), upd, s.d_sent))
    s.a_cold = H.insert_action(conn, "cold_email", "sent", company_id=s.k1, contact_id=s.p1, draft_id=s.d_sent,
                               recipient="alex.rivera@example.com", token="TAAAAAAAAAAA2")
    s.t_cold = H.insert_thread(conn, s.a_cold, contact_id=s.p1, company_id=s.k1, channel="email", state="replied")
    conn.execute("UPDATE threads SET reply_class = 'positive', reply_at = ?, reply_summary = ?, "
                 "followup_due_at = ?, updated_at = ? WHERE id = ?",
                 (now, "Asks for a call next week.", canon.ts_add(now, days=5), upd, s.t_cold))
    conn.execute("UPDATE actions SET thread_key = (SELECT thread_key FROM threads WHERE id = ?), updated_at = ? "
                 "WHERE id = ?", (s.t_cold, upd, s.a_cold))
    _ins(conn, "replies", {"thread_id": s.t_cold, "received_at": now, "classification": "positive",
                           "classified_by": "agent", "summary": "Asks for a call next week.",
                           "platform_msg_ref": "gm:1", "created_at": now})
    s.thread_key = conn.execute("SELECT thread_key FROM threads WHERE id = ?", (s.t_cold,)).fetchone()[0]

    # application to the Business Analyst job
    s.a_app = H.insert_action(conn, "application", "sent", company_id=s.k1, job_id=s.j_applied,
                              platform="greenhouse", token="TAAAAAAAAAAA3")
    s.rv = _ins(conn, "resume_variants", {"variant_uid": "VAAAAAA2", "job_id": s.j_applied, "mode": "light",
                                          "base_sha256": "0" * 8, "pdf_path": "/tmp/x/Alex_Rivera_Resume.pdf",
                                          "txt_path": "/tmp/x/r.txt", "pdf_sha256": "1" * 8, "created_at": now})
    s.app = _ins(conn, "applications", {"action_id": s.a_app, "job_id": s.j_applied, "route": "ats_form",
                                        "resume_variant_id": s.rv, "confirmation": "Thanks for applying",
                                        "created_at": now, "updated_at": now})

    # breakers: LinkedIn open, Gmail resolved
    _ins(conn, "breakers", {"scope": "linkedin", "state": "open", "reason_code": "captcha", "detail": "Checkpoint page",
                            "tripped_at": now, "min_cooldown_until": canon.ts_add(now, hours=72),
                            "requires_human": 1, "updated_at": now})
    s.be_li = _ins(conn, "breaker_events", {"scope": "linkedin", "event": "trip", "reason_code": "captcha",
                                            "detail": "Checkpoint page", "by": "guard", "created_at": now})
    _ins(conn, "breakers", {"scope": "gmail", "state": "closed", "reason_code": "smtp_rate_limit",
                            "tripped_at": clock.ago(days=2), "requires_human": 1, "reset_at": clock.ago(days=1),
                            "updated_at": clock.ago(days=1)})
    s.be_gm = _ins(conn, "breaker_events", {"scope": "gmail", "event": "trip", "reason_code": "smtp_rate_limit",
                                            "by": "system", "created_at": clock.ago(days=2)})
    _ins(conn, "breaker_events", {"scope": "gmail", "event": "reset", "by": "human", "created_at": clock.ago(days=1)})

    _ins(conn, "notifications", {"dedupe_key": "breaker:linkedin:x", "priority": "high", "kind": "alert",
                                 "text": "LinkedIn stopped: a CAPTCHA appeared.", "created_at": now})
    _ins(conn, "cycles", {"cycle_id": "C20260927T041500ZAAAA", "lane": "scout", "started_at": now, "ended_at": now,
                          "status": "ok"})
    _ins(conn, "human_tasks", {"task_uid": "HAAAAAA2", "kind": "answer_question", "question": "Q3: notice period?",
                               "created_at": now})
    return s


class FakeHandlers:
    """Records the calls a Sheet edit makes; `fail` maps a method name to a Denied to raise."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail: dict[str, Denied] = {}

    def _rec(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail:
            raise self.fail[name]
        return {"ok": True}

    def approve(self, conn, draft_uid, by):
        return self._rec("approve", draft_uid, by)

    def skip(self, conn, draft_uid, reason, by):
        return self._rec("skip", draft_uid, reason, by)

    def set_human_call(self, conn, job_uid, call_, by):
        return self._rec("set_human_call", job_uid, call_, by)

    def set_outcome(self, conn, *, thread_key=None, job_uid=None, outcome, by):
        return self._rec("set_outcome", thread_key, job_uid, outcome, by)

    def set_application_notes(self, conn, job_uid, notes, by):
        return self._rec("set_application_notes", job_uid, notes, by)


class FakeSender:
    """Stands in for ocrun.message_send: records (channel, target, text); `ok` decides the result."""

    def __init__(self, ok: bool = True, error: str = "no active listener"):
        self.ok = ok
        self.error = error
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, channel, target, message_file):
        with open(message_file, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.sent.append((channel, target, text))
        return {"ok": True} if self.ok else {"ok": False, "error": self.error}


class FakeDesktop:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.shown: list[tuple[str, str]] = []

    def __call__(self, title, text):
        self.shown.append((title, text))
        return self.ok
