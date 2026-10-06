"""Breakers, the kill switch and pause areas (design 4.5).

- trip(): one transaction writes the breakers row (open, reason, detail, evidence, min_cooldown_until,
  requires_human, resume_policy), a breaker_events row and a high-priority notification that ends with the
  owner's one thing to do (todo_sentence: for an expired session the login or re-import command, the check
  and the reset). Open breakers
  survive restarts; only reset() (human, never before min_cooldown_until) closes a requires_human breaker;
  api:<source> breakers close themselves at auto_close_at.
- check_breakers(): state/PAUSED first (E_PAUSED), then every scope, its parents, 'global' and the
  matching pause:<area> breaker (E_BREAKER_OPEN).
- Resume policies are applied by reset(): warm-up restarts and temporary cap clamps (meta 'clamp:<scope>').
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re

from . import paths
from .canon import fmt_ts, now, parse_ts, ts_add, utcnow
from .errors import Denied
from .events import enqueue_notification, log_event

H = 3600
D = 86400
# reason_code -> policy. scope None means "the scope passed to trip()". cooldown: seconds, or one of
# 'next_active_day', 'next_month_pst'. clamp: (clamp scope, factor, days) applied at reset.
POLICIES: dict = {
    "li_challenge": {"scope": "linkedin", "cooldown": 72 * H, "resume": "warmup_week1"},
    "li_restricted": {"scope": "linkedin", "cooldown": 7 * D, "resume": "manual_7d_then_warmup_week1",
                      "clamp": ("linkedin", 0.0, 7)},
    "li_logged_out": {"scope": "linkedin", "cooldown": 0, "resume": "next_day_50pct", "clamp": ("linkedin", 0.5, 1)},
    "li_invite_limit": {"scope": "linkedin.invites", "cooldown": 7 * D, "resume": "half_invites_2w",
                        "clamp": ("linkedin.invites", 0.5, 14)},
    "li_email_needed": {"scope": "linkedin.invites", "cooldown": 30 * D, "resume": "notify_targeting"},
    "li_commercial_limit": {"scope": "linkedin.search", "cooldown": "next_month_pst", "resume": None},
    "li_easy_apply_limit": {"scope": "linkedin.easy_apply", "cooldown": 48 * H, "resume": "half_easy_apply_7d",
                            "clamp": ("linkedin.easy_apply", 0.5, 7)},
    "li_messaging_blocked": {"scope": "linkedin.messages", "cooldown": 72 * H, "resume": None},
    "li_http_429": {"scope": "linkedin", "cooldown": "next_active_day", "resume": "next_day_50pct",
                    "clamp": ("linkedin", 0.5, 1)},
    "li_consecutive_failures": {"scope": "linkedin", "cooldown": "next_active_day", "resume": "next_day_50pct",
                                "clamp": ("linkedin", 0.5, 1)},
    "li_daily_failures": {"scope": "linkedin", "cooldown": "next_active_day", "resume": None},
    "li_unknown_modal": {"scope": "linkedin", "cooldown": "next_active_day", "resume": None},
    "li_security_email": {"scope": "linkedin", "cooldown": 0, "resume": "warmup_week1"},
    "li_low_acceptance": {"scope": "linkedin.invites", "cooldown": 14 * D, "resume": None},
    "li_identity_mismatch": {"scope": "linkedin", "cooldown": 0, "resume": None},
    "gmail_sending_limit": {"scope": "gmail", "cooldown": 24 * H, "resume": "half_cap", "clamp": ("gmail", 0.5, 7)},
    "gmail_auth_failed": {"scope": "gmail", "cooldown": 0, "resume": "warmup_week_down"},
    "gmail_security": {"scope": "gmail", "cooldown": 0, "resume": "warmup_week_down"},
    # the agent's browser lost its Gmail session (a sign-in page, no verification): log in or copy the login
    # again, check it, reset; nothing about the account's standing changed, so no warm-up step down
    "gmail_logged_out": {"scope": "gmail", "cooldown": 0, "resume": None},
    "gmail_identity_mismatch": {"scope": "gmail", "cooldown": 0, "resume": None},
    "gmail_unexpected_state": {"scope": "gmail", "cooldown": 0, "resume": None},
    "gmail_bounces_24h": {"scope": "gmail.cold", "cooldown": 24 * H, "resume": None},
    "bounce_rate_pause": {"scope": "gmail.cold", "cooldown": 0, "resume": None},
    "bounce_rate_stop": {"scope": "gmail.cold", "cooldown": 0, "resume": None},
    "complaints_30d": {"scope": "gmail.cold", "cooldown": 0, "resume": None},
    "site_challenge": {"scope": None, "cooldown": 24 * H, "resume": None},
    # a job board asks the agent's browser to log in again (session expired): no waiting time
    "site_logged_out": {"scope": None, "cooldown": 0, "resume": None},
    "ats_blocked": {"scope": "ats", "cooldown": 24 * H, "resume": None},
    # email codes, site accounts and the CAPTCHA hand-off (scope ats:<platform>, FEATURES-OTP-ACCOUNTS-CAPTCHA 1.4)
    "otp_failures": {"scope": None, "cooldown": 24 * H, "resume": None},
    "account_failures": {"scope": None, "cooldown": 24 * H, "resume": None},
    "captcha_repeat": {"scope": None, "cooldown": 24 * H, "resume": None},
    # an SMS, authenticator, social sign-in or identity check on an ATS page: the owner looks first
    "ats_security": {"scope": None, "cooldown": 0, "resume": None},
    "clock_skew": {"scope": "global", "cooldown": 0, "resume": None},
    "audit_mismatch": {"scope": "global", "cooldown": 0, "resume": None},
    "dup_denials": {"scope": "global", "cooldown": 0, "resume": None},
    "paused": {"scope": None, "cooldown": 0, "resume": None},
    # the owner revoked a site's browser consent (identity.revoke_consent): open until consent is granted again
    "consent_revoked": {"scope": None, "cooldown": 0, "resume": None},
    # the email finder (U10, scope enrich or enrich:<provider>): the owner fixes the cause, then resets at once
    "auth_failed": {"scope": None, "cooldown": 0, "resume": None},
    "tls": {"scope": None, "cooldown": 0, "resume": None},
    "schema_changed": {"scope": None, "cooldown": 0, "resume": None},
    "unexpected_phone": {"scope": None, "cooldown": 0, "resume": None},
    "bounce_strikes": {"scope": None, "cooldown": 0, "resume": None},
    "rate_limited": {"scope": None, "cooldown": 0, "resume": None},
    "provider_errors": {"scope": None, "cooldown": 0, "resume": None},
}
DEFAULT_POLICY = {"scope": None, "cooldown": 24 * H, "resume": None}
SCOPE_RE = re.compile(r"^(global|gmail|gmail\.cold|linkedin|linkedin\.(invites|messages|easy_apply|search)|ats|"
                      r"ats:[a-z0-9_]{2,40}|"
                      r"enrich|enrich:[a-z0-9_]{2,40}|"
                      r"site:[a-z0-9_.-]{2,40}|api:[a-z0-9_.:-]{2,60}|pause:[a-z0-9_.:-]{2,60})$")
AREA_LABEL = {"global": "Everything", "gmail": "Gmail", "gmail.cold": "Cold email", "linkedin": "LinkedIn",
              "linkedin.invites": "LinkedIn invites", "linkedin.messages": "LinkedIn messages",
              "linkedin.easy_apply": "LinkedIn Easy Apply", "linkedin.search": "LinkedIn search", "ats": "Job forms",
              "enrich": "Email finder"}
# enrich: the optional email finder (U10); it checks pause:enrich and the enrich / enrich:<provider> breakers
PAUSE_AREAS = ("all", "linkedin", "gmail", "applications", "enrich")
# Resume policies from mildest to strictest. A second trip on an open breaker keeps the stricter one on the
# row, and reset applies the policies of every trip since the breaker opened (4.5).
RESUME_RANK = {None: 0, "notify_targeting": 1, "half_cap": 2, "half_easy_apply_7d": 2, "half_invites_2w": 2,
               "next_day_50pct": 3, "warmup_week_down": 4, "warmup_week1": 5, "manual_7d_then_warmup_week1": 6}
# Board sites whose pause:site:<name> breaker the lanes check (searches.site_scope, pacing, ceilings, gate).
# linkedin_jobs and linkedin_posts are stopped by `pause linkedin`, the ATS forms by `pause applications`.
_NOT_SITE_PAUSES = ("ats_forms", "linkedin_jobs", "linkedin_posts", "linkedin")


def valid_scope(scope: str) -> bool:
    return bool(SCOPE_RE.match(scope or ""))


def area_label(scope: str) -> str:
    """The owner-facing name of a breaker area: the same words as the Sheet (sheets_labels.scope_label)."""
    from . import sheets_labels
    return sheets_labels.scope_label(scope) or scope


def _resume_rank(policy) -> int:
    return RESUME_RANK.get(policy, 1)


def pausable_sites(cfg: dict | None = None) -> list[str]:
    """Site names `pause site:<name>` accepts: the board ids the lanes check."""
    from .keys import BOARD_SITES
    names = set(BOARD_SITES)
    try:
        from . import config
        names |= set((cfg or config.load())["boards"]["sites"])
    except Exception:
        pass
    return sorted(n for n in names if n not in _NOT_SITE_PAUSES)


# ---------------------------------------------------------------- kill switch
def is_paused() -> bool:
    return os.path.exists(paths.paused_file())


def paused_info() -> dict | None:
    if not is_paused():
        return None
    try:
        with open(paths.paused_file(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"reason": "state/PAUSED exists"}


# ---------------------------------------------------------------- scopes
def platform_scope(platform: str) -> str:
    """Breaker scope of a platform id: gmail, linkedin, ats (ATS form hosts), site:<name> (boards)."""
    from .keys import ATS_NAMES
    p = (platform or "").lower()
    if p in ("gmail", "linkedin", "ats", "global"):
        return p
    if p.startswith("api:"):
        return p
    name = p[5:] if p.startswith("site:") else p
    if name in ATS_NAMES or name in ("ats", "ats_forms"):
        return "ats"                        # site:greenhouse is the greenhouse ATS, not a board
    return "site:" + name


def ats_platform_key(platform: str) -> str | None:
    """The canonical ATS platform key of a platform id (workday, site:workday), or None for the ats family."""
    from .keys import ATS_NAMES
    p = (platform or "").lower()
    p = p[5:] if p.startswith("site:") else p
    return p if p in ATS_NAMES else None


def parents(scope: str) -> list[str]:
    out = [scope]
    if scope.startswith("enrich:"):
        out.append("enrich")        # a provider of the email finder is stopped with the whole finder
    elif scope.startswith("ats:"):
        out.append("ats")           # one ATS platform is stopped with every job form
    elif "." in scope and not scope.startswith(("site:", "api:", "pause:")):
        out.append(scope.split(".", 1)[0])
    return out


def scopes_for(platform: str, kind: str) -> list[str]:
    """Scopes that must be closed before a write of `kind` on `platform` (plus 'global')."""
    ps = platform_scope(platform)
    out = ["global", ps]
    if ps == "gmail":
        out.append("pause:gmail")
        if kind == "cold_email":
            out.append("gmail.cold")
    elif ps == "linkedin":
        out.append("pause:linkedin")
        if kind in ("li_invite", "li_withdraw"):
            out.append("linkedin.invites")
        elif kind in ("li_message", "li_followup", "inmail", "referral_ask"):
            out.append("linkedin.messages")
        elif kind == "application":
            out.append("linkedin.easy_apply")
        elif kind in ("people_search", "content_search"):
            out.append("linkedin.search")
    else:
        out.append("pause:" + ps)
        if ps == "ats":
            plat = ats_platform_key(platform)
            if plat:
                out.append("ats:" + plat)
    if kind in ("application", "application_email"):
        out.append("pause:applications")
    return out


def _open_rows(conn, scopes: list[str]) -> list:
    wanted = []
    for s in scopes:
        for p in parents(s):
            if p not in wanted:
                wanted.append(p)
            if not p.startswith("pause:") and p != "global":
                root = "enrich" if p.startswith("enrich") else ("ats" if p.startswith("ats:") else (
                    p.split(".", 1)[0] if not p.startswith(("site:", "api:")) else p))
                if "pause:" + root not in wanted:
                    wanted.append("pause:" + root)
    if "global" not in wanted:
        wanted.append("global")
    rows = conn.execute("SELECT * FROM breakers WHERE state = 'open' AND scope IN (%s)" % ",".join("?" * len(wanted)),
                        wanted).fetchall()
    ts = now()
    return [r for r in rows if not (not r["requires_human"] and r["auto_close_at"] and r["auto_close_at"] <= ts)]


def check_breakers(conn, scopes: list[str]) -> None:
    """Denied(E_PAUSED) when state/PAUSED exists, Denied(E_BREAKER_OPEN) when any scope (or a parent,
    'global' or its pause area) is open."""
    if is_paused():
        raise Denied("E_PAUSED", "the job hunter is paused (state/PAUSED)", data=paused_info() or {})
    rows = _open_rows(conn, scopes)
    if rows:
        r = rows[0]
        retry = None
        if r["min_cooldown_until"]:
            retry = max(0, int((parse_ts(r["min_cooldown_until"]) - utcnow()).total_seconds())) or None
        raise Denied("E_BREAKER_OPEN", "%s is stopped: %s" % (area_label(r["scope"]), r["reason_code"] or "breaker"),
                     retry_after=retry, data={"scope": r["scope"], "reason_code": r["reason_code"],
                                              "open": [x["scope"] for x in rows]})


def open_scopes(conn) -> list[str]:
    ts = now()
    return [r["scope"] for r in conn.execute("SELECT scope, requires_human, auto_close_at FROM breakers "
                                             "WHERE state = 'open' ORDER BY scope")
            if not (not r["requires_human"] and r["auto_close_at"] and r["auto_close_at"] <= ts)]


# ---------------------------------------------------------------- cooldown arithmetic
def _next_month_pst(ts: str) -> str:
    from .config import tzinfo
    la = tzinfo("America/Los_Angeles")
    d = parse_ts(ts).astimezone(la)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    first = _dt.datetime(y, m, 1, 0, 0, 0, tzinfo=la)
    return fmt_ts(first.astimezone(_dt.timezone.utc))


def _next_active_day(ts: str) -> str:
    from . import config
    try:
        cfg = config.load()
        days = cfg["linkedin"]["active"]["days"]
        tz = config.tzinfo(cfg)
    except Exception:
        days, tz = [1, 2, 3, 4, 5], _dt.timezone.utc
    d = parse_ts(ts).astimezone(tz)
    for i in range(1, 9):
        cand = (d + _dt.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        if cand.isoweekday() in days:
            return fmt_ts(cand.astimezone(_dt.timezone.utc))
    return ts_add(ts, days=1)


def cooldown_until(policy: dict, ts: str) -> str:
    c = policy.get("cooldown", 0)
    if c == "next_month_pst":
        return _next_month_pst(ts)
    if c == "next_active_day":
        return _next_active_day(ts)
    return ts_add(ts, seconds=int(c or 0))


# ---------------------------------------------------------------- trip / reset
def _evidence(scope: str, detail: str) -> str | None:
    try:
        edir = os.path.join(paths.logs_dir(), "evidence")
        os.makedirs(edir, exist_ok=True)
        path = os.path.join(edir, "%s-%s.txt" % (now().replace(":", "").replace("-", ""),
                                                 re.sub(r"[^a-z0-9_.-]", "_", scope)))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(detail[:20000])
        return path
    except OSError:
        return None


def trip(conn, scope: str, reason_code: str, detail: str, evidence_path: str | None = None, by: str = "system",
         cycle_id: str | None = None, requires_human: bool | None = None, auto_close_at: str | None = None) -> dict:
    """Open (or re-open) a breaker. Runs inside the caller's transaction. api:* scopes close by themselves
    after a doubling back-off; `requires_human=False` with `auto_close_at` gives any scope an auto-close time
    (the email finder's quota and back-off breakers, U10), which close_expired() then closes."""
    if not valid_scope(scope):
        raise Denied("E_VALIDATION", "unknown breaker scope %r" % scope)
    if not re.match(r"^[a-z0-9_]{2,40}$", reason_code or ""):
        raise Denied("E_VALIDATION", "bad reason code %r" % reason_code)
    policy = POLICIES.get(reason_code, DEFAULT_POLICY)
    ts = now()
    detail = (detail or reason_code)[:2000]
    auto_close = None
    if requires_human is False and auto_close_at:
        requires_human = 0
        auto_close = until = auto_close_at
    elif scope.startswith("api:"):
        requires_human = 0
        n = conn.execute("SELECT count(*) FROM breaker_events WHERE scope = ? AND event = 'trip' AND created_at > ?",
                         (scope, ts_add(ts, days=-1))).fetchone()[0]
        auto_close = ts_add(ts, seconds=min(24 * H, H * (2 ** n)))
        until = auto_close
    else:
        requires_human = 1
        until = cooldown_until(policy, ts)
    if evidence_path is None and detail:
        evidence_path = _evidence(scope, detail)
    row = conn.execute("SELECT * FROM breakers WHERE scope = ?", (scope,)).fetchone()
    if row is not None and row["state"] == "open" and row["requires_human"] and not requires_human:
        # an auto-close trip never turns a stop that waits for the owner into one that ends by itself
        requires_human, auto_close = 1, None
    if row is not None and row["state"] == "open" and row["min_cooldown_until"] and row["min_cooldown_until"] > until:
        until = row["min_cooldown_until"]
    row_code, row_policy, row_detail = reason_code, policy.get("resume"), detail
    if row is not None and row["state"] == "open" and _resume_rank(row["resume_policy"]) > _resume_rank(row_policy):
        # a milder second trip never replaces the stricter resume policy of the open breaker
        row_code, row_policy, row_detail = row["reason_code"], row["resume_policy"], row["detail"]
    conn.execute(
        "INSERT INTO breakers (scope, state, reason_code, detail, evidence_path, tripped_at, min_cooldown_until, "
        "requires_human, auto_close_at, resume_policy, tripped_by_cycle, reset_at, reset_by, reset_note, updated_at) "
        "VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?) ON CONFLICT (scope) DO UPDATE SET "
        "state = 'open', reason_code = excluded.reason_code, detail = excluded.detail, "
        "evidence_path = excluded.evidence_path, tripped_at = excluded.tripped_at, "
        "min_cooldown_until = excluded.min_cooldown_until, requires_human = excluded.requires_human, "
        "auto_close_at = excluded.auto_close_at, resume_policy = excluded.resume_policy, "
        "tripped_by_cycle = excluded.tripped_by_cycle, reset_at = NULL, reset_by = NULL, reset_note = NULL, "
        "updated_at = excluded.updated_at",
        (scope, row_code, row_detail, evidence_path, ts, until, requires_human, auto_close, row_policy,
         cycle_id, ts))
    conn.execute("INSERT INTO breaker_events (scope, event, reason_code, detail, by, created_at) "
                 "VALUES (?, 'trip', ?, ?, ?, ?)", (scope, reason_code, detail, by, ts))
    if requires_human and not scope.startswith("pause:"):
        enqueue_notification(conn, "breaker:%s:%s" % (scope, ts), "high", "alert",
                             "Stopped %s: %s. Nothing goes out there until you reset it%s. %s"
                             % (area_label(scope), alert_sentence(reason_code, detail),
                                "" if until <= ts else " (not before %s UTC)" % until.replace("T", " ")[:16],
                                todo_sentence(scope, row_code, until > ts)))
    if cycle_id:
        conn.execute("UPDATE cycles SET status = 'aborted_breaker' WHERE cycle_id = ? AND status = 'running'",
                     (cycle_id,))
    log_event(conn, "breaker_trip", scope=scope, reason_code=reason_code, by=by, min_cooldown_until=until)
    return {"scope": scope, "reason_code": reason_code, "min_cooldown_until": until, "requires_human": bool(requires_human),
            "auto_close_at": auto_close}


_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)


def alert_sentence(reason_code: str, detail: str | None) -> str:
    """The owner's words for a stop (7.3): the reason as a sentence, plus the detail when it holds no URL and
    no internal code. The full detail stays in the evidence file and the breakers row."""
    from . import sheets_labels
    d = " ".join(_URL_RE.sub("", detail or "").split()).strip(" .:;,")
    if not d or re.search(r"\b[a-z]+_[a-z0-9_]+\b", d) or d == reason_code:
        d = None
    return sheets_labels.reason_sentence(reason_code, d).rstrip(".")


def todo_sentence(scope: str, reason_code: str | None, waiting: bool) -> str:
    """The one thing the owner does about a stop (the Sheet's 'What you need to do' words, 7.3): for a logged-out
    or expired session the command that logs in or copies the login again, then the check and the reset."""
    from . import sheets_labels
    try:
        return sheets_labels.todo_sentence(scope, reason_code, True, waiting=waiting)
    except Exception:
        return "Then run ./jobhunter breaker reset %s." % scope


def _trip_reasons_since_open(conn, scope: str, row) -> list[str]:
    """Reason codes of every trip on the scope since it was last closed, oldest first (the row's own last)."""
    last = conn.execute("SELECT max(created_at) FROM breaker_events WHERE scope = ? AND event IN ('reset','auto_close')",
                        (scope,)).fetchone()[0]
    q = "SELECT reason_code FROM breaker_events WHERE scope = ? AND event = 'trip'"
    args: list = [scope]
    if last:
        q += " AND created_at >= ?"
        args.append(last)
    out = []
    for (rc,) in conn.execute(q + " ORDER BY id", args):
        if rc and rc not in out:
            out.append(rc)
    if row["reason_code"] and row["reason_code"] not in out:
        out.append(row["reason_code"])
    return out


def reset(conn, scope: str, note: str, by: str) -> dict:
    """Close a breaker (human). Refuses before min_cooldown_until; applies the resume policy."""
    row = conn.execute("SELECT * FROM breakers WHERE scope = ?", (scope,)).fetchone()
    if row is None or row["state"] != "open":
        raise Denied("E_PRECONDITION", "breaker %s is not open" % scope)
    ts = now()
    if row["min_cooldown_until"] and row["min_cooldown_until"] > ts:
        retry = int((parse_ts(row["min_cooldown_until"]) - utcnow()).total_seconds())
        raise Denied("E_PRECONDITION", "breaker %s cannot be reset before %s" % (scope, row["min_cooldown_until"]),
                     retry_after=retry, data={"min_cooldown_until": row["min_cooldown_until"]})
    reasons = _trip_reasons_since_open(conn, scope, row)
    _close(conn, scope, "reset", by, note)
    applied: list[str] = []
    # every trip's policy, mildest first, so warm-up restarts and clamps end at the strictest of them
    for rc in sorted(reasons, key=lambda c: _resume_rank(POLICIES.get(c, DEFAULT_POLICY).get("resume"))):
        pol = row["resume_policy"] if rc == row["reason_code"] else POLICIES.get(rc, DEFAULT_POLICY).get("resume")
        for a in apply_resume_policy(conn, row["scope"], rc, pol):
            if a not in applied:
                applied.append(a)
    return {"scope": scope, "state": "closed", "resume_policy": row["resume_policy"], "applied": applied,
            "reasons": reasons}


def _close(conn, scope: str, event: str, by: str, note: str | None) -> None:
    ts = now()
    conn.execute("UPDATE breakers SET state = 'closed', reset_at = ?, reset_by = ?, reset_note = ?, updated_at = ? "
                 "WHERE scope = ?", (ts, by, (note or "")[:500], ts, scope))
    conn.execute("INSERT INTO breaker_events (scope, event, reason_code, detail, by, created_at) "
                 "VALUES (?, ?, NULL, ?, ?, ?)", (scope, event, (note or "")[:500], by, ts))
    log_event(conn, "breaker_" + event, scope=scope, by=by)


def close_expired(conn) -> list[str]:
    """Close api:* breakers whose auto_close_at passed (housekeeping, dispatcher, sources)."""
    ts = now()
    out = []
    for r in conn.execute("SELECT scope FROM breakers WHERE state = 'open' AND requires_human = 0 "
                          "AND auto_close_at IS NOT NULL AND auto_close_at <= ?", (ts,)).fetchall():
        _close(conn, r["scope"], "auto_close", "system", "auto close")
        out.append(r["scope"])
    return out


def apply_resume_policy(conn, scope: str, reason_code: str | None, policy: str | None) -> list[str]:
    applied = []
    pol = POLICIES.get(reason_code or "", {})
    root = scope.split(".", 1)[0]
    if policy == "warmup_week1":
        restart_warmup(conn, root, 1, reason_code or policy)
        applied.append("warmup_week1")
    elif policy == "manual_7d_then_warmup_week1":
        # 7 days manual only (the 0.0 clamp), then warm-up week 1: week 1 starts when the clamp lifts
        days = (pol.get("clamp") or (None, None, 7))[2]
        restart_warmup(conn, root, 1, reason_code or policy, at=ts_add(now(), days=days))
        applied.append("warmup_week1")
    elif policy == "warmup_week_down":
        from . import ceilings
        week = ceilings.warmup_week(conn, root)
        restart_warmup(conn, root, max(1, week - 1), reason_code or policy)
        applied.append("warmup_week_%d" % max(1, week - 1))
    clamp = pol.get("clamp")
    if clamp:
        cscope, factor, days = clamp
        set_clamp(conn, cscope, factor, ts_add(now(), days=days), reason_code)
        applied.append("clamp:%s=%s for %d days" % (cscope, factor, days))
    if policy == "notify_targeting":
        enqueue_notification(conn, "targeting:%s" % now()[:10], "normal", "info",
                             "LinkedIn asked for email addresses: your invites reach people who do not know you. "
                             "Target people closer to your field before inviting again.")
        applied.append("notified")
    return applied


def set_clamp(conn, scope: str, factor: float, until: str, reason: str | None) -> dict:
    """Add a temporary cap clamp to meta 'clamp:<scope>' without weakening an active one. The stored value is
    the strictest active clamp ({factor, until, reason}) plus, under "then", weaker clamps that outlast it,
    so a short full stop followed by a longer half cap are both kept (ceilings.clamp reads them in order)."""
    from . import db
    ts = now()
    entries = [{"factor": float(factor), "until": until, "reason": reason}]
    row = conn.execute("SELECT value FROM meta WHERE key = ?", ("clamp:" + scope,)).fetchone()
    if row and row[0]:
        try:
            old = json.loads(row[0])
            for e in [old] + list(old.get("then") or []):
                if isinstance(e, dict) and e.get("until") and e["until"] > ts:
                    entries.append({"factor": float(e.get("factor", 1.0)), "until": e["until"],
                                    "reason": e.get("reason")})
        except (ValueError, TypeError, AttributeError):
            entries.append({"factor": 0.0, "until": until, "reason": "unreadable clamp"})
    entries.sort(key=lambda e: e["until"], reverse=True)
    entries.sort(key=lambda e: e["factor"])          # stable: for one factor the latest end comes first
    stair: list[dict] = []
    for e in entries:                                # keep a clamp only when it outlasts every stricter one
        if not stair or e["until"] > stair[-1]["until"]:
            stair.append(e)
    value = dict(stair[0])
    if len(stair) > 1:
        value["then"] = stair[1:]
    db.meta_set(conn, "clamp:" + scope, json.dumps(value, sort_keys=True), "system")
    return value


def restart_warmup(conn, platform: str, week: int, reason: str, at: str | None = None) -> None:
    """Restart warm-up at `week` (from `at`, default now). A restart never raises the current week."""
    from . import ceilings
    ts = at or now()
    cur = ceilings.warmup_week(conn, platform)
    week = min(int(week), cur)
    lane0 = "cold" if platform == "gmail" else "all"
    pending = conn.execute("SELECT restarted_at FROM warmup WHERE platform = ? AND lane_kind = ?",
                           (platform, lane0)).fetchone()
    if pending is not None and pending[0] and pending[0] > ts:
        ts = pending[0]                  # a restart that starts later (after a manual-only week) is kept
    lane = "cold" if platform == "gmail" else "all"
    row = conn.execute("SELECT 1 FROM warmup WHERE platform = ? AND lane_kind = ?", (platform, lane)).fetchone()
    if row:
        conn.execute("UPDATE warmup SET restarted_at = ?, restart_reason = ?, restart_week = ? "
                     "WHERE platform = ? AND lane_kind = ?", (ts, reason, week, platform, lane))
    else:
        conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restarted_at, restart_reason, restart_week) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (platform, lane, ts, ts, reason, week))
    log_event(conn, "warmup_restart", platform=platform, week=week, reason=reason)


# ---------------------------------------------------------------- pause
def pause(conn, scope: str = "all", reason: str | None = None, by: str = "system") -> dict:
    """all: write state/PAUSED; another area opens the pause:<area> breaker."""
    scope = scope or "all"
    if scope != "all" and scope not in PAUSE_AREAS and not re.match(r"^site:[a-z0-9_.-]{2,40}$", scope):
        raise Denied("E_VALIDATION", "pause scope must be all, linkedin, gmail, applications, enrich or site:<name>")
    if scope.startswith("site:") and scope[5:] not in pausable_sites():
        raise Denied("E_VALIDATION", "no job site called %r; pause one of: %s (LinkedIn: pause linkedin; company "
                     "job forms: pause applications)" % (scope[5:], ", ".join("site:" + n for n in pausable_sites())))
    if scope == "all":
        os.makedirs(paths.state_dir(), exist_ok=True)
        with open(paths.paused_file(), "w", encoding="utf-8") as fh:
            json.dump({"at": now(), "by": by, "reason": (reason or "")[:300]}, fh)
        log_event(conn, "paused", scope="all", by=by)
        enqueue_notification(conn, "paused:%s" % now(), "normal", "info", "Job hunter paused (%s)." % (reason or by))
        return {"scope": "all", "paused": True, "file": paths.paused_file()}
    res = trip(conn, "pause:" + scope, "paused", reason or ("paused by " + by), by=by)
    return {"scope": scope, "paused": True, "breaker": res["scope"]}


def unpause(conn, scope: str = "all", by: str = "human") -> dict:
    scope = scope or "all"
    if scope == "all":
        existed = is_paused()
        since = (paused_info() or {}).get("at") if existed else None
        try:
            os.unlink(paths.paused_file())
        except FileNotFoundError:
            pass
        log_event(conn, "unpaused", scope="all", by=by)
        out = {"scope": "all", "paused": False, "was_paused": existed}
        if _long_pause(since):
            out["applied"] = _after_long_pause(conn)
        return out
    row = conn.execute("SELECT state, tripped_at FROM breakers WHERE scope = ?", ("pause:" + scope,)).fetchone()
    if row is None or row["state"] != "open":
        raise Denied("E_PRECONDITION", "%s is not paused" % scope)
    _close(conn, "pause:" + scope, "reset", by, "unpause")
    out = {"scope": scope, "paused": False}
    if scope == "gmail" and _long_pause(row["tripped_at"]):
        out["applied"] = _after_long_pause(conn)
    return out


LONG_PAUSE_DAYS = 7


def _long_pause(since: str | None) -> bool:
    try:
        return bool(since) and (utcnow() - parse_ts(since)).days >= LONG_PAUSE_DAYS
    except (ValueError, TypeError):
        return False


def _after_long_pause(conn) -> list[str]:
    """4.4: a pause of 7 days or more restarts Gmail cold warm-up at week 2 (never higher than it was)."""
    from . import ceilings
    if conn.execute("SELECT 1 FROM warmup WHERE platform = 'gmail' AND lane_kind = 'cold'").fetchone() is None:
        return []
    if ceilings.warmup_week(conn, "gmail") <= 2:
        return []
    restart_warmup(conn, "gmail", 2, "long_pause")
    return ["warmup_week_2"]


def status(conn) -> list[dict]:
    out = []
    ts = now()
    for r in conn.execute("SELECT * FROM breakers ORDER BY state DESC, scope"):
        d = dict(r)
        d["effective_state"] = "closed" if (r["state"] == "open" and not r["requires_human"] and r["auto_close_at"]
                                            and r["auto_close_at"] <= ts) else r["state"]
        d["area"] = area_label(r["scope"])
        out.append(d)
    return out



def _is_open(conn, scope: str) -> bool:
    row = conn.execute("SELECT state FROM breakers WHERE scope = ?", (scope,)).fetchone()
    return row is not None and row[0] == "open"


def email_health(conn, cfg: dict | None = None) -> dict:
    """Gmail stop rules from ledger data (4.4, 4.5): hard bounces in 24 h, the rolling bounce rate over the
    last sends, complaints in 30 days, the one-complaint cut of the cold cap, and the reply-rate floor that
    stops warm-up advancement. Runs inside the caller's transaction (housekeeping, the mailer)."""
    from . import config as _config
    from . import db
    cfg = cfg or _config.load(conn)
    g = cfg["gmail"]
    out = {"tripped": [], "clamped": [], "notified": []}
    day = ts_add(now(), days=-1)
    bounces_24h = conn.execute("SELECT count(*) FROM threads WHERE channel = 'email' AND reply_class = 'bounce' "
                               "AND COALESCE(reply_at, updated_at) > ?", (day,)).fetchone()[0]
    if bounces_24h >= int(g["bounce_stop"]["per_24h"]) and not _is_open(conn, "gmail.cold"):
        trip(conn, "gmail.cold", "gmail_bounces_24h", "%d hard bounces in 24 hours" % bounces_24h, by="email_health")
        out["tripped"].append("gmail_bounces_24h")
    n = int(g["bounce_stop"]["rolling_window_sends"])
    rows = conn.execute("SELECT a.id, t.reply_class FROM actions a LEFT JOIN threads t ON t.first_action_id = a.id "
                        "WHERE a.platform = 'gmail' AND a.status = 'sent' AND a.kind IN ('cold_email','application_email') "
                        "ORDER BY a.sent_at DESC LIMIT ?", (n,)).fetchall()
    if len(rows) >= 20:
        rate = sum(1 for r in rows if r["reply_class"] == "bounce") / float(len(rows))
        for code, lim in (("bounce_rate_stop", g["bounce_stop"]["rolling_rate_stop"]),
                          ("bounce_rate_pause", g["bounce_stop"]["rolling_rate_pause"])):
            if rate >= float(lim):
                if not _is_open(conn, "gmail.cold"):
                    trip(conn, "gmail.cold", code, "hard-bounce rate %.1f%% over the last %d sends"
                         % (rate * 100, len(rows)), by="email_health")
                    out["tripped"].append(code)
                break
    month = ts_add(now(), days=-30)
    complaints = conn.execute("SELECT count(*), max(created_at) FROM exclusions WHERE source = 'complaint' AND "
                              "created_at > ?", (month,)).fetchone()
    if complaints[0] >= int(g["complaint_stop"]["per_30d"]) and not _is_open(conn, "gmail.cold"):
        trip(conn, "gmail.cold", "complaints_30d", "%d complaints in 30 days" % complaints[0], by="email_health")
        out["tripped"].append("complaints_30d")
    elif complaints[0]:
        until = ts_add(complaints[1], days=int(g["complaint_stop"]["cut_days"]))
        if until > now():
            factor = 1.0 - float(g["complaint_stop"]["first_complaint_cut_pct"]) / 100.0
            set_clamp(conn, "gmail.cold", factor, until, "complaint")
            out["clamped"].append("gmail.cold")
    rf = g["reply_rate_floor"]
    cold = conn.execute("SELECT count(*) FROM actions WHERE kind = 'cold_email' AND status = 'sent'").fetchone()[0]
    if cold >= int(rf["after_sends"]):
        replied = conn.execute("SELECT count(*) FROM threads t JOIN actions a ON a.id = t.first_action_id WHERE "
                               "a.kind = 'cold_email' AND t.reply_class IN ('positive','neutral','negative','not_hiring',"
                               "'referral_offered')").fetchone()[0]
        if replied / float(cold) < float(rf["min_rate"]):
            from . import ceilings
            week = ceilings.warmup_week(conn, "gmail")
            restart_warmup(conn, "gmail", week, "reply_rate_floor")
            enqueue_notification(conn, "reply_rate_floor:%s" % now()[:10], "normal", "info",
                                 "Fewer than %d%% of %d cold emails got a reply: the cold email warm-up stops here. "
                                 "Improve targeting and research before sending more." % (int(float(rf["min_rate"]) * 100),
                                                                                          cold))
            out["notified"].append("reply_rate_floor")
    return out
