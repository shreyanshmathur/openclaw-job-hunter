"""Twice-daily digest: only what changed since the last digest (design 1.3.8).

`build_digest(conn, since)` returns plain chat text, or None when nothing new happened (then nothing is
sent). `run(conn, since_last, deliver)` with deliver queues the digest as a notification, stores
`meta.last_digest_at` and flushes the queue at once (delivery is checked by notify). Without deliver it only
shows the text and moves nothing, so a preview never swallows the next digest.
"""
from __future__ import annotations

from . import canon, db
from . import notify as N
from . import sheets_labels as L
from . import status as S

DEFAULT_WINDOW_H = 24
MAX_LIST = 5


def _borderline(conn, since: str, style_tz) -> list[str]:
    rows = conn.execute(
        "SELECT j.title, COALESCE(co.display_name, j.company_name_raw) AS company, e.score FROM jobs j "
        "JOIN evaluations e ON e.job_id = j.id LEFT JOIN companies co ON co.id = j.company_id "
        "WHERE j.status = 'borderline' AND e.stage = 'llm' AND e.evaluated_at >= ? AND j.human_call IS NULL "
        "ORDER BY e.score DESC LIMIT ?", (since, MAX_LIST)).fetchall()
    return ["%s, %s (fit %s)" % (r["company"], r["title"], r["score"]) for r in rows]


def _replies(conn, since: str, style: str) -> list[str]:
    rows = conn.execute(
        "SELECT r.classification, r.summary, c.full_name, c.first_name, COALESCE(co.display_name, '') AS company "
        "FROM replies r JOIN threads t ON t.id = r.thread_id LEFT JOIN contacts c ON c.id = t.contact_id "
        "LEFT JOIN companies co ON co.id = COALESCE(t.company_id, c.company_id) "
        "WHERE r.received_at >= ? AND r.classification NOT IN ('auto_ack','out_of_office','bounce') "
        "ORDER BY r.received_at LIMIT ?", (since, MAX_LIST)).fetchall()
    out = []
    for r in rows:
        who = L.person_name(r["full_name"], r["first_name"], style) or "Someone"
        where = (" at %s" % r["company"]) if r["company"] else ""
        label = L.REPLY_DETAIL.get(r["classification"], "Replied")
        out.append("%s%s: %s. %s" % (who, where, label, L.clip(r["summary"], 160)))
    return out


def _codes_accounts_captchas(conn, since: str) -> str | None:
    """'Job sites: 2 email codes used, 1 account created, CAPTCHAs: 1 solved by you, 1 skipped' (counts only)."""
    try:
        codes = conn.execute("SELECT count(*) FROM code_uses WHERE used_at >= ?", (since,)).fetchone()[0]
        made = conn.execute("SELECT count(*) FROM ats_accounts WHERE created_at >= ? AND status <> 'failed'",
                            (since,)).fetchone()[0]
        solved = conn.execute("SELECT count(*) FROM captcha_tasks WHERE status = 'resolved' AND resolved_at >= ?",
                              (since,)).fetchone()[0]
        skipped = conn.execute("SELECT count(*) FROM captcha_tasks WHERE status = 'timed_out' AND resolved_at >= ?",
                               (since,)).fetchone()[0]
    except Exception:
        return None
    parts = []
    if codes:
        parts.append("%d email code%s used" % (codes, "" if codes == 1 else "s"))
    if made:
        parts.append("%d account%s created" % (made, "" if made == 1 else "s"))
    if solved or skipped:
        parts.append("CAPTCHAs: %d solved by you, %d skipped" % (solved, skipped))
    return ("Job sites: " + ", ".join(parts)) if parts else None


def build_digest(conn, since: str | None, config: dict | None = None) -> str | None:
    """Delta text since `since` (UTC timestamp; None means the last 24 hours), or None when nothing is new."""
    config = S.load_config() if config is None else config
    tz = S.tzinfo(config)
    style = str(S.cfg(config, "sheets.person_name_style", "first_last_initial"))
    now = canon.now()
    # (since, now]: whatever was already reported at `since` is not reported again
    since = canon.ts_add(since, seconds=1) if since else canon.ts_add(now, hours=-DEFAULT_WINDOW_H)
    c = S.count_activity(conn, since, canon.ts_add(now, seconds=1))
    breakers_new = conn.execute("SELECT count(*) FROM breaker_events WHERE created_at >= ?", (since,)).fetchone()[0]
    activity = sum(c[k] for k in ("jobs_found", "evaluated", "applications", "emails", "li_invites", "li_messages",
                                  "follow_ups", "replies", "drafts", "stops"))
    if activity == 0 and not breakers_new:
        return None

    lines = ["Job Hunter digest, %s" % S.fmt_local(now, tz),
             "Since %s:" % S.fmt_local(since, tz)]
    if c["jobs_found"]:
        lines.append("Jobs found: %d (%d passed your filters, %d good fits)"
                     % (c["jobs_found"], c["passed_filters"], c["good_fits"]))
    elif c["evaluated"]:
        lines.append("Jobs evaluated: %d (%d good fits)" % (c["evaluated"], c["good_fits"]))
    if c["applications"]:
        lines.append("Applications sent: %d" % c["applications"])
    sends = []
    if c["emails"]:
        sends.append("%d emails" % c["emails"])
    if c["follow_ups"]:
        sends.append("%d follow-ups" % c["follow_ups"])
    if c["li_invites"]:
        sends.append("%d LinkedIn invites" % c["li_invites"])
    if c["li_messages"]:
        sends.append("%d LinkedIn messages" % c["li_messages"])
    if sends:
        lines.append("Outreach: " + ", ".join(sends))
    if c["replies"]:
        lines.append("Replies: %d (%d positive)" % (c["replies"], c["positive"]))
        lines.extend("  " + x for x in _replies(conn, since, style))
    sites = _codes_accounts_captchas(conn, since)
    if sites:
        lines.append(sites)
    if c["drafts"]:
        lines.append("Drafts written: %d (%d passed QC on the first try, %d dropped)"
                     % (c["drafts"], c["first_pass"], c["dropped"]))
    border = _borderline(conn, since, tz)
    if border:
        lines.append("Borderline jobs you could still pick (Jobs tab, Your call: Apply anyway):")
        lines.extend("  " + b for b in border)
    q = S.queues(conn)
    waiting = []
    if q["approvals_waiting"]:
        waiting.append("%d approvals" % q["approvals_waiting"])
    if q["human_tasks"]:
        waiting.append("%d questions or tasks" % q["human_tasks"])
    if q["jobs_needing_you"]:
        waiting.append("%d jobs to apply to yourself" % q["jobs_needing_you"])
    if waiting:
        lines.append("Waiting for you: " + ", ".join(waiting) + ". Send /jh inbox to see them.")
    for b in S.open_breakers(conn):
        lines.append("Stopped: %s since %s. %s" % (b["area"], S.fmt_local(b["since"], tz), b["todo"]))
    if S.is_paused():
        lines.append("The agent is paused. Run ./jobhunter resume to continue.")
    lines.extend(_settings_lines(conn, config))
    return "\n".join(lines)


def _settings_lines(conn, config: dict) -> list[str]:
    """Which browser sites the agent may use (U1 consent, read only) and, while the optional email finder is
    on, used or stopped, its budget line (U10). Nothing when the consent API is not installed."""
    out = []
    sites = S.consent_line(S.browser_consent(conn))
    if sites:
        out.append(sites)
    finder = S.email_finder(conn, config)
    if finder is not None and S.finder_active(finder):
        out.append(S.email_finder_line(finder))
    return out


def run(conn, since_last: bool = False, deliver: bool = False, *, sender=None, desktop=None,
        config: dict | None = None) -> dict:
    """`digest [--since-last] [--deliver]`."""
    config = S.load_config() if config is None else config
    since = None
    if since_last:
        since = db.meta_get(conn, "last_digest_at")
    now = canon.now()
    text = build_digest(conn, since, config)
    if text is None:
        return {"sent": False, "text": None, "since": since, "nothing_new": True}
    out = {"sent": False, "text": text, "since": since, "nothing_new": False}
    if deliver:
        with db.tx(conn):
            db.enqueue_notification(conn, "digest:%s" % now, "normal", "digest", text)
            db.meta_set(conn, "last_digest_at", now, "system")
        res = N.flush(conn, True, max_items=8, sender=sender, desktop=desktop, config=config)
        out["sent"] = bool(res.get("sent"))
        out["flush"] = res
    return out


build = build_digest   # the name in the section 11 table
