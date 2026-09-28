"""Row builders for the Google Sheet mirror (design 7.3 and 7.4).

One builder per table tab. Each takes the connection, a watermark (`since`, a UTC timestamp or None for a
full rebuild) and a RowCtx, and returns `[(row_id, row_dict, stamp)]`, where `stamp` is the largest
`updated_at` (or `created_at` for append-only tables) over the joined rows. `build_rows()` is the 12.10
form without the stamp. Read-only; no builder writes to the database.

Values follow the Sheet conventions: timestamps stay UTC ISO strings (Code.gs turns them into real dates
shown in the sheet's time zone), `date` columns are the person's local calendar date `YYYY-MM-DD`,
links are `{"text", "url"}`, numbers are numbers, labels come from sheets_labels.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
from dataclasses import dataclass, field

from . import canon
from . import sheets_labels as L
from . import status as S
from .errors import Denied

KEPT_LOCAL = "(kept on your computer)"
MAX_ATTEMPTS = 3
DAILY_MAX_DAYS = 400


@dataclass
class RowCtx:
    config: dict = field(default_factory=dict)
    tz: object = _dt.timezone.utc
    style: str = "first_last_initial"
    store_text: bool = True
    skipped_days: int = 30
    experience: float | None = None
    now: str = ""

    @classmethod
    def load(cls, config: dict | None = None) -> "RowCtx":
        config = S.load_config() if config is None else config
        try:
            days = int(S.cfg(config, "sheets.skipped_tab_days", 30))
        except (TypeError, ValueError):
            days = 30
        return cls(config=config, tz=S.tzinfo(config),
                   style=str(S.cfg(config, "sheets.person_name_style", "first_last_initial")),
                   store_text=bool(S.cfg(config, "sheets.store_message_text", True)),
                   skipped_days=max(days, 1), experience=_experience_years(), now=canon.now())


def _experience_years() -> float | None:
    """Confirmed years of experience (U4 profile.load_confirmed), used in skip reasons."""
    try:
        prof = importlib.import_module("jobhunter.profile").load_confirmed()
    except Exception:  # U4 missing, a stub, or no confirmed profile yet
        prof = None
    if prof is None:
        from . import paths
        try:
            with open(os.path.join(paths.private_dir(), "profile.json"), "r", encoding="utf-8") as fh:
                prof = json.load(fh)
        except (OSError, ValueError):
            return None
    try:
        f = (prof.get("fields") or {}).get("experience_years") or {}
        if isinstance(f, dict):
            if f.get("source") not in (None, "user_confirmed"):
                return None
            return float(f.get("value"))
        return float(f)
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- helpers
def _max(*vals) -> str:
    vals = [v for v in vals if v]
    return max(vals) if vals else ""


def _since_clause(exprs: list[str], since: str | None) -> tuple[str, list]:
    """SQL fragment 'AND (e1 > ? OR e2 > ? ...)' for a watermark."""
    if not since:
        return "", []
    return " AND (" + " OR ".join("COALESCE(%s, '') > ?" % e for e in exprs) + ")", [since] * len(exprs)


def _link(text: str, url: str | None) -> dict | None:
    if not url or not str(url).lower().startswith(("https://", "http://")):
        return None
    return {"text": text, "url": str(url)}


def _local_date(ts: str | None, ctx: RowCtx) -> str | None:
    return S.local_date(ts, ctx.tz) if ts else None


def _json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


def _send_text(conn, draft, ctx: RowCtx) -> str:
    """The exact text for the Full text / Message column (U3 drafts.send_text when available)."""
    if not ctx.store_text:
        return KEPT_LOCAL
    try:
        text = importlib.import_module("jobhunter.drafts").send_text(conn, draft["id"])
        if isinstance(text, str) and text:
            return L.clip(text, 5000)
    except Exception:  # U3 missing or the draft cannot be rendered: fall back to the stored text
        pass
    kind = draft["kind"]
    if kind in canon.EMAIL_KINDS:
        return L.clip("Subject: %s\n\n%s" % (draft["subject"] or "", draft["body"] or ""), 5000)
    if kind == "application_package":
        payload = _json(draft["payload_json"]) or {}
        lines = []
        for f in payload.get("fields") or []:
            if isinstance(f, dict):
                v = f.get("value")
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                lines.append("%s: %s" % (f.get("label", ""), "" if v is None else v))
        if payload.get("resume_variant_uid"):
            lines.append("Resume: %s" % payload["resume_variant_uid"])
        return L.clip("\n".join(lines) or (draft["body"] or ""), 5000)
    return L.clip(draft["body"] or "", 5000)


def _subject(draft, job_title: str | None = None) -> str:
    if draft["subject"]:
        return L.clip(draft["subject"], 200)
    if draft["kind"] == "application_package" and job_title:
        return "Application: %s" % job_title
    return L.first_line(draft["body"], 80)


def _latest_review(conn, draft_id: int | None):
    if not draft_id:
        return None
    return conn.execute("SELECT weighted_score, created_at FROM qc_results WHERE draft_id = ? AND stage = 'review' "
                        "ORDER BY attempt DESC, human_edit_no DESC, id DESC LIMIT 1", (draft_id,)).fetchone()


def _num(v):
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


SOURCE_TYPE = {"linkedin_post": "Their LinkedIn post", "linkedin_profile": "Their LinkedIn profile",
               "company_blog": "Company blog", "news": "News", "company_site": "Company site",
               "podcast": "Podcast", "talk": "Talk", "github": "GitHub", "job_post": "The job post"}


def _hook_text(payload: dict | None, ctx: RowCtx) -> tuple[str, dict | None]:
    hook = (payload or {}).get("hook") if isinstance(payload, dict) else None
    if not isinstance(hook, dict):
        return "", None
    src = SOURCE_TYPE.get(str(hook.get("source_type") or ""), L._titled(str(hook.get("source_type") or "Source")))
    when = S.fmt_local(hook.get("published_at"), ctx.tz, with_time=False)
    snippet = hook.get("snippet") or hook.get("anchor") or ""
    text = "%s%s: %s" % (src, (" (%s)" % when) if when else "", snippet) if snippet else ""
    return L.clip(text, 600), _link("Source", hook.get("source_url"))


# ---------------------------------------------------------------- approvals
def approvals(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    recent = canon.ts_add(ctx.now or canon.now(), days=-7)
    stamp_exprs = ["d.updated_at", "(SELECT max(created_at) FROM qc_results q WHERE q.draft_id = d.id)",
                   "(SELECT max(COALESCE(closed_at, issued_at)) FROM approval_codes a WHERE a.draft_id = d.id)"]
    where, args = _since_clause(stamp_exprs, since)
    sql = ("SELECT d.*, c.full_name, c.first_name, c.title, c.role_type, c.email, co.display_name AS company, "
           "j.company_name_raw, j.title AS job_title, %s AS s1, %s AS s2, "
           "(SELECT code FROM approval_codes a WHERE a.draft_id = d.id ORDER BY (a.closed_at IS NULL) DESC, "
           "a.issued_at DESC LIMIT 1) AS code "
           "FROM drafts d LEFT JOIN contacts c ON c.id = d.contact_id "
           "LEFT JOIN jobs j ON j.id = d.job_id "
           "LEFT JOIN companies co ON co.id = COALESCE(d.company_id, c.company_id, j.company_id) "
           "WHERE d.send_route <> 'none' AND (d.status = 'awaiting_approval' OR (d.updated_at >= ? AND "
           "((d.approved_at IS NOT NULL AND COALESCE(d.approved_by, '') <> 'auto') "
           "OR d.status IN ('skipped_by_human','expired') OR EXISTS "
           "(SELECT 1 FROM approval_codes a WHERE a.draft_id = d.id))))" % (stamp_exprs[1], stamp_exprs[2])
           + where + " ORDER BY d.created_at")
    out = []
    for d in conn.execute(sql, [recent] + args):
        rev = _latest_review(conn, d["id"])
        to = S.recipient_label(d, ctx.style) if d["contact_id"] else "Company form"
        row = {
            "created": d["created_at"], "code": d["code"] or "", "what": L.DRAFT_KIND.get(d["kind"], d["kind"]),
            "to": to, "company": d["company"] or d["company_name_raw"] or "",
            "subject": _subject(d, d["job_title"]), "message": _send_text(conn, d, ctx),
            "qc": _num(rev["weighted_score"]) if rev else None, "expires": d["expires_at"],
            "status": L.DRAFT_STATUS.get(d["status"], d["status"]), "decision": "",
        }
        out.append((d["draft_uid"], row, _max(d["updated_at"], d["s1"], d["s2"])))
    return out


# ---------------------------------------------------------------- jobs and skipped
_FIRST_APP = ("(SELECT a.id FROM actions a WHERE a.job_id = j.id AND a.kind IN ('application','application_email') "
              "AND a.status IN ('sent','imported') ORDER BY a.sent_at LIMIT 1)")


def jobs(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    exprs = ["j.updated_at", "e.updated_at", "co.updated_at", "a.updated_at"]
    where, args = _since_clause(exprs, since)
    sql = ("SELECT j.*, co.display_name AS company, co.updated_at AS co_up, e.score, e.verdict, e.reason_text, "
           "e.gates_failed, e.stage, e.updated_at AS e_up, a.sent_at AS applied_at, a.updated_at AS a_up "
           "FROM jobs j LEFT JOIN companies co ON co.id = j.company_id "
           "LEFT JOIN evaluations e ON e.job_id = j.id LEFT JOIN actions a ON a.id = " + _FIRST_APP +
           " WHERE j.status NOT IN ('new','prefilter_rejected')" + where + " ORDER BY j.discovered_at")
    out = []
    for j in conn.execute(sql, args):
        llm = j["stage"] == "llm"
        row = {
            "found_on": j["discovered_at"], "company": j["company"] or j["company_name_raw"] or "",
            "role": j["title"], "location": j["location_raw"] or (j["norm_city"] or "").title(),
            "work_mode": L.WORK_MODE.get(j["work_mode"], "Unknown"), "source": L.source_label(j["source"]),
            "posting": _link("Open posting", j["source_url"]),
            "fit": j["score"] if llm and j["score"] is not None else None,
            "verdict": L.VERDICT.get(j["verdict"], "") if llm else "",
            "why": L.clip(j["reason_text"], 1000) if llm else "",
            "gates": L.gates_sentences(j["gates_failed"]) if llm else "",
            "status": L.JOB_STATUS.get(j["status"], j["status"]),
            "applied_on": _local_date(j["applied_at"], ctx),
            "your_call": L.HUMAN_CALL_LABEL.get(j["human_call"] or "", ""),
        }
        out.append((j["job_uid"], row, _max(j["updated_at"], j["e_up"], j["co_up"], j["a_up"])))
    return out


def skipped(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    cutoff = canon.ts_add(ctx.now or canon.now(), days=-ctx.skipped_days)
    exprs = ["j.updated_at", "e.updated_at", "co.updated_at"]
    where, args = _since_clause(exprs, since)
    sql = ("SELECT j.*, co.display_name AS company, co.updated_at AS co_up, e.reason_code, e.reason_text, "
           "e.updated_at AS e_up FROM jobs j LEFT JOIN companies co ON co.id = j.company_id "
           "LEFT JOIN evaluations e ON e.job_id = j.id "
           "WHERE j.status = 'prefilter_rejected' AND j.discovered_at >= ?" + where + " ORDER BY j.discovered_at")
    out = []
    for j in conn.execute(sql, [cutoff] + args):
        code = j["status_reason"] or j["reason_code"]
        row = {
            "found_on": j["discovered_at"], "company": j["company"] or j["company_name_raw"] or "",
            "role": j["title"], "location": j["location_raw"] or (j["norm_city"] or "").title(),
            "reason": L.prefilter_sentence(code, j["years_min"], j["years_max"], ctx.experience,
                                           fallback=j["reason_text"]),
            "posting": _link("Open posting", j["source_url"]),
            "your_call": "Apply anyway" if j["human_call"] == "apply_anyway" else "",
        }
        out.append((j["job_uid"], row, _max(j["updated_at"], j["e_up"], j["co_up"])))
    return out


def skipped_expired(conn, ctx: RowCtx, window_days: int | None = 14) -> list[str]:
    """job_uids that must leave the Skipped tab: rejected jobs older than the tab window (only the last
    `window_days` beyond it unless None) and skipped jobs that were moved on by Apply anyway."""
    now = ctx.now or canon.now()
    cutoff = canon.ts_add(now, days=-ctx.skipped_days)
    sql = "SELECT job_uid FROM jobs WHERE status = 'prefilter_rejected' AND discovered_at < ?"
    args = [cutoff]
    if window_days is not None:
        sql += " AND discovered_at >= ?"
        args.append(canon.ts_add(cutoff, days=-window_days))
    uids = [r[0] for r in conn.execute(sql, args)]
    uids += [r[0] for r in conn.execute(
        "SELECT job_uid FROM jobs WHERE human_call = 'apply_anyway' AND status <> 'prefilter_rejected'")]
    return uids


# ---------------------------------------------------------------- applications
def _how(a, style: str) -> str:
    if a["kind"] == "application_email":
        if a["role_type"] == "role_inbox" and a["email"]:
            return "Email to %s@" % str(a["email"]).split("@", 1)[0]
        name = L.person_name(a["full_name"], a["first_name"], style)
        return "Email to %s" % name if name else "Email"
    route = a["app_route"] or a["apply_route"]
    platform = a["platform"] or ""
    if route == "easy_apply" or platform == "linkedin":
        return "LinkedIn Easy Apply"
    if route == "ats_form" or platform in L.ATS_NAMES:
        return "Company form (%s)" % L.platform_label(platform) if platform else "Company form"
    return L.platform_label(platform) or L.source_label(a["source"])


def applications(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    recent_fail = canon.ts_add(ctx.now or canon.now(), days=-7)
    exprs = ["a.updated_at", "ap.updated_at", "j.updated_at", "t.updated_at", "e.updated_at"]
    where, args = _since_clause(exprs, since)
    sql = ("SELECT a.*, j.job_uid, j.title, j.location_raw, j.norm_city, j.source, j.source_url, j.apply_route, "
           "j.company_name_raw, j.updated_at AS j_up, co.display_name AS company, ap.route AS app_route, "
           "ap.confirmation, ap.confirmation_email_at, ap.outcome, ap.notes, ap.updated_at AS ap_up, "
           "rv.mode AS rv_mode, rv.pdf_path, e.score, e.stage, e.updated_at AS e_up, t.followup_due_at, "
           "t.followup_action_id, t.updated_at AS t_up, c.full_name, c.first_name, c.role_type, c.email "
           "FROM actions a JOIN jobs j ON j.id = a.job_id "
           "LEFT JOIN companies co ON co.id = COALESCE(a.company_id, j.company_id) "
           "LEFT JOIN applications ap ON ap.action_id = a.id "
           "LEFT JOIN resume_variants rv ON rv.id = ap.resume_variant_id "
           "LEFT JOIN evaluations e ON e.job_id = j.id "
           "LEFT JOIN threads t ON t.first_action_id = a.id "
           "LEFT JOIN contacts c ON c.id = a.contact_id "
           "WHERE a.id = (SELECT max(b.id) FROM actions b WHERE b.job_id = a.job_id "
           "AND b.kind IN ('application','application_email') "
           "AND NOT (b.status = 'failed' AND b.updated_at < ?))" + where + " ORDER BY a.reserved_at")
    out = []
    for a in conn.execute(sql, [recent_fail] + args):
        resume = ""
        if a["pdf_path"]:
            resume = "%s: %s" % (L.RESUME_MODE.get(a["rv_mode"], "Resume"), os.path.basename(a["pdf_path"]))
        proof = L.clip(a["confirmation"], 300)
        if a["confirmation_email_at"]:
            extra = "Confirmation email received on %s" % S.fmt_local(a["confirmation_email_at"], ctx.tz, False)
            proof = (proof + ". " + extra) if proof else extra
        if a["followup_due_at"] and not a["followup_action_id"]:
            follow = "Due %s" % S.fmt_local(a["followup_due_at"], ctx.tz, False)
        elif a["followup_action_id"]:
            follow = "Sent"
        else:
            follow = "None"
        row = {
            "applied_on": a["sent_at"] or a["reserved_at"], "company": a["company"] or a["company_name_raw"] or "",
            "role": a["title"], "location": a["location_raw"] or (a["norm_city"] or "").title(),
            "how": _how(a, ctx.style), "posting": _link("Open posting", a["source_url"]), "resume": resume,
            "status": L.ACTION_STATUS_APPLICATION.get(a["status"], a["status"]), "proof": proof,
            "fit": a["score"] if a["stage"] == "llm" else None, "follow_up": follow,
            "outcome": L.OUTCOME_LABEL.get(a["outcome"] or "none", ""), "notes": a["notes"] or "",
        }
        out.append((a["job_uid"], row, _max(a["updated_at"], a["ap_up"], a["j_up"], a["t_up"], a["e_up"])))
    return out


# ---------------------------------------------------------------- outreach
def outreach(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    kinds = L.OUTREACH_KINDS
    exprs = ["a.updated_at", "d.updated_at", "t.updated_at", "c.updated_at"]
    where, args = _since_clause(exprs, since)
    sql = ("SELECT a.*, d.id AS d_id, d.kind AS d_kind, d.subject, d.body, d.payload_json, d.approved_by, "
           "d.updated_at AS d_up, c.full_name, c.first_name, c.title AS c_title, c.linkedin_url, "
           "c.updated_at AS c_up, co.display_name AS company, t.reply_class, t.state AS t_state, "
           "t.followup_due_at, t.followup_action_id, t.first_action_id, t.updated_at AS t_up "
           "FROM actions a LEFT JOIN drafts d ON d.id = a.draft_id "
           "LEFT JOIN contacts c ON c.id = a.contact_id "
           "LEFT JOIN companies co ON co.id = COALESCE(a.company_id, c.company_id) "
           "LEFT JOIN threads t ON t.thread_key = a.thread_key OR (a.thread_key IS NULL AND t.first_action_id = a.id) "
           "WHERE a.kind IN (%s)" % ",".join("?" for _ in kinds) + where + " ORDER BY a.reserved_at")
    out = []
    seen = set()
    for a in conn.execute(sql, list(kinds) + args):
        if a["token"] in seen:
            continue
        seen.add(a["token"])
        payload = _json(a["payload_json"])
        why, _ = _hook_text(payload, ctx)
        status = L.ACTION_STATUS_OUTREACH.get(a["status"], a["status"])
        if a["t_state"] == "bounced" and a["status"] == "sent":
            status = "Bounced"
        reply = L.REPLY_SHORT.get(a["reply_class"] or "", "No reply") if a["t_state"] else ""
        rev = _latest_review(conn, a["d_id"])
        is_first = a["first_action_id"] == a["id"]
        draft_like = {"id": a["d_id"], "kind": a["d_kind"] or "", "subject": a["subject"], "body": a["body"],
                      "payload_json": a["payload_json"]}
        row = {
            "sent_on": a["sent_at"] or a["reserved_at"], "channel": L.ACTION_CHANNEL.get(a["kind"], a["kind"]),
            "person": L.person_name(a["full_name"], a["first_name"], ctx.style), "their_role": a["c_title"] or "",
            "company": a["company"] or "", "profile": _link("Profile", a["linkedin_url"]), "why_them": why,
            "subject": (L.clip(a["subject"], 200) if a["subject"] else L.first_line(a["body"], 80)),
            "message": _send_text(conn, draft_like, ctx) if a["d_id"] else "",
            "qc": _num(rev["weighted_score"]) if rev else None,
            "approved_by": L.APPROVED_BY.get(a["approved_by"] or "", "") if a["d_id"] else
            ("Found in your Sent mail" if a["status"] == "imported" else ""),
            "status": status, "reply": reply,
            "follow_up_due": _local_date(a["followup_due_at"], ctx) if is_first and not a["followup_action_id"]
            else None,
            "notes": "",
        }
        out.append((a["token"], row, _max(a["updated_at"], a["d_up"], a["t_up"], a["c_up"])))
    return out


# ---------------------------------------------------------------- follow-ups
def _gmail_url(thread_id) -> str | None:
    try:
        return "https://mail.google.com/mail/u/0/#all/%x" % int(str(thread_id))
    except (TypeError, ValueError):
        return None


def _next_step(t, ctx: RowCtx) -> str:
    cls = t["reply_class"]
    if t["state"] == "closed":
        return "Closed"
    if t["state"] == "bounced" or cls == "bounce":
        return "The address did not work; closed"
    if cls in ("negative", "not_hiring", "opt_out", "complaint"):
        return "They said no; closed"
    if cls in ("positive", "neutral", "referral_offered") or t["state"] == "replied":
        return "They replied: your move"
    if t["state"] == "invite_pending":
        return "Waiting for them to accept the invite"
    if t["followup_action_id"]:
        return "Follow-up sent; waiting for a reply"
    if t["followup_due_at"]:
        return "Follow-up planned for %s" % S.fmt_day_month(t["followup_due_at"], ctx.tz)
    return "Waiting for a reply"


def followups(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    exprs = ["t.updated_at", "(SELECT max(created_at) FROM replies r WHERE r.thread_id = t.id)", "c.updated_at"]
    where, args = _since_clause(exprs, since)
    sql = ("SELECT t.*, c.full_name, c.first_name, c.updated_at AS c_up, co.display_name AS company, "
           "fa.sent_at AS first_sent, fa.reserved_at AS first_reserved, fu.sent_at AS fu_sent, "
           "(SELECT max(created_at) FROM replies r WHERE r.thread_id = t.id) AS r_up, "
           "(SELECT summary FROM replies r WHERE r.thread_id = t.id ORDER BY received_at DESC, id DESC LIMIT 1) "
           "AS last_summary FROM threads t LEFT JOIN contacts c ON c.id = t.contact_id "
           "LEFT JOIN companies co ON co.id = COALESCE(t.company_id, c.company_id) "
           "LEFT JOIN actions fa ON fa.id = t.first_action_id LEFT JOIN actions fu ON fu.id = t.followup_action_id "
           "WHERE 1 = 1" + where + " ORDER BY t.created_at")
    out = []
    for t in conn.execute(sql, args):
        url = t["platform_ref"] or (_gmail_url(t["gmail_thread_id"]) if t["gmail_thread_id"] else None)
        reply_type = L.REPLY_DETAIL.get(t["reply_class"] or "", "No reply")
        if t["state"] == "invite_pending" and not t["reply_class"]:
            reply_type = "Invite pending"
        row = {
            "started_on": t["first_sent"] or t["first_reserved"] or t["created_at"],
            "channel": "Email" if t["channel"] == "email" else "LinkedIn",
            "person": L.person_name(t["full_name"], t["first_name"], ctx.style), "company": t["company"] or "",
            "follow_up_due": _local_date(t["followup_due_at"], ctx) if not t["followup_action_id"] else None,
            "follow_up_sent": t["fu_sent"], "last_reply": t["reply_at"], "reply_type": reply_type,
            "what_they_said": L.clip(t["reply_summary"] or t["last_summary"] or "", 300),
            "next_step": _next_step(t, ctx), "thread": _link("Thread", url),
            "outcome": L.OUTCOME_LABEL.get(t["outcome"] or "none", ""),
        }
        out.append((t["thread_key"], row, _max(t["updated_at"], t["r_up"], t["c_up"])))
    return out


# ---------------------------------------------------------------- QC log
def _findings(blocks_json, warns_json) -> str:
    """One line per lint finding, "RULE: detail". qc.lint stores [[rule, detail]] pairs; dicts with code/rule
    and message/detail are accepted too."""
    out = []
    for raw, prefix in ((blocks_json, ""), (warns_json, "Warning: ")):
        items = _json(raw) or []
        for f in items if isinstance(items, list) else []:
            if isinstance(f, dict):
                code = f.get("code") or f.get("rule") or ""
                msg = f.get("message") or f.get("detail") or f.get("text") or ""
            elif isinstance(f, (list, tuple)):
                parts = [" ".join(str(x).split()) for x in f if x is not None and str(x).strip()]
                code, msg = (parts[0], ", ".join(parts[1:])) if parts else ("", "")
            else:
                code, msg = str(f or "").strip(), ""
            if code or msg:
                out.append("%s%s%s" % (prefix, code, (": " + str(msg)) if code and msg else msg))
    return L.clip("\n".join(out), 1500)


def _top_issue(review_json) -> str:
    r = _json(review_json)
    if not isinstance(r, dict):
        return ""
    for key in ("top_issue", "main_issue", "summary"):
        if isinstance(r.get(key), str) and r[key]:
            return L.clip(r[key], 500)
    issues = r.get("issues")
    if isinstance(issues, list) and issues:
        first = issues[0]
        if isinstance(first, dict):
            return L.clip(str(first.get("text") or first.get("issue") or first.get("message") or ""), 500)
        return L.clip(str(first), 500)
    return ""


def _gates_text(g) -> str:
    items = _json(g) if isinstance(g, str) else g
    if isinstance(items, list):
        return ", ".join(str(x) for x in items)
    return str(g or "")


def _final(draft, attempt: int, edit_no: int) -> str:
    if edit_no == 0 and attempt < (draft["attempt"] or 1):
        return "Rewritten"
    if edit_no and edit_no < (draft["human_edits"] or 0):
        return "Rewritten"
    st = draft["status"]
    return {"approved": "Approved", "sent": "Sent", "superseded": "Rewritten", "dropped_qc": "Dropped",
            "awaiting_approval": "Awaiting approval", "skipped_by_human": "Skipped", "expired": "Expired",
            "qc_passed": "Passed"}.get(st, "Pending")


def qc(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    if since:
        keys = conn.execute(
            "SELECT DISTINCT q.draft_id, q.attempt, q.human_edit_no FROM qc_results q JOIN drafts d ON d.id = q.draft_id "
            "WHERE q.stage <> 'presend' AND (q.created_at > ? OR d.updated_at > ?)", (since, since)).fetchall()
    else:
        keys = conn.execute("SELECT DISTINCT draft_id, attempt, human_edit_no FROM qc_results "
                            "WHERE stage <> 'presend'").fetchall()
    out = []
    for k in sorted(keys, key=lambda r: (r[0], r[1], r[2])):
        draft_id, attempt, edit_no = k[0], k[1], k[2]
        d = conn.execute(
            "SELECT d.*, c.full_name, c.first_name, c.title, c.role_type, c.email, co.display_name AS company "
            "FROM drafts d LEFT JOIN contacts c ON c.id = d.contact_id LEFT JOIN jobs j ON j.id = d.job_id "
            "LEFT JOIN companies co ON co.id = COALESCE(d.company_id, c.company_id, j.company_id) WHERE d.id = ?",
            (draft_id,)).fetchone()
        if d is None:
            continue
        rows = conn.execute("SELECT * FROM qc_results WHERE draft_id = ? AND attempt = ? AND human_edit_no = ? "
                            "AND stage <> 'presend' ORDER BY id", (draft_id, attempt, edit_no)).fetchall()
        lint = next((r for r in rows if r["stage"] in ("lint", "human_edit_lint")), None)
        rev = next((r for r in reversed(rows) if r["stage"] == "review"), None)
        stamp = _max(d["updated_at"], *[r["created_at"] for r in rows])
        payload = _json(d["payload_json"])
        _, hook_link = _hook_text(payload, ctx)
        text_hash = (rev or lint or rows[0])["text_sha256"] if rows else d["text_sha256"]
        rid = "%s-e%d" % (d["draft_uid"], edit_no) if edit_no else "%s-%d" % (d["draft_uid"], attempt)
        row = {
            "time": max(r["created_at"] for r in rows) if rows else d["created_at"], "item": d["draft_uid"],
            "channel": L.DRAFT_KIND.get(d["kind"], d["kind"]),
            "recipient": S.recipient_label(d, ctx.style) if d["contact_id"] else "Company form",
            "company": d["company"] or "",
            "attempt": ("your edit %d" % edit_no) if edit_no else ("%d of %d" % (attempt, MAX_ATTEMPTS)),
            "lint": ("Passed" if lint["passed"] else "Failed QC") if lint else "Not run",
            "lint_findings": _findings(lint["blocks_json"], lint["warns_json"]) if lint else "",
            "reviewer": ("Passed" if rev["passed"] else "Failed QC") if rev else "Not run",
            "score": _num(rev["weighted_score"]) if rev else None,
            "lowest": (rev["lowest_criterion"] or "") if rev else "",
            "gates_failed": _gates_text(rev["gates_failed"]) if rev else "",
            "top_issue": _top_issue(rev["review_json"]) if rev else "",
            "final": _final(d, attempt, edit_no), "hook": hook_link, "hash": (text_hash or "")[:12],
        }
        out.append((rid, row, stamp))
    return out


# ---------------------------------------------------------------- daily summary
def _first_day(conn, ctx: RowCtx) -> str | None:
    firsts = []
    for sql in ("SELECT min(discovered_at) FROM jobs", "SELECT min(reserved_at) FROM actions",
                "SELECT min(started_at) FROM cycles", "SELECT min(created_at) FROM drafts"):
        v = conn.execute(sql).fetchone()[0]
        if v:
            firsts.append(v)
    return S.local_date(min(firsts), ctx.tz) if firsts else None


def daily(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    now = ctx.now or canon.now()
    today = S.today_local(ctx.tz)
    if since:
        start_day = S.local_date(since, ctx.tz) or today
    else:
        start_day = _first_day(conn, ctx) or today
    d0 = _dt.datetime.strptime(min(start_day, today), "%Y-%m-%d")
    d1 = _dt.datetime.strptime(today, "%Y-%m-%d")
    if (d1 - d0).days > DAILY_MAX_DAYS:
        d0 = d1 - _dt.timedelta(days=DAILY_MAX_DAYS)
    out = []
    cur = d0
    while cur <= d1:
        day = cur.strftime("%Y-%m-%d")
        s, e = S.day_bounds(day, ctx.tz)
        row = {"date": day}
        row.update(S.count_activity(conn, s, e))
        out.append(("D" + day, row, now))
        cur += _dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- alerts
def alerts(conn, since: str | None, ctx: RowCtx) -> list[tuple[str, dict, str]]:
    args: list = []
    where = ""
    if since:
        where = (" AND (be.created_at > ? OR COALESCE(b.updated_at, '') > ? OR EXISTS (SELECT 1 FROM breaker_events x "
                 "WHERE x.scope = be.scope AND x.id > be.id AND x.created_at > ?))")
        args = [since, since, since]
    sql = ("SELECT be.*, b.state AS b_state, b.min_cooldown_until, b.requires_human, b.updated_at AS b_up, "
           "b.tripped_at AS b_tripped, (SELECT min(x.id) FROM breaker_events x WHERE x.scope = be.scope "
           "AND x.id > be.id AND x.event IN ('reset','auto_close')) AS resolved_id, "
           "(SELECT max(x.id) FROM breaker_events x WHERE x.scope = be.scope AND x.event = 'trip') AS last_trip "
           "FROM breaker_events be LEFT JOIN breakers b ON b.scope = be.scope WHERE be.event = 'trip'"
           + where + " ORDER BY be.id")
    out = []
    for r in conn.execute(sql, args):
        resolved_at = None
        if r["resolved_id"]:
            res = conn.execute("SELECT created_at FROM breaker_events WHERE id = ?", (r["resolved_id"],)).fetchone()
            resolved_at = res[0] if res else None
        is_open = not resolved_at and r["b_state"] == "open" and r["last_trip"] == r["id"]
        requires_human = bool(r["requires_human"]) if r["requires_human"] is not None else True
        auto = str(r["scope"]).startswith("api:") or not requires_human
        # open: this event is the breaker's latest trip, so breakers.tripped_at is this trip's time
        until = L.cooldown_until(r["min_cooldown_until"], r["b_tripped"] or r["created_at"], not auto) \
            if is_open else None
        row = {
            "time": r["created_at"], "area": L.scope_label(r["scope"]),
            "what": L.reason_sentence(r["reason_code"], r["detail"]),
            "severity": "Warning" if auto else "Stopped",
            "todo": (L.todo_sentence(r["scope"], r["reason_code"], not auto, waiting=bool(until)) if is_open
                     else "Nothing, this is resolved."),
            "until": until,
            "resolved": resolved_at if resolved_at else (None if is_open else (r["b_up"] if r["b_state"] == "closed"
                                                                               else None)),
            "status": "Open" if is_open else "Resolved",
        }
        out.append(("A%d" % r["id"], row, _max(r["created_at"], r["b_up"], resolved_at)))
    return out


BUILDERS = {"approvals": approvals, "jobs": jobs, "skipped": skipped, "applications": applications,
            "outreach": outreach, "followups": followups, "qc": qc, "daily": daily, "alerts": alerts}


def build_rows_stamped(conn, tab: str, since: str | None, ctx: RowCtx | None = None) -> list[tuple[str, dict, str]]:
    if tab not in BUILDERS:
        raise Denied("E_USAGE", "unknown sheet tab %r" % tab, data={"tabs": list(BUILDERS)})
    return BUILDERS[tab](conn, since, ctx or RowCtx.load())


def build_rows(conn, tab: str, since: str | None) -> list[tuple[str, dict]]:
    """12.10: [(row_id, row_dict)] for one tab, rows changed after the watermark `since` (None: all)."""
    return [(rid, row) for rid, row, _stamp in build_rows_stamped(conn, tab, since)]
