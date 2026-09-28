"""Reserve, act, confirm (design 2.3, 12.7): the only way anything goes out.

precheck_plan -> record_precheck -> reserve (token) -> [arm | mark_armed] -> confirm | mark_unknown | fail.
Every action row is created by reserve() inside the caller's BEGIN IMMEDIATE. The checks run in the order
of 2.3 and stop at the first failure; the triggers and unique indexes stay the last line (their errors
are mapped by errors.map_sqlite_error). dedup_hits() is the read-only set of duplicate and company rules
that reserve, `dedup check` and `precheck-plan` share; dedup_check() is the function behind `dedup check`
(other units call it, for example the email finder before a lookup).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3

from . import (breakers, ceilings, companies, config as _config, db, hooks, identity, jobstate, keys, pacing, paths,
               people)
from .canon import (canonical_send_text, fmt_ts, new_token, normalize_text, now, parse_ts, seconds_between, sha256_text,
                    ts_add)
from .errors import Denied
from .events import log_event, open_human_task

LIVE = ceilings.LIVE
KINDS = ("application", "application_email", "cold_email", "followup_email", "li_invite", "li_message",
         "li_followup", "inmail", "li_withdraw", "referral_ask")
EMAIL_KINDS = ("cold_email", "followup_email", "application_email")
LI_KINDS = ("li_invite", "li_message", "li_followup", "inmail", "li_withdraw")
FOLLOWUP_KINDS = ("followup_email", "li_followup")
APP_KINDS = ("application", "application_email")
DRAFT_KINDS = {"cold_email": ("cold_email",), "followup_email": ("followup_email",),
               "application_email": ("application_email",), "application": ("application_package",),
               "li_invite": ("li_invite_note",), "li_message": ("li_message",), "li_followup": ("li_followup",),
               "inmail": ("inmail",), "referral_ask": ("li_message", "followup_email", "cold_email"),
               "li_withdraw": ("li_invite_note",)}
# "No LinkedIn or Gmail cold writes on weekends on the conservative tier" (4.3): outreach to people who are
# not in a conversation. Applications, referral asks (only after a reply) and invite withdrawals are not.
COLD_WRITE_KINDS = ("cold_email", "followup_email", "li_invite", "li_message", "li_followup", "inmail")
# A referral ask needs a reply from the person first (6.1 step 9, outreach.referral_ask_only_after_reply).
REPLIED_CLASSES = ("positive", "neutral", "referral_offered")
PRECHECK_TTL_S = 900
DETECT_TTL_S = 600
TOKEN_TTL_S = 1800
# The lanes whose agent may hold a token (1.1.3). The replies lane reads untrusted mail and messages: it never
# holds one, although its agent (jobhunter-outreach) holds tokens in its outreach cycles.
TOKEN_LANES = ("outreach", "applier")
AGENT_FAIL_REASONS = ("not_attempted", "precondition_changed", "form_blocked_before_submit")
SYSTEM_FAIL_REASONS = {"smtp_rejected_before_data": "reserved", "smtp_rejected": "armed"}
LOCALE_TZ = {"IN": "Asia/Kolkata", "US": "America/New_York", "GB": "Europe/London", "UK": "Europe/London",
             "SG": "Asia/Singapore", "AE": "Asia/Dubai", "DE": "Europe/Berlin", "FR": "Europe/Paris",
             "NL": "Europe/Amsterdam", "AU": "Australia/Sydney", "CA": "America/Toronto", "IE": "Europe/Dublin",
             "JP": "Asia/Tokyo"}


# ---------------------------------------------------------------- lookups
def action_by_token(conn, token: str):
    if not re.match(r"^T[A-Z2-7]{11}$", token or ""):
        raise Denied("E_NOT_FOUND", "not a token: %r" % token)
    row = conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no action with token %s" % token)
    return row


def _row(conn, table: str, rid):
    if rid is None:
        return None
    return conn.execute("SELECT * FROM %s WHERE id = ?" % table, (rid,)).fetchone()


def email_route(cfg: dict) -> str:
    return "mailer" if cfg["gmail"]["route"] == "app_password" else "browser"


def canonical_platform(platform: str) -> str:
    """One spelling per platform for the ledger: a board is stored by its id (naukri), never as site:naukri,
    so every ceiling, pace and breaker count sees one platform (ceilings.site_name, detect.platform_names)."""
    p = (platform or "").strip().lower()
    return p[5:] if p.startswith("site:") else p


def platform_ok(kind: str, platform: str) -> bool:
    p = (platform or "").lower()
    if kind in EMAIL_KINDS:
        return p == "gmail"
    if kind in LI_KINDS:
        return p == "linkedin"
    if kind == "referral_ask":
        return p in ("gmail", "linkedin")
    return p == "linkedin" or bool(re.match(r"^[a-z0-9_.:-]{2,40}$", p)) and p != "gmail"


def _person_ids(conn, contact_id) -> list[int]:
    return people.group(conn, contact_id) if contact_id else []


def _company_ids(conn, company_id) -> list[int]:
    return companies.group(conn, company_id) if company_id else []


def _in(ids) -> str:
    return ",".join("?" * len(ids))


def _live_in() -> str:
    return "status IN (%s)" % ",".join("'%s'" % s for s in LIVE)


def compute_first_touch(conn, kind: str, contact_id) -> int:
    """The first_touch value the code computes (t_first_touch_rules checks it)."""
    if kind in ("cold_email", "li_invite", "inmail"):
        return 1
    if kind == "application_email":
        if contact_id is None:
            return 0
        row = _row(conn, "contacts", contact_id)
        return 0 if (row is not None and row["role_type"] == "role_inbox") else 1
    if kind == "li_message":
        ids = _person_ids(conn, contact_id) or [contact_id]
        hit = conn.execute("SELECT 1 FROM threads WHERE contact_id IN (%s) AND channel = 'linkedin' "
                           "AND state = 'invite_accepted'" % _in(ids), ids).fetchone()
        return 0 if hit else 1
    return 0


def next_li_seq(conn, contact_id) -> int:
    ids = _person_ids(conn, contact_id) or [contact_id]
    n = conn.execute("SELECT count(*) FROM actions WHERE contact_id IN (%s) AND li_msg_seq IS NOT NULL AND %s"
                     % (_in(ids), _live_in()), ids).fetchone()[0]
    return n + 1


def _meta_int(conn, key: str, fallback: int, positive: bool = False) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        v = int(row[0]) if row else None
    except (TypeError, ValueError):
        v = None
    if v is None or v < 0 or (positive and v <= 0):
        return fallback
    return v


# ---------------------------------------------------------------- dedup rules (shared)
def dedup_hits(conn, kind: str, *, job_id=None, contact_id=None, company_id=None, email=None, thread_key=None,
               first_touch=None, li_seq=None, at: str | None = None) -> list[dict]:
    """Read-only duplicate, company and exclusion rules for one planned action (the Python mirror of the
    triggers plus exclusions and target_skips). [{rule, code, detail}], empty when allowed."""
    from . import exclusions
    at = at or now()
    hits: list[dict] = []

    def hit(rule, code, detail):
        hits.append({"rule": rule, "code": code, "detail": detail})

    pids = _person_ids(conn, contact_id)
    cids = _company_ids(conn, company_id)
    # an email counts for the company that owns the recipient's domain too (2.4.2: a recipient address is a
    # company key source), so a job listed under another name cannot reach an emailed company's inbox
    email_cids, email_doms = _email_scope(conn, cids, email) if kind in ("cold_email", "application_email") \
        else ([], [])
    if first_touch is None:
        first_touch = compute_first_touch(conn, kind, contact_id) if kind in KINDS else 0
    ex = exclusions.match(conn, company_id=company_id, contact_id=contact_id, job_id=job_id, email=email)
    for e in ex:
        hit("exclusion", "E_EXCLUDED", "%s %s" % (e["type"], e["value_key"]))
    if cids and (first_touch or kind in ("followup_email", "li_followup", "li_message", "application",
                                         "application_email")):
        st = conn.execute("SELECT contact_state FROM companies WHERE id = ?", (cids[0],)).fetchone()
        if st and st[0] in ("active_thread", "do_not_contact"):
            hit("company_state", "E_COMPANY_BLOCKED", "company is %s" % st[0])
    for other in [c for c in email_cids if c not in cids]:
        st = conn.execute("SELECT contact_state FROM companies WHERE id = ? AND merged_into IS NULL", (other,)).fetchone()
        if st and st[0] in ("active_thread", "do_not_contact"):
            hit("company_state", "E_COMPANY_BLOCKED", "the address's company is %s" % st[0])
    if kind == "referral_ask":
        th = conn.execute("SELECT * FROM threads WHERE thread_key = ?", (thread_key,)).fetchone() if thread_key else None
        if th is None or th["reply_class"] not in REPLIED_CLASSES or th["state"] in ("closed", "bounced"):
            hit("referral_after_reply", "E_PRECONDITION", "a referral ask needs a reply from the person in this thread")
    if pids:
        if conn.execute("SELECT 1 FROM contacts WHERE id IN (%s) AND do_not_contact = 1" % _in(pids), pids).fetchone():
            hit("contact_dnc", "E_CONTACT_DNC", "the person is marked do not contact")
        if first_touch and conn.execute("SELECT 1 FROM actions WHERE contact_id IN (%s) AND first_touch = 1 AND %s"
                                        % (_in(pids), _live_in()), pids).fetchone():
            hit("first_touch_person", "E_DUP_PERSON", "this person already had a first touch")
        if kind == "li_message" and conn.execute("SELECT 1 FROM actions WHERE contact_id IN (%s) AND kind = "
                                                 "'li_message' AND %s" % (_in(pids), _live_in()), pids).fetchone():
            hit("li_message_person", "E_DUP_PERSON", "a LinkedIn message to this person exists")
        if kind == "referral_ask" and conn.execute("SELECT 1 FROM actions WHERE contact_id IN (%s) AND kind = "
                                                   "'referral_ask' AND %s" % (_in(pids), _live_in()), pids).fetchone():
            hit("referral_person", "E_DUP_PERSON", "a referral ask to this person exists")
        if li_seq is not None and li_seq > 2:
            hit("li_seq", "E_DUP_LI_TOUCH", "two message-bearing LinkedIn touches already")
    if cids:
        agency = conn.execute("SELECT is_agency FROM companies WHERE id = ?", (cids[0],)).fetchone()[0] == 1

        def cnt(kinds, days, extra=""):
            return conn.execute("SELECT count(*) FROM actions WHERE company_id IN (%s) AND kind IN (%s) AND %s AND "
                                "reserved_at > ?%s" % (_in(cids), _in(kinds), _live_in(), extra),
                                list(cids) + list(kinds) + [ts_add(at, days=-days)]).fetchone()[0]

        def cnt_email(days):
            return _email_count(conn, email_cids or cids, email_doms, ts_add(at, days=-days))
        if kind in ("cold_email", "application_email") and not agency:
            days = _meta_int(conn, "company_email_cooldown_days", 365, positive=True)
            if cnt_email(days):
                hit("company_cooldown", "E_COMPANY_COOLDOWN", "company emailed within %d days" % days)
        if kind in APP_KINDS and not agency:
            for key, days in (("company_apps_per_day", 1), ("company_apps_per_30d", 30), ("company_apps_per_90d", 90)):
                if cnt(APP_KINDS, days) >= _meta_int(conn, key, 1):
                    hit("company_app_cap", "E_COMPANY_APP_CAP", "%s reached" % key)
                    break
        if agency:
            if kind in ("cold_email", "application_email"):
                for key, days in (("agency_emails_per_day", 1), ("agency_emails_per_30d", 30)):
                    if cnt_email(days) >= _meta_int(conn, key, 1):
                        hit("agency_cap", "E_AGENCY_CAP", "%s reached" % key)
                        break
            if kind in APP_KINDS:
                for key, days in (("agency_apps_per_day", 1), ("agency_apps_per_30d", 30)):
                    if cnt(APP_KINDS, days) >= _meta_int(conn, key, 1):
                        hit("agency_cap", "E_AGENCY_CAP", "%s reached" % key)
                        break
        if kind in ("li_invite", "inmail") or (kind == "li_message" and first_touch):
            n = conn.execute("SELECT count(*) FROM actions WHERE company_id IN (%s) AND (kind IN ('li_invite','inmail') "
                             "OR (kind = 'li_message' AND first_touch = 1)) AND %s AND reserved_at > ?"
                             % (_in(cids), _live_in()), list(cids) + [ts_add(at, days=-7)]).fetchone()[0]
            if n >= _meta_int(conn, "li_invites_per_company_per_7d", 1):
                hit("company_li_cap", "E_COMPANY_LI_CAP", "LinkedIn first touches at this company this week")
    if job_id and kind in APP_KINDS:
        if conn.execute("SELECT 1 FROM actions WHERE job_id = ? AND kind IN ('application','application_email') AND %s"
                        % _live_in(), (job_id,)).fetchone():
            hit("application_job", "E_DUP_JOB", "this job already has an application")
    if kind in FOLLOWUP_KINDS:
        if not thread_key:
            hit("followup_binding", "E_FOLLOWUP_BINDING", "a follow-up needs its thread")
        else:
            if conn.execute("SELECT 1 FROM actions WHERE thread_key = ? AND kind IN ('followup_email','li_followup') "
                            "AND %s" % _live_in(), (thread_key,)).fetchone():
                hit("followup_thread", "E_DUP_THREAD_FOLLOWUP", "this thread already had its follow-up")
            t = conn.execute("SELECT state, followup_action_id FROM threads WHERE thread_key = ?", (thread_key,)).fetchone()
            if t is None or t["state"] != "open" or t["followup_action_id"] is not None:
                hit("followup_binding", "E_FOLLOWUP_BINDING", "the thread is not open for a follow-up")
    tkeys = []
    for table, col, rid, prefix in (("contacts", "contact_uid", contact_id, "contact:"),
                                    ("jobs", "job_uid", job_id, "job:"), ("companies", "company_uid", company_id, "company:")):
        if rid:
            r = conn.execute("SELECT %s FROM %s WHERE id = ?" % (col, table), (rid,)).fetchone()
            if r:
                tkeys.append(prefix + r[0])
    if tkeys:
        r = conn.execute("SELECT target_key, until FROM target_skips WHERE target_key IN (%s) AND until > ?"
                         % _in(tkeys), tkeys + [at]).fetchone()
        if r:
            hit("target_skip", "E_TARGET_SKIPPED", "%s is skipped until %s" % (r[0], r[1]))
    return hits


# `dedup check --kind` names: the action a check stands for, and the rules a person, company or job check
# reports (the other kinds report every rule of their action).
DEDUP_ACTION_FOR = {"job": "application", "person": "cold_email", "company": "cold_email", "application": "application",
                    "cold_email": "cold_email", "li_invite": "li_invite", "followup": "followup_email"}
DEDUP_RULES_BY_KIND = {
    "person": {"exclusion", "contact_dnc", "first_touch_person", "li_message_person", "target_skip"},
    "company": {"exclusion", "company_state", "company_cooldown", "company_app_cap", "agency_cap", "company_li_cap",
                "target_skip"},
    "job": {"exclusion", "application_job", "company_state", "target_skip"},
}


def dedup_check(conn, kind: str, *, job_id=None, contact_id=None, company_id=None, email=None,
                thread_key=None) -> dict:
    """The read-only rules behind `dedup check` (12.10): {allowed, hits: [{rule, code, detail}]}. `kind` is a
    `dedup check --kind` name (job, person, company, application, cold_email, li_invite, followup) or an action
    kind. The company comes from the contact or the job when not given. Writes nothing."""
    if kind in DEDUP_ACTION_FOR:
        action = DEDUP_ACTION_FOR[kind]
    elif kind in KINDS:
        action = kind
    else:
        raise Denied("E_VALIDATION", "unknown dedup kind %r" % kind)
    if contact_id is not None and company_id is None:
        row = conn.execute("SELECT company_id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        company_id = row[0] if row is not None else None
    if job_id is not None and company_id is None:
        row = conn.execute("SELECT company_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        company_id = row[0] if row is not None else None
    hits = dedup_hits(conn, action, job_id=job_id, contact_id=contact_id, company_id=company_id, email=email,
                      thread_key=thread_key)
    if kind in DEDUP_RULES_BY_KIND:
        hits = [h for h in hits if h["rule"] in DEDUP_RULES_BY_KIND[kind]]
    return {"allowed": not hits, "hits": hits}


def recipient_company(conn, recipient) -> int | None:
    """The surviving company that owns the address's registrable domain (a dom: alias), read only."""
    if not recipient or "@" not in str(recipient):
        return None
    reg = keys.company_domain(recipient)
    if not reg:
        return None
    row = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", ("dom:" + reg,)).fetchone()
    return companies.survivor(conn, row[0]) if row else None


def _email_scope(conn, cids: list[int], email) -> tuple[list[int], list[str]]:
    """(company ids, domains) an email to `email` counts for: the target company group, the group of the
    company owning the address's domain, and the dom: aliases of all of them plus the address's own domain."""
    if not email and not cids:
        return [], []
    out = list(cids)
    rc = recipient_company(conn, email)
    if rc is not None:
        for c in _company_ids(conn, rc):
            if c not in out:
                out.append(c)
    doms = set()
    if out:
        for (k,) in conn.execute("SELECT alias_key FROM company_aliases WHERE company_id IN (%s) AND alias_key LIKE "
                                 "'dom:%%'" % _in(out), out):
            doms.add(k[4:])
    reg = keys.company_domain(email) if email and "@" in str(email) else None
    if reg:
        doms.add(reg)
    return out, sorted(doms)


def _email_count(conn, cids: list[int], doms: list[str], since: str) -> int:
    """Live cold and application emails since `since` to the companies or to any address at the domains."""
    conds, args = [], []
    if cids:
        conds.append("company_id IN (%s)" % _in(cids))
        args += list(cids)
    for d in doms:
        conds.append("lower(recipient) LIKE ? OR lower(recipient) LIKE ?")
        args += ["%@" + d, "%@%." + d]
    if not conds:
        return 0
    return conn.execute("SELECT count(*) FROM actions WHERE kind IN ('cold_email','application_email') AND %s AND "
                        "reserved_at > ? AND (%s)" % (_live_in(), " OR ".join(conds)), [since] + args).fetchone()[0]


def first_blocking(hits: list[dict]) -> None:
    if hits:
        h = hits[0]
        raise Denied(h["code"], h["detail"], data={"hits": hits})


# ---------------------------------------------------------------- targets
def resolve_target(conn, kind: str, *, draft=None, job_id=None, contact_id=None, thread_key=None) -> dict:
    """Job, contact, company, thread and recipient of a planned action. Follow-up kinds take everything
    from the thread; nothing else may contradict the draft."""
    t = {"job_id": None, "contact_id": None, "company_id": None, "thread_key": None, "recipient": None,
         "thread": None}
    if draft is not None:
        for k in ("job_id", "contact_id", "company_id", "thread_key"):
            t[k] = draft[k]
        t["recipient"] = draft["recipient"]
        for k, v in (("job_id", job_id), ("contact_id", contact_id), ("thread_key", thread_key)):
            if v is not None and t[k] is not None and v != t[k]:
                raise Denied("E_VALIDATION", "--%s does not match the draft" % k.replace("_id", ""))
            if v is not None and t[k] is None and k != "thread_key":
                raise Denied("E_VALIDATION", "--%s is not the draft's target" % k.replace("_id", ""))
        if thread_key and not t["thread_key"]:
            t["thread_key"] = thread_key
    else:
        t.update(job_id=job_id, contact_id=contact_id, thread_key=thread_key)
    if kind == "referral_ask" and not t["thread_key"]:
        raise Denied("E_FOLLOWUP_BINDING", "a referral ask belongs to the thread in which the person replied "
                     "(--thread)")
    if kind in FOLLOWUP_KINDS or kind == "referral_ask":
        if not t["thread_key"]:
            raise Denied("E_FOLLOWUP_BINDING", "a follow-up needs --thread")
        th = conn.execute("SELECT * FROM threads WHERE thread_key = ?", (t["thread_key"],)).fetchone()
        if th is None:
            raise Denied("E_FOLLOWUP_BINDING", "no thread %s" % t["thread_key"])
        t["thread"] = th
        t["contact_id"], t["company_id"], t["job_id"] = th["contact_id"], th["company_id"], th["job_id"]
        first = conn.execute("SELECT recipient FROM actions WHERE id = ?", (th["first_action_id"],)).fetchone()
        t["recipient"] = first[0] if first else None
        if draft is not None and draft["contact_id"] is not None and th["contact_id"] is not None and \
                people.survivor(conn, draft["contact_id"]) != people.survivor(conn, th["contact_id"]):
            raise Denied("E_FOLLOWUP_BINDING", "the draft names another person than the thread")
        return _survivors(conn, t)
    if t["contact_id"] is not None:
        t["contact_id"] = people.survivor(conn, t["contact_id"])
        c = _row(conn, "contacts", t["contact_id"])
        if t["company_id"] is None and c is not None:
            t["company_id"] = c["company_id"]
        if t["recipient"] is None and c is not None:
            if kind in EMAIL_KINDS and c["email"]:
                t["recipient"] = keys.normalize_email(c["email"])
            elif kind in LI_KINDS and c["li_slug"]:
                t["recipient"] = c["li_slug"]
    if t["job_id"] is not None:
        j = _row(conn, "jobs", t["job_id"])
        if j is None:
            raise Denied("E_NOT_FOUND", "no job %r" % t["job_id"])
        if t["company_id"] is None:
            t["company_id"] = j["company_id"]
        if kind == "application_email" and t["recipient"] is None and j["apply_email"]:
            t["recipient"] = keys.normalize_email(j["apply_email"])
    if kind in ("cold_email", "application_email") and t["recipient"]:
        rc = recipient_company(conn, t["recipient"])
        if t["company_id"] is None and rc is not None:
            t["company_id"] = rc
    if kind in ("cold_email", "li_invite", "li_message", "inmail", "li_withdraw") and t["contact_id"] is None:
        raise Denied("E_VALIDATION", "%s needs a contact" % kind)
    if kind in APP_KINDS and t["job_id"] is None:
        raise Denied("E_VALIDATION", "%s needs a job" % kind)
    return _survivors(conn, t)


def _survivors(conn, t: dict) -> dict:
    if t.get("company_id") is not None:
        t["company_id"] = companies.survivor(conn, t["company_id"])
    if t.get("contact_id") is not None:
        t["contact_id"] = people.survivor(conn, t["contact_id"])
    return t


def _uids(conn, t: dict) -> dict:
    def uid(table, col, rid):
        if rid is None:
            return None
        r = conn.execute("SELECT %s FROM %s WHERE id = ?" % (col, table), (rid,)).fetchone()
        return r[0] if r else None
    return {"job_uid": uid("jobs", "job_uid", t.get("job_id")), "contact_uid": uid("contacts", "contact_uid",
                                                                                     t.get("contact_id")),
            "company_uid": uid("companies", "company_uid", t.get("company_id")), "thread_key": t.get("thread_key"),
            "recipient": t.get("recipient")}


# ---------------------------------------------------------------- precheck plan and record
CHECK_SPECS = {
    "email": [("sent_to_address", "int"), ("sent_to_other_addresses", "int"), ("sent_company_query", "int"),
              ("outbox_query", "int"), ("scheduled_query", "int")],
    "followup_email": [("thread_has_reply", "bool"), ("company_inbound_since_first", "int")],
    "li_invite": [("profile_button", "enum:Connect|Pending|Message|Follow|None"), ("vanity_slug", "str")],
    "li_message": [("conversation_has_our_message", "bool"), ("connection_degree", "enum:1st|2nd|3rd")],
    "li_followup": [("conversation_has_reply", "bool")],
    "inmail": [("conversation_has_our_message", "bool")],
    "li_withdraw": [("invite_pending", "bool")],
    "referral_ask": [("already_asked", "bool")],
    "application": [("applied_badge", "bool"), ("already_applied_text", "bool")],
}


def _spec_key(kind: str) -> str:
    return "email" if kind in ("cold_email", "application_email") else kind


def _gmail_date(ts: str) -> str:
    return ts[:10].replace("-", "/")


def precheck_plan(conn, kind: str, *, route: str, job_id=None, contact_id=None, thread_key=None) -> dict:
    """The exact checks (and Gmail queries) to run before `gate precheck` (12.7). Cheap dedup first."""
    if kind not in KINDS:
        raise Denied("E_VALIDATION", "unknown action kind %r" % kind)
    t = resolve_target(conn, kind, job_id=job_id, contact_id=contact_id, thread_key=thread_key)
    first_blocking(dedup_hits(conn, kind, job_id=t["job_id"], contact_id=t["contact_id"],
                              company_id=t["company_id"], thread_key=t["thread_key"], email=t["recipient"]
                              if kind in EMAIL_KINDS else None))
    checks = []
    addr = t["recipient"] or ""
    if kind in ("cold_email", "application_email"):
        others = []
        if t["contact_id"]:
            ids = _person_ids(conn, t["contact_id"])
            others = sorted({r[0][6:] for r in conn.execute("SELECT key FROM contact_keys WHERE contact_id IN (%s) "
                                                            "AND kind = 'email'" % _in(ids), ids)} - {addr})
        cq = _company_query(conn, t["company_id"], addr)
        checks = [
            {"name": "sent_to_address", "how": "count Sent messages to the address", "query": "in:sent to:%s" % addr},
            {"name": "sent_to_other_addresses", "how": "count Sent messages to the person's other addresses (0 when "
             "none)", "query": ("in:sent {%s}" % " ".join("to:" + o for o in others)) if others else None},
            {"name": "sent_company_query", "how": "count Sent messages that name the company",
             "query": ("in:sent " + cq) if cq else None},
            {"name": "outbox_query", "how": "count messages waiting in the Outbox", "query": "in:outbox to:%s" % addr},
            {"name": "scheduled_query", "how": "count scheduled messages", "query": "in:scheduled to:%s" % addr}]
    elif kind == "followup_email":
        th = t["thread"]
        first = conn.execute("SELECT sent_at, reserved_at FROM actions WHERE id = ?", (th["first_action_id"],)).fetchone()
        co = _row(conn, "companies", t["company_id"])
        dom = co["domain"] if co is not None else None
        # no company domain on file: the registrable domain of the address we wrote to (never free mail)
        dom = dom or (keys.company_domain(addr) if "@" in addr else None)
        since = _gmail_date(first["sent_at"] or first["reserved_at"]) if first else None
        if th["first_message_id"]:
            reply_q = "rfc822msgid:%s" % th["first_message_id"]
        else:
            # a web-route send has no Message-ID: any message from the person since the first send
            reply_q = ("from:%s after:%s -in:sent" % (addr, since)) if addr and since else None
        checks = [{"name": "thread_has_reply", "how": "open the thread and look for any inbound message",
                   "query": reply_q},
                  {"name": "company_inbound_since_first", "how": "count inbound messages from the company domain",
                   "query": ("from:(%s) after:%s" % (dom, since)) if dom and since else None}]
    else:
        hows = {"profile_button": "read the main button on the person's profile page",
                "vanity_slug": "read the /in/<slug> part of the profile URL",
                "conversation_has_our_message": "open the conversation and look for a message from you",
                "connection_degree": "read the connection degree on the profile",
                "conversation_has_reply": "open the conversation and look for a reply",
                "invite_pending": "find the invite on the invitation manager Sent page",
                "already_asked": "look for an earlier referral request in the conversation",
                "applied_badge": "look for an Applied badge or state on the posting",
                "already_applied_text": "look for 'already applied' text on the page"}
        for name, _typ in CHECK_SPECS[kind]:
            checks.append({"name": name, "how": hows[name], "query": None})
    for c, spec in zip(checks, CHECK_SPECS[_spec_key(kind)]):
        c["type"] = spec[1]
    return {"kind": kind, "route": route, "checks": checks, "valid_for_s": PRECHECK_TTL_S, "target": _uids(conn, t)}


def _company_query(conn, company_id, addr: str) -> str | None:
    """Gmail search terms for the company and for the company owning the address's domain, plus that domain,
    e.g. ("kc retail systems" OR kcretail OR kestrel.example)."""
    parts = []
    rc = recipient_company(conn, addr)
    for cid in (company_id, rc):
        if cid is not None:
            q = companies.precheck_query(conn, cid)
            if q and q != "()":
                parts.append(q[1:-1])
    reg = keys.company_domain(addr) if addr and "@" in addr else None
    if reg:
        parts.append(reg)
    terms = []
    for part in parts:
        for term in part.split(" OR "):
            if term and term not in terms:
                terms.append(term)
    return "(" + " OR ".join(terms) + ")" if terms else None


def _validate_checks(kind: str, evidence: dict) -> dict:
    spec = CHECK_SPECS[_spec_key(kind)]
    got = evidence.get("checks")
    if not isinstance(got, list) or not all(isinstance(c, dict) and set(c) <= {"name", "value"} for c in got):
        raise Denied("E_SCHEMA", "checks must be a list of {name, value}")
    names = [c.get("name") for c in got]
    want = [n for n, _ in spec]
    if sorted(names) != sorted(want) or len(set(names)) != len(names):
        raise Denied("E_VALIDATION", "precheck needs exactly the checks %s" % ", ".join(want), data={"got": names})
    vals = {}
    for c in got:
        typ = dict(spec)[c["name"]]
        v = c.get("value")
        if typ == "int":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise Denied("E_VALIDATION", "%s must be a non-negative integer" % c["name"])
        elif typ == "bool":
            if not isinstance(v, bool):
                raise Denied("E_VALIDATION", "%s must be true or false" % c["name"])
        elif typ.startswith("enum:"):
            if v not in typ[5:].split("|"):
                raise Denied("E_VALIDATION", "%s must be one of %s" % (c["name"], typ[5:].replace("|", ", ")))
        elif not isinstance(v, str) or not v:
            raise Denied("E_VALIDATION", "%s must be a string" % c["name"])
        vals[c["name"]] = v
    return vals


def _precheck_result(conn, kind: str, vals: dict, t: dict) -> tuple[str, str]:
    if _spec_key(kind) == "email":
        return ("already_done", "sent before") if any(v > 0 for v in vals.values()) else ("clear", "")
    if kind == "followup_email":
        return ("already_done", "they replied") if (vals["thread_has_reply"] or vals["company_inbound_since_first"] > 0) \
            else ("clear", "")
    if kind == "li_invite":
        c = _row(conn, "contacts", t["contact_id"])
        want = ("li:" + c["li_slug"]) if c is not None and c["li_slug"] else None
        got = keys.linkedin_keys("https://www.linkedin.com/in/" + vals["vanity_slug"].strip("/"))[0][0]
        if want is None or got != want:
            raise Denied("E_VALIDATION", "the profile slug does not match the contact", data={"got": got, "want": want})
        b = vals["profile_button"]
        if b == "Pending":
            return "already_done", "invite pending"
        if b == "Message":
            return "uncertain", "already connected: use a first-touch li_message"
        if b in ("Follow", "None"):
            return "uncertain", "no Connect button"
        return "clear", ""
    if kind == "li_message":
        if vals["conversation_has_our_message"]:
            return "already_done", "we already wrote"
        if vals["connection_degree"] != "1st":
            return "uncertain", "not a first-degree connection"
        return "clear", ""
    if kind == "li_followup":
        return ("already_done", "they replied") if vals["conversation_has_reply"] else ("clear", "")
    if kind == "inmail":
        return ("already_done", "we already wrote") if vals["conversation_has_our_message"] else ("clear", "")
    if kind == "li_withdraw":
        return ("clear", "") if vals["invite_pending"] else ("already_done", "nothing pending")
    if kind == "referral_ask":
        return ("already_done", "already asked") if vals["already_asked"] else ("clear", "")
    return ("already_done", "applied before") if (vals["applied_badge"] or vals["already_applied_text"]) \
        else ("clear", "")


def record_precheck(conn, kind: str, platform: str, evidence: dict, source: str, **target) -> dict:
    """Validate the precheck evidence (12.7) and record the result. already_done records an imported
    action (blocked forever); uncertain opens a human task."""
    if kind not in KINDS:
        raise Denied("E_VALIDATION", "unknown action kind %r" % kind)
    if source not in ("agent", "code_imap"):
        raise Denied("E_VALIDATION", "precheck source must be agent or code_imap")
    if not isinstance(evidence, dict):
        raise Denied("E_SCHEMA", "precheck evidence must be a JSON object")
    unknown = set(evidence) - {"kind", "platform", "observed_at", "page_url", "checks"}
    if unknown:
        raise Denied("E_SCHEMA", "unknown keys: %s" % ", ".join(sorted(unknown)))
    if evidence.get("kind") != kind or evidence.get("platform") != platform:
        raise Denied("E_VALIDATION", "evidence kind and platform must match the command")
    if not platform_ok(kind, platform):
        raise Denied("E_VALIDATION", "platform %s does not fit %s" % (platform, kind))
    platform = canonical_platform(platform)
    try:
        observed = evidence.get("observed_at") or now()
        age = seconds_between(observed, now())
    except ValueError:
        raise Denied("E_VALIDATION", "observed_at must be a UTC timestamp")
    if age > PRECHECK_TTL_S or age < -300:
        raise Denied("E_VALIDATION", "observed_at is not recent")
    vals = _validate_checks(kind, evidence)
    t = resolve_target(conn, kind, job_id=target.get("job_id"), contact_id=target.get("contact_id"),
                       thread_key=target.get("thread_key"))
    _referral_channel(kind, platform, t)
    result, why = _precheck_result(conn, kind, vals, t)
    ts = now()
    cur = conn.execute("INSERT INTO prechecks (kind, platform, source, contact_id, company_id, job_id, thread_key, result, "
                       "checks_json, cycle_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (kind, platform, source, t["contact_id"], t["company_id"], t["job_id"], t["thread_key"], result,
                        json.dumps(evidence.get("checks"), sort_keys=True), target.get("cycle_id"), ts))
    pid = cur.lastrowid
    out = {"precheck_id": pid, "result": result, "valid_until": ts_add(ts, seconds=PRECHECK_TTL_S), "why": why}
    if result == "already_done":
        out["imported_token"] = _import_action(conn, kind, platform, t, "precheck %d: %s" % (pid, why))
        if kind in APP_KINDS and t["job_id"]:
            st = conn.execute("SELECT status FROM jobs WHERE id = ?", (t["job_id"],)).fetchone()[0]
            if jobstate.job_transition_allowed(st, "closed") and st != "closed":
                jobstate.set_job_status(conn, t["job_id"], "closed", "already_applied", "gate.precheck")
        if kind == "followup_email" and t["thread"] is not None:
            open_human_task(conn, "review_reply", "They wrote back before the follow-up; read the thread.",
                            thread_id=t["thread"]["id"], company_id=t["company_id"])
    elif result == "uncertain":
        open_human_task(conn, "resolve_unknown", "Precheck for %s was uncertain: %s" % (kind, why),
                        job_id=t["job_id"], company_id=t["company_id"])
    log_event(conn, "precheck", precheck_id=pid, action_kind=kind, result=result)
    return out


def _import_action(conn, kind: str, platform: str, t: dict, evidence: str, sent_at: str | None = None) -> str | None:
    return _import_action_ex(conn, kind, platform, t, evidence, sent_at)[0]


def _import_action_ex(conn, kind: str, platform: str, t: dict, evidence: str,
                      sent_at: str | None = None) -> tuple[str | None, str | None]:
    """Record an action that happened outside the ledger (status imported): (token, None), or (None, code)
    when a unique index or a sequence rule already holds the slot (the slot is blocked either way). Imported
    history is never refused by the company cooldown, caps or blocked-state triggers (route 'import' is
    exempt): it happened, and it must block what comes after it. Any other error is raised."""
    from .errors import exit_code, map_sqlite_error
    token = new_token()
    ts = now()
    if sent_at:
        try:
            sent_at = fmt_ts(parse_ts(sent_at))       # '+00:00' spelling to the stored 'Z' form
        except ValueError:
            raise Denied("E_VALIDATION", "sent_at must be a UTC timestamp like 2026-09-20T10:30:00Z")
        if sent_at > ts:
            raise Denied("E_VALIDATION", "sent_at is in the future")
    ft = compute_first_touch(conn, kind, t.get("contact_id"))
    seq = None
    if kind in ("li_message", "li_followup", "inmail"):
        seq = next_li_seq(conn, t["contact_id"])
        if seq > 2:
            return None, "E_DUP_LI_TOUCH"
    conn.execute("SAVEPOINT jh_import")
    try:
        conn.execute(
            "INSERT INTO actions (token, kind, route, first_touch, li_note, li_msg_seq, platform, agent_id, contact_id, "
            "company_id, job_id, thread_key, recipient, status, reserved_at, expires_at, sent_at, resolved_at, evidence, "
            "created_at, updated_at) VALUES (?, ?, 'import', ?, 0, ?, ?, NULL, ?, ?, ?, ?, ?, 'imported', ?, ?, ?, ?, ?, ?, ?)",
            (token, kind, ft, seq, platform, t.get("contact_id"), t.get("company_id"), t.get("job_id"),
             t.get("thread_key") if (kind in FOLLOWUP_KINDS or kind == "referral_ask") else None, t.get("recipient"),
             sent_at or ts, ts, sent_at or ts, ts, evidence[:4000], ts, ts))
        conn.execute("RELEASE jh_import")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK TO jh_import")
        conn.execute("RELEASE jh_import")
        d = map_sqlite_error(exc)
        if exit_code(d.code) != 3:
            raise d
        return None, d.code
    if t.get("company_id") and kind not in ("application", "li_withdraw"):
        conn.execute("UPDATE companies SET contact_state = 'contacted', updated_at = ? WHERE id = ? AND "
                     "contact_state = 'none'", (ts, t["company_id"]))
    log_event(conn, "action_imported", token=token, action_kind=kind)
    return token, None


def import_actions(conn, items: list[dict], source: str) -> dict:
    """`actions import` (12.20): each item becomes an imported action."""
    out = {"imported": [], "skipped": []}
    for i, it in enumerate(items):
        kind = it.get("kind")
        if kind not in KINDS or not isinstance(it.get("recipient"), str):
            raise Denied("E_VALIDATION", "item %d: kind and recipient are required" % i)
        platform = canonical_platform(it.get("platform") or ("gmail" if kind in EMAIL_KINDS else "linkedin"))
        cid = None
        dom = keys.company_domain(it["recipient"]) if "@" in it["recipient"] else None
        if it.get("company_name"):
            # name and the recipient's domain together (2.4.2): the domain links this company to the one a
            # later contact at the same domain resolves to, whatever name that contact gives
            cid = companies.resolve(conn, name=it["company_name"], domain=dom, source="human")
        if "@" in it["recipient"]:
            pk = keys.person_keys(email=it["recipient"])
            if cid is None and dom:
                cid = companies.resolve(conn, domain=dom, source="email")
            fields = {"email": keys.normalize_email(it["recipient"]), "company_id": cid}
        else:
            pk = keys.person_keys(linkedin_url=it["recipient"])
            fields = {"linkedin_url": it["recipient"], "company_id": cid}
        pid = people.resolve(conn, keys=pk, fields=fields)
        sent_at = it.get("sent_at")
        if sent_at is not None and not isinstance(sent_at, str):
            raise Denied("E_VALIDATION", "item %d: sent_at must be a timestamp string" % i)
        tok, why = _import_action_ex(conn, kind, platform, {"contact_id": pid, "company_id": cid, "job_id": None,
                                                            "thread_key": None, "recipient": fields.get("email") or
                                                            it["recipient"]},
                                     "%s: %s" % (source, it.get("evidence") or ""), sent_at=sent_at or None)
        if tok:
            out["imported"].append({"index": i, "token": tok})
        else:
            out["skipped"].append({"index": i, "token": None, "reason": why})
    return out


# ---------------------------------------------------------------- hours and windows
def linkedin_day_window(conn, cfg: dict, local_date: str) -> tuple[str, str]:
    a = cfg["linkedin"]["active"]
    s0, s1 = _config.hhmm(a["start_window"][0]), _config.hhmm(a["start_window"][1])
    e0, e1 = _config.hhmm(a["end_window"][0]), _config.hhmm(a["end_window"][1])
    s = s0 + int((s1 - s0) * ceilings.unit_draw(conn, "li_start|" + local_date))
    e = e0 + int((e1 - e0) * ceilings.unit_draw(conn, "li_end|" + local_date))
    return "%02d:%02d" % divmod(s, 60), "%02d:%02d" % divmod(e, 60)


def _recipient_tz(conn, cfg: dict, contact_id):
    c = _row(conn, "contacts", contact_id) if contact_id else None
    loc = (c["locale"] or "").upper() if c is not None and c["locale"] else ""
    loc = loc.split("-")[-1] if "-" in loc else loc
    return _config.tzinfo(LOCALE_TZ.get(loc) or cfg)


def hours_ok(conn, cfg: dict, kind: str, platform: str, contact_id=None, at: _dt.datetime | None = None) -> bool:
    tz = _config.tzinfo(cfg)
    at = at or parse_ts(now())
    d = at.astimezone(tz)
    p = (platform or "").lower()
    if p in ("gmail", "linkedin") and kind in COLD_WRITE_KINDS and d.isoweekday() in (6, 7) and \
            cfg[p].get("tier") == "conservative":
        return False                    # 4.3: no cold writes on weekends on the conservative tier
    if p == "gmail":
        g = cfg["gmail"]
        if d.isoweekday() not in g["active_days"] or not _config.in_window(d, g["sender_window"]):
            return False
        r = at.astimezone(_recipient_tz(conn, cfg, contact_id))
        return r.isoweekday() in g["active_days"] and _config.in_window(r, g["recipient_window"])
    if p == "linkedin":
        a = cfg["linkedin"]["active"]
        if d.isoweekday() not in a["days"] or _config.in_window(d, a["never_between"]):
            return False
        start, end = linkedin_day_window(conn, cfg, d.strftime("%Y-%m-%d"))
        return _config.in_window(d, [start, end])
    ah = cfg["active_hours"]
    return d.isoweekday() in ah["browser_days"] and _config.in_window(d, ah["browser_window"])


def check_hours(conn, cfg: dict, kind: str, platform: str, contact_id=None) -> None:
    if hours_ok(conn, cfg, kind, platform, contact_id):
        return
    wait = _config.seconds_until(lambda t: hours_ok(conn, cfg, kind, platform, contact_id, t))
    raise Denied("E_OUTSIDE_HOURS", "outside the send window for %s" % platform, retry_after=wait)


# ---------------------------------------------------------------- guard files
def guard_log(token: str) -> list[dict]:
    path = os.path.join(paths.guard_dir(), "%s.jsonl" % token)
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"class": "commit", "unparsable": True})   # fail closed
    except FileNotFoundError:
        pass
    return out


def commit_count(token: str) -> int:
    return sum(1 for r in guard_log(token) if r.get("class") == "commit")


def guard_heartbeat() -> dict:
    """{present, fresh, age_s, install_id_ok} from state/guard/heartbeat.json (12.18)."""
    path = os.path.join(paths.guard_dir(), "heartbeat.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        return {"present": False, "fresh": False, "age_s": None, "install_id_ok": False}
    try:
        age = seconds_between(hb.get("beat_at") or "", now())
    except (ValueError, TypeError):
        age = None
    try:
        iid_ok = hb.get("install_id") == paths.home().get("install_id")
    except Denied:
        iid_ok = False
    fresh = age is not None and -60 <= age <= 600 and iid_ok
    return {"present": True, "fresh": fresh, "age_s": age, "install_id_ok": iid_ok}


# ---------------------------------------------------------------- reserve
def _referral_channel(kind: str, platform: str, t: dict) -> None:
    """A referral ask goes out in the thread where the person replied, on that thread's channel."""
    if kind != "referral_ask" or t.get("thread") is None:
        return
    want = "email" if platform == "gmail" else "linkedin"
    if t["thread"]["channel"] != want:
        raise Denied("E_FOLLOWUP_BINDING", "the referral ask must go out on the thread's own channel (%s)"
                     % ("gmail" if t["thread"]["channel"] == "email" else "linkedin"))


def agent_cycle(conn, agent_id: str, cycle_id: str | None) -> dict:
    """The running cycle an agent reserves in (per-cycle ceilings, cycle-scoped stop rules). Without --cycle
    it is the agent's newest running cycle; a named cycle must exist, be running and belong to the agent's
    lanes. No running cycle: E_PRECONDITION (run preflight first)."""
    from .cycles import LANE_AGENTS
    lanes = [lane for lane, a in LANE_AGENTS.items() if a == agent_id]
    if not lanes:
        raise Denied("E_CALLER_NOT_ALLOWED", "%s runs no cycles" % agent_id)
    if cycle_id is None:
        row = conn.execute("SELECT * FROM cycles WHERE status = 'running' AND lane IN (%s) ORDER BY started_at DESC, "
                           "rowid DESC LIMIT 1" % _in(lanes), lanes).fetchone()
        if row is None:
            raise Denied("E_PRECONDITION", "no running cycle for %s: run preflight and pass --cycle" % agent_id)
        return row
    row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if row is None:
        raise Denied("E_VALIDATION", "no cycle %s" % cycle_id)
    if row["lane"] not in lanes or (row["agent_id"] not in (agent_id, "system", "human", None)):
        raise Denied("E_CALLER_NOT_ALLOWED", "cycle %s belongs to %s" % (cycle_id, row["agent_id"] or row["lane"]))
    if row["status"] != "running":
        raise Denied("E_PRECONDITION", "cycle %s is %s; run preflight for a new cycle" % (cycle_id, row["status"]))
    return row


def running_cycle_id(conn, agent_id: str | None) -> str | None:
    """The agent's newest running cycle id, or None (commands that take an optional --cycle)."""
    if not agent_id or not agent_id.startswith("jobhunter-"):
        return None
    try:
        return agent_cycle(conn, agent_id, None)["cycle_id"]
    except Denied:
        return None


def linkedin_cycle_check(conn, cfg: dict, cycle) -> None:
    """LinkedIn writes only in an admitted LinkedIn cycle (4.3): fewer than cycles_day earlier LinkedIn cycles
    in the last 24 hours and the idle gap since the previous one, both counted at this cycle's start."""
    from .cycles import linkedin_window
    if cycle is None:
        return
    w = linkedin_window(conn, cfg, cycle["lane"], cycle_id=cycle["cycle_id"])
    if w.get("cycles_today") is not None and w["cycles_today"] >= w["cycles_max"]:
        raise Denied("E_CEILING", "LinkedIn cycles: %d of %d today before this one; no LinkedIn writes in this cycle"
                     % (w["cycles_today"], w["cycles_max"]), data={"window": "day", "linkedin_window": w})
    if not w.get("idle_ok", True):
        raise Denied("E_PACING", "this cycle started %d minutes after the last LinkedIn cycle (at least %d)"
                     % (w["idle_s"] // 60, w["idle_min_s"] // 60), data={"linkedin_window": w})


def channel_check(conn, cfg: dict, kind: str, platform: str, route: str, agent_id: str) -> None:
    ch = cfg["channels"]
    p = (platform or "").lower()
    if kind in ("cold_email", "followup_email") and not ch["email_outreach"]["enabled"]:
        raise Denied("E_CHANNEL_DISABLED", "email outreach is off")
    if kind in APP_KINDS and not ch["applications"]["enabled"]:
        raise Denied("E_CHANNEL_DISABLED", "applications are off")
    if p == "linkedin":
        if not ch["linkedin"]["enabled"]:
            raise Denied("E_CHANNEL_DISABLED", "LinkedIn writes are off (./jobhunter linkedin enable)")
        w = ch["linkedin"]["writes"]
        need = {"li_invite": "invites", "li_message": "messages", "li_followup": "messages", "referral_ask": "messages",
                "inmail": "inmail", "application": "easy_apply", "li_withdraw": "withdraw"}.get(kind)
        if need and not w.get(need):
            raise Denied("E_CHANNEL_DISABLED", "LinkedIn %s are off" % need.replace("_", " "))
    if kind in EMAIL_KINDS or (kind == "referral_ask" and p == "gmail"):
        want = email_route(cfg)
        if route != want or (want == "mailer" and agent_id != "system:mailer"):
            raise Denied("E_ROUTE_UNAVAILABLE", "email on this install goes through the %s route" % want)
    elif route != "browser":
        raise Denied("E_ROUTE_UNAVAILABLE", "%s actions use the browser route" % kind)
    if kind == "application" and p != "linkedin":
        site = ceilings.site_name(p)
        s = cfg["boards"]["sites"].get(site)
        mode = s.get("apply") if s else None
        if mode != "browser":
            raise Denied("E_CHANNEL_DISABLED", "applying on %s is %s" % (site, mode or "not configured"))
        if site == "ats_forms":
            meta = conn.execute("SELECT value FROM meta WHERE key = ?", ("ats_human_queue:" + p,)).fetchone()
            if meta and meta[0] == "1":
                raise Denied("E_CHANNEL_DISABLED", "%s applications go to your queue (no confirmation emails seen)" % p)


# qc.presend (U3) names the precise refusal; the mailer's expire-on-permanent-refusal and the agents' next
# step depend on it, so gate reserve passes it on instead of folding it into one code.
PRESEND_REFUSALS = {
    "E_QC_LINT_FAILED": "the stored text fails the presend lint",
    "E_QC_NOT_APPROVED": "the draft has no approval or no passing review for this exact text",
    "E_DRAFT_EXPIRED": "the approval expired",
    "E_QC_HASH_MISMATCH": "the text or its attachment differs from what was approved",
    "E_RESEARCH_STALE": "the research behind the text is too old; refresh it and redraft",
    "E_NOT_FOUND": "no such draft",
}


def _check_draft(conn, cfg: dict, kind: str, draft) -> dict:
    if draft is None:
        raise Denied("E_NOT_FOUND", "no such draft")
    if draft["kind"] not in DRAFT_KINDS[kind]:
        raise Denied("E_VALIDATION", "a %s draft cannot be sent as %s" % (draft["kind"], kind))
    if kind == "li_withdraw":
        return {"ok": True, "sha256": None}
    if draft["status"] != "approved" or not draft["approved_by"]:
        if draft["status"] == "expired":
            raise Denied("E_DRAFT_EXPIRED", "the draft expired")
        raise Denied("E_QC_NOT_APPROVED", "the draft is %s, not approved" % draft["status"])
    if draft["expires_at"] and draft["expires_at"] <= now():
        raise Denied("E_DRAFT_EXPIRED", "the approval expired at %s" % draft["expires_at"])
    try:
        payload = json.loads(draft["payload_json"] or "{}")
    except ValueError:
        payload = {}
    hook = payload.get("hook") if isinstance(payload, dict) else None
    if isinstance(hook, dict) and hook.get("fact_id"):
        fact = conn.execute("SELECT retrieved_at FROM research_facts WHERE fact_uid = ?", (hook["fact_id"],)).fetchone()
        max_age = int(cfg["outreach"]["research"]["facts_max_age_days"])
        if fact is None or seconds_between(fact[0] if "T" in fact[0] else fact[0] + "T00:00:00Z", now()) > max_age * 86400:
            raise Denied("E_RESEARCH_STALE", "the research behind the hook is older than %d days" % max_age)
    res = hooks.presend(conn, draft["id"])
    if not isinstance(res, dict) or not res.get("ok"):
        code = res.get("code") if isinstance(res, dict) else None
        if code not in PRESEND_REFUSALS:
            code = "E_QC_LINT_FAILED"
        raise Denied(code, PRESEND_REFUSALS[code],
                     data={"blocks": res.get("blocks") if isinstance(res, dict) else None})
    if res.get("sha256") != draft["text_sha256"]:
        raise Denied("E_QC_HASH_MISMATCH", "the text differs from what was approved")
    return res


def _check_precheck(conn, precheck_id, kind: str, platform: str, t: dict):
    pc = _row(conn, "prechecks", precheck_id)
    if pc is None:
        raise Denied("E_PRECHECK_MISSING", "run gate precheck first")
    if pc["kind"] != kind or canonical_platform(pc["platform"]) != canonical_platform(platform):
        raise Denied("E_PRECHECK_MISSING", "the precheck was for another kind or platform")
    same_person = (pc["contact_id"] is None and t.get("contact_id") is None) or (
        pc["contact_id"] is not None and t.get("contact_id") is not None and
        people.survivor(conn, pc["contact_id"]) == people.survivor(conn, t["contact_id"]))
    same_job = pc["job_id"] == t.get("job_id")
    same_thread = (kind not in FOLLOWUP_KINDS and kind != "referral_ask") or (pc["thread_key"] == t.get("thread_key"))
    if not (same_person and same_job and same_thread):
        raise Denied("E_PRECHECK_MISSING", "the precheck was for another target")
    if pc["used_by_action"] is not None:
        raise Denied("E_PRECHECK_STALE", "the precheck was already used")
    if seconds_between(pc["created_at"], now()) > PRECHECK_TTL_S:
        raise Denied("E_PRECHECK_STALE", "the precheck is older than 15 minutes")
    if pc["result"] == "already_done":
        raise Denied("E_ALREADY_DONE", "the precheck found it was already done")
    if pc["result"] != "clear":
        raise Denied("E_PRECONDITION", "the precheck was uncertain")
    return pc


def _role_similarity(conn, cfg: dict, t: dict) -> None:
    job = _row(conn, "jobs", t["job_id"])
    if job is None or not t.get("company_id"):
        return
    pc = cfg["boards"]["per_company"]
    cids = _company_ids(conn, t["company_id"])
    rows = conn.execute("SELECT a.role_key, a.reserved_at, j.role_key AS jrk FROM actions a LEFT JOIN jobs j "
                        "ON j.id = a.job_id WHERE a.company_id IN (%s) AND a.kind IN ('application','application_email') "
                        "AND a.%s AND a.reserved_at > ? AND (a.job_id IS NULL OR a.job_id <> ?)"
                        % (_in(cids), _live_in()), list(cids) + [ts_add(now(), days=-90), t["job_id"]]).fetchall()
    for r in rows:
        other = r["role_key"] or r["jrk"] or ""
        sim = keys.jaccard(job["role_key"], other)
        if sim >= pc["same_role_jaccard_block"]:
            raise Denied("E_ROLE_SIMILAR", "a similar role at this company was applied to (%.2f)" % sim)
        if sim > pc["second_role_max_jaccard"]:
            raise Denied("E_ROLE_SIMILAR", "a second role at one company must differ more (%.2f)" % sim)
        if seconds_between(r["reserved_at"], now()) < pc["second_role_min_gap_days"] * 86400:
            raise Denied("E_ROLE_SIMILAR", "a second role at one company needs a day's gap")


def _job_to_applying(conn, job_id: int) -> None:
    st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    path = {"apply_queued": ["applying"], "eligible": ["apply_queued", "applying"],
            "awaiting_approval": ["apply_queued", "applying"], "applying": []}.get(st)
    if path is None:
        raise Denied("E_PRECONDITION", "the job is %s; it cannot be applied to now" % st)
    for s in path:
        jobstate.set_job_status(conn, job_id, s, "gate.reserve", "gate")


def reserve(conn, *, kind: str, draft_id: int, precheck_id: int, platform: str, agent_id: str, route: str,
            job_id=None, contact_id=None, thread_key=None, cycle_id=None, lane: str | None = None) -> dict:
    """Create a reserved action (2.3) or refuse with the first failing check. Runs inside the caller's tx."""
    if kind not in KINDS:
        raise Denied("E_VALIDATION", "unknown action kind %r" % kind)
    if not platform_ok(kind, platform):
        raise Denied("E_VALIDATION", "platform %s does not fit %s" % (platform, kind))
    if route not in ("mailer", "browser"):
        raise Denied("E_VALIDATION", "route must be mailer or browser")
    platform = canonical_platform(platform)
    cfg = _config.load(conn)
    # 1-2 kill switch and breakers
    breakers.check_breakers(conn, breakers.scopes_for(platform, kind))
    # 3 channel and route
    channel_check(conn, cfg, kind, platform, route, agent_id)
    # 3b browser route: the owner consented to the agent using this site's login (private/consent.json)
    if route == "browser":
        identity.require_consent(conn, platform)
    # 4 agent callers: guard heartbeat
    if agent_id != "system:mailer":
        if not (agent_id or "").startswith("jobhunter-"):
            raise Denied("E_GUARD_MISSING", "no agent identity (JH_AGENT_ID)")
        hb = guard_heartbeat()
        if not hb["fresh"]:
            raise Denied("E_GUARD_MISSING", "the jobhunter-guard heartbeat is missing or stale", data=hb)
    # 4b agent callers reserve inside their running cycle, so per-cycle ceilings and cycle stop rules count
    cycle = None
    if agent_id != "system:mailer":
        cycle = agent_cycle(conn, agent_id, cycle_id)
        if cycle["lane"] not in TOKEN_LANES:
            raise Denied("E_CALLER_NOT_ALLOWED", "the %s lane never holds a token (1.1.3); send only in an %s cycle"
                         % (cycle["lane"], " or ".join(TOKEN_LANES)))
        cycle_id = cycle["cycle_id"]
        lane = cycle["lane"]
    # 5 one open token per agent
    if conn.execute("SELECT 1 FROM actions WHERE agent_id = ? AND status IN ('reserved','armed')", (agent_id,)).fetchone():
        raise Denied("E_TOKEN_OPEN", "%s already holds an open token" % agent_id)
    draft = _row(conn, "drafts", draft_id)
    if draft is None:
        raise Denied("E_NOT_FOUND", "no such draft")
    t = resolve_target(conn, kind, draft=draft if kind != "li_withdraw" else None,
                       job_id=job_id if kind != "li_withdraw" else (job_id or draft["job_id"]),
                       contact_id=contact_id if kind != "li_withdraw" else (contact_id or draft["contact_id"]),
                       thread_key=thread_key)
    # 6 hours and windows
    check_hours(conn, cfg, kind, platform, t["contact_id"])
    # 7 pacing (and, on LinkedIn, the cycles per day and the idle gap between LinkedIn cycles)
    pacing.pace_check(conn, platform, kind, cfg)
    if platform == "linkedin":
        linkedin_cycle_check(conn, cfg, cycle)
    # 8 ceilings
    li_note = 1 if (kind == "li_invite" and (draft["body"] or "").strip()) else 0
    ceilings.check(conn, platform, kind, cycle_id=cycle_id, li_note=li_note, cfg=cfg)
    # 9-10 exclusions and target skips (with the rest of the dedup rules below)
    ft = compute_first_touch(conn, kind, t["contact_id"])
    seq = None
    if kind in ("li_message", "li_followup", "inmail") or (kind == "li_invite" and li_note):
        seq = next_li_seq(conn, t["contact_id"])
    hits = dedup_hits(conn, kind, job_id=t["job_id"], contact_id=t["contact_id"], company_id=t["company_id"],
                      thread_key=t["thread_key"], email=t["recipient"] if kind in EMAIL_KINDS else None,
                      first_touch=ft, li_seq=seq)
    order = ("exclusion", "target_skip")
    first_blocking([h for h in hits if h["rule"] in order])
    # 11 address grade and MX
    if kind in EMAIL_KINDS or (kind == "referral_ask" and platform == "gmail"):
        if not t["recipient"]:
            raise Denied("E_VALIDATION", "no recipient address")
        c = _row(conn, "contacts", t["contact_id"])
        if c is not None:
            if c["email_invalid"]:
                raise Denied("E_ADDRESS_GRADE", "the address bounced before")
            grade = c["email_grade"] or ("A" if c["role_type"] == "role_inbox" else None)
            if grade not in cfg["gmail"]["address_grades_allowed"]:
                raise Denied("E_ADDRESS_GRADE", "address grade %s is not allowed" % (grade or "unknown"))
            if c["email_mx_ok"] != 1:
                raise Denied("E_NO_MX", "the address domain has no verified mail server (email verify)")
            # 11b provider-found addresses (email finder, U10): result age, bounce strikes, share of cold email
            if kind in ("cold_email", "application_email"):
                hooks.on_reserve_address(conn, {"kind": kind, "contact_id": c["id"], "recipient": t["recipient"],
                                                "company_id": t["company_id"], "reserved_at": now()})
    if kind in LI_KINDS:
        c = _row(conn, "contacts", t["contact_id"])
        if c is None or c["needs_vanity"] or not c["li_slug"]:
            raise Denied("E_PRECONDITION", "open the person's profile first so the vanity URL is known")
    # 12 draft approved, not expired, research fresh, presend; a referral ask is always approved by the human
    res = _check_draft(conn, cfg, kind, draft)
    if kind == "referral_ask":
        if not (draft["approved_by"] or "").startswith("human:"):
            raise Denied("E_QC_NOT_APPROVED", "a referral ask needs your own approval (approval.always_human)")
        _referral_channel(kind, platform, t)
    # 13 (follow-up targets came from the thread) and the remaining duplicate rules
    first_blocking(hits)
    # 15 precheck
    pc = _check_precheck(conn, precheck_id, kind, platform, t)
    # 16 browser: a clear detection in the last 10 minutes
    detect_id = None
    if route == "browser":
        from .detect import recent_clear
        d = recent_clear(conn, platform, DETECT_TTL_S)      # board id and site:<id> are one platform
        if d is None:
            raise Denied("E_DETECT_MISSING", "run the page check (detect) on %s first" % platform)
        detect_id = d["id"]
    # 17 role similarity
    if kind in APP_KINDS:
        _role_similarity(conn, cfg, t)
    # insert
    token = new_token()
    ts = now()
    job = _row(conn, "jobs", t["job_id"])
    attach = None
    if kind == "application_email" and draft["attachment_variant_id"]:
        v = _row(conn, "resume_variants", draft["attachment_variant_id"])
        attach = v["pdf_sha256"] if v is not None else None
    tk = t["thread_key"]
    if kind in ("cold_email", "application_email"):
        tk = "em:" + token
    elif kind in ("li_invite", "li_message", "inmail") and t["contact_id"]:
        tk = "li:" + conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (t["contact_id"],)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO actions (token, kind, route, first_touch, li_note, li_msg_seq, platform, agent_id, contact_id, "
        "company_id, job_id, role_key, thread_key, recipient, draft_id, approved_sha256, attachment_sha256, precheck_id, "
        "detect_id, status, reserved_at, expires_at, cycle_id, lane, created_at, updated_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
        (token, kind, route, ft, li_note, seq, platform, agent_id, t["contact_id"], t["company_id"], t["job_id"],
         job["role_key"] if job is not None else None, tk, t["recipient"],
         draft["id"] if kind != "li_withdraw" else None, res.get("sha256") or draft["text_sha256"], attach,
         pc["id"], detect_id, ts, ts_add(ts, seconds=TOKEN_TTL_S), cycle_id, lane, ts, ts))
    aid = cur.lastrowid
    conn.execute("UPDATE prechecks SET used_by_action = ? WHERE id = ?", (aid, pc["id"]))
    if kind in APP_KINDS and t["job_id"]:
        _job_to_applying(conn, t["job_id"])
    log_event(conn, "reserved", token=token, action_kind=kind, platform=platform, agent_id=agent_id, cycle_id=cycle_id)
    rem = ceilings.remaining(conn, platform, kind, cycle_id, cfg)
    last, gap = pacing.required_gap(conn, platform, kind, cfg)
    return {"token": token, "expires_at": ts_add(ts, seconds=TOKEN_TTL_S), "status": "reserved",
            "guard": {"fill": True, "commit_after_arm": 2}, "pace": {"min_wait_s": gap},
            "remaining": {k: rem.get(k) for k in ("cycle", "hour", "day", "week")}, "action_id": aid}


def note_denial(conn, cycle_id: str | None, denied: Denied) -> None:
    """After a refused reserve (in a new transaction): 3 duplicate-rule denials in one cycle abort the
    cycle and trip 'global' (bug suspicion, 4.5)."""
    from .errors import exit_code
    if not cycle_id or exit_code(denied.code) != 3:
        return
    conn.execute("INSERT INTO counters (ts, platform, metric, n, kind, cycle_id) VALUES (?, 'global', "
                 "'dup_denial', 1, 'inc', ?)", (now(), cycle_id))
    n = conn.execute("SELECT count(*) FROM counters WHERE metric = 'dup_denial' AND cycle_id = ?", (cycle_id,)).fetchone()[0]
    if n == 3:
        breakers.trip(conn, "global", "dup_denials", "3 duplicate-rule denials in cycle %s" % cycle_id,
                      by="gate", cycle_id=cycle_id)


# ---------------------------------------------------------------- arm, confirm, fail, unknown
EMAIL_READBACK_HEADERS = ("subject", "to", "cc", "bcc")


def email_readback(observed_text: str) -> dict:
    """Split a web-route email read-back (12.7) into {subject, to, cc, bcc, body}. The lines before the first
    blank line are headers: `Subject: <subject>`, `To: <address>` and, when the window shows them, `Cc: ...` and
    `Bcc: ...`, each at most once, in any order; the rest is the body. A header that is absent is None."""
    text = normalize_text(observed_text)
    head, _sep, body = text.partition("\n\n")
    out = {k: None for k in EMAIL_READBACK_HEADERS}
    for line in head.split("\n"):
        name, colon, value = line.partition(":")
        key = name.strip().lower()
        if not colon or key not in EMAIL_READBACK_HEADERS or out[key] is not None:
            raise Denied("E_VALIDATION", "the observed email is the lines 'Subject: <subject>' and 'To: <address>' "
                         "(and 'Cc: ...', 'Bcc: ...' when the window shows them), a blank line, then the body (12.7)")
        out[key] = value.strip()
    if out["subject"] is None:
        raise Denied("E_VALIDATION", "the observed email must hold a 'Subject: ' line before the blank line")
    out["body"] = body
    return out


def _addresses(value: str | None) -> list[str]:
    from email.utils import getaddresses
    return [addr.strip().lower() for _name, addr in getaddresses([value or ""]) if addr.strip()]


def recipient_problem(action, readback: dict) -> str | None:
    """Why a web-route email read-back is not addressed to the reserved recipient alone, or None: exactly one
    To address, equal to actions.recipient, and no Cc or Bcc address. The address the precheck, the duplicate
    rules, the exclusions and the MX check saw is the only one the message may go to."""
    if readback.get("to") is None:
        return "to_missing"
    if _addresses(readback.get("cc")) or _addresses(readback.get("bcc")):
        return "cc_or_bcc"
    to = _addresses(readback.get("to"))
    if len(to) != 1:
        return "to_not_one_address"
    want = (action["recipient"] or "").strip().lower()
    if not want or "@" not in to[0] or to[0] != want:
        return "to_other_address"
    return None


def _observed_canonical(conn, action, observed_text: str) -> str:
    kind = action["kind"]
    if kind in EMAIL_KINDS or (kind == "referral_ask" and action["platform"] == "gmail"):
        rb = email_readback(observed_text)
        subject, body = rb["subject"], rb["body"]
        attach = None
        sf = conn.execute("SELECT path, sha256 FROM staged_files WHERE token = ? AND removed_at IS NULL",
                          (action["token"],)).fetchone()
        if sf is not None:
            attach = {"filename": os.path.basename(sf["path"]), "sha256": sf["sha256"]}
        return canonical_send_text("cold_email" if kind == "referral_ask" else kind, subject, body, None, None, attach)
    if kind == "application":
        try:
            data = json.loads(observed_text)
        except ValueError:
            raise Denied("E_VALIDATION", "the observed form file must be JSON (12.7)")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or \
                set(data) - {"fields", "resume_filename_visible"}:
            raise Denied("E_VALIDATION", "observed form: {fields: [{label, value}], resume_filename_visible}")
        name = data.get("resume_filename_visible")
        return canonical_send_text("application", None, None, data["fields"], None,
                                   {"filename": name} if name else None)
    if kind == "inmail":
        # an InMail read-back names its subject like an email ("Subject: <s>", blank line, body); without the
        # line it is body only, which matches only a draft approved without a subject
        text = normalize_text(observed_text)
        if text.startswith("Subject:"):
            head, _sep, body = text.partition("\n\n")
            return canonical_send_text("inmail", head[len("Subject:"):].strip(), body, None, None)
        return canonical_send_text("inmail", None, text, None, None)
    return canonical_send_text("li_message" if kind == "referral_ask" else kind, None, observed_text, None, None)


def _approved_text(conn, draft_id) -> str | None:
    """The canonical send text of the approved draft, only for the mismatch diff. Read with drafts.send_text
    (U3), which writes nothing; hooks.presend would record a presend qc_results row on every mismatch."""
    if not draft_id:
        return None
    try:
        from . import drafts
        text = drafts.send_text(conn, draft_id)
    except Exception:
        return None
    return text if isinstance(text, str) else None


def _mismatch_info(conn, a, canon_obs: str) -> dict:
    """Where a read-back first differs from the approved text (no text itself, only offsets and lengths)."""
    approved = _approved_text(conn, a["draft_id"])
    diff = None
    if isinstance(approved, str):
        diff = next((i for i, (x, y) in enumerate(zip(canon_obs, approved)) if x != y), min(len(canon_obs),
                                                                                          len(approved)))
    return {"first_diff_at": diff, "observed_len": len(canon_obs),
            "approved_len": len(approved) if isinstance(approved, str) else None}


def _is_web_email(a) -> bool:
    """A browser-route email send (the kinds whose read-back is the 'Subject: ...' and 'To: ...' lines, a blank
    line, the body)."""
    return a["route"] == "browser" and (a["kind"] in EMAIL_KINDS or
                                        (a["kind"] == "referral_ask" and a["platform"] == "gmail"))


def arm(conn, token: str, observed_text: str, agent_id: str | None = None) -> dict:
    """Compare the page's read-back with the approved text (and, for a web-route email, its To, Cc and Bcc with
    the reserved recipient: recipient_problem). Match: armed. Mismatch: the token becomes failed
    (observed_text_mismatch; this survives the refusal) and Denied(E_OBSERVED_MISMATCH)."""
    a = action_by_token(conn, token)
    if agent_id and a["agent_id"] != agent_id:
        raise Denied("E_NOT_FOUND", "the token belongs to another agent")
    if a["status"] != "reserved":
        raise Denied("E_BAD_TRANSITION", "the token is %s, not reserved" % a["status"])
    if a["expires_at"] <= now():
        raise Denied("E_PRECONDITION", "the token expired")
    canon_obs = _observed_canonical(conn, a, observed_text or "")
    obs_sha = sha256_text(canon_obs)
    problem = recipient_problem(a, email_readback(observed_text or "")) if _is_web_email(a) else None
    if obs_sha != a["approved_sha256"] or problem:
        info = _mismatch_info(conn, a, canon_obs)
        if problem:
            info["recipient"] = problem

        def _fail(c, aid=a["id"], sha=obs_sha, tok=token, job=a["job_id"], kind=a["kind"], why=problem):
            ts = now()
            c.execute("UPDATE actions SET status = 'failed', fail_reason = 'observed_text_mismatch', observed_sha256 = ?, "
                      "resolved_at = ?, updated_at = ? WHERE id = ? AND status = 'reserved'", (sha, ts, ts, aid))
            if kind in APP_KINDS and job:
                _job_after_fail(c, job, "observed_text_mismatch")
            log_event(c, "arm_mismatch", token=tok, recipient=why)
        db.defer_write(conn, _fail)
        raise Denied("E_OBSERVED_MISMATCH", "the compose window is not addressed to the approved recipient alone (one "
                     "To address, no Cc or Bcc); nothing was submitted" if problem else
                     "the page does not hold the approved text; nothing was submitted", data={"mismatch": info})
    ts = now()
    conn.execute("UPDATE actions SET status = 'armed', armed_at = ?, observed_sha256 = ?, updated_at = ? WHERE id = ?",
                 (ts, obs_sha, ts, a["id"]))
    log_event(conn, "armed", token=token)
    return {"armed": True, "token": token, "armed_at": ts}


def mark_armed(conn, token: str, message_id: str | None = None) -> None:
    """Mailer: reserved -> armed just before SMTP DATA, recording the Message-ID it is about to send (so the
    audit and the reply matcher know the message even when the confirm never comes)."""
    a = action_by_token(conn, token)
    if a["status"] != "reserved":
        raise Denied("E_BAD_TRANSITION", "the token is %s, not reserved" % a["status"])
    if message_id is not None and (not isinstance(message_id, str) or not message_id.strip() or
                                   len(message_id) > 998 or any(ch in message_id for ch in "\r\n")):
        raise Denied("E_VALIDATION", "message_id must be one header value")
    ts = now()
    conn.execute("UPDATE actions SET status = 'armed', armed_at = ?, observed_sha256 = approved_sha256, "
                 "message_id = COALESCE(?, message_id), updated_at = ? WHERE id = ?",
                 (ts, message_id.strip() if message_id else None, ts, a["id"]))
    log_event(conn, "armed", token=token, by="mailer")


def _job_after_fail(conn, job_id: int, reason: str) -> None:
    st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if st is None or st[0] != "applying":
        return
    other = conn.execute("SELECT 1 FROM actions WHERE job_id = ? AND kind IN ('application','application_email') AND %s"
                         % _live_in(), (job_id,)).fetchone()
    if other:
        return
    jobstate.set_job_status(conn, job_id, "apply_failed", reason, "gate")
    nxt = "needs_human" if reason == "form_blocked_before_submit" else "eligible"
    jobstate.set_job_status(conn, job_id, nxt, reason, "gate")


def variant_uid_of_payload(payload_json) -> str | None:
    """The resume variant a draft names: payload.resume_variant_uid of an application package (U3 stores
    {"payload": {...}}), else the attachment's variant_uid, else a top-level resume_variant_uid."""
    try:
        pj = json.loads(payload_json or "{}") if isinstance(payload_json, str) else (payload_json or {})
    except ValueError:
        return None
    if not isinstance(pj, dict):
        return None
    inner = pj.get("payload") if isinstance(pj.get("payload"), dict) else {}
    att = pj.get("attachment") if isinstance(pj.get("attachment"), dict) else {}
    for v in (inner.get("resume_variant_uid"), att.get("variant_uid"), pj.get("resume_variant_uid")):
        if isinstance(v, str) and v:
            return v
    return None


def _sent_variant(conn, kind: str, draft_id) -> int | None:
    """resume_variants.id for the applications row: drafts.attachment_variant_id (application_email only),
    else the variant named in the draft's payload (application packages)."""
    if not draft_id:
        return None
    dr = conn.execute("SELECT attachment_variant_id, payload_json FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if dr is None:
        return None
    if kind == "application_email" and dr["attachment_variant_id"]:
        return dr["attachment_variant_id"]
    vu = variant_uid_of_payload(dr["payload_json"])
    if vu:
        vr = conn.execute("SELECT id FROM resume_variants WHERE variant_uid = ?", (vu,)).fetchone()
        if vr is not None:
            return vr[0]
    return dr["attachment_variant_id"] or None


def _mark_sent(conn, a, evidence: str, *, detect_id=None, platform_ref=None, message_id=None, by="gate") -> dict:
    ts = now()
    conn.execute("UPDATE actions SET status = 'sent', sent_at = COALESCE(sent_at, ?), resolved_at = ?, "
                 "evidence = ?, detect_id = COALESCE(?, detect_id), message_id = COALESCE(?, message_id), "
                 "updated_at = ? WHERE id = ?", (ts, ts, (evidence or "")[:4000], detect_id, message_id, ts, a["id"]))
    if a["draft_id"]:
        d = conn.execute("SELECT status FROM drafts WHERE id = ?", (a["draft_id"],)).fetchone()
        if d and d[0] == "approved":
            jobstate.set_draft_status(conn, a["draft_id"], "sent", "confirmed:" + a["token"], by)
    if a["company_id"] and a["kind"] not in ("application", "li_withdraw"):
        conn.execute("UPDATE companies SET contact_state = 'contacted', contact_state_reason = ?, updated_at = ? "
                     "WHERE id = ? AND contact_state = 'none'", ("sent:" + a["token"], ts, a["company_id"]))
    if a["kind"] in APP_KINDS and a["job_id"]:
        variant = _sent_variant(conn, a["kind"], a["draft_id"])
        job = _row(conn, "jobs", a["job_id"])
        conn.execute("INSERT INTO applications (action_id, job_id, route, resume_variant_id, package_draft_id, "
                     "confirmation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                     (a["id"], a["job_id"], job["apply_route"] if job is not None else a["route"], variant,
                      a["draft_id"] if a["kind"] == "application" else None, (evidence or "")[:300], ts, ts))
        st = job["status"] if job is not None else None
        if st in ("applying", "needs_human"):
            jobstate.set_job_status(conn, a["job_id"], "applied", "sent:" + a["token"], by)
    if a["kind"] == "cold_email":
        ceilings.start_warmup(conn, "gmail")
    row = dict(conn.execute("SELECT * FROM actions WHERE id = ?", (a["id"],)).fetchone())
    row["platform_ref"] = platform_ref
    info = hooks.on_confirm(conn, row) or {}
    log_event(conn, "sent", token=a["token"], action_kind=a["kind"], by=by)
    return {"status": "sent", "token": a["token"], "thread_key": info.get("thread_key") if isinstance(info, dict) else None,
            "followup_due_at": info.get("followup_due_at") if isinstance(info, dict) else None}


def confirm(conn, token: str, evidence: str, *, post_detect_id=None, platform_ref=None, message_id=None,
            agent_id: str | None = None, observed_text: str | None = None) -> dict:
    """armed -> sent with the side effects of 2.3.1 step 5 (thread hook, draft sent, company contacted,
    applications row and job applied).

    observed_text (web-route email only, and required there: E_EVIDENCE_MISSING without it): the Sent-folder
    read-back ("Subject: <s>" and "To: <address>" lines, a blank line, the body, from read_gmail_message.js
    readback_text). It goes through the same canonical form and recipient rule as `arm`; a hash other than
    approved_sha256, or recipients other than the one reserved address, mean the message that went out is not
    the approved one, so the action becomes unknown (reconcile decides; a resolve_unknown human task names it),
    never sent, and the call is Denied(E_OBSERVED_MISMATCH). The unknown state survives the refusal."""
    a = action_by_token(conn, token)
    if agent_id and a["agent_id"] != agent_id:
        raise Denied("E_NOT_FOUND", "the token belongs to another agent")
    if a["status"] != "armed":
        raise Denied("E_BAD_TRANSITION", "the token is %s; only an armed token can be confirmed" % a["status"])
    if not (evidence or "").strip():
        raise Denied("E_EVIDENCE_MISSING", "confirm needs the evidence text")
    if post_detect_id is not None:
        from .detect import same_platform
        d = _row(conn, "detections", post_detect_id)
        if d is None or not same_platform(d["platform"], a["platform"]):
            raise Denied("E_NOT_FOUND", "no detection %s for %s" % (post_detect_id, a["platform"]))
        if d["verdict"] != "clear":
            raise Denied("E_STOP_DETECTED", "the page after the click showed a stop signature")
    if platform_ref is not None and not str(platform_ref).startswith("https://"):
        raise Denied("E_VALIDATION", "platform_ref must be an https URL")
    if observed_text is None and _is_web_email(a):
        raise Denied("E_EVIDENCE_MISSING", "a web-route email is confirmed only with its Sent-folder read-back "
                     "(--observed-file)")
    if observed_text is not None:
        if not _is_web_email(a):
            raise Denied("E_VALIDATION", "a Sent read-back (--observed-file) applies only to a web-route email")
        canon_obs = _observed_canonical(conn, a, observed_text)
        obs_sha = sha256_text(canon_obs)
        problem = recipient_problem(a, email_readback(observed_text))
        if obs_sha != a["approved_sha256"] or problem:
            info = _mismatch_info(conn, a, canon_obs)
            if problem:
                info["recipient"] = problem

            def _unknown(c, tok=token, sha=obs_sha, info=info, ev=evidence):
                _readback_unknown(c, tok, sha, info, ev)
            db.defer_write(conn, _unknown)
            raise Denied("E_OBSERVED_MISMATCH", "the Sent folder copy does not hold the approved text or went to "
                         "other recipients; the action is unknown now and reconcile decides. Never send it again.",
                         data={"mismatch": info, "status": "unknown", "token": token})
    return _mark_sent(conn, a, evidence, detect_id=post_detect_id, platform_ref=platform_ref, message_id=message_id)


def _readback_unknown(conn, token: str, observed_sha: str, info: dict, evidence: str) -> None:
    """armed -> unknown after a Sent read-back that differs from the approved text or names other recipients
    than the reserved address, with a human task."""
    a = action_by_token(conn, token)
    if a["status"] != "armed":
        return
    what = ("went to other recipients than the approved one (%s)" % info["recipient"]) if info.get("recipient") \
        else "differs from the approved text"
    note = ("sent_readback_mismatch: the Sent folder copy %s (sha256 %s, first_diff_at %s, observed_len %s, "
            "approved_len %s)\n%s" % (what, observed_sha, info.get("first_diff_at"), info.get("observed_len"),
                                       info.get("approved_len"), evidence or ""))
    mark_unknown(conn, token, note=note)
    open_human_task(conn, "resolve_unknown", "The Sent folder copy of email %s to %s %s; check what went out."
                    % (token, a["recipient"] or "the recipient", what),
                    action_id=a["id"], job_id=a["job_id"], company_id=a["company_id"],
                    detail="sent read-back sha256 %s, first_diff_at %s%s" % (
                        observed_sha, info.get("first_diff_at"),
                        (", recipient %s" % info["recipient"]) if info.get("recipient") else ""))
    log_event(conn, "confirm_readback_mismatch", token=token, recipient=info.get("recipient"))


def fail(conn, token: str, reason: str, evidence: str, caller) -> None:
    """Free a slot with a code-checkable reason (M1). Agents: only from reserved, only not_attempted,
    precondition_changed or form_blocked_before_submit, and only without a commit line in the guard log.
    System (mailer): smtp_rejected_before_data from reserved, smtp_rejected from armed."""
    a = action_by_token(conn, token)
    cls = getattr(caller, "cls", "system")
    if cls == "agent":
        if a["agent_id"] != getattr(caller, "agent_id", None):
            raise Denied("E_NOT_FOUND", "the token belongs to another agent")
        if reason not in AGENT_FAIL_REASONS:
            raise Denied("E_FAIL_NOT_ALLOWED", "agents may fail a token only with %s" % ", ".join(AGENT_FAIL_REASONS))
        if a["status"] != "reserved":
            raise Denied("E_FAIL_NOT_ALLOWED", "only a reserved token can be failed (it is %s)" % a["status"])
        if commit_count(token):
            raise Denied("E_FAIL_NOT_ALLOWED", "the guard log shows a commit action for this token")
    else:
        allowed = dict(SYSTEM_FAIL_REASONS)
        for r in AGENT_FAIL_REASONS:
            allowed[r] = "reserved"
        if reason not in allowed:
            raise Denied("E_FAIL_NOT_ALLOWED", "reason %r cannot free a slot" % reason)
        if a["status"] != allowed[reason]:
            raise Denied("E_FAIL_NOT_ALLOWED", "%s needs status %s (it is %s)" % (reason, allowed[reason], a["status"]))
        if a["route"] == "browser" and commit_count(token):
            raise Denied("E_FAIL_NOT_ALLOWED", "the guard log shows a commit action for this token")
    if not (evidence or "").strip():
        raise Denied("E_EVIDENCE_MISSING", "fail needs the evidence text")
    ts = now()
    conn.execute("UPDATE actions SET status = 'failed', fail_reason = ?, resolved_at = ?, evidence = ?, updated_at = ? "
                 "WHERE id = ?", (reason, ts, evidence[:4000], ts, a["id"]))
    if a["kind"] in APP_KINDS and a["job_id"]:
        _job_after_fail(conn, a["job_id"], reason)
    log_event(conn, "failed", token=token, reason=reason, by=cls)


def mark_unknown(conn, token: str, note: str = "", after_click_error: bool = False) -> dict:
    """reserved or armed -> unknown (blocking); armed with a platform error after the click ->
    failed_after_click (also blocking). LinkedIn failure counts may trip the linkedin breaker."""
    a = action_by_token(conn, token)
    if a["status"] not in ("reserved", "armed"):
        raise Denied("E_BAD_TRANSITION", "the token is %s" % a["status"])
    ts = now()
    new = "failed_after_click" if (after_click_error and a["status"] == "armed") else "unknown"
    conn.execute("UPDATE actions SET status = ?, fail_reason = ?, note = ?, resolved_at = CASE WHEN ? = "
                 "'failed_after_click' THEN ? ELSE NULL END, updated_at = ? WHERE id = ?",
                 (new, "platform_error_after_click" if new == "failed_after_click" else None, (note or "")[:4000],
                  new, ts, ts, a["id"]))
    if a["kind"] == "application" and a["platform"] != "linkedin":
        open_human_task(conn, "resolve_unknown", "An application may or may not have gone through; check it.",
                        job_id=a["job_id"], action_id=a["id"], company_id=a["company_id"])
    log_event(conn, "unknown", token=token, status=new)
    if a["platform"] == "linkedin":
        _linkedin_failures(conn, a)
    return {"status": new, "token": token}


def _linkedin_failures(conn, a) -> None:
    bad = ("unknown", "failed_after_click")
    if a["cycle_id"]:
        last2 = [r[0] for r in conn.execute("SELECT status FROM actions WHERE platform = 'linkedin' AND cycle_id = ? "
                                            "ORDER BY id DESC LIMIT 2", (a["cycle_id"],))]
        if len(last2) == 2 and all(s in bad for s in last2):
            breakers.trip(conn, "linkedin", "li_consecutive_failures", "2 failed or unknown LinkedIn actions in a row",
                          by="gate", cycle_id=a["cycle_id"])
            return
    n = conn.execute("SELECT count(*) FROM actions WHERE platform = 'linkedin' AND status IN ('unknown',"
                     "'failed_after_click') AND updated_at > ?", (ts_add(now(), days=-1),)).fetchone()[0]
    if n >= 3:
        breakers.trip(conn, "linkedin", "li_daily_failures", "%d failed or unknown LinkedIn actions today" % n, by="gate")


def open_token(conn, agent_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM actions WHERE agent_id = ? AND status IN ('reserved','armed') ORDER BY id DESC "
                     "LIMIT 1", (agent_id,)).fetchone()
    if r is None:
        return None
    return {"token": r["token"], "kind": r["kind"], "platform": r["platform"], "status": r["status"],
            "expires_at": r["expires_at"], "armed_at": r["armed_at"], "commit_used": commit_count(r["token"])}


def expire(conn) -> int:
    """reserved or armed tokens whose expires_at passed become unknown (2.3.3)."""
    ts = now()
    rows = conn.execute("SELECT token FROM actions WHERE status IN ('reserved','armed') AND expires_at <= ?",
                        (ts,)).fetchall()
    for (tok,) in rows:
        mark_unknown(conn, tok, note="expired")
    return len(rows)


def status_for(conn, agent_id: str | None) -> dict:
    if agent_id:
        return {"open": open_token(conn, agent_id)}
    out = {}
    for (aid,) in conn.execute("SELECT DISTINCT agent_id FROM actions WHERE status IN ('reserved','armed') "
                               "AND agent_id IS NOT NULL").fetchall():
        out[aid] = open_token(conn, aid)
    return {"open": out or None}

