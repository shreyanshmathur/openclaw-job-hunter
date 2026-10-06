"""Human labels for the Google Sheet, the digest, `status` and the CSV export (design 7.3).

Pure data and pure functions, no database access. `sheets/Code.gs` holds the same tab and column lists
and the same status groups; tests/test_sheets_labels.py parses Code.gs and fails when the two drift.

Rules for every label here: plain ASCII, short, no internal codes, no dash used as punctuation.
"""
from __future__ import annotations

import json
import re

SCHEMA_VERSION = 2
CLIENT = "openclaw-job-hunter/2.0.0"

# ---------------------------------------------------------------- tabs and columns
# (key, header, type, editable, choices). Types: datetime, date, text, long, link, score, num2, int,
# status, choice, id. Identical to TABS in sheets/Code.gs.
OUTCOME_CHOICES = ("No response yet", "Rejected", "Screening call", "Interview", "Offer", "Withdrawn", "Role closed")
# Follow-ups outcome: threads.outcome has no "withdrawn" but has "referred" (design 7.3, U5 fix).
THREAD_OUTCOME_CHOICES = ("No response yet", "Rejected", "Screening call", "Interview", "Offer", "Referred",
                          "Role closed")


def _c(key, header, typ, editable=False, choices=None):
    return (key, header, typ, editable, tuple(choices) if choices else None)


TABLE_TABS: dict[str, list[tuple]] = {
    "approvals": [
        _c("created", "Created", "datetime"), _c("code", "Code", "text"), _c("what", "What", "text"),
        _c("to", "To", "text"), _c("company", "Company", "text"),
        _c("subject", "Subject or first line", "text"), _c("message", "Full text", "long"),
        _c("qc", "QC score", "num2"), _c("expires", "Expires", "datetime"), _c("status", "Status", "status"),
        _c("decision", "Your decision", "choice", True, ("Approve", "Skip")), _c("id", "ID", "id")],
    "jobs": [
        _c("found_on", "Found on", "datetime"), _c("company", "Company", "text"), _c("role", "Role", "text"),
        _c("location", "Location", "text"), _c("work_mode", "Work mode", "text"), _c("source", "Found via", "text"),
        _c("posting", "Job posting", "link"), _c("fit", "Fit score", "score"), _c("verdict", "Verdict", "status"),
        _c("why", "Why", "long"), _c("gates", "Deal breakers", "long"), _c("status", "Status", "status"),
        _c("applied_on", "Applied on", "date"),
        _c("your_call", "Your call", "choice", True, ("Apply anyway", "Never apply")), _c("id", "ID", "id")],
    "skipped": [
        _c("found_on", "Found on", "datetime"), _c("company", "Company", "text"), _c("role", "Role", "text"),
        _c("location", "Location", "text"), _c("reason", "Why it was skipped", "long"),
        _c("posting", "Job posting", "link"),
        _c("your_call", "Your call", "choice", True, ("Apply anyway",)), _c("id", "ID", "id")],
    "applications": [
        _c("applied_on", "Applied on", "datetime"), _c("company", "Company", "text"), _c("role", "Role", "text"),
        _c("location", "Location", "text"), _c("how", "How", "text"), _c("posting", "Job posting", "link"),
        _c("resume", "Resume sent", "text"), _c("status", "Status", "status"), _c("proof", "Proof", "long"),
        _c("fit", "Fit score", "score"), _c("follow_up", "Follow-up", "text"),
        _c("outcome", "Outcome", "choice", True, OUTCOME_CHOICES), _c("notes", "Your notes", "long", True),
        _c("id", "ID", "id")],
    "outreach": [
        _c("sent_on", "Sent on", "datetime"), _c("channel", "Channel", "text"), _c("person", "Person", "text"),
        _c("their_role", "Their role", "text"), _c("company", "Company", "text"), _c("profile", "Profile", "link"),
        _c("why_them", "Why this person", "long"), _c("subject", "Subject or first line", "text"),
        _c("message", "Message", "long"), _c("qc", "QC score", "num2"), _c("approved_by", "Approved by", "text"),
        _c("status", "Status", "status"), _c("reply", "Reply", "status"),
        _c("follow_up_due", "Follow-up due", "date"), _c("notes", "Your notes", "long", True), _c("id", "ID", "id")],
    "followups": [
        _c("started_on", "Started on", "datetime"), _c("channel", "Channel", "text"), _c("person", "Person", "text"),
        _c("company", "Company", "text"), _c("follow_up_due", "Follow-up due", "date"),
        _c("follow_up_sent", "Follow-up sent", "datetime"), _c("last_reply", "Last reply", "datetime"),
        _c("reply_type", "Reply type", "status"), _c("what_they_said", "What they said", "long"),
        _c("next_step", "Next step", "long"), _c("thread", "Thread", "link"),
        _c("outcome", "Outcome", "choice", True, THREAD_OUTCOME_CHOICES), _c("id", "ID", "id")],
    "qc": [
        _c("time", "Time", "datetime"), _c("item", "Item", "text"), _c("channel", "Channel", "text"),
        _c("recipient", "Recipient", "text"), _c("company", "Company", "text"), _c("attempt", "Attempt", "text"),
        _c("lint", "Lint", "status"), _c("lint_findings", "Lint findings", "long"),
        _c("reviewer", "Reviewer", "status"), _c("score", "Score", "num2"),
        _c("lowest", "Lowest criterion", "text"), _c("gates_failed", "Gates failed", "text"),
        _c("top_issue", "Top issue", "long"), _c("final", "Final action", "status"),
        _c("hook", "Hook source", "link"), _c("hash", "Text hash", "text"), _c("id", "ID", "id")],
    "daily": [
        _c("date", "Date", "date"), _c("jobs_found", "Jobs found", "int"),
        _c("passed_filters", "Passed filters", "int"), _c("evaluated", "Evaluated", "int"),
        _c("good_fits", "Good fits", "int"), _c("applications", "Applications", "int"), _c("emails", "Emails", "int"),
        _c("li_invites", "LinkedIn invites", "int"), _c("li_messages", "LinkedIn messages", "int"),
        _c("follow_ups", "Follow-ups", "int"), _c("replies", "Replies", "int"),
        _c("positive", "Positive replies", "int"), _c("interviews", "Interviews", "int"),
        _c("drafts", "Drafts", "int"), _c("first_pass", "Passed QC first try", "int"),
        _c("dropped", "Dropped by QC", "int"), _c("stops", "Stops", "int"), _c("cycles", "Cycles run", "int"),
        _c("id", "ID", "id")],
    "alerts": [
        _c("time", "Time", "datetime"), _c("area", "Area", "text"), _c("what", "What happened", "long"),
        _c("severity", "Severity", "status"), _c("todo", "What you need to do", "long"),
        _c("until", "Paused until", "datetime"), _c("resolved", "Resolved on", "datetime"),
        _c("status", "Status", "status"), _c("id", "ID", "id")],
}

TAB_ORDER = ("start", "dashboard", "approvals", "jobs", "skipped", "applications", "outreach", "followups", "qc",
             "daily", "alerts", "settings")
TAB_TITLES = {
    "start": "Start here", "dashboard": "Dashboard", "approvals": "Approvals", "jobs": "Jobs",
    "skipped": "Skipped by filters", "applications": "Applications", "outreach": "Outreach",
    "followups": "Follow-ups", "qc": "QC log", "daily": "Daily summary", "alerts": "Alerts",
    "settings": "Limits and settings",
}
RENDER_TABS = ("start", "dashboard", "settings")
TABLE_ORDER = tuple(k for k in TAB_ORDER if k in TABLE_TABS)


def columns(tab: str) -> list[tuple]:
    return TABLE_TABS[tab]


def column_keys(tab: str) -> list[str]:
    return [c[0] for c in TABLE_TABS[tab]]


def editable_columns(tab: str) -> dict[str, tuple | None]:
    """{col_key: choices or None} for the columns the person may edit."""
    return {c[0]: c[4] for c in TABLE_TABS.get(tab, []) if c[3]}


# ---------------------------------------------------------------- status groups (colors)
# Identical to STATUS_GROUPS in Code.gs. Every status label below belongs to exactly one group.
STATUS_GROUPS = {
    "good": ("Applied", "Sent", "Accepted", "Passed", "Approved", "Good fit", "Resolved", "Done", "Connected"),
    "wait": ("Queued", "Awaiting approval", "Awaiting reply", "Follow-up due", "Submitting", "Pending", "Rewritten",
             "Borderline", "Needs you", "Invite pending", "Warning"),
    "bad": ("Failed", "Blocked", "Stopped", "Bounced", "Dropped", "Failed QC", "Error", "Opted out",
            "Unknown (checking)", "Complaint", "Open"),
    "muted": ("Skipped", "Duplicate", "Closed", "Expired", "Not a fit", "Withdrawn", "No reply", "Auto-reply",
              "Not run", "Not interested", "Not hiring", "Out of office"),
    "info": ("Replied", "Positive reply", "Screening call", "Interview", "Offer", "Referred", "Referral offered",
             "Info"),
}


def status_group(label: str) -> str | None:
    for g, labels in STATUS_GROUPS.items():
        if label in labels:
            return g
    return None


# ---------------------------------------------------------------- label maps
DRAFT_KIND = {
    "cold_email": "Cold email", "followup_email": "Follow-up email", "application_email": "Application email",
    "li_invite_note": "LinkedIn invite note", "li_message": "LinkedIn message", "li_followup": "LinkedIn follow-up",
    "inmail": "LinkedIn InMail", "form_answer": "Form answer", "cover_note": "Cover note", "resume": "Resume",
    "application_package": "Application",
}

DRAFT_STATUS = {
    "awaiting_approval": "Awaiting approval", "qc_passed": "Awaiting approval", "approved": "Approved",
    "skipped_by_human": "Skipped", "expired": "Expired", "sent": "Sent", "dropped_qc": "Dropped",
    "superseded": "Rewritten", "drafted": "Rewritten", "lint_failed": "Rewritten", "review_pending": "Rewritten",
    "review_failed": "Rewritten",
}

JOB_STATUS = {
    "new": "Queued", "eval_queued": "Queued", "evaluating": "Queued", "eligible": "Good fit",
    "borderline": "Borderline", "rejected": "Not a fit", "apply_queued": "Queued",
    "awaiting_approval": "Awaiting approval", "applying": "Submitting", "applied": "Applied",
    "apply_failed": "Failed", "needs_human": "Needs you", "closed": "Closed", "excluded": "Blocked",
    "duplicate": "Duplicate", "prefilter_rejected": "Skipped",
}

VERDICT = {"apply": "Good fit", "borderline": "Borderline", "skip": "Not a fit", "human_only": "Needs you"}

WORK_MODE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On site", "unknown": "Unknown"}

ATS_NAMES = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday",
    "smartrecruiters": "SmartRecruiters", "workable": "Workable", "recruitee": "Recruitee", "bamboohr": "BambooHR",
    "icims": "iCIMS", "successfactors": "SAP SuccessFactors", "taleo": "Oracle Taleo", "oracle_hcm": "Oracle Cloud HCM",
    "jobvite": "Jobvite",
}
BOARD_NAMES = {
    "linkedin_jobs": "LinkedIn Jobs", "linkedin": "LinkedIn", "linkedin_post": "LinkedIn post", "naukri": "Naukri",
    "instahyre": "Instahyre", "wellfound": "Wellfound", "cutshort": "Cutshort", "hirist": "Hirist",
    "iimjobs": "iimjobs", "foundit": "Foundit", "yc": "YC Work at a Startup", "remotive": "Remotive",
    "remoteok": "Remote OK", "hn": "Hacker News jobs", "serpapi": "Google Jobs (SerpApi)", "glassdoor": "Glassdoor",
    "indeed": "Indeed", "human": "Added by you", "gmail": "Gmail", "weworkremotely": "We Work Remotely",
    "workingnomads": "Working Nomads", "himalayas": "Himalayas", "jobicy": "Jobicy",
}
# Job API ids (sources.SPECS id, the X in an api:X breaker scope) whose jobs.source value differs.
# tests/test_sheets_labels.py fails when this drifts from sources.SPECS.
API_SOURCE_IDS = {"hn_whoishiring": "hn", "serpapi_google_jobs": "serpapi"}


def source_label(source: str | None) -> str:
    if not source:
        return ""
    s = str(source).lower()
    s = API_SOURCE_IDS.get(s, s)
    if s in ATS_NAMES:
        return "Company site (%s)" % ATS_NAMES[s]
    if s in BOARD_NAMES:
        return BOARD_NAMES[s]
    if s.startswith("site:"):
        return source_label(s[5:])
    return _titled(s)


def platform_label(platform: str | None) -> str:
    if not platform:
        return ""
    p = str(platform).lower()
    if p in ATS_NAMES:
        return ATS_NAMES[p]
    return BOARD_NAMES.get(p, _titled(p))


def _titled(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"[_\s]+", s) if w)


ACTION_CHANNEL = {
    "cold_email": "Email", "followup_email": "Email follow-up", "application_email": "Application email",
    "li_invite": "LinkedIn invite", "li_message": "LinkedIn message", "li_followup": "LinkedIn follow-up",
    "inmail": "LinkedIn InMail", "li_withdraw": "LinkedIn invite withdrawn", "referral_ask": "Email (referral ask)",
    "application": "Application",
}
OUTREACH_KINDS = ("cold_email", "followup_email", "li_invite", "li_message", "li_followup", "inmail", "li_withdraw",
                  "referral_ask")
APPLICATION_KINDS = ("application", "application_email")
LIVE_STATUSES = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")

ACTION_STATUS_OUTREACH = {
    "reserved": "Submitting", "armed": "Submitting", "sent": "Sent", "imported": "Sent", "failed": "Failed",
    "failed_after_click": "Unknown (checking)", "unknown": "Unknown (checking)",
}
ACTION_STATUS_APPLICATION = dict(ACTION_STATUS_OUTREACH, sent="Applied", imported="Applied")

APPROVED_BY = {"human:chat": "You (chat)", "human:sheet": "You (sheet)", "human:cli": "You (terminal)",
               "auto": "QC (auto)"}

# threads.reply_class -> short label (Outreach "Reply") and detailed label (Follow-ups "Reply type")
REPLY_SHORT = {
    "positive": "Positive reply", "referral_offered": "Positive reply", "neutral": "Replied", "negative": "Replied",
    "not_hiring": "Replied", "auto_ack": "Auto-reply", "out_of_office": "Auto-reply", "bounce": "Bounced",
    "complaint": "Opted out", "opt_out": "Opted out",
}
REPLY_DETAIL = {
    "positive": "Positive reply", "referral_offered": "Referral offered", "neutral": "Replied",
    "negative": "Not interested", "not_hiring": "Not hiring", "auto_ack": "Auto-reply",
    "out_of_office": "Out of office", "bounce": "Bounced", "complaint": "Complaint", "opt_out": "Opted out",
}

# outcome codes (applications.outcome, threads.outcome) <-> the choice labels
OUTCOME_LABEL = {"none": "No response yet", "rejected": "Rejected", "screening_call": "Screening call",
                 "interview": "Interview", "offer": "Offer", "withdrawn": "Withdrawn", "role_closed": "Role closed",
                 "referred": "Referred", "ghosted": "No response yet"}
OUTCOME_CODE = {v: k for k, v in OUTCOME_LABEL.items() if k != "ghosted"}

HUMAN_CALL_LABEL = {"apply_anyway": "Apply anyway", "never": "Never apply"}
HUMAN_CALL_CODE = {"Apply anyway": "apply_anyway", "Never apply": "never"}
DECISIONS = ("Approve", "Skip")

RESUME_MODE = {"base": "Base resume", "light": "Lightly tailored", "full": "Fully tailored"}

# ---------------------------------------------------------------- reason sentences
PREFILTER_REASON = {
    "excluded_company": "This company is on your exclusion list",
    "excluded_job_url": "This posting is on your exclusion list",
    "stale": "The posting is too old or no longer accepts applications",
    "location": "The location or work mode is not one you chose",
    "years_required": "The years of experience asked for do not match yours",
    "seniority_title": "The title is more senior or junior than you want",
    "role_family": "The role is not one of your target roles",
    "employment_type": "Internship, contract or part time, which you did not ask for",
    "comp_below_floor": "The pay shown is below your minimum",
    "language_or_authorization": "It needs a language or work permit you do not have",
    "agency_unnamed": "A recruiting agency post that does not name the employer",
}

GATE_SENTENCE = {
    "must_have_missing": "Missing a must-have requirement",
    "years_gap": "Years of experience do not match",
    "location_incompatible": "Location or work mode does not match",
    "comp_below_floor": "Pay is below your minimum",
    "role_family_excluded": "A role type you excluded",
    "requires_account_creation": "Needs an account on the company site, so you apply yourself",
    "role_closed": "The role is closed",
}


def _fmt_years(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else ("%.1f" % f)


def prefilter_sentence(reason_code: str | None, years_min=None, years_max=None, experience=None,
                       fallback: str | None = None) -> str:
    """Reason code (6.2) as a sentence, e.g. 'Needs 5 or more years; you have 2'."""
    code = (reason_code or "").strip()
    if code == "years_required" and (years_min is not None or years_max is not None):
        if years_min is not None and (experience is None or float(years_min) > float(experience)):
            s = "Needs %s or more years" % _fmt_years(years_min)
        elif years_max is not None:
            s = "Asks for at most %s years" % _fmt_years(years_max)
        else:
            s = "Needs %s or more years" % _fmt_years(years_min)
        if experience is not None:
            s += "; you have %s" % _fmt_years(experience)
        return s
    if code in PREFILTER_REASON:
        return PREFILTER_REASON[code]
    if fallback:
        return fallback
    if code:
        return code.replace("_", " ").capitalize()
    return "Did not pass your filters"


def gates_sentences(gates_json) -> str:
    """evaluations.gates_failed (JSON list of names or {gate, jd_quote, why}) as short sentences."""
    if not gates_json:
        return ""
    try:
        items = json.loads(gates_json) if isinstance(gates_json, str) else gates_json
    except ValueError:
        return str(gates_json)[:300]
    if isinstance(items, dict):
        items = [dict(v, gate=k) if isinstance(v, dict) else {"gate": k, "value": v} for k, v in items.items()
                 if v not in (False, None, [], "")]
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            out.append(GATE_SENTENCE.get(it, it.replace("_", " ").capitalize()))
        elif isinstance(it, dict):
            name = str(it.get("gate") or it.get("name") or "must_have_missing")
            s = GATE_SENTENCE.get(name, name.replace("_", " ").capitalize())
            quote = it.get("jd_quote") or it.get("why")
            if quote:
                s += ': "%s"' % str(quote)[:160]
            out.append(s)
    return ". ".join(out)


# ---------------------------------------------------------------- breakers and alerts
# Same words as breakers.AREA_LABEL (the owner's chat alert and `breaker status`), so one breaker has one name
# everywhere; tests/test_sheets_labels.py fails when the two drift.
SCOPE_LABEL = {
    "global": "Everything", "gmail": "Gmail", "gmail.cold": "Cold email", "linkedin": "LinkedIn",
    "linkedin.invites": "LinkedIn invites", "linkedin.search": "LinkedIn search",
    "linkedin.easy_apply": "LinkedIn Easy Apply", "linkedin.messages": "LinkedIn messages", "ats": "Job forms",
    "applications": "Applications",
}


def scope_label(scope: str | None) -> str:
    s = str(scope or "")
    if s in SCOPE_LABEL:
        return SCOPE_LABEL[s]
    if s.startswith("site:"):
        return "Site: " + platform_label(s[5:])
    if s.startswith("api:"):
        return "Job API: " + source_label(s[4:])
    if s.startswith("pause:"):
        return "Paused by you: " + scope_label(s[6:])
    if s == "enrich":
        return "Email finder"
    if s.startswith("enrich:"):
        return "Email finder: " + enrich_provider_label(s[7:])
    if s.startswith("ats:"):
        return "Job forms: " + platform_label(s[4:])
    return _titled(s) if s else ""


# ---------------------------------------------------------------- email finder (U10) and browser consent (U1)
# Display names of the optional email-finder services (enrich.settings.PROVIDERS keys).
ENRICH_PROVIDER_NAMES = {
    "prospeo": "Prospeo", "hunter": "Hunter", "tomba": "Tomba", "getprospect": "GetProspect",
    "anymailfinder": "Anymail Finder", "findymail": "Findymail", "apollo": "Apollo", "zerobounce": "ZeroBounce",
}
# What to do about an email-finder stop (docs/EMAIL-FINDER.md section 8).
TODO_ENRICH_KEY = "Run ./jobhunter enrich connect {provider} with a working key; that also clears this stop."
TODO_ENRICH = "Read \"When it stops itself\" in docs/EMAIL-FINDER.md, then run ./jobhunter breaker reset {scope}."
# The sites the consent step asks about, in the order the Sheet and `status` list them (other sites the
# person allowed follow). Each one is "Not allowed" until the person says yes to it.
CONSENT_SITES = ("gmail", "linkedin", "naukri", "indeed", "glassdoor", "foundit", "instahyre", "wellfound")
CONSENT_STATE_LABEL = {"granted": "Allowed", "revoked": "Revoked", "not_granted": "Not allowed",
                       "needs_you": "Needs you", "unknown": "Unknown"}
# Code.gs colours the Value cell of a settings row by this tone (PALETTE keys).
CONSENT_STATE_TONE = {"granted": "good", "revoked": "muted", "not_granted": "muted", "needs_you": "wait",
                      "unknown": "wait"}
_CONSENT_HOSTS = {"mail.google.com": "gmail", "gmail.com": "gmail", "accounts.google.com": "gmail",
                  "google.com": "gmail", "workatastartup.com": "yc", "ycombinator.com": "yc"}


def enrich_provider_label(provider: str | None) -> str:
    p = str(provider or "").strip().lower()
    return ENRICH_PROVIDER_NAMES.get(p, _titled(p))


def consent_site_key(site) -> str:
    """'LinkedIn', 'linkedin.com', 'https://www.linkedin.com/feed' and 'site:linkedin' all give 'linkedin';
    Google hosts give 'gmail'."""
    s = str(site or "").strip().lower()
    if s.startswith("site:"):
        s = s[5:]
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s).split("/", 1)[0].split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    if s in _CONSENT_HOSTS:
        return _CONSENT_HOSTS[s]
    parts = [x for x in s.split(".") if x]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac"):
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return s


def consent_site_label(site) -> str:
    return platform_label(consent_site_key(site))


REASON_SENTENCE = {
    "captcha": "A CAPTCHA or bot check appeared",
    "checkpoint": "A security checkpoint appeared",
    "verification": "The site asked to verify your identity",
    "security_check": "The site showed a security check",
    "restricted": "The site said your account is restricted",
    "unusual_activity": "The site reported unusual activity",
    "login_wall": "The site asked to log in again",
    "logged_out": "The browser was logged out",
    "invite_limit": "LinkedIn said the weekly invitation limit was reached",
    "email_required": "LinkedIn asked for the person's email address",
    "commercial_use_limit": "LinkedIn showed the commercial use limit",
    "easy_apply_limit": "LinkedIn said today's Easy Apply limit was reached",
    "message_limit": "LinkedIn blocked a message or said a limit was reached",
    "rate_limited": "The site answered with too many requests",
    "http_429": "The site answered with too many requests",
    "http_999": "The site refused the request (code 999)",
    "consecutive_failures": "Two actions in a row failed or could not be confirmed",
    "daily_failures": "Three actions today failed or could not be confirmed",
    "unknown_state": "A page showed something the agent could not classify",
    "security_email": "A security email from the site arrived in your inbox",
    "low_acceptance": "Fewer than 1 in 4 invitations were accepted",
    "smtp_daily_limit": "Gmail said the daily sending limit was reached",
    "smtp_rate_limit": "Gmail asked to slow down",
    "smtp_policy": "Gmail refused a message for a policy reason",
    "smtp_auth": "Gmail refused the app password",
    "imap_auth": "Gmail refused the app password",
    "bounces": "Two emails bounced within a day",
    "bounce_rate": "Too many emails bounced",
    "complaints": "Two people marked emails as spam within 30 days",
    "complaint": "Someone marked an email as spam",
    "identity_mismatch": "The browser is logged in to a different account than yours",
    "duplicate_denials": "The duplicate check refused three sends in one cycle",
    "clock_skew": "Your computer clock looks wrong",
    "audit_mismatch": "Something was sent that is not in the agent's records",
    "guard_missing": "The safety plugin is not running",
    "reviewer_tampered": "The quality reviewer's instructions were changed",
    "month_stop": "The monthly limit for this site was reached",
    "paused": "You paused this",
    "consent_revoked": "You took back the agent's use of your login on this site",
    "manual": "Stopped by hand",
    # The codes breakers are actually tripped with: detect/*.json signatures, breakers.POLICIES and the
    # trip() call sites (gate, housekeeping, mail, identity, sources, replies). tests/test_sheets_labels.py
    # fails when a code in detect/*.json or breakers.POLICIES has no sentence here. The numbers (how many
    # bounces, which acceptance rate) come from the detail, which reason_sentence appends.
    "li_challenge": "LinkedIn showed a security check or CAPTCHA",
    "li_restricted": "LinkedIn said your account is restricted or asked to verify your identity",
    "li_logged_out": "LinkedIn logged the browser out",
    "li_invite_limit": "LinkedIn said the weekly invitation limit was reached",
    "li_email_needed": "LinkedIn asked for the person's email address before connecting",
    "li_commercial_limit": "LinkedIn showed the commercial use limit for search",
    "li_easy_apply_limit": "LinkedIn said today's Easy Apply limit was reached",
    "li_messaging_blocked": "LinkedIn blocked a message or said a limit was reached",
    "li_http_429": "LinkedIn answered with too many requests",
    "li_consecutive_failures": "Two LinkedIn actions in a row failed or could not be confirmed",
    "li_daily_failures": "Several LinkedIn actions today failed or could not be confirmed",
    "li_unknown_modal": "LinkedIn showed a popup the agent could not classify",
    "li_security_email": "A security email from LinkedIn arrived in your inbox",
    "li_low_acceptance": "Too few of your LinkedIn invitations were accepted",
    "li_identity_mismatch": "The browser is logged in to a different LinkedIn account than yours",
    "gmail_sending_limit": "Gmail said the sending limit was reached",
    "gmail_auth_failed": "Gmail refused the app password",
    "gmail_security": "Google asked to verify it is you or sent a security alert",
    "gmail_logged_out": "The agent's browser is signed out of Gmail (the session expired)",
    "gmail_identity_mismatch": "The browser is logged in to a different Gmail account than yours",
    "gmail_unexpected_state": "Gmail answered in a way the agent could not classify",
    "gmail_bounces_24h": "Too many emails bounced within a day",
    "bounces_24h": "Too many emails bounced within a day",
    "bounce_rate_pause": "Too many recent emails bounced, so cold email is paused",
    "bounce_rate_stop": "Far too many recent emails bounced, so cold email is stopped",
    "complaints_30d": "People marked emails as spam too often in the last 30 days",
    "site_challenge": "The job site showed a security check or CAPTCHA",
    "site_logged_out": "The job site signed the agent's browser out (the session expired)",
    "ats_blocked": "A job application site blocked the agent or showed a bot check",
    "dup_denials": "The duplicate check refused three sends in one cycle",
    # the optional email finder (U10 enrich.budget.trip; scopes enrich and enrich:<service>)
    "auth_failed": "The service refused your API key",
    "tls": "The secure connection to the service could not be verified",
    "schema_changed": "The service's answers could not be read; it may have changed",
    "unexpected_phone": "The service sent a phone number although phone lookups are off",
    "bounce_strikes": "Too many addresses from this service bounced",
    "provider_errors": "Several calls to the service failed in a row",
    "consecutive_errors": "A job source failed several times in a row",
    # email codes, site accounts and CAPTCHAs on job forms (scope ats:<platform>)
    "otp_failures": "Email codes for this site failed 3 times today",
    "account_failures": "Creating or signing in to a site account failed twice today",
    "captcha_repeat": "CAPTCHAs kept coming back on this site today",
    "ats_security": "A job form asked for a phone or authenticator code, an identity check or a social sign-in",
}
# Dynamic codes from sources/__init__.py: "http_<status>" for a job API answer (http_429 has its own sentence).
_HTTP_CODE = re.compile(r"^http_(\d{3})$")

TODO_BY_REASON = {
    "captcha": "Open the site yourself, solve the check if you want to, then run ./jobhunter breaker reset {scope} "
               "after the waiting time.",
    "checkpoint": "Open the site yourself and clear the check. Then run ./jobhunter breaker reset {scope} after the "
                  "waiting time.",
    "restricted": "Log in yourself and follow the site's steps. Wait at least 7 days after it is restored, then run "
                  "./jobhunter breaker reset {scope}.",
    "unusual_activity": "Log in yourself and follow the site's steps. Then run ./jobhunter breaker reset {scope}.",
    "login_wall": "Run ./jobhunter browser login {site} and log in again, then ./jobhunter breaker reset {scope}.",
    "logged_out": "Run ./jobhunter browser login {site} and log in again, then ./jobhunter breaker reset {scope}.",
    "invite_limit": "Nothing on LinkedIn. Run ./jobhunter breaker reset {scope} after the waiting time; invitations "
                    "then resume at a lower pace.",
    "smtp_auth": "Make a new Google app password and run ./jobhunter mail connect.",
    "imap_auth": "Make a new Google app password and run ./jobhunter mail connect.",
    "identity_mismatch": "Log in with your own account in the job hunter browser, then run ./jobhunter breaker reset "
                         "{scope}.",
    "clock_skew": "Fix your computer's date and time, then run ./jobhunter breaker reset {scope}.",
    "audit_mismatch": "Run ./jobhunter inbox and review the item, then ./jobhunter breaker reset {scope}.",
    "guard_missing": "Run ./jobhunter doctor. It checks the safety plugin and says how to fix it.",
    "reviewer_tampered": "Run ./install.sh again to restore the reviewer instructions.",
    "paused": "Run ./jobhunter resume when you want the agent to continue.",
    "consent_revoked": "Nothing, if you meant it: the agent leaves this site alone. To let it use your login "
                       "there again, run ./jobhunter browser consent {site}; that also clears this stop.",
}
# A pause of one area (a pause:<area> breaker): plain `./jobhunter resume` means all and only clears the global
# pause file, so the area must be named.
TODO_PAUSED_AREA = "Run ./jobhunter resume {area} when you want the agent to continue there."
# ats:<platform> stops of email codes, site accounts and CAPTCHAs
TODO_ATS = {
    "otp_failures": "Check the site in the agent's browser and the sender in your mailbox, then ./jobhunter breaker "
                    "reset {scope}.",
    "account_failures": "Check the site in the agent's browser (sign in yourself once if needed), then ./jobhunter "
                        "breaker reset {scope}.",
    "captcha_repeat": "Nothing now. The site keeps asking for CAPTCHAs; after the waiting time run ./jobhunter breaker "
                      "reset {scope}.",
    "ats_security": "Open the site yourself and look at the check. The agent never answers phone or identity checks. "
                    "Then run ./jobhunter breaker reset {scope}.",
    "consent_revoked": "Nothing, if you meant it: the agent leaves this site's codes and accounts alone. To allow them "
                       "again, run ./jobhunter browser consent; that also clears this stop.",
}
# job status reasons of the CAPTCHA hand-off and the account steps (Sheet and status)
STATUS_REASON_LABEL = {"captcha_wait": "Waiting for you to solve a CAPTCHA",
                       "captcha_resolved": "CAPTCHA solved; queued again",
                       "captcha_timeout": "CAPTCHA not solved in time",
                       "account_terms": "The account form needs a box only you can tick"}
# The sites `./jobhunter browser login <site>` opens (open_login_page in the jobhunter wrapper): every site the
# consent step knows (identity.CONSENT_SITES). Other scopes get TODO_LOGIN_OTHER instead of a command that
# would stop with a usage error.
BROWSER_LOGIN_SITES = ("gmail", "linkedin", "naukri", "indeed", "glassdoor", "foundit", "instahyre", "wellfound",
                       "cutshort", "hirist", "iimjobs", "yc")
TODO_LOGIN_OTHER = "Open the site in the jobhunter browser profile and log in again yourself, then run " \
                   "./jobhunter breaker reset {scope}."
# The real breaker codes (see REASON_SENTENCE) reuse the advice written for the generic codes.
TODO_BY_REASON.update({
    "li_challenge": TODO_BY_REASON["checkpoint"],
    "site_challenge": TODO_BY_REASON["checkpoint"],
    "ats_blocked": TODO_BY_REASON["checkpoint"],
    "li_restricted": TODO_BY_REASON["restricted"],
    "li_logged_out": TODO_BY_REASON["logged_out"],
    "li_invite_limit": TODO_BY_REASON["invite_limit"],
    "gmail_auth_failed": TODO_BY_REASON["smtp_auth"],
    "li_identity_mismatch": TODO_BY_REASON["identity_mismatch"],
    "gmail_identity_mismatch": TODO_BY_REASON["identity_mismatch"],
    "li_security_email": "Read the security email yourself and follow its steps if it is real. Then run "
                         "./jobhunter breaker reset {scope} after the waiting time.",
    "gmail_security": "Log in to Gmail yourself and follow Google's steps if it asks. When things look normal, "
                      "run ./jobhunter breaker reset {scope} after the waiting time.",
})
# An expired session of a consented site (gmail_logged_out, site_logged_out; change request item 4): the login
# comes back by copying it from Chrome again (macOS) or by logging in inside the agent's window, then the read-only
# check and the reset. No cooldown, so no waiting time. A scope without a login page gets TODO_LOGIN_OTHER.
TODO_SESSION_EXPIRED = "Log in again: on a Mac run ./jobhunter browser import to copy your Chrome login (sign in " \
                       "to the site in Chrome first if it is signed out there too), or run ./jobhunter browser " \
                       "login {site} and log in in the agent's window (./jobhunter browser consent {site} changes " \
                       "which way). Then run ./jobhunter browser check {site} and ./jobhunter breaker reset {scope}."
TODO_BY_REASON.update({"gmail_logged_out": TODO_SESSION_EXPIRED, "site_logged_out": TODO_SESSION_EXPIRED})
TODO_DEFAULT = "Look at the site or inbox yourself. When things look normal, run ./jobhunter breaker reset {scope} " \
               "after the waiting time."
TODO_AUTO = "Nothing. The agent waits and tries again by itself later."


def reason_sentence(reason_code: str | None, detail: str | None = None) -> str:
    code = (reason_code or "").strip()
    s = REASON_SENTENCE.get(code)
    if not s:
        http = _HTTP_CODE.match(code)
        if http:
            s = "The site answered with an error (HTTP %s)" % http.group(1)
        else:
            s = code.replace("_", " ").capitalize() if code else "The agent stopped"
    if detail:
        d = " ".join(str(detail).split())[:300]
        if d and d.lower() not in s.lower():
            s += ". " + d
    return s


def login_site(scope: str | None) -> str | None:
    """The <site> argument of `./jobhunter browser login` for a breaker scope, or None when the wrapper has no
    login page for it (linkedin.invites -> linkedin, site:naukri -> naukri, ats -> None)."""
    s = str(scope or "")
    root = s[5:] if s.startswith("site:") else s.split(".", 1)[0]
    return root if root in BROWSER_LOGIN_SITES else None


def todo_sentence(scope: str | None, reason_code: str | None, requires_human: bool = True,
                  waiting: bool = True) -> str:
    """What the person should do about a stop. waiting=False: the breaker has no cooldown (it only needs the
    person's reset), so the sentence does not mention a waiting time."""
    scope = scope or "global"
    if scope.startswith("api:") or not requires_human:
        return TODO_AUTO
    if scope.startswith("pause:"):
        return TODO_PAUSED_AREA.format(area=scope[6:] or "all")
    if scope == "enrich" or scope.startswith("enrich:"):
        provider = scope[7:]
        if provider and (reason_code or "").strip() == "auth_failed":
            return TODO_ENRICH_KEY.format(provider=provider)
        return TODO_ENRICH.format(scope=scope)
    if scope.startswith("ats:") and (reason_code or "").strip() in TODO_ATS:
        s = TODO_ATS[(reason_code or "").strip()].format(scope=scope)
        return s if waiting else s.replace(" after the waiting time", "")
    template = TODO_BY_REASON.get((reason_code or "").strip()) or TODO_DEFAULT
    site = login_site(scope)
    if "{site}" in template and site is None:
        template = TODO_LOGIN_OTHER
    s = template.format(scope=scope, site=site or "")
    if not waiting:
        s = s.replace(" after the waiting time", "")
    return s


def cooldown_until(until: str | None, tripped_at: str | None, requires_human: bool) -> str | None:
    """The 'Paused until' value for an open stop. A stop that waits for the person's reset and has no cooldown
    stores min_cooldown_until equal to (or before) the trip time; showing that time would read as a stop that
    already ended, so it is left blank (the 'What you need to do' column says to reset it)."""
    if not until:
        return None
    if requires_human and tripped_at and str(until) <= str(tripped_at):
        return None
    return until


# ---------------------------------------------------------------- people and text helpers
def person_name(full_name: str | None, first_name: str | None = None, style: str = "first_last_initial") -> str:
    """'Meera Nair' -> 'Meera N.' (first_last_initial, default), 'Meera Nair' (full) or 'Meera' (first)."""
    full = " ".join(str(full_name or "").split())
    first = " ".join(str(first_name or "").split())
    parts = full.split(" ") if full else []
    if not first and parts:
        first = parts[0]
    if style == "full":
        return full or first
    if style == "first":
        return first or full
    if len(parts) >= 2:
        last = parts[-1]
        initial = last[:1].upper()
        return "%s %s." % (first or parts[0], initial) if initial else (first or parts[0])
    return first or full


def first_line(text: str | None, limit: int = 80) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."
    return ""


def clip(text: str | None, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit - 3].rstrip() + "..."


def safe_cell(s: str) -> str:
    """Same rule as Code.gs safeText_: a value starting with =, +, @ or a minus sign gets a
    leading apostrophe."""
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s
