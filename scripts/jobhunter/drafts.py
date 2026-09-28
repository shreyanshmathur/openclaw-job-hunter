"""Drafts: create, revise, show, canonical send text, lint context, expiry (design 5.1, 5.4, 12.4).

Every function that writes runs inside the caller's db.tx(conn) and never commits. Status changes go through
jobstate.set_draft_status (U1, the single writer); the schema trigger is the last line.

Stored columns: subject and body hold the exact text (sanitized: curly quotes, NBSP, trailing and double
spaces, extra blank lines; dashes are never auto-fixed). payload_json holds everything else the linter and
reviewer need:
    {"hook": {...} | null, "claims": [...], "links": [...], "is_reply": bool, "field_char_limit": int | null,
     "field_label": str | null, "payload": {...} | null (application_package 12.4, resume R-* model),
     "attachment": {"variant_uid", "filename", "sha256"} | null, "edited_by_human": int}
text_sha256 = sha256(send_text(conn, draft_id)).
"""
from __future__ import annotations

import json
import os
import re

from . import canon, jobstate
from .canon import now, sha256_text, ts_add
from .errors import Denied
from .events import log_event

KINDS = ("cold_email", "followup_email", "application_email", "li_invite_note", "li_message", "li_followup",
         "inmail", "form_answer", "cover_note", "resume", "application_package")
EMAIL_KINDS = ("cold_email", "followup_email", "application_email")
LI_KINDS = ("li_invite_note", "li_message", "li_followup", "inmail")
PART_KINDS = ("form_answer", "cover_note", "resume")          # parts of a package; approved with it
FOLLOWUP_KINDS = ("followup_email", "li_followup")
SUBJECT_KINDS = ("cold_email", "followup_email", "application_email", "inmail")
KIND_CHANNELS = {
    "cold_email": ("email_cold", "email_founder", "email_recruiter"),
    "followup_email": ("email_followup",),
    "application_email": ("email_application",),
    "li_invite_note": ("li_connect",),
    "li_message": ("li_message",),
    "li_followup": ("li_followup",),
    "inmail": ("inmail",),
    "form_answer": ("form_answer",),
    "cover_note": ("cover_note",),
    "resume": ("resume",),
    "application_package": ("application_package",),
}
ACTION_KIND = {"li_invite_note": "li_invite", "application_package": "application"}
AGENT_KINDS = {
    "jobhunter-outreach": ("cold_email", "followup_email", "li_invite_note", "li_message", "li_followup", "inmail"),
    "jobhunter-applier": ("application_email", "form_answer", "cover_note", "resume", "application_package"),
}
LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")
_LIVE_SQL = "('reserved','armed','sent','failed_after_click','unknown','imported')"
OPEN_STATUSES = ("drafted", "lint_failed", "review_pending", "review_failed", "qc_passed", "awaiting_approval",
                 "approved")
ALLOWED_KEYS = frozenset({"kind", "channel", "job_uid", "contact_uid", "thread_key", "subject", "body", "is_reply",
                          "field_char_limit", "field_label", "hook", "claims", "links", "attachment_variant_uid",
                          "payload", "recipient", "item_id"})
HOOK_KEYS = frozenset({"anchor", "source_type", "source_url", "snippet", "published_at", "retrieved_at", "fact_id"})
PACKAGE_KEYS = frozenset({"job_uid", "resume_variant_uid", "fields", "cover_note_draft_uid"})
FIELD_KEYS = frozenset({"label", "type", "value", "answer_key", "form_answer_draft_uid", "choices"})
MAX_BODY = 20000
_UID = {"job": re.compile(r"^J[A-Z2-7]{7}$"), "contact": re.compile(r"^P[A-Z2-7]{7}$"),
        "draft": re.compile(r"^D[A-Z2-7]{7}$"), "variant": re.compile(r"^V[A-Z2-7]{7}$")}
_THREAD = re.compile(r"^(em:T[A-Z2-7]{11}|li:P[A-Z2-7]{7})$")
_CODE = re.compile(r"^[ACDEFGHJKMNPQRTUVWXY34679]{4}$")


# ---------------------------------------------------------------- small helpers
def _settings(conn=None) -> dict:
    from .qc import settings
    return settings(conn)


def max_attempts(cfg: dict | None = None) -> int:
    cfg = cfg or _settings()
    return max(1, min(3, int(cfg["qc"].get("max_rewrites", 2)) + 1))


def payload_of(row) -> dict:
    try:
        data = json.loads(row["payload_json"] or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _one(conn, sql: str, args=()):
    return conn.execute(sql, args).fetchone()


def _follow(conn, table: str, row_id):
    """Follow merged_into to the surviving row id (companies, contacts)."""
    seen = set()
    while row_id is not None and row_id not in seen:
        seen.add(row_id)
        r = _one(conn, "SELECT merged_into FROM %s WHERE id = ?" % table, (row_id,))
        if r is None or r[0] is None:
            return row_id
        row_id = r[0]
    return row_id


def _by_uid(conn, table: str, col: str, uid: str, what: str):
    r = _one(conn, "SELECT * FROM %s WHERE %s = ?" % (table, col), (uid,))
    if r is None:
        raise Denied("E_NOT_FOUND", "unknown %s %s" % (what, uid))
    return r


def get(conn, uid_or_code: str):
    """The draft row for a draft uid or an approval code (open or closed). E_NOT_FOUND otherwise."""
    key = (uid_or_code or "").strip()
    if _CODE.match(key):
        r = _one(conn, "SELECT d.* FROM approval_codes a JOIN drafts d ON d.id = a.draft_id WHERE a.code = ?", (key,))
    else:
        r = _one(conn, "SELECT * FROM drafts WHERE draft_uid = ?", (key,))
    if r is None:
        raise Denied("E_NOT_FOUND", "no draft with id or code %r" % uid_or_code)
    return r


def get_by_id(conn, draft_id: int):
    r = _one(conn, "SELECT * FROM drafts WHERE id = ?", (draft_id,))
    if r is None:
        raise Denied("E_NOT_FOUND", "no draft with id %r" % draft_id)
    return r


def signature_text(cfg: dict | None = None) -> str | None:
    """The owner's signature block from config (appended by code to email bodies)."""
    cfg = cfg or _settings()
    owner = cfg.get("owner") or {}
    sig = owner.get("signature") or {}
    full = (sig.get("full_name") or "").strip() or \
        " ".join(x for x in ((owner.get("first_name") or "").strip(), (owner.get("last_name") or "").strip()) if x)
    lines = [full, (sig.get("phone") or "").strip()] + [str(x).strip() for x in (sig.get("links") or [])]
    lines = [x for x in lines if x]
    return "\n".join(lines) if lines else None


def _base_contact() -> dict:
    """The contact block of private/resume/base.json (U4), or {} when there is none or it is unreadable."""
    from . import resume as _resume   # local import: keeps the resume package off the drafts import path
    try:
        base = _resume.load_base(required=False)
    except Denied:
        return {}
    c = (base or {}).get("contact")
    return c if isinstance(c, dict) else {}


def resume_filename(cfg: dict | None = None, contact: dict | None = None) -> str:
    """The attachment filename of a resume variant. It is the same name the stager gives the upload copy
    (jobhunter.resume.file_stem: config owner names when both are set, else the base resume's contact names,
    then resume.filename), so the form package, the application_email attachment line and the observed
    read-back all name one file."""
    from . import resume as _resume
    cfg = cfg or _settings()
    contact = _base_contact() if contact is None else contact
    return _resume.file_stem(contact, _resume.resume_config(cfg), cfg) + ".pdf"


def _fields_for_text(kind: str, p: dict, body: str | None) -> list:
    if kind == "form_answer":
        return [{"label": p.get("field_label") or "", "value": body or ""}]
    pkg = p.get("payload") or {}
    return [{"label": f.get("label"), "value": f.get("value")} for f in pkg.get("fields") or [] if isinstance(f, dict)]


def compute_send_text(kind: str, subject, body, p: dict, cfg: dict | None = None) -> str:
    """Canonical send text (12.10) from stored values; the signature comes from config for email kinds.
    An InMail carries its subject ("Subject: <s>", blank line, body), so the approved hash covers the subject
    typed into LinkedIn as well as the body; no signature or attachment."""
    if kind in EMAIL_KINDS:
        return canon.canonical_send_text(kind, subject, body, None, signature_text(cfg), p.get("attachment"))
    if kind in ("form_answer", "application_package"):
        return canon.canonical_send_text(kind, None, None, _fields_for_text(kind, p, body), None, p.get("attachment"))
    if kind == "inmail":
        return canon.canonical_send_text("inmail", subject, body, None, None)
    return canon.canonical_send_text(kind, None, body, None, None)


def send_text(conn, draft_id: int, cfg: dict | None = None) -> str:
    """Canonical text incl. signature (and attachment line) of a stored draft."""
    row = get_by_id(conn, draft_id)
    return compute_send_text(row["kind"], row["subject"], row["body"], payload_of(row), cfg or _settings(conn))


# ---------------------------------------------------------------- validation of the writer file (12.4)
def _str(v, what: str, max_len: int, required: bool = False):
    if v is None:
        if required:
            raise Denied("E_VALIDATION", "%s is required" % what)
        return None
    if not isinstance(v, str):
        raise Denied("E_SCHEMA", "%s must be a string" % what)
    if len(v) > max_len:
        raise Denied("E_VALIDATION", "%s is longer than %d characters" % (what, max_len))
    if required and not v.strip():
        raise Denied("E_VALIDATION", "%s is empty" % what)
    return v


def _uid(v, kind: str, what: str):
    if v is None:
        return None
    if not isinstance(v, str) or not _UID[kind].match(v):
        raise Denied("E_VALIDATION", "%s must look like %s" % (what, _UID[kind].pattern))
    return v


def validate_file(d) -> dict:
    """Shape checks of a draft file (unknown keys rejected). Returns a normalized copy."""
    if not isinstance(d, dict):
        raise Denied("E_SCHEMA", "the draft file must be a JSON object")
    unknown = sorted(set(d) - ALLOWED_KEYS)
    if unknown:
        raise Denied("E_SCHEMA", "unknown keys in the draft file: %s" % ", ".join(unknown), data={"keys": unknown})
    kind = d.get("kind")
    if kind not in KINDS:
        raise Denied("E_VALIDATION", "kind must be one of %s" % ", ".join(KINDS))
    channel = d.get("channel") or KIND_CHANNELS[kind][0]
    if channel not in KIND_CHANNELS[kind]:
        raise Denied("E_VALIDATION", "channel %r does not fit kind %s (allowed: %s)"
                     % (channel, kind, ", ".join(KIND_CHANNELS[kind])))
    out = {"kind": kind, "channel": channel,
           "job_uid": _uid(d.get("job_uid"), "job", "job_uid"),
           "contact_uid": _uid(d.get("contact_uid"), "contact", "contact_uid"),
           "attachment_variant_uid": _uid(d.get("attachment_variant_uid"), "variant", "attachment_variant_uid"),
           "subject": _str(d.get("subject"), "subject", 200),
           "field_label": _str(d.get("field_label"), "field_label", 500)}
    tk = d.get("thread_key")
    if tk is not None and (not isinstance(tk, str) or not _THREAD.match(tk)):
        raise Denied("E_VALIDATION", "thread_key must look like em:T... or li:P...")
    out["thread_key"] = tk
    structured = kind in ("application_package",)
    out["body"] = _str(d.get("body"), "body", MAX_BODY, required=not structured)
    ir = d.get("is_reply", False)
    if not isinstance(ir, bool):
        raise Denied("E_SCHEMA", "is_reply must be true or false")
    out["is_reply"] = ir
    fcl = d.get("field_char_limit")
    if fcl is not None and (isinstance(fcl, bool) or not isinstance(fcl, int) or fcl < 1 or fcl > 100000):
        raise Denied("E_SCHEMA", "field_char_limit must be a positive integer or null")
    out["field_char_limit"] = fcl
    hook = d.get("hook")
    if hook is not None:
        if not isinstance(hook, dict):
            raise Denied("E_SCHEMA", "hook must be an object")
        bad = sorted(set(hook) - HOOK_KEYS)
        if bad:
            raise Denied("E_SCHEMA", "unknown hook keys: %s" % ", ".join(bad))
        for k, v in hook.items():
            if v is not None and not isinstance(v, str):
                raise Denied("E_SCHEMA", "hook.%s must be a string" % k)
            if isinstance(v, str) and len(v) > 2000:
                raise Denied("E_VALIDATION", "hook.%s is too long" % k)
        hook = dict(hook)
    out["hook"] = hook
    claims = d.get("claims") or []
    if not isinstance(claims, list) or len(claims) > 10:
        raise Denied("E_SCHEMA", "claims must be a list of at most 10 objects")
    for c in claims:
        if not isinstance(c, dict) or set(c) - {"text", "fact_id"} or not isinstance(c.get("text"), str) \
                or not isinstance(c.get("fact_id"), (str, type(None))):
            raise Denied("E_SCHEMA", "each claim is {text, fact_id}")
    out["claims"] = [dict(c) for c in claims]
    links = d.get("links") or []
    if not isinstance(links, list) or len(links) > 5 or not all(isinstance(x, str) and len(x) < 500 for x in links):
        raise Denied("E_SCHEMA", "links must be a list of at most 5 strings")
    out["links"] = list(links)
    payload = d.get("payload")
    if kind in ("application_package", "resume"):
        if not isinstance(payload, dict):
            raise Denied("E_SCHEMA", "%s drafts need a payload object" % kind)
        if kind == "application_package":
            bad = sorted(set(payload) - PACKAGE_KEYS)
            if bad:
                raise Denied("E_SCHEMA", "unknown payload keys: %s" % ", ".join(bad))
            fields = payload.get("fields")
            if not isinstance(fields, list) or not fields or len(fields) > 100:
                raise Denied("E_SCHEMA", "payload.fields must be a non-empty list")
            for f in fields:
                if not isinstance(f, dict) or set(f) - FIELD_KEYS or not isinstance(f.get("label"), str):
                    raise Denied("E_SCHEMA", "each field is {label, type, value, answer_key | form_answer_draft_uid, "
                                             "choices}")
            _uid(payload.get("job_uid"), "job", "payload.job_uid")
            _uid(payload.get("resume_variant_uid"), "variant", "payload.resume_variant_uid")
            _uid(payload.get("cover_note_draft_uid"), "draft", "payload.cover_note_draft_uid")
            if not payload.get("resume_variant_uid"):
                raise Denied("E_VALIDATION", "payload.resume_variant_uid is required")
            if out["job_uid"] and payload.get("job_uid") and payload["job_uid"] != out["job_uid"]:
                raise Denied("E_VALIDATION", "payload.job_uid differs from job_uid")
            out["job_uid"] = out["job_uid"] or payload.get("job_uid")
    elif payload is not None:
        raise Denied("E_SCHEMA", "payload is only for application_package and resume drafts")
    out["payload"] = payload
    if kind == "form_answer" and not (out["field_label"] or "").strip():
        raise Denied("E_VALIDATION", "form_answer drafts need field_label (the question text)")
    if kind in FOLLOWUP_KINDS and not tk:
        raise Denied("E_VALIDATION", "%s drafts must give thread_key" % kind)
    if kind in ("cold_email", "li_invite_note", "li_message", "inmail") and not out["contact_uid"]:
        raise Denied("E_VALIDATION", "%s drafts must give contact_uid" % kind)
    if kind in ("application_email", "application_package", "form_answer", "cover_note") and not out["job_uid"]:
        raise Denied("E_VALIDATION", "%s drafts must give job_uid" % kind)
    if kind == "application_email" and not out["attachment_variant_uid"]:
        raise Denied("E_VALIDATION", "application_email drafts must give attachment_variant_uid")
    return out


# ---------------------------------------------------------------- routing (code derives, never the file)
def _route(conn, d: dict, cfg: dict) -> dict:
    kind = d["kind"]
    r = {"contact_id": None, "company_id": None, "job_id": None, "thread_key": None, "recipient": None,
         "subject": d["subject"] if kind in SUBJECT_KINDS else None}
    if kind in FOLLOWUP_KINDS:
        t = _by_uid(conn, "threads", "thread_key", d["thread_key"], "thread")
        want = "email" if kind == "followup_email" else "linkedin"
        if t["channel"] != want:
            raise Denied("E_FOLLOWUP_BINDING", "thread %s is a %s thread" % (t["thread_key"], t["channel"]))
        r.update(thread_key=t["thread_key"], contact_id=_follow(conn, "contacts", t["contact_id"]),
                 company_id=_follow(conn, "companies", t["company_id"]), job_id=t["job_id"])
        first = _one(conn, "SELECT recipient FROM actions WHERE id = ?", (t["first_action_id"],))
        r["recipient"] = first["recipient"] if first else None
        if kind == "followup_email":
            subj = (t["subject"] or "").strip()
            if subj:
                r["subject"] = subj if re.match(r"^re\s*:", subj, re.I) else "Re: " + subj
            elif d["subject"]:
                r["subject"] = d["subject"]
        return r
    contact = job = None
    if d["contact_uid"]:
        contact = _by_uid(conn, "contacts", "contact_uid", d["contact_uid"], "contact")
        cid = _follow(conn, "contacts", contact["id"])
        if cid != contact["id"]:
            contact = _one(conn, "SELECT * FROM contacts WHERE id = ?", (cid,))
        r["contact_id"] = contact["id"]
    if d["job_uid"]:
        job = _by_uid(conn, "jobs", "job_uid", d["job_uid"], "job")
        r["job_id"] = job["id"]
    c_company = _follow(conn, "companies", contact["company_id"]) if contact is not None else None
    j_company = _follow(conn, "companies", job["company_id"]) if job is not None else None
    if c_company and j_company and c_company != j_company:
        raise Denied("E_VALIDATION", "the contact and the job belong to different companies")
    r["company_id"] = c_company or j_company
    if kind in EMAIL_KINDS:
        email = (contact["email"] if contact is not None else None) or \
                (job["apply_email"] if (job is not None and kind == "application_email") else None)
        if not email:
            raise Denied("E_VALIDATION", "no email address is stored for this target; add it with contact add")
        if contact is not None and contact["email"] and contact["email_invalid"]:
            raise Denied("E_ADDRESS_GRADE", "the stored address is marked invalid")
        r["recipient"] = email.strip().lower()
    elif kind in LI_KINDS and contact is not None:
        r["recipient"] = contact["li_slug"]
    return r


def _is_first_touch(conn, kind: str, contact_id) -> bool:
    if kind in ("cold_email", "li_invite_note", "inmail"):
        return True
    if contact_id is None:
        return False
    if kind == "application_email":
        row = _one(conn, "SELECT role_type FROM contacts WHERE id = ?", (contact_id,))
        return bool(row) and row[0] != "role_inbox"
    if kind == "li_message":
        return _one(conn, "SELECT 1 FROM threads WHERE contact_id = ? AND channel = 'linkedin' AND "
                          "state = 'invite_accepted'", (contact_id,)) is None
    return False


def _uid_of(conn, table: str, col: str, row_id):
    if row_id is None:
        return None
    r = _one(conn, "SELECT %s FROM %s WHERE id = ?" % (col, table), (row_id,))
    return r[0] if r else None


def target_keys(conn, contact_id=None, job_id=None, company_id=None) -> list:
    out = []
    for prefix, table, col, rid in (("contact", "contacts", "contact_uid", contact_id),
                                    ("job", "jobs", "job_uid", job_id),
                                    ("company", "companies", "company_uid", company_id)):
        uid = _uid_of(conn, table, col, rid)
        if uid:
            out.append("%s:%s" % (prefix, uid))
    return out


def dedup_findings(conn, kind: str, contact_id=None, company_id=None, job_id=None, thread_key=None,
                   check_skips: bool = True) -> list:
    """[[rule, detail, code]] for the L-DEDUP-* rules (same facts the ledger triggers use; reserve stays the
    authority). Empty when nothing blocks."""
    out = []
    if kind in PART_KINDS or kind == "resume":
        return out
    stamp = now()
    if contact_id is not None:
        c = _one(conn, "SELECT do_not_contact FROM contacts WHERE id = ?", (contact_id,))
        if c and c[0]:
            out.append(["L-DEDUP-DNC", "the person is marked do-not-contact", "E_CONTACT_DNC"])
    if company_id is not None:
        c = _one(conn, "SELECT contact_state, is_agency FROM companies WHERE id = ?", (company_id,))
        if c and c["contact_state"] in ("do_not_contact", "active_thread"):
            out.append(["L-DEDUP-COMPANY-BLOCKED", "company state %s" % c["contact_state"], "E_COMPANY_BLOCKED"])
        if c and kind in ("cold_email", "application_email") and not c["is_agency"]:
            days = _one(conn, "SELECT value FROM meta WHERE key = 'company_email_cooldown_days'")
            try:
                days = int(days[0]) if days else 365
            except (TypeError, ValueError):
                days = 365
            days = days if days > 0 else 365
            hit = _one(conn, "SELECT token FROM actions WHERE company_id = ? AND kind IN ('cold_email',"
                             "'application_email') AND status IN %s AND reserved_at > ?" % _LIVE_SQL,
                       (company_id, ts_add(stamp, days=-days)))
            if hit:
                out.append(["L-DEDUP-COMPANY", "this company was emailed within %d days" % days,
                            "E_COMPANY_COOLDOWN"])
    if contact_id is not None and _is_first_touch(conn, kind, contact_id):
        hit = _one(conn, "SELECT token, kind FROM actions WHERE contact_id = ? AND first_touch = 1 AND status IN %s"
                   % _LIVE_SQL, (contact_id,))
        if hit:
            out.append(["L-DEDUP-PERSON", "the person already had a first touch (%s)" % hit["kind"], "E_DUP_PERSON"])
    if job_id is not None and kind in ("application_email", "application_package"):
        hit = _one(conn, "SELECT token FROM actions WHERE job_id = ? AND kind IN ('application','application_email') "
                         "AND status IN %s" % _LIVE_SQL, (job_id,))
        if hit:
            out.append(["L-DEDUP-JOB", "an application for this job exists", "E_DUP_JOB"])
    if kind in FOLLOWUP_KINDS and thread_key:
        t = _one(conn, "SELECT state, followup_action_id FROM threads WHERE thread_key = ?", (thread_key,))
        hit = _one(conn, "SELECT token FROM actions WHERE thread_key = ? AND kind IN ('followup_email','li_followup') "
                         "AND status IN %s" % _LIVE_SQL, (thread_key,))
        if hit or (t and t["followup_action_id"] is not None):
            out.append(["L-DEDUP-FOLLOWUP", "this thread already had its one follow-up", "E_DUP_THREAD_FOLLOWUP"])
        elif t and t["state"] != "open":
            out.append(["L-DEDUP-FOLLOWUP", "thread state is %s" % t["state"], "E_FOLLOWUP_BINDING"])
    if check_skips and kind not in FOLLOWUP_KINDS:
        for key in target_keys(conn, contact_id, job_id, company_id if kind in ("cold_email",) else None):
            s = _one(conn, "SELECT until, reason FROM target_skips WHERE target_key = ? AND until > ?", (key, stamp))
            if s:
                out.append(["L-DEDUP-SKIPPED", "%s is skipped until %s (%s)" % (key, s["until"], s["reason"]),
                            "E_TARGET_SKIPPED"])
    try:
        from . import exclusions as _excl   # U1
        hits = _excl.match(conn, contact_id=contact_id, company_id=company_id, job_id=job_id)
        if hits:
            out.append(["L-DEDUP-EXCLUDED", "matches a private exclusion", "E_EXCLUDED"])
    except (ImportError, AttributeError, NotImplementedError, TypeError):
        pass   # interface not available yet; gate.reserve checks exclusions (authoritative)
    return out


# ---------------------------------------------------------------- evidence, attachments
def _norm_ws(s) -> str:
    return " ".join(str(s or "").split())


def _bind_hook(conn, hook: dict | None, contact_id, company_id, job_id) -> dict | None:
    """The hook's evidence comes from the stored research fact (the agent cannot alter it)."""
    if not hook or not hook.get("fact_id"):
        return hook
    f = _one(conn, "SELECT * FROM research_facts WHERE fact_uid = ?", (hook["fact_id"],))
    owners = {("person", contact_id), ("company", company_id), ("job", job_id)}
    if f is None or (f["subject_kind"], f["subject_id"]) not in owners:
        raise Denied("E_VALIDATION", "hook.fact_id %s is not a research fact of this contact, company or job"
                     % hook["fact_id"])
    if hook.get("snippet") and _norm_ws(hook["snippet"]) != _norm_ws(f["snippet"]):
        raise Denied("E_VALIDATION", "hook.snippet differs from the stored research fact %s" % hook["fact_id"])
    out = dict(hook)
    out.update(snippet=f["snippet"], source_url=f["source_url"], source_type=f["source_type"],
               published_at=f["published_at"], retrieved_at=f["retrieved_at"])
    return out


def variant_info(conn, variant_uid: str | None, cfg: dict | None = None) -> dict | None:
    """{uid, id, job_id, filename, sha256, qc_ok, draft_id} of a resume variant, or None."""
    if not variant_uid:
        return None
    v = _one(conn, "SELECT * FROM resume_variants WHERE variant_uid = ?", (variant_uid,))
    if v is None:
        return None
    qc_ok = False
    if v["draft_id"] is not None:
        d = _one(conn, "SELECT status FROM drafts WHERE id = ?", (v["draft_id"],))
        qc_ok = bool(d) and d[0] in ("qc_passed", "approved", "sent")
    return {"uid": v["variant_uid"], "id": v["id"], "job_id": v["job_id"],
            "filename": resume_filename(cfg or _settings(conn)),
            "sha256": v["pdf_sha256"], "qc_ok": qc_ok, "draft_id": v["draft_id"]}


def _attachment(conn, d: dict, job_id, cfg: dict) -> tuple:
    """(attachment dict or None, variant row id or None) for application_email and application_package."""
    uid = d["attachment_variant_uid"] if d["kind"] == "application_email" else \
        ((d.get("payload") or {}).get("resume_variant_uid") if d["kind"] == "application_package" else None)
    if not uid:
        return None, None
    v = variant_info(conn, uid, cfg)
    if v is None:
        raise Denied("E_NOT_FOUND", "unknown resume variant %s" % uid)
    if v["job_id"] is not None and v["job_id"] != job_id:
        raise Denied("E_VALIDATION", "resume variant %s was built for another job" % uid)
    if not v["qc_ok"]:
        raise Denied("E_QC_NOT_APPROVED", "resume variant %s has not passed QC" % uid)
    return {"variant_uid": uid, "filename": v["filename"], "sha256": v["sha256"]}, v["id"]


# ---------------------------------------------------------------- lint input and context
def lint_input(conn, row, subject=None, body=None) -> dict:
    """The linter's draft dict for a stored row (subject/body can be overridden for a human edit)."""
    p = payload_of(row)
    recipient = {"first_name": "", "last_name": "", "company": "", "company_short": "", "locale": None}
    if row["contact_id"] is not None:
        c = _one(conn, "SELECT * FROM contacts WHERE id = ?", (row["contact_id"],))
        if c is not None and c["role_type"] != "role_inbox":
            full = (c["full_name"] or "").split()
            recipient["first_name"] = c["first_name"] or (full[0] if full else "")
            recipient["last_name"] = full[-1] if len(full) > 1 else ""
            recipient["locale"] = c["locale"]
    if row["company_id"] is not None:
        co = _one(conn, "SELECT display_name FROM companies WHERE id = ?", (row["company_id"],))
        if co is not None:
            recipient["company"] = co[0]
            first = (co[0] or "").split()
            if len(first) > 1 and len(first[0]) >= 4:
                recipient["company_short"] = first[0]
    return {"kind": row["kind"], "channel": row["channel"],
            "subject": row["subject"] if subject is None else subject,
            "body": row["body"] if body is None else body,
            "recipient": recipient, "hook": p.get("hook"), "claims": p.get("claims") or [],
            "links": p.get("links") or [], "is_reply": bool(p.get("is_reply")),
            "field_char_limit": p.get("field_char_limit"), "payload": p.get("payload")}


def _hosts(urls) -> list:
    from urllib.parse import urlparse
    out = []
    for u in urls:
        u = (u or "").strip()
        if not u:
            continue
        host = urlparse(u if "://" in u else "https://" + u).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            out.append(host)
    return out


def answer_bank() -> dict:
    """{key: {value, source, sensitive}} from private/answers.json (6.5 format, owned by U4)."""
    from . import paths
    path = os.path.join(paths.private_dir(), "answers.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for a in (data.get("answers") if isinstance(data, dict) else None) or []:
        if isinstance(a, dict) and a.get("key"):
            out[str(a["key"])] = {"value": a.get("value"), "source": a.get("source"),
                                  "sensitive": bool(a.get("sensitive"))}
    return out


def lint_context(conn, draft_id: int, cfg: dict | None = None) -> dict:
    from .qc import profile_facts
    cfg = cfg or _settings(conn)
    row = get_by_id(conn, draft_id)
    p = payload_of(row)
    stamp = now()
    lc = dict(cfg["qc"]["lint"])
    rs = cfg["outreach"]["research"]
    lc.update(hook_max_age_days=rs["hook_max_age_days"], hook_warn_age_days=rs["hook_preferred_age_days"],
              retrieved_max_age_days=rs["facts_max_age_days"])
    owner = cfg.get("owner") or {}
    sig = owner.get("signature") or {}
    lc["allowed_link_hosts"] = sorted(set(_hosts(list(sig.get("links") or []) + [owner.get("linkedin_profile_url")])
                                          + [h for h in lc.get("allowed_link_hosts") or [] if h]))
    research, flagged = {}, []
    for f in conn.execute(
            "SELECT fact_uid, text, snippet, injection_flag FROM research_facts WHERE "
            "(subject_kind = 'person' AND subject_id IS ?) OR (subject_kind = 'company' AND subject_id IS ?) OR "
            "(subject_kind = 'job' AND subject_id IS ?)", (row["contact_id"], row["company_id"], row["job_id"])):
        research[f["fact_uid"]] = (f["text"] or "") + " " + (f["snippet"] or "")
        if f["injection_flag"]:
            flagged.append(f["fact_uid"])
    others = [r[0] for r in conn.execute(
        "SELECT DISTINCT c.display_name FROM drafts d JOIN companies c ON c.id = d.company_id "
        "WHERE d.created_at >= ? AND d.id <> ? AND d.company_id IS NOT ?",
        (ts_add(stamp, hours=-24), row["id"], row["company_id"]))]
    names = [owner.get("first_name"), owner.get("last_name"), sig.get("full_name")]
    if row["contact_id"] is not None:
        c = _one(conn, "SELECT full_name, first_name FROM contacts WHERE id = ?", (row["contact_id"],))
        if c:
            names += [c["full_name"], c["first_name"]]
    if row["company_id"] is not None:
        names += [r[0] for r in conn.execute("SELECT display_name FROM companies WHERE id = ?", (row["company_id"],))]
    if row["job_id"] is not None:
        j = _one(conn, "SELECT location_raw, norm_city, company_name_raw FROM jobs WHERE id = ?", (row["job_id"],))
        if j:
            names += [j["location_raw"], j["norm_city"], j["company_name_raw"]]
    names += [_base_contact().get(k) for k in ("full_name", "first_name", "last_name")]   # the person's own name
    if row["kind"] == "resume":
        names += [str(n) for n in ((p.get("payload") or {}).get("names") or []) if n]
    recent = [r[0] for r in conn.execute(
        "SELECT d.body FROM actions a JOIN drafts d ON d.id = a.draft_id WHERE a.status IN ('sent','imported') "
        "AND COALESCE(a.sent_at, a.reserved_at) >= ? AND d.id <> ? AND d.body IS NOT NULL "
        "ORDER BY COALESCE(a.sent_at, a.reserved_at) DESC LIMIT 300", (ts_add(stamp, days=-30), row["id"]))]
    ctx = {"profile_facts": profile_facts(), "research_facts": research, "flagged_facts": flagged,
           "other_companies": others, "names": [n for n in names if n], "config": lc, "now": stamp,
           "signature": signature_text(cfg) if row["kind"] in EMAIL_KINDS else None, "recent_texts": recent,
           "dedup": [[f[0], f[1]] for f in dedup_findings(conn, row["kind"], row["contact_id"], row["company_id"],
                                                           row["job_id"], row["thread_key"])]}
    if row["kind"] == "application_package":
        pkg = p.get("payload") or {}
        ctx["answers"] = answer_bank()
        forms = {}
        uids = [f.get("form_answer_draft_uid") for f in pkg.get("fields") or [] if isinstance(f, dict)]
        uids.append(pkg.get("cover_note_draft_uid"))
        for uid in [u for u in uids if u]:
            fd = _one(conn, "SELECT kind, status, body FROM drafts WHERE draft_uid = ?", (uid,))
            if fd:
                forms[uid] = {"kind": fd["kind"], "status": fd["status"], "body": fd["body"]}
        ctx["form_drafts"] = forms
        ctx["variant"] = variant_info(conn, pkg.get("resume_variant_uid"), cfg)
        ctx["attachment"] = p.get("attachment")
    return ctx


def run_lint(conn, row, cfg: dict | None = None, subject=None, body=None) -> dict:
    from .qc.lint import lint
    cfg = cfg or _settings(conn)
    return lint(lint_input(conn, row, subject, body), lint_context(conn, row["id"], cfg))


def record_qc(conn, draft_id: int, attempt: int, stage: str, passed: bool, text_sha: str, blocks=(), warns=(),
              human_edit_no: int = 0, **review) -> None:
    """One qc_results row per (draft, attempt, stage, human edit); a re-run replaces it."""
    cols = {"review_json": None, "weighted_score": None, "lowest_criterion": None, "gates_failed": None,
            "model_verdict": None, "code_verdict": None, "reviewer_model": None}
    cols.update({k: v for k, v in review.items() if k in cols})
    conn.execute(
        "INSERT INTO qc_results (draft_id, attempt, stage, human_edit_no, passed, blocks_json, warns_json, review_json, "
        "weighted_score, lowest_criterion, gates_failed, model_verdict, code_verdict, text_sha256, reviewer_model, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (draft_id, attempt, stage, human_edit_no) DO UPDATE SET passed = excluded.passed, "
        "blocks_json = excluded.blocks_json, warns_json = excluded.warns_json, review_json = excluded.review_json, "
        "weighted_score = excluded.weighted_score, lowest_criterion = excluded.lowest_criterion, "
        "gates_failed = excluded.gates_failed, model_verdict = excluded.model_verdict, "
        "code_verdict = excluded.code_verdict, text_sha256 = excluded.text_sha256, "
        "reviewer_model = excluded.reviewer_model, created_at = excluded.created_at",
        (draft_id, attempt, stage, human_edit_no, 1 if passed else 0, json.dumps(list(blocks)), json.dumps(list(warns)),
         cols["review_json"], cols["weighted_score"], cols["lowest_criterion"], cols["gates_failed"],
         cols["model_verdict"], cols["code_verdict"], text_sha, cols["reviewer_model"], now()))


def lint_passed_for_current_text(conn, row) -> bool:
    r = _one(conn, "SELECT passed FROM qc_results WHERE draft_id = ? AND stage IN ('lint','human_edit_lint') AND "
                   "text_sha256 = ? ORDER BY id DESC LIMIT 1", (row["id"], row["text_sha256"]))
    return bool(r) and bool(r[0])


# ---------------------------------------------------------------- drop and skips
def add_target_skip(conn, target_key: str, reason: str, days: int) -> None:
    stamp = now()
    until = ts_add(stamp, days=days)
    conn.execute(
        "INSERT INTO target_skips (target_key, reason, drops, until, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?) "
        "ON CONFLICT (target_key) DO UPDATE SET reason = excluded.reason, drops = target_skips.drops + 1, "
        "until = MAX(target_skips.until, excluded.until), updated_at = excluded.updated_at",
        (target_key, reason, until, stamp, stamp))


def drop(conn, row, reason: str, findings=None, cfg: dict | None = None) -> None:
    """Budget exhausted: dropped_qc (never sent), target skipped for outreach.target_skip_days, logged."""
    cfg = cfg or _settings(conn)
    jobstate.set_draft_status(conn, row["id"], "dropped_qc", reason, "system:qc")
    keys = target_keys(conn, row["contact_id"], row["job_id"] if row["contact_id"] is None else None, None)
    if not keys:
        keys = target_keys(conn, None, None, row["company_id"])
    for key in keys[:1]:
        add_target_skip(conn, key, "dropped_qc", int(cfg["outreach"]["target_skip_days"]))
    log_event(conn, "draft_dropped", draft_uid=row["draft_uid"], draft_kind=row["kind"], attempt=row["attempt"],
              reason=reason, findings=(findings or [])[:10], target=keys[:1])


# ---------------------------------------------------------------- create and revise
def _store_payload(d: dict, hook, attachment) -> dict:
    return {"hook": hook, "claims": d["claims"], "links": d["links"], "is_reply": d["is_reply"],
            "field_char_limit": d["field_char_limit"], "field_label": d["field_label"], "payload": d["payload"],
            "attachment": attachment, "edited_by_human": 0}


def _clean(s):
    from .qc.lint import sanitize
    return sanitize(s) if s is not None else None


def create_draft(conn, draft: dict, cycle_id: str | None, caller) -> dict:
    """Validate a writer file (12.4), derive routing by code, run the dedup pre-check (exit 3/7 codes, no row
    is written), store the draft and lint it (qc_results stage lint, attempt 1). Returns
    {draft_uid, draft_id, status: drafted | lint_failed, attempt, lint}."""
    cfg = _settings(conn)
    d = validate_file(draft)
    kind = d["kind"]
    agent = getattr(caller, "agent_id", None) if getattr(caller, "cls", None) == "agent" else None
    if agent is not None and kind not in AGENT_KINDS.get(agent, ()):
        raise Denied("E_CALLER_NOT_ALLOWED", "%s may not create %s drafts" % (agent, kind))
    r = _route(conn, d, cfg)
    for rule, detail, code in dedup_findings(conn, kind, r["contact_id"], r["company_id"], r["job_id"],
                                             r["thread_key"]):
        raise Denied(code, "%s: %s" % (rule, detail), data={"lint_rule": rule})
    hook = _bind_hook(conn, d["hook"], r["contact_id"], r["company_id"], r["job_id"])
    attachment, variant_id = _attachment(conn, d, r["job_id"], cfg)
    if kind == "application_package":
        pkg = d["payload"]
        job = _one(conn, "SELECT job_uid FROM jobs WHERE id = ?", (r["job_id"],))
        pkg["job_uid"] = job[0] if job else pkg.get("job_uid")
    p = _store_payload(d, hook, attachment)
    subject, body = _clean(r["subject"]), _clean(d["body"])
    text = compute_send_text(kind, subject, body, p, cfg)
    stamp = now()
    route = "none" if kind in PART_KINDS else ("browser" if kind in LI_KINDS or kind == "application_package" else
                                               ("mailer" if cfg["gmail"].get("route", "app_password") == "app_password"
                                                else "browser"))
    uid = canon.new_uid("D")
    cur = conn.execute(
        "INSERT INTO drafts (draft_uid, kind, channel, send_route, job_id, contact_id, company_id, thread_key, "
        "recipient, subject, body, payload_json, attachment_variant_id, text_sha256, attempt, human_edits, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 'drafted', ?, ?)",
        (uid, kind, d["channel"], route, r["job_id"], r["contact_id"], r["company_id"], r["thread_key"],
         r["recipient"], subject, body, json.dumps(p, sort_keys=True), variant_id if kind == "application_email"
         else None, sha256_text(text), stamp, stamp))
    draft_id = cur.lastrowid
    row = get_by_id(conn, draft_id)
    res = run_lint(conn, row, cfg)
    record_qc(conn, draft_id, 1, "lint", res["pass"], row["text_sha256"], res["blocks"], res["warns"])
    status = "drafted"
    if not res["pass"]:
        jobstate.set_draft_status(conn, draft_id, "lint_failed", "lint", "system:qc")
        status = "lint_failed"
        if max_attempts(cfg) <= 1:
            drop(conn, get_by_id(conn, draft_id), "lint_failed_budget", res["blocks"], cfg)
            status = "dropped_qc"
    log_event(conn, "draft_created", draft_uid=uid, draft_kind=kind, channel=d["channel"], cycle_id=cycle_id,
              lint_pass=res["pass"], by=agent or getattr(caller, "cls", "system"))
    return {"draft_uid": uid, "draft_id": draft_id, "status": status, "attempt": 1, "lint": res,
            "attempts_left": max_attempts(cfg) - 1}


def revise_draft(conn, draft_uid: str, draft: dict) -> dict:
    """A rewrite after a lint or review failure (attempt + 1, at most 3 attempts). The routing cannot change.
    Returns {draft_uid, status, attempt, lint}; status dropped_qc when the last attempt failed lint."""
    cfg = _settings(conn)
    row = get(conn, draft_uid)
    if row["draft_uid"] != draft_uid:
        raise Denied("E_USAGE", "draft revise takes the draft uid, not an approval code")
    if row["status"] == "dropped_qc":
        raise Denied("E_QC_BUDGET_EXHAUSTED", "the draft was dropped by QC; move on", data={"draft_uid": draft_uid})
    p_old = payload_of(row)
    if row["status"] not in ("lint_failed", "review_failed"):
        raise Denied("E_PRECONDITION", "draft revise is only for a draft whose lint or review failed (status %s)"
                     % row["status"], data={"status": row["status"]})
    if row["status"] == "review_failed" and (row["status_reason"] or "") == "reviewer_unavailable":
        raise Denied("E_PRECONDITION", "the reviewer did not answer; that is not a verdict. Run qc review start again")
    if p_old.get("edited_by_human"):
        raise Denied("E_PRECONDITION", "this text is the person's own edit; the model never rewrites it")
    if row["attempt"] >= max_attempts(cfg):
        drop(conn, row, "budget_exhausted", None, cfg)
        return {"draft_uid": draft_uid, "status": "dropped_qc", "attempt": row["attempt"], "lint": None}
    d = validate_file(draft)
    if d["kind"] != row["kind"] or d["channel"] != row["channel"]:
        raise Denied("E_VALIDATION", "a revision keeps the kind and channel of the draft")
    same = {"contact_uid": _uid_of(conn, "contacts", "contact_uid", row["contact_id"]),
            "job_uid": _uid_of(conn, "jobs", "job_uid", row["job_id"]), "thread_key": row["thread_key"]}
    for k, v in same.items():
        if d.get(k) and d[k] != v and not (k == "contact_uid" and row["kind"] in FOLLOWUP_KINDS):
            raise Denied("E_VALIDATION", "a revision cannot change %s" % k)
    hook = _bind_hook(conn, d["hook"], row["contact_id"], row["company_id"], row["job_id"])
    attachment = p_old.get("attachment")
    if row["kind"] == "application_email" and d["attachment_variant_uid"] and attachment and \
            d["attachment_variant_uid"] != attachment.get("variant_uid"):
        raise Denied("E_VALIDATION", "a revision cannot change the attached resume variant")
    if row["kind"] == "application_package":
        attachment, _vid = _attachment(conn, d, row["job_id"], cfg)
    p = _store_payload(d, hook, attachment)
    subject = row["subject"] if row["kind"] == "followup_email" else _clean(d["subject"] if row["kind"] in
                                                                           SUBJECT_KINDS else None)
    body = _clean(d["body"])
    text = compute_send_text(row["kind"], subject, body, p, cfg)
    attempt = row["attempt"] + 1
    jobstate.set_draft_status(conn, row["id"], "drafted", "revision %d" % attempt, "system:qc")
    conn.execute("UPDATE drafts SET subject = ?, body = ?, payload_json = ?, text_sha256 = ?, attempt = ?, "
                 "updated_at = ? WHERE id = ?",
                 (subject, body, json.dumps(p, sort_keys=True), sha256_text(text), attempt, now(), row["id"]))
    row = get_by_id(conn, row["id"])
    res = run_lint(conn, row, cfg)
    record_qc(conn, row["id"], attempt, "lint", res["pass"], row["text_sha256"], res["blocks"], res["warns"])
    status = "drafted"
    if not res["pass"]:
        jobstate.set_draft_status(conn, row["id"], "lint_failed", "lint", "system:qc")
        status = "lint_failed"
        if attempt >= max_attempts(cfg):
            drop(conn, get_by_id(conn, row["id"]), "lint_failed_last_attempt", res["blocks"], cfg)
            status = "dropped_qc"
    log_event(conn, "draft_revised", draft_uid=row["draft_uid"], attempt=attempt, lint_pass=res["pass"])
    return {"draft_uid": row["draft_uid"], "status": status, "attempt": attempt, "lint": res,
            "attempts_left": max(0, max_attempts(cfg) - attempt)}


# ---------------------------------------------------------------- expiry
def expire(conn) -> int:
    """awaiting_approval past expires_at -> expired (code closed; a package's job goes back to eligible);
    approved but unsent past expires_at and without a live action -> expired. Returns the count."""
    stamp = now()
    n = 0
    rows = conn.execute("SELECT * FROM drafts WHERE status IN ('awaiting_approval','approved') AND expires_at IS NOT "
                        "NULL AND expires_at < ? AND send_route <> 'none'", (stamp,)).fetchall()
    for row in rows:
        if row["status"] == "approved" and _one(conn, "SELECT 1 FROM actions WHERE draft_id = ? AND status IN %s"
                                                % _LIVE_SQL, (row["id"],)):
            continue
        jobstate.set_draft_status(conn, row["id"], "expired", "ttl", "system:qc")
        conn.execute("UPDATE approval_codes SET closed_at = ? WHERE draft_id = ? AND closed_at IS NULL",
                     (stamp, row["id"]))
        if row["kind"] == "application_package" and row["job_id"] is not None:
            j = _one(conn, "SELECT status FROM jobs WHERE id = ?", (row["job_id"],))
            if j and j[0] == "awaiting_approval":
                jobstate.set_job_status(conn, row["job_id"], "eligible", "approval_expired", "system:qc")
        n += 1
    return n


# ---------------------------------------------------------------- views
def summary(conn, row, with_text: bool = False) -> dict:
    code = _one(conn, "SELECT code FROM approval_codes WHERE draft_id = ? ORDER BY issued_at DESC LIMIT 1", (row["id"],))
    out = {"draft_uid": row["draft_uid"], "kind": row["kind"], "channel": row["channel"], "status": row["status"],
           "status_reason": row["status_reason"], "attempt": row["attempt"], "human_edits": row["human_edits"],
           "code": code[0] if code else None, "recipient": row["recipient"], "subject": row["subject"],
           "job_uid": _uid_of(conn, "jobs", "job_uid", row["job_id"]),
           "contact_uid": _uid_of(conn, "contacts", "contact_uid", row["contact_id"]),
           "thread_key": row["thread_key"], "text_sha256": row["text_sha256"], "approved_by": row["approved_by"],
           "expires_at": row["expires_at"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if with_text:
        out["body"] = row["body"]
    return out


def field(conn, row, name: str):
    if name == "subject":
        return row["subject"]
    if name == "body":
        return row["body"]
    if name == "send_text":
        return send_text(conn, row["id"])
    if name == "form":
        p = payload_of(row)
        if row["kind"] == "form_answer":
            return {"fields": _fields_for_text("form_answer", p, row["body"])}
        pkg = p.get("payload") or {}
        return {"fields": [{"label": f.get("label"), "type": f.get("type"), "value": f.get("value")}
                           for f in pkg.get("fields") or [] if isinstance(f, dict)],
                "resume_filename": (p.get("attachment") or {}).get("filename")}
    raise Denied("E_USAGE", "unknown field %r" % name)


def list_drafts(conn, status: str | None = None, kinds=None, limit: int = 50) -> list:
    sql = "SELECT * FROM drafts"
    args: list = []
    where = []
    if status:
        where.append("status = ?")
        args.append(status)
    if kinds:
        where.append("kind IN (%s)" % ", ".join("?" for _ in kinds))
        args.extend(kinds)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [summary(conn, r) for r in conn.execute(sql, args).fetchall()]
