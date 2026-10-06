"""Approvals: codes, routing after QC, approve, skip, human edit, pending list (design 5.4, 5.5).

Where approvals come from: `/jh approve` (chat, guard-signed grant, by = human:chat), the Sheet Approvals tab
(sheet sync, by = human:sheet) and `./jobhunter approve` (PIN, by = human:cli). No agent ACL contains approve,
skip or edit, and approve() itself refuses any `by` that is not human:*. The only automatic approval is
route_after_qc() in `auto` mode (approved_by = auto).

Approval codes are 4 characters from ACDEFGHJKMNPQRTUVWXY34679 (acl.json class `code`); a code row is kept 30 days
and a code is never issued again while any row with it exists, so a late reply cannot approve a different draft.
All functions run inside the caller's transaction and never commit.
"""
from __future__ import annotations

import re
import secrets

from . import drafts, jobstate
from .canon import now, parse_ts, sha256_text, ts_add
from .errors import Denied
from .events import enqueue_notification, log_event

CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
HUMAN_BY = ("human:chat", "human:sheet", "human:cli")
KIND_LABEL = {"cold_email": "Cold email", "followup_email": "Follow-up email", "application_email": "Application email",
              "li_invite_note": "LinkedIn invite note", "li_message": "LinkedIn message",
              "li_followup": "LinkedIn follow-up", "inmail": "InMail", "application_package": "Application",
              "resume": "Resume", "form_answer": "Form answer", "cover_note": "Cover note"}
SALARY_RE = re.compile(r"\b(salary|salaries|ctc|lpa|compensation|pay range|expected pay|remuneration|stipend|"
                       r"lakhs?|per annum)\b", re.I)
REFERRAL_RE = re.compile(r"\b(refer|referral|referrals|referred|referring)\b", re.I)
SOFT_GATES = frozenset({"swap_test", "no_ai_voice"})


def _one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


# ---------------------------------------------------------------- codes
def issue_code(conn, draft_id: int) -> str:
    """The open approval code of a draft, issuing a fresh one (never used in the last 30 days) if needed."""
    row = _one(conn, "SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL", (draft_id,))
    if row:
        return row[0]
    for _ in range(200):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        if _one(conn, "SELECT 1 FROM approval_codes WHERE code = ?", (code,)) is None and \
                _one(conn, "SELECT 1 FROM captcha_tasks WHERE code = ? AND (status = 'open' OR opened_at > ?)",
                     (code, ts_add(now(), days=-30))) is None:
            conn.execute("INSERT INTO approval_codes (code, draft_id, issued_at) VALUES (?, ?, ?)",
                         (code, draft_id, now()))
            return code
    raise Denied("E_INTERNAL", "no free approval code; run housekeeping to prune codes older than 30 days")


def _close_code(conn, draft_id: int) -> None:
    conn.execute("UPDATE approval_codes SET closed_at = ? WHERE draft_id = ? AND closed_at IS NULL", (now(), draft_id))


def _open_code(conn, draft_id: int):
    r = _one(conn, "SELECT code FROM approval_codes WHERE draft_id = ? AND closed_at IS NULL", (draft_id,))
    return r[0] if r else None


# ---------------------------------------------------------------- descriptions
def _short_name(full: str | None) -> str:
    parts = (full or "").split()
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "%s %s." % (parts[0], parts[-1][0])


def describe(conn, row) -> dict:
    """{what, to, company, job_title} in the words the person sees."""
    to, company, title = "", "", ""
    if row["contact_id"] is not None:
        c = _one(conn, "SELECT full_name, title, role_type, email FROM contacts WHERE id = ?", (row["contact_id"],))
        if c is not None:
            if c["role_type"] == "role_inbox":
                to = "%s@ (role inbox)" % (c["email"] or "").split("@")[0]
            else:
                to = _short_name(c["full_name"]) + ((" (%s)" % c["title"]) if c["title"] else "")
    if row["company_id"] is not None:
        co = _one(conn, "SELECT display_name FROM companies WHERE id = ?", (row["company_id"],))
        company = co[0] if co else ""
    if row["job_id"] is not None:
        j = _one(conn, "SELECT title, company_name_raw FROM jobs WHERE id = ?", (row["job_id"],))
        if j is not None:
            title = j["title"]
            company = company or j["company_name_raw"]
    if not to:
        to = "Company form" if row["kind"] == "application_package" else (row["recipient"] or company)
    return {"what": KIND_LABEL.get(row["kind"], row["kind"]), "to": to, "company": company, "job_title": title}


def _latest_review(conn, draft_id: int):
    return _one(conn, "SELECT * FROM qc_results WHERE draft_id = ? AND stage = 'review' ORDER BY id DESC LIMIT 1",
                (draft_id,))


def _preview(conn, row) -> str:
    if row["kind"] == "application_package":
        f = drafts.field(conn, row, "form")
        return "; ".join("%s: %s" % (x["label"], x["value"]) for x in f["fields"])[:80]
    return (row["subject"] or (row["body"] or "").strip().split("\n")[0])[:80]


def notification_text(conn, row, code: str, cfg: dict, note: str | None = None) -> str:
    info = describe(conn, row)
    rv = _latest_review(conn, row["id"])
    score = ("%.2f" % rv["weighted_score"]) if rv is not None and rv["weighted_score"] is not None else "n/a"
    p = drafts.payload_of(row)
    lines = []
    if row["kind"] == "application_package":
        lines.append("Approve %s? Application to %s for %s" % (code, info["company"], info["job_title"]))
    else:
        lines.append("Approve %s? %s to %s%s" % (code, info["what"], info["to"],
                                                 (", " + info["company"]) if info["company"] else ""))
    hook = p.get("hook") or {}
    if hook.get("anchor"):
        lines.append("Why this person: %s (%s, %s)" % (hook["anchor"], hook.get("source_type") or "source",
                                                         hook.get("published_at") or hook.get("retrieved_at") or ""))
    if note:
        lines.append(note)
    if row["subject"]:
        lines.append("Subject: %s" % row["subject"])
    lines.append("---")
    if row["kind"] == "application_package":
        form = drafts.field(conn, row, "form")
        for f in form["fields"]:
            lines.append("%s: %s" % (f["label"], f["value"]))
        if form.get("resume_filename"):
            lines.append("Resume: %s" % form["resume_filename"])
    else:
        lines.append(row["body"] or "")
    lines.append("---")
    ttl = int(cfg["approval"]["approval_ttl_hours"])
    lines.append('QC %s/5. Reply "/jh approve %s", "/jh skip %s", or "/jh edit %s <your text>". Expires in %d h.'
                 % (score, code, code, code, ttl))
    return "\n".join(lines)


# ---------------------------------------------------------------- routing after QC
def always_human_reasons(conn, row, cfg: dict) -> list:
    """The approval.always_human categories (and LinkedIn per-channel rule) that apply to this draft."""
    reasons = []
    cats = set(cfg["approval"].get("always_human") or [])
    text = drafts.send_text(conn, row["id"], cfg)
    if "positive_or_ambiguous_reply" in cats and row["thread_key"]:
        t = _one(conn, "SELECT state, reply_class FROM threads WHERE thread_key = ?", (row["thread_key"],))
        if t is not None and (t["state"] == "replied" or t["reply_class"] in ("positive", "neutral",
                                                                                "referral_offered")):
            reasons.append("positive_or_ambiguous_reply")
    if "mentions_salary" in cats and SALARY_RE.search(text):
        reasons.append("mentions_salary")
    if "referral_ask" in cats and REFERRAL_RE.search(row["body"] or ""):
        reasons.append("referral_ask")
    if "sensitive_form_field" in cats and row["kind"] == "application_package":
        bank = drafts.answer_bank()
        from .qc.lint import EEO_LABEL
        for f in (drafts.payload_of(row).get("payload") or {}).get("fields") or []:
            key = f.get("answer_key") if isinstance(f, dict) else None
            if (key and (bank.get(key) or {}).get("sensitive")) or EEO_LABEL.search(str(f.get("label") or "")) \
                    or SALARY_RE.search(str(f.get("label") or "")):
                reasons.append("sensitive_form_field")
                break
    if row["kind"] in drafts.LI_KINDS and (cfg["approval"].get("per_channel") or {}).get("linkedin", "human") == \
            "human":
        reasons.append("linkedin_human")
    return reasons


def effective_mode(conn, row, cfg: dict) -> tuple:
    """('auto' | 'human', reasons). auto only when meta.approval_mode is auto, the file says inherit, no
    always-human category applies and the text is not a human edit."""
    meta = _one(conn, "SELECT value FROM meta WHERE key = 'approval_mode'")
    if not meta or meta[0] != "auto":
        return "human", ["approval_mode_human"]
    if cfg["approval"].get("mode", "inherit") not in ("inherit", "auto"):
        return "human", ["config_forces_human"]
    reasons = always_human_reasons(conn, row, cfg)
    if drafts.payload_of(row).get("edited_by_human"):
        reasons.append("human_edit")
    return ("human", reasons) if reasons else ("auto", [])


def _approved_expiry(row, cfg: dict) -> str:
    days = int(cfg["outreach"]["research"]["facts_max_age_days"])
    hook = drafts.payload_of(row).get("hook") or {}
    stamp = now()
    base = stamp
    ret = hook.get("retrieved_at")
    if ret:
        try:
            base = ret if "T" in ret else ret + "T00:00:00Z"
            parse_ts(base)
        except ValueError:
            base = stamp
    exp = ts_add(base, days=days)
    return exp if exp > stamp else ts_add(stamp, hours=1)


def _approve_parts(conn, row, by: str) -> None:
    p = drafts.payload_of(row)
    ids = []
    att = p.get("attachment") or {}
    if att.get("variant_uid"):
        v = _one(conn, "SELECT draft_id FROM resume_variants WHERE variant_uid = ?", (att["variant_uid"],))
        if v and v[0]:
            ids.append(v[0])
    pkg = p.get("payload") or {}
    if row["kind"] == "application_package":
        uids = [f.get("form_answer_draft_uid") for f in pkg.get("fields") or [] if isinstance(f, dict)]
        uids.append(pkg.get("cover_note_draft_uid"))
        for uid in [u for u in uids if u]:
            d = _one(conn, "SELECT id FROM drafts WHERE draft_uid = ?", (uid,))
            if d:
                ids.append(d[0])
    for pid in ids:
        st = _one(conn, "SELECT status FROM drafts WHERE id = ?", (pid,))
        if st and st[0] == "qc_passed":
            jobstate.set_draft_status(conn, pid, "approved", "approved with %s" % row["draft_uid"], by)


def _set_job(conn, job_id, frm: str, to: str, reason: str) -> None:
    if job_id is None:
        return
    j = _one(conn, "SELECT status FROM jobs WHERE id = ?", (job_id,))
    if j and j[0] == frm:
        jobstate.set_job_status(conn, job_id, to, reason, "system:qc")


def _mark_approved(conn, row, by: str, reason: str, cfg: dict) -> None:
    jobstate.set_draft_status(conn, row["id"], "approved", reason, by)
    stamp = now()
    conn.execute("UPDATE drafts SET expires_at = ?, send_after = ?, updated_at = ? WHERE id = ?",
                 (_approved_expiry(row, cfg), stamp, stamp, row["id"]))
    _close_code(conn, row["id"])
    _approve_parts(conn, row, by)


def route_after_qc(conn, draft_id: int, note: str | None = None, cfg: dict | None = None) -> dict:
    """Called when a draft reached qc_passed. Parts (form_answer, cover_note, resume) stay qc_passed and are
    approved together with their package or application email. Otherwise auto mode approves (approved_by =
    auto) and human mode moves the draft to awaiting_approval with a code, a notification and an expiry."""
    cfg = cfg or drafts._settings(conn)
    row = drafts.get_by_id(conn, draft_id)
    if row["status"] != "qc_passed":
        raise Denied("E_BAD_TRANSITION", "route_after_qc needs a qc_passed draft (status %s)" % row["status"])
    if row["kind"] in drafts.PART_KINDS:
        log_event(conn, "draft_qc_passed", draft_uid=row["draft_uid"], draft_kind=row["kind"], part=True)
        return {"draft_uid": row["draft_uid"], "status": "qc_passed", "mode": "part"}
    mode, reasons = effective_mode(conn, row, cfg)
    if note:
        mode = "human"
    if mode == "auto":
        _mark_approved(conn, row, "auto", "qc_passed_auto", cfg)
        log_event(conn, "draft_approved", draft_uid=row["draft_uid"], by="auto")
        return {"draft_uid": row["draft_uid"], "status": "approved", "mode": "auto"}
    stamp = now()
    jobstate.set_draft_status(conn, row["id"], "awaiting_approval", note and "human_edit_review_soft_fail" or
                              "qc_passed", "system:qc")
    conn.execute("UPDATE drafts SET expires_at = ?, updated_at = ? WHERE id = ?",
                 (ts_add(stamp, hours=int(cfg["approval"]["approval_ttl_hours"])), stamp, row["id"]))
    if row["kind"] == "application_package":
        _set_job(conn, row["job_id"], "apply_queued", "awaiting_approval", "package_awaiting_approval")
    code = issue_code(conn, row["id"])
    row = drafts.get_by_id(conn, row["id"])
    enqueue_notification(conn, "approval:%s:%s" % (code, row["text_sha256"][:8]), "normal", "approval",
                         notification_text(conn, row, code, cfg, note))
    log_event(conn, "draft_awaiting_approval", draft_uid=row["draft_uid"], code=code, reasons=reasons)
    return {"draft_uid": row["draft_uid"], "status": "awaiting_approval", "mode": "human", "code": code,
            "reasons": reasons}


# ---------------------------------------------------------------- human decisions
def _check_by(by: str) -> None:
    if by not in HUMAN_BY:
        raise Denied("E_CALLER_NOT_ALLOWED", "approvals come only from the person (human:chat, human:sheet, human:cli)")


def _code_state(conn, code_or_uid: str):
    """For an approval code: (is_code, open). A draft uid counts as open."""
    key = (code_or_uid or "").strip()
    r = _one(conn, "SELECT closed_at FROM approval_codes WHERE code = ?", (key,))
    if r is None:
        return False, True
    return True, r[0] is None


def _qc_evidence_ok(conn, row) -> bool:
    """The latest review of the current text passed, or it was a human edit that failed only on soft items."""
    from .qc import review
    rv = _one(conn, "SELECT * FROM qc_results WHERE draft_id = ? AND stage = 'review' AND text_sha256 = ? "
                    "ORDER BY id DESC LIMIT 1", (row["id"], row["text_sha256"]))
    if rv is None:
        return False
    if rv["code_verdict"] == "pass" and rv["model_verdict"] == "pass" and rv["passed"]:
        return True
    if drafts.payload_of(row).get("edited_by_human"):
        dec = review.decision_from_row(rv, row["channel"])
        return bool(dec.get("soft_only"))
    return False


def approve(conn, code_or_uid: str, by: str) -> dict:
    """awaiting_approval -> approved. Idempotent for an already approved draft. E_NOT_FOUND, E_DRAFT_EXPIRED,
    E_QC_NOT_APPROVED (not awaiting approval, or QC evidence missing), E_QC_HASH_MISMATCH."""
    _check_by(by)
    cfg = drafts._settings(conn)
    row = drafts.get(conn, code_or_uid)
    info = describe(conn, row)
    out = {"draft_uid": row["draft_uid"], "kind": row["kind"], "to": info["to"], "company": info["company"]}
    if row["status"] == "approved":
        return dict(out, status="approved", already=True,
                    message="Already approved %s: %s to %s" % (code_or_uid, info["what"].lower(), info["to"]))
    if row["status"] == "expired" or (row["status"] == "awaiting_approval" and row["expires_at"]
                                      and row["expires_at"] < now()):
        raise Denied("E_DRAFT_EXPIRED", "this approval expired; the item will be drafted again if still relevant",
                     data=out)
    if row["status"] != "awaiting_approval":
        raise Denied("E_QC_NOT_APPROVED", "this draft is %s and cannot be approved" % row["status"],
                     data=dict(out, status=row["status"]))
    is_code, is_open = _code_state(conn, code_or_uid)
    if is_code and not is_open:
        raise Denied("E_QC_NOT_APPROVED", "code %s belongs to an older version of this draft; use code %s"
                     % (code_or_uid, _open_code(conn, row["id"])), data=out)
    if sha256_text(drafts.send_text(conn, row["id"], cfg)) != row["text_sha256"]:
        raise Denied("E_QC_HASH_MISMATCH", "the stored text changed after QC (signature or attachment?)", data=out)
    if not _qc_evidence_ok(conn, row):
        raise Denied("E_QC_NOT_APPROVED", "no passing QC review for the current text", data=out)
    _mark_approved(conn, row, by, "approved_by_person", cfg)
    if row["kind"] == "application_package":
        _set_job(conn, row["job_id"], "awaiting_approval", "apply_queued", "package_approved")
    log_event(conn, "draft_approved", draft_uid=row["draft_uid"], by=by)
    return dict(out, status="approved", already=False,
                message="Approved %s: %s to %s%s" % (code_or_uid, info["what"].lower(), info["to"],
                                                     (" at " + info["company"]) if info["company"] else ""))


def skip(conn, code_or_uid: str, reason: str, by: str) -> dict:
    """awaiting_approval -> skipped_by_human (a package's job closes; the person target is skipped for
    outreach.target_skip_days); a human edit that failed review -> dropped_qc."""
    _check_by(by)
    cfg = drafts._settings(conn)
    row = drafts.get(conn, code_or_uid)
    info = describe(conn, row)
    out = {"draft_uid": row["draft_uid"], "kind": row["kind"], "to": info["to"], "company": info["company"]}
    reason = (reason or "skipped by the person").strip()[:300]
    if row["status"] in ("skipped_by_human", "dropped_qc"):
        return dict(out, status=row["status"], already=True, message="Already skipped %s" % code_or_uid)
    is_code, is_open = _code_state(conn, code_or_uid)
    if is_code and not is_open:
        raise Denied("E_NOT_FOUND", "code %s is no longer open" % code_or_uid, data=out)
    if row["status"] == "review_failed" and drafts.payload_of(row).get("edited_by_human"):
        jobstate.set_draft_status(conn, row["id"], "dropped_qc", "skipped_by_human: " + reason, "system:qc")
    elif row["status"] == "awaiting_approval":
        jobstate.set_draft_status(conn, row["id"], "skipped_by_human", reason, "system:qc")
        if row["kind"] == "application_package":
            _set_job(conn, row["job_id"], "awaiting_approval", "closed", "skipped_by_human")
    else:
        raise Denied("E_NOT_FOUND", "nothing to skip: this draft is %s" % row["status"], data=out)
    _close_code(conn, row["id"])
    if row["contact_id"] is not None and row["kind"] not in drafts.FOLLOWUP_KINDS:
        for key in drafts.target_keys(conn, row["contact_id"]):
            drafts.add_target_skip(conn, key, "skipped_by_human", int(cfg["outreach"]["target_skip_days"]))
    log_event(conn, "draft_skipped", draft_uid=row["draft_uid"], by=by, reason=reason)
    return dict(out, status="skipped", already=False, message="Skipped %s: %s to %s" % (code_or_uid,
                                                                                     info["what"].lower(), info["to"]))


def _split_edit(kind: str, text: str, old_subject):
    t = (text or "").replace("\r\n", "\n").strip("\n")
    m = re.match(r"^subject:[ \t]*([^\n]*)\n[ \t]*\n?(.*)$", t, re.I | re.S)
    if m and kind in ("cold_email", "application_email", "inmail"):
        return m.group(1).strip(), m.group(2)
    return old_subject, t


def human_edit(conn, code_or_uid: str, text: str, by: str) -> dict:
    """The person's own text (5.4): linted first (stage human_edit_lint, own budget of qc.max_human_edits); a
    failing edit leaves the draft as it was and returns the findings as plain sentences; a clean edit replaces
    the text, closes the code and queues a review (the caller spawns the worker after COMMIT)."""
    from .qc import review
    from .qc.lint import explain
    _check_by(by)
    cfg = drafts._settings(conn)
    row = drafts.get(conn, code_or_uid)
    if row["kind"] in ("application_package", "resume"):
        raise Denied("E_VALIDATION", "a package or resume cannot be edited as text; skip it instead")
    p = drafts.payload_of(row)
    if not (row["status"] == "awaiting_approval" or (row["status"] == "review_failed" and p.get("edited_by_human"))):
        raise Denied("E_PRECONDITION", "only a draft that waits for you can be edited (status %s)" % row["status"])
    if row["status"] == "awaiting_approval" and row["expires_at"] and row["expires_at"] < now():
        raise Denied("E_DRAFT_EXPIRED", "this approval expired")
    max_edits = int(cfg["qc"]["max_human_edits"])
    if row["human_edits"] >= max_edits:
        raise Denied("E_QC_BUDGET_EXHAUSTED", "no edits left for this draft (%d used); approve or skip it" % max_edits)
    if not isinstance(text, str) or not text.strip():
        raise Denied("E_VALIDATION", "the edit is empty")
    subject, body = _split_edit(row["kind"], text, row["subject"])
    subject, body = drafts._clean(subject), drafts._clean(body)
    n = row["human_edits"] + 1
    new_p = dict(p, edited_by_human=n)
    sha = sha256_text(drafts.compute_send_text(row["kind"], subject, body, new_p, cfg))
    res = drafts.run_lint(conn, row, cfg, subject=subject, body=body)
    drafts.record_qc(conn, row["id"], row["attempt"], "human_edit_lint", res["pass"], sha, res["blocks"], res["warns"],
                     human_edit_no=n)
    conn.execute("UPDATE drafts SET human_edits = ?, updated_at = ? WHERE id = ?", (n, now(), row["id"]))
    base = {"draft_uid": row["draft_uid"], "edit_no": n, "edits_left": max_edits - n, "lint": res}
    if not res["pass"]:
        findings = res["blocks"] + (res["warns"] if len(res["warns"]) > int(cfg["qc"]["lint"]["max_warnings"]) else [])
        log_event(conn, "human_edit_lint_failed", draft_uid=row["draft_uid"], edit_no=n)
        return dict(base, passed=False, status=row["status"], messages=explain(findings, body or ""))
    _close_code(conn, row["id"])
    jobstate.set_draft_status(conn, row["id"], "drafted", "human_edit %d" % n, "system:qc")
    import json as _json
    conn.execute("UPDATE drafts SET subject = ?, body = ?, payload_json = ?, text_sha256 = ?, updated_at = ? "
                 "WHERE id = ?", (subject, body, _json.dumps(new_p, sort_keys=True), sha, now(), row["id"]))
    job = review.enqueue(conn, drafts.get_by_id(conn, row["id"]))
    log_event(conn, "human_edit_queued", draft_uid=row["draft_uid"], edit_no=n, qjob_uid=job["qjob_uid"], by=by)
    return dict(base, passed=True, status="review_pending", qjob_uid=job["qjob_uid"],
                messages=["Your edit passed the checks and went to the reviewer."])


def pending(conn) -> list:
    """Drafts that wait for the person: [{code, draft_uid, kind, to, company, preview, qc_score, expires_at,
    status}]."""
    out = []
    for row in conn.execute("SELECT d.* FROM drafts d WHERE d.status = 'awaiting_approval' OR (d.status = "
                            "'review_failed' AND EXISTS (SELECT 1 FROM approval_codes a WHERE a.draft_id = d.id AND "
                            "a.closed_at IS NULL)) ORDER BY d.expires_at, d.id").fetchall():
        info = describe(conn, row)
        rv = _latest_review(conn, row["id"])
        out.append({"code": _open_code(conn, row["id"]), "draft_uid": row["draft_uid"], "kind": row["kind"],
                    "to": info["to"], "company": info["company"], "preview": _preview(conn, row),
                    "qc_score": rv["weighted_score"] if rv is not None else None, "expires_at": row["expires_at"],
                    "status": row["status"]})
    return out
