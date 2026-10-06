"""Frozen result and error codes (design section 3.2).

Every code that any command may return lives here with its exit number. No unit adds codes: a new
situation maps to the closest existing code. Codes are added only by U1 through a DESIGN change request
(E_NOT_TARGET and E_ENRICH_UNAVAILABLE for the email finder, E_CONSENT_MISSING for per-site browser
consent, E_CRON_DRIFT for an OpenClaw cron job that no longer matches the install manifest). `Denied` is
the single exception type that carries a code through the stack to the CLI envelope (`jobhunter.cli`).
"""
from __future__ import annotations

import re
import sqlite3

# exit number -> codes (3.2). Kept as the single literal source; CODES is derived from it.
_EXIT_TABLE = {
    0: ("OK", "NOTHING_TO_DO", "PENDING"),
    1: ("E_INTERNAL",),
    2: ("E_USAGE",),
    3: ("E_DUP_JOB", "E_DUP_PERSON", "E_DUP_THREAD_FOLLOWUP", "E_DUP_DRAFT", "E_DUP_LI_TOUCH",
        "E_COMPANY_COOLDOWN", "E_COMPANY_APP_CAP", "E_COMPANY_LI_CAP", "E_AGENCY_CAP", "E_ROLE_SIMILAR",
        "E_ALREADY_DONE", "E_TARGET_SKIPPED"),
    4: ("E_CEILING", "E_PACING", "E_OUTSIDE_HOURS", "E_WARMUP", "E_TOO_EARLY", "E_ENRICH_UNAVAILABLE"),
    5: ("E_PAUSED", "E_BREAKER_OPEN", "E_STOP_DETECTED", "E_IDENTITY_MISMATCH", "E_CLOCK_SKEW"),
    6: ("E_QC_NOT_APPROVED", "E_QC_HASH_MISMATCH", "E_QC_LINT_FAILED", "E_QC_REVIEW_FAILED",
        "E_QC_BUDGET_EXHAUSTED", "E_DRAFT_EXPIRED", "E_RESEARCH_STALE", "E_OBSERVED_MISMATCH"),
    7: ("E_EXCLUDED", "E_COMPANY_BLOCKED", "E_CONTACT_DNC", "E_CHANNEL_DISABLED", "E_ADDRESS_GRADE", "E_NO_MX",
        "E_COMPANY_AMBIGUOUS", "E_FOLLOWUP_BINDING", "E_NOT_TARGET", "E_CONSENT_MISSING"),
    8: ("E_LOCKED", "E_CLAIMED"),
    9: ("E_NOT_FOUND",),
    10: ("E_VALIDATION", "E_INVENTED_KEY", "E_SCHEMA", "E_EVIDENCE_MISSING", "E_PATH_NOT_ALLOWED"),
    11: ("E_PRECONDITION", "E_PRECHECK_MISSING", "E_PRECHECK_STALE", "E_DETECT_MISSING", "E_PROFILE_UNCONFIRMED",
         "E_CONFIG_INVALID", "E_HOME_MISMATCH", "E_HUMAN_ONLY", "E_AUTH_FAILED", "E_AUTH_LOCKED",
         "E_CALLER_NOT_ALLOWED", "E_GUARD_MISSING", "E_TOKEN_OPEN", "E_FAIL_NOT_ALLOWED", "E_ROUTE_UNAVAILABLE",
         "E_REVIEWER_TAMPERED", "E_BAD_TRANSITION", "E_CRON_DRIFT"),
    12: ("E_SHEET_ACCESS", "E_SHEET_ERROR", "E_MAIL_TRANSPORT", "E_OPENCLAW_CALL", "E_NETWORK"),
}

CODES: dict[str, int] = {code: exit_no for exit_no, codes in _EXIT_TABLE.items() for code in codes}

# codes that mean success (exit 0)
SUCCESS_CODES = frozenset(_EXIT_TABLE[0])

# Module-level names for every code, so callers can write errors.E_CEILING instead of a string literal.
globals().update({code: code for code in CODES})

# The plain word an agent run ends with (CLI route M6): CYCLE_DONE in a lane cycle, the word its message names in
# an onboarding or probe run (ONBOARD_DONE, PROBE_DONE). Never NO_REPLY in a hint to an agent: on OpenClaw 9.8 it
# risks a "keep working" loop (V16). Generic hints use FINAL_WORD_HINT because onboarding runs see them too.
CYCLE_DONE = "CYCLE_DONE"
FINAL_WORD_HINT = "reply with your final word (CYCLE_DONE in a cycle)"


def exit_code(code: str) -> int:
    """Exit number for a code; an unknown code is a bug and exits like E_INTERNAL."""
    return CODES.get(code, 1)


class Denied(Exception):
    """A refusal or failure with a frozen code. Raised anywhere, rendered by the CLI envelope."""

    def __init__(self, code: str, message: str, retry_after: int | None = None, data: dict | None = None):
        if code not in CODES:
            data = dict(data or {})
            data["unknown_code"] = code
            code = "E_INTERNAL"
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.data = data or {}

    @property
    def exit_code(self) -> int:
        return exit_code(self.code)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retry_after_s": self.retry_after, "data": self.data}


# UNIQUE violations. SQLite names the table and columns for a column index ("UNIQUE constraint failed:
# actions.contact_id") and the index only for expression indexes ("... failed: index 'u_x'"), so both
# spellings are mapped. Several partial indexes share a column list; they all map to the same code.
_UNIQUE_BY_INDEX = {
    "u_first_touch_person": "E_DUP_PERSON",
    "u_li_message_person": "E_DUP_PERSON",
    "u_referral_person": "E_DUP_PERSON",
    "u_application_job": "E_DUP_JOB",
    "u_followup_thread": "E_DUP_THREAD_FOLLOWUP",
    "u_action_draft": "E_DUP_DRAFT",
    "u_open_email_draft_company": "E_DUP_DRAFT",
    "u_open_first_touch_draft_person": "E_DUP_DRAFT",
    "u_li_seq": "E_DUP_LI_TOUCH",
    "u_agent_open_token": "E_TOKEN_OPEN",
    "u_action_precheck": "E_PRECHECK_STALE",
    # email finder (migrations/0002_enrich.sql)
    "u_enrich_find_once": "E_ALREADY_DONE",
    "u_enrich_verify_once": "E_ALREADY_DONE",
    "u_enrich_one_retry": "E_ALREADY_DONE",
    "u_enrich_running_contact": "E_LOCKED",
    # email codes, ATS accounts, CAPTCHA hand-off (migrations/0003_otp_accounts.sql)
    "u_code_use_message": "E_ALREADY_DONE",
    "u_code_use_value": "E_ALREADY_DONE",
    "u_code_use_request": "E_ALREADY_DONE",
    "u_code_req_waiting_tab": "E_LOCKED",
    "u_account_tenant": "E_ALREADY_DONE",
    "u_captcha_open_code": "E_LOCKED",
    "u_captcha_open_job": "E_ALREADY_DONE",
}
_UNIQUE_BY_COLUMNS = {
    "actions.contact_id": "E_DUP_PERSON",               # u_first_touch_person, u_li_message_person, u_referral_person
    "actions.job_id": "E_DUP_JOB",                      # u_application_job
    "actions.thread_key": "E_DUP_THREAD_FOLLOWUP",      # u_followup_thread
    "actions.draft_id": "E_DUP_DRAFT",                  # u_action_draft
    "drafts.company_id": "E_DUP_DRAFT",                 # u_open_email_draft_company
    "drafts.contact_id": "E_DUP_DRAFT",                 # u_open_first_touch_draft_person
    "actions.contact_id, actions.li_msg_seq": "E_DUP_LI_TOUCH",   # u_li_seq
    "actions.agent_id": "E_TOKEN_OPEN",                 # u_agent_open_token
    "actions.precheck_id": "E_PRECHECK_STALE",          # u_action_precheck
    "prechecks.used_by_action": "E_PRECHECK_STALE",     # precheck already consumed by another action
    "enrich_calls.request_id, enrich_calls.provider": "E_ALREADY_DONE",   # u_enrich_find_once, u_enrich_verify_once
    "enrich_requests.retry_of": "E_ALREADY_DONE",       # u_enrich_one_retry
    "enrich_request_keys.key_hash": "E_ALREADY_DONE",   # primary key: a person is never looked up twice
    "enrich_requests.contact_id": "E_LOCKED",           # u_enrich_running_contact: a lookup is running
    "code_uses.message_hmac": "E_ALREADY_DONE",         # u_code_use_message: a message is used once, ever
    "code_uses.value_hmac": "E_ALREADY_DONE",           # u_code_use_value: a code or link is used once, ever
    "code_uses.request_id": "E_ALREADY_DONE",           # u_code_use_request: a request consumes one message
    "code_requests.tab_id": "E_LOCKED",                 # u_code_req_waiting_tab: one waiting request per tab
    "ats_accounts.platform, ats_accounts.tenant": "E_ALREADY_DONE",   # u_account_tenant
    "captcha_tasks.code": "E_LOCKED",                   # u_captcha_open_code
    "captcha_tasks.job_id": "E_ALREADY_DONE",           # u_captcha_open_job
}

_RAISE_RE = re.compile(r"^(E_[A-Z_]+)$")


def map_sqlite_error(exc: sqlite3.DatabaseError) -> Denied:
    """Map a trigger RAISE(ABORT, 'E_...') or a UNIQUE violation to its Denied code (3.2).

    Anything not recognised is E_INTERNAL, except a busy database after the busy timeout, which is
    E_LOCKED (another process holds the write lock; the next run retries).
    """
    msg = str(exc).strip()
    data = {"sqlite": msg}
    m = _RAISE_RE.match(msg)
    if m and m.group(1) in CODES:
        return Denied(m.group(1), "refused by database rule %s" % m.group(1), data=data)
    if msg.startswith("UNIQUE constraint failed:"):
        target = msg[len("UNIQUE constraint failed:"):].strip()
        im = re.match(r"^index '([A-Za-z0-9_]+)'$", target)
        if im:
            code = _UNIQUE_BY_INDEX.get(im.group(1))
        else:
            code = _UNIQUE_BY_COLUMNS.get(target)
        if code:
            data["unique"] = target
            return Denied(code, "duplicate refused by unique index on %s" % target, data=data)
    if isinstance(exc, sqlite3.OperationalError) and ("database is locked" in msg or "database is busy" in msg):
        return Denied("E_LOCKED", "the database is busy; another process holds the write lock", data=data)
    return Denied("E_INTERNAL", "database error: %s" % msg, data=data)
