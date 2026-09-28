"""Conversation threads: creation on confirm, follow-up timing, invite acceptance, outcomes, reply checks
(design 2.1 threads, 2.3.1 step 5, 2.3.2 step 6, 2.5, 6.1 steps 8 and 9, 12.10 U6).

- on_confirm (hook): called by gate.confirm through hooks.on_confirm inside the caller's BEGIN IMMEDIATE
  transaction. Creates or updates the thread of a sent action and draws its follow-up due date once.
  Never commits, never opens a transaction, no network, only a few indexed statements.
- Thread keys: 'em:<first action token>' (email) and 'li:<contact_uid>' (LinkedIn).
- followup_due_at holds, per state: open -> when the single follow-up is due; invite_accepted -> the
  earliest time for the post-accept LinkedIn message (1 to 3 days after acceptance); otherwise NULL.
- LinkedIn sequence (M3, at most two message-bearing touches, li_msg_seq 1 and 2): after a message with
  li_msg_seq 1 one li_followup is allowed (7 to 10 days); after seq 2 nothing more is scheduled.

The small helpers at the top (dep, cfg, business-day math) are shared by the other U6 modules.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import random

from . import canon
from .errors import Denied
from .events import log_event

LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")
LIVE_SQL = "('reserved','armed','sent','failed_after_click','unknown','imported')"
THREAD_STATES = ("open", "invite_pending", "invite_accepted", "invite_withdrawn", "followed_up", "replied",
                 "closed", "bounced")
THREAD_OUTCOMES = ("none", "rejected", "screening_call", "interview", "offer", "ghosted", "role_closed", "referred")
APPLICATION_OUTCOMES = ("none", "rejected", "screening_call", "interview", "offer", "withdrawn", "role_closed",
                        "ghosted")
POSITIVE_OUTCOMES = ("screening_call", "interview", "offer", "referred")
CLOSING_OUTCOMES = ("rejected", "role_closed", "ghosted", "withdrawn")
EMAIL_FIRST_KINDS = ("cold_email", "application_email")
OPEN_DRAFT_STATUSES = ("drafted", "lint_failed", "review_pending", "review_failed", "qc_passed", "awaiting_approval",
                       "approved")
OPEN_DRAFT_SQL = "('drafted','lint_failed','review_pending','review_failed','qc_passed','awaiting_approval','approved')"
NOTES_MAX = 4000                # applications.notes and the sheet's Your notes cell
CHECK_EVERY_HOURS = 20          # a thread handed out for a browser check is not handed out again sooner
CHECK_MAX_AGE_DAYS = 60         # threads quiet for longer are not checked any more (invite_pending excepted)

# defaults used when jobhunter.config is unavailable (the researched defaults of config.example.json)
DEFAULTS = {
    "gmail.followup_after_business_days": [5, 7],
    "gmail.route": "web_ui",
    "linkedin.delays_sec.post_accept_message_days": [1, 3],
    "linkedin.delays_sec.followup_days": [7, 10],
    "channels.linkedin.enabled": True,
    "channels.email_outreach.enabled": True,
    "channels.applications.enabled": True,
}

_rng = random.SystemRandom()   # tests may replace it with random.Random(seed)


# ---------------------------------------------------------------- shared helpers
def dep(name: str):
    """Another unit's module (imported lazily, so the core never depends on import order)."""
    return importlib.import_module("jobhunter." + name)


def load_config() -> dict:
    """Effective config (U1 config.load); {} when it is unavailable, so callers use the defaults."""
    try:
        cfg_mod = dep("config")
        data = cfg_mod.load()
    except (ImportError, NotImplementedError, AttributeError, Denied, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cfg(path: str, default=None, config: dict | None = None):
    """Dotted config lookup with the documented default."""
    data = load_config() if config is None else config
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return DEFAULTS.get(path, default) if default is None else default
        cur = cur[part]
    return cur


def rv(row, key: str, default=None):
    """Value of a sqlite3.Row or mapping, or default when the key is absent."""
    if row is None:
        return default
    try:
        val = row[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def row_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else {}


def linkedin_enabled(conn, config: dict | None = None) -> bool:
    """LinkedIn writes need meta (channel_linkedin_enabled AND linkedin_tos_ack) and the file switch."""
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('channel_linkedin_enabled','linkedin_tos_ack')")}
    if rows.get("channel_linkedin_enabled") != "1" or rows.get("linkedin_tos_ack") != "1":
        return False
    return bool(cfg("channels.linkedin.enabled", True, config))


def add_business_days(ts: str, n: int) -> str:
    """ts plus n weekdays (Saturday and Sunday skipped), same time of day, UTC."""
    d = canon.parse_ts(ts)
    step = 1 if n >= 0 else -1
    left = abs(int(n))
    while left:
        d = d + _dt.timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return canon.fmt_ts(d)


def _pair(value, default: list) -> tuple[float, float]:
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        lo, hi = float(default[0]), float(default[1])
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def draw_business_days(config: dict | None = None) -> int:
    lo, hi = _pair(cfg("gmail.followup_after_business_days", None, config), [5, 7])
    lo, hi = max(1, int(round(lo))), max(1, int(round(hi)))
    return _rng.randint(min(lo, hi), max(lo, hi))


def draw_days(path: str, config: dict | None = None) -> float:
    lo, hi = _pair(cfg(path, None, config), DEFAULTS.get(path, [1, 3]))
    return _rng.uniform(lo, hi)


def recipient_domain(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].strip().lower() or None


# ---------------------------------------------------------------- hook: on_confirm
def _thread_by_key(conn, key: str):
    return conn.execute("SELECT * FROM threads WHERE thread_key = ?", (key,)).fetchone()


def _contact_uid(conn, contact_id) -> str:
    row = conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row is None:
        raise Denied("E_INTERNAL", "LinkedIn action without a contact")
    return row[0]


def _draft_subject(conn, draft_id) -> str | None:
    if not draft_id:
        return None
    row = conn.execute("SELECT subject FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return row[0] if row else None


def on_confirm(conn, action_row) -> dict | None:
    """(hook) Create or update the thread of a confirmed action. Returns {thread_key, followup_due_at}
    (or None for kinds without a thread, such as form applications). Optional extra keys in action_row:
    platform_ref (conversation or Gmail thread URL), gmail_thread_id."""
    kind = rv(action_row, "kind")
    action_id = rv(action_row, "id")
    token = rv(action_row, "token")
    stamp = canon.now()
    sent_at = rv(action_row, "sent_at") or stamp
    platform_ref = rv(action_row, "platform_ref")
    if kind in ("application", None):
        return None
    config = load_config()

    if kind in EMAIL_FIRST_KINDS:
        key = rv(action_row, "thread_key")
        if not (isinstance(key, str) and key.startswith("em:")):
            key = "em:" + token
        due = add_business_days(sent_at, draw_business_days(config))
        existing = _thread_by_key(conn, key)
        if existing is None:
            conn.execute(
                "INSERT INTO threads (thread_key, channel, contact_id, company_id, job_id, first_action_id, "
                "platform_ref, gmail_thread_id, first_message_id, subject, state, followup_due_at, last_outbound_at, "
                "created_at, updated_at) VALUES (?, 'email', ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (key, rv(action_row, "contact_id"), rv(action_row, "company_id"), rv(action_row, "job_id"),
                 action_id, platform_ref, rv(action_row, "gmail_thread_id"), rv(action_row, "message_id"),
                 _draft_subject(conn, rv(action_row, "draft_id")), due, sent_at, stamp, stamp))
        else:
            due = existing["followup_due_at"]
            conn.execute("UPDATE threads SET last_outbound_at = ?, platform_ref = COALESCE(platform_ref, ?), "
                         "updated_at = ? WHERE id = ?", (sent_at, platform_ref, stamp, existing["id"]))
        log_event(conn, "thread_open", thread_key=key, action_kind=kind, followup_due_at=due)
        return {"thread_key": key, "followup_due_at": due}

    if kind == "followup_email":
        key = rv(action_row, "thread_key")
        t = _thread_by_key(conn, key) if key else None
        if t is None:
            raise Denied("E_FOLLOWUP_BINDING", "follow-up confirmed without its thread")
        conn.execute("UPDATE threads SET state = 'followed_up', followup_action_id = ?, followup_due_at = NULL, "
                     "last_outbound_at = ?, updated_at = ? WHERE id = ?", (action_id, sent_at, stamp, t["id"]))
        log_event(conn, "thread_followed_up", thread_key=key)
        return {"thread_key": key, "followup_due_at": None}

    if kind in ("li_invite", "li_message", "inmail", "li_followup", "li_withdraw", "referral_ask"):
        contact_id = rv(action_row, "contact_id")
        key = rv(action_row, "thread_key")
        if kind in ("li_invite", "li_message", "inmail") or not key:
            key = "li:" + _contact_uid(conn, contact_id) if contact_id else key
        if not key:
            raise Denied("E_INTERNAL", "%s confirmed without a contact or thread" % kind)
        t = _thread_by_key(conn, key)
        seq = rv(action_row, "li_msg_seq")
        due = None
        if kind == "li_invite":
            state = "invite_pending"
        elif kind == "li_message":
            state = "open"
            if seq == 1:
                due = canon.ts_add(sent_at, days=draw_days("linkedin.delays_sec.followup_days", config))
        elif kind == "inmail":
            state = "open"
        elif kind == "li_followup":
            state = "followed_up"
        elif kind == "li_withdraw":
            state = "invite_withdrawn"
        else:   # referral_ask: the person replied earlier; the state stays as it is
            state = t["state"] if t is not None else "replied"
        if t is None:
            if kind in ("li_followup", "li_withdraw", "referral_ask") and not contact_id:
                raise Denied("E_INTERNAL", "%s confirmed without its thread" % kind)
            conn.execute(
                "INSERT INTO threads (thread_key, channel, contact_id, company_id, job_id, first_action_id, "
                "platform_ref, state, followup_due_at, last_outbound_at, created_at, updated_at) "
                "VALUES (?, 'linkedin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, contact_id, rv(action_row, "company_id"), rv(action_row, "job_id"), action_id, platform_ref,
                 state, due, sent_at, stamp, stamp))
        else:
            sets = ["state = ?", "last_outbound_at = ?", "updated_at = ?",
                    "platform_ref = COALESCE(?, platform_ref)"]
            vals = [state, sent_at, stamp, platform_ref]
            if kind == "li_invite":
                sets += ["first_action_id = ?", "followup_due_at = NULL", "followup_action_id = NULL"]
                vals += [action_id]
            elif kind == "li_followup":
                sets += ["followup_action_id = ?", "followup_due_at = NULL"]
                vals += [action_id]
            elif kind in ("li_message", "inmail", "li_withdraw"):
                sets += ["followup_due_at = ?"]
                vals += [due]
            conn.execute("UPDATE threads SET %s WHERE id = ?" % ", ".join(sets), vals + [t["id"]])
        log_event(conn, "thread_" + kind, thread_key=key, state=state, followup_due_at=due)
        return {"thread_key": key, "followup_due_at": due}
    return None


# ---------------------------------------------------------------- invite accepted
def mark_invite_accepted(conn, thread_key: str, at: str) -> None:
    """invite_pending -> invite_accepted; draws the post-accept message time (1 to 3 days later).
    Idempotent for a thread that is already accepted."""
    t = _thread_by_key(conn, thread_key)
    if t is None:
        raise Denied("E_NOT_FOUND", "no thread %s" % thread_key)
    if t["channel"] != "linkedin":
        raise Denied("E_VALIDATION", "only LinkedIn threads have invitations")
    if t["state"] == "invite_accepted":
        return
    if t["state"] != "invite_pending":
        raise Denied("E_PRECONDITION", "thread is %s, not invite_pending" % t["state"], data={"state": t["state"]})
    try:
        at = canon.fmt_ts(canon.parse_ts(at))
    except ValueError:
        raise Denied("E_VALIDATION", "bad acceptance time %r" % at)
    stamp = canon.now()
    if at > stamp:
        at = stamp
    due = canon.ts_add(at, days=draw_days("linkedin.delays_sec.post_accept_message_days"))
    conn.execute("UPDATE threads SET state = 'invite_accepted', followup_due_at = ?, last_checked_at = ?, "
                 "updated_at = ? WHERE id = ?", (due, stamp, stamp, t["id"]))
    log_event(conn, "invite_accepted", thread_key=thread_key, at=at, message_after=due)


# ---------------------------------------------------------------- outcomes
def set_outcome(conn, *, thread_key: str | None = None, job_uid: str | None = None, outcome: str, by: str,
                notes: str | None = None) -> dict:
    """Human outcome for a thread (threads.outcome) or a job's application (applications.outcome, notes).
    Positive outcomes put the company in active_thread (no more automation there); closing outcomes close
    the thread. Denied: E_USAGE, E_VALIDATION, E_NOT_FOUND."""
    if bool(thread_key) == bool(job_uid):
        raise Denied("E_USAGE", "give exactly one of thread_key or job_uid")
    stamp = canon.now()
    result: dict = {"outcome": outcome}
    if thread_key:
        if outcome not in THREAD_OUTCOMES:
            raise Denied("E_VALIDATION", "thread outcome must be one of %s" % ", ".join(THREAD_OUTCOMES))
        t = _thread_by_key(conn, thread_key)
        if t is None:
            raise Denied("E_NOT_FOUND", "no thread %s" % thread_key)
        sets, vals = ["outcome = ?", "updated_at = ?"], [outcome, stamp]
        if outcome != "none":
            sets.append("followup_due_at = NULL")
        if outcome in CLOSING_OUTCOMES:
            sets.append("state = 'closed'")
        conn.execute("UPDATE threads SET %s WHERE id = ?" % ", ".join(sets), vals + [t["id"]])
        company_id = t["company_id"]
        result["thread_key"] = thread_key
        if notes is not None and t["job_id"]:
            app = conn.execute("SELECT id FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                               (t["job_id"],)).fetchone()
            if app:
                conn.execute("UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
                             (notes[:NOTES_MAX], stamp, app["id"]))
                result["notes_stored"] = True
    else:
        if outcome not in APPLICATION_OUTCOMES:
            raise Denied("E_VALIDATION", "application outcome must be one of %s" % ", ".join(APPLICATION_OUTCOMES))
        job = conn.execute("SELECT id, company_id FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
        if job is None:
            raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
        app = conn.execute("SELECT id FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                           (job["id"],)).fetchone()
        if app is None:
            raise Denied("E_NOT_FOUND", "job %s has no application" % job_uid)
        if notes is not None:
            conn.execute("UPDATE applications SET outcome = ?, outcome_at = ?, notes = ?, updated_at = ? WHERE id = ?",
                         (outcome, stamp if outcome != "none" else None, notes[:NOTES_MAX], stamp, app["id"]))
            result["notes_stored"] = True
        else:
            conn.execute("UPDATE applications SET outcome = ?, outcome_at = ?, updated_at = ? WHERE id = ?",
                         (outcome, stamp if outcome != "none" else None, stamp, app["id"]))
        company_id = job["company_id"]
        result["job_uid"] = job_uid
    if outcome in POSITIVE_OUTCOMES and company_id:
        conn.execute("UPDATE companies SET contact_state = 'active_thread', contact_state_reason = ?, updated_at = ? "
                     "WHERE id = ? AND contact_state IN ('none','contacted')",
                     ("outcome:" + outcome, stamp, company_id))
    log_event(conn, "outcome_set", thread_key=thread_key, job_uid=job_uid, outcome=outcome, by=by)
    return result


def set_notes(conn, *, job_uid: str, notes: str, by: str) -> dict:
    """The person's notes on a job's application (applications.notes, the sheet's Your notes column pulled by
    U5, design 2.5: applications rows are written through threads). Stored as given, cut at NOTES_MAX
    characters; an empty text clears them. Runs inside the caller's transaction. Returns {job_uid,
    notes_stored, changed}. Denied: E_VALIDATION, E_NOT_FOUND."""
    if not isinstance(notes, str):
        raise Denied("E_VALIDATION", "notes must be text")
    job = conn.execute("SELECT id FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
    if job is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    app = conn.execute("SELECT id, notes FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                       (job["id"],)).fetchone()
    if app is None:
        raise Denied("E_NOT_FOUND", "no application for job %s" % job_uid)
    text = notes[:NOTES_MAX]
    changed = (app["notes"] or "") != text
    if changed:
        conn.execute("UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?", (text, canon.now(), app["id"]))
        log_event(conn, "notes_set", job_uid=job_uid, chars=len(text), by=by)
    return {"job_uid": job_uid, "notes_stored": True, "changed": changed}


# ---------------------------------------------------------------- reply checks (replies lane)
def _check_filters(conn, config: dict | None) -> list[str]:
    chans = []
    if linkedin_enabled(conn, config):
        chans.append("linkedin")
    if cfg("gmail.route", "web_ui", config) == "web_ui":
        chans.append("email")
    return chans


def needs_check(conn, limit: int = 200) -> list[dict]:
    """Threads the replies lane should look at in the browser: LinkedIn threads (when LinkedIn is enabled)
    and email threads on the web_ui route, not handed out in the last CHECK_EVERY_HOURS hours."""
    config = load_config()
    chans = _check_filters(conn, config)
    if not chans:
        return []
    stamp = canon.now()
    cutoff = canon.ts_add(stamp, hours=-CHECK_EVERY_HOURS)
    quiet = canon.ts_add(stamp, days=-CHECK_MAX_AGE_DAYS)
    rows = conn.execute(
        "SELECT t.*, c.contact_uid, c.full_name, c.linkedin_url, c.li_slug, co.company_uid, co.display_name, "
        "a.recipient AS first_recipient, a.sent_at AS first_sent_at FROM threads t "
        "LEFT JOIN contacts c ON c.id = t.contact_id LEFT JOIN companies co ON co.id = t.company_id "
        "LEFT JOIN actions a ON a.id = t.first_action_id "
        "WHERE t.channel IN (%s) AND t.state IN ('invite_pending','invite_accepted','open','followed_up') "
        "AND (t.last_checked_at IS NULL OR t.last_checked_at < ?) "
        "AND (t.state = 'invite_pending' OR COALESCE(t.last_outbound_at, t.created_at) >= ?) "
        "ORDER BY COALESCE(t.last_checked_at, ''), t.id LIMIT ?" % ",".join("?" for _ in chans),
        list(chans) + [cutoff, quiet, int(limit)]).fetchall()
    out = []
    for r in rows:
        item = {"thread_key": r["thread_key"], "channel": r["channel"], "state": r["state"],
                "contact_uid": r["contact_uid"], "company_uid": r["company_uid"],
                "platform_ref": r["platform_ref"], "last_checked_at": r["last_checked_at"]}
        if r["channel"] == "linkedin":
            item["how"] = "linkedin_invitation_manager" if r["state"] == "invite_pending" else "linkedin_conversation"
            item["profile_url"] = r["linkedin_url"]
        else:
            item["how"] = "gmail_search"
            day = (r["first_sent_at"] or r["created_at"])[:10].replace("-", "/")
            # All Mail, not in:inbox: the owner reads Gmail on the same account and often archives replies (or a
            # filter skips the inbox); the IMAP route searches All Mail too. -in:sent keeps our own messages out.
            q = "from:%s after:%s -in:sent" % (r["first_recipient"], day) if r["first_recipient"] else None
            item["query"] = q
            dom = recipient_domain(r["first_recipient"])
            if dom:
                item["company_query"] = "from:(@%s) after:%s -in:sent" % (dom, day)
        out.append(item)
    return out


def mark_checked(conn, thread_keys: list[str]) -> None:
    """Stamp last_checked_at (a thread handed out for a check is not handed out again for 20 hours)."""
    stamp = canon.now()
    for key in thread_keys:
        conn.execute("UPDATE threads SET last_checked_at = ? WHERE thread_key = ?", (stamp, key))


def pending_packets_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM inbound_messages WHERE status = 'pending' AND packet_path IS NOT NULL"
                        ).fetchone()[0]


def needs_check_count(conn) -> int:
    """Work for the replies lane: threads to check in the browser plus reply packets to classify."""
    return len(needs_check(conn, limit=1000)) + pending_packets_count(conn)


# ---------------------------------------------------------------- follow-ups
def due_followups(conn, limit: int) -> list[dict]:
    """Threads due for their single follow-up: email threads (state open, no reply, no follow-up yet) and
    LinkedIn threads whose first message carried li_msg_seq 1. Skips threads whose person or company is
    blocked, threads that need the human, and threads that already have an open follow-up draft."""
    config = load_config()
    stamp = canon.now()
    chans = []
    if cfg("channels.email_outreach.enabled", True, config):
        chans.append("email")
    if linkedin_enabled(conn, config):
        chans.append("linkedin")
    if not chans:
        return []
    rows = conn.execute(
        "SELECT t.*, c.contact_uid, co.company_uid, a.sent_at AS first_sent_at FROM threads t "
        "LEFT JOIN contacts c ON c.id = t.contact_id LEFT JOIN companies co ON co.id = t.company_id "
        "LEFT JOIN actions a ON a.id = t.first_action_id "
        "WHERE t.state = 'open' AND t.followup_action_id IS NULL AND t.followup_due_at IS NOT NULL "
        "AND t.followup_due_at <= ? AND t.needs_human = 0 AND t.outcome = 'none' "
        "AND (t.reply_class IS NULL OR t.reply_class IN ('auto_ack','out_of_office')) "
        "AND t.channel IN (%s) "
        "AND COALESCE(c.do_not_contact, 0) = 0 AND COALESCE(c.email_invalid, 0) = 0 "
        "AND COALESCE(co.contact_state, 'none') NOT IN ('active_thread','do_not_contact') "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.thread_key = t.thread_key "
        "  AND d.kind IN ('followup_email','li_followup') AND d.status IN %s) "
        "ORDER BY t.followup_due_at LIMIT ?" % (",".join("?" for _ in chans), OPEN_DRAFT_SQL),
        [stamp] + chans + [int(limit)]).fetchall()
    return [{"thread_key": r["thread_key"], "channel": r["channel"], "contact_uid": r["contact_uid"],
             "company_uid": r["company_uid"], "first_sent_at": r["first_sent_at"] or r["created_at"],
             "due_at": r["followup_due_at"], "original_subject": r["subject"],
             "kind": "followup_email" if r["channel"] == "email" else "li_followup"} for r in rows]


# ---------------------------------------------------------------- listing
def list_threads(conn, state: str | None = None, limit: int = 200) -> list[dict]:
    if state is not None and state not in THREAD_STATES:
        raise Denied("E_VALIDATION", "unknown thread state %r" % state)
    sql = ("SELECT t.*, c.contact_uid, co.company_uid, j.job_uid FROM threads t "
           "LEFT JOIN contacts c ON c.id = t.contact_id LEFT JOIN companies co ON co.id = t.company_id "
           "LEFT JOIN jobs j ON j.id = t.job_id")
    args: list = []
    if state:
        sql += " WHERE t.state = ?"
        args.append(state)
    sql += " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args):
        out.append({k: r[k] for k in ("thread_key", "channel", "state", "contact_uid", "company_uid", "job_uid",
                                      "subject", "followup_due_at", "last_outbound_at", "last_checked_at",
                                      "reply_class", "reply_at", "outcome", "needs_human", "platform_ref")})
    return out
