"""Inbound replies: packets for the agent, agent and code classifications, and their consequences
(design 2.1 replies and inbound_messages, 6.1 step 8, 12.13, 12.10 U6).

- write_packet (U9 mailer): stores an inbound message as `pending` and writes the reply packet
  WS/outreach/inbox/reply-<inbound_id>.json for the replies lane.
- record_code_class (U9 mailer): code pre-rules (auto_ack, out_of_office, bounce, application_confirmation).
- record (reply record --file): the agent's classification of a packet or of a LinkedIn reply found in
  the browser; also accepts the invitation-accepted event for LinkedIn threads (see ACCEPT_KEYS).
Consequences are applied in code, in the caller's transaction, exactly once per (thread, message):

  positive, neutral, referral_offered -> thread replied, company active_thread, high notification, review task
  negative, not_hiring               -> thread closed, person do-not-contact (company cooldown stays)
  opt_out, complaint                 -> person and company exclusions, thread closed, complaint breaker at 2/30 days,
                                        the email finder's data about the person purged (hooks.on_optout)
  auto_ack                           -> not a reply; nothing changes
  out_of_office                      -> follow-up due moved to the return date plus 2 business days
  bounce                             -> address invalid, thread bounced, bounce breaker, no-guessing for B/C domains,
                                        the email finder's strike rules (hooks.on_bounce)

On the web_ui email route (the default) the replies lane reads Gmail in the browser: web_checks() lists the
searches per open thread, and web_lane() adds the delivery-failure search, the daily Sent-folder audit and a
requested history scan (U9 mail.audit.web_status) when the agent may record them.
"""
from __future__ import annotations

import json
import os
import re

from . import canon, db, paths
from .errors import Denied
from .events import enqueue_notification, log_event, open_human_task
from .threads import (add_business_days, cfg, dep, load_config, mark_invite_accepted, recipient_domain, rv)

REPLY_CLASSES = ("positive", "neutral", "negative", "not_hiring", "referral_offered", "auto_ack", "out_of_office",
                 "bounce", "complaint", "opt_out")
CODE_CLASSES = ("auto_ack", "out_of_office", "bounce", "application_confirmation")
HUMAN_CLASSES = ("positive", "neutral", "referral_offered")
RECORD_KEYS = {"inbound_id", "thread_key", "class", "summary", "received_at", "msg_ref", "return_date"}
ACCEPT_KEYS = {"thread_key", "event", "observed_at"}
THREAD_KEY_RE = re.compile(r"^(em:T[A-Z2-7]{11}|li:P[A-Z2-7]{7})$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
PACKET_TEXT_MAX = 2000
EXCERPT_MAX = 300
CODE_SUMMARIES = {"auto_ack": "Automatic acknowledgement.", "out_of_office": "Out of office auto-reply.",
                  "bounce": "Delivery failure notice.", "application_confirmation": "Application confirmation."}
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_QUOTE_HEAD_RE = re.compile(r"^\s*(On .{0,200}wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+)$", re.I)


def _clean(s: str, n: int) -> str:
    s = _CTRL_RE.sub("", canon.normalize_text(s))
    return s[:n]


def _by_class(by: str) -> str:
    if by in ("code", "system") or by.startswith("system"):
        return "code"
    if by.startswith("human"):
        return "human"
    return "agent"


def strip_quoted(text: str) -> str:
    """Reply text without quoted history (lines starting with '>' and everything after 'On ... wrote:')."""
    out = []
    for line in canon.normalize_text(text).split("\n"):
        if _QUOTE_HEAD_RE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return canon.normalize_text("\n".join(out))


def _thread(conn, thread_key=None, thread_id=None):
    if thread_key:
        return conn.execute("SELECT * FROM threads WHERE thread_key = ?", (thread_key,)).fetchone()
    if thread_id:
        return conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return None


def _our_last_excerpt(conn, thread) -> str:
    row = conn.execute(
        "SELECT d.body FROM actions a JOIN drafts d ON d.id = a.draft_id WHERE a.id IN (?, ?) "
        "ORDER BY a.id DESC LIMIT 1", (thread["followup_action_id"] or -1, thread["first_action_id"])).fetchone()
    return _clean(row[0], EXCERPT_MAX) if row and row[0] else ""


# ---------------------------------------------------------------- packets (U9 -> agent)
def packet_dir() -> str:
    return paths.inbox_dir("outreach")


def _write_packet_file(path: str, packet: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(packet, fh, ensure_ascii=True, indent=1)
    os.replace(tmp, path)


def write_packet(conn, inbound: dict, text: str) -> str:
    """Store an inbound email the code rules could not classify (status pending) and write its packet
    (12.13) under WS/outreach/inbox/. Idempotent on msg_ref. Returns the packet path.
    The file is written only once the caller's transaction has committed (db.defer_write): a rollback
    leaves neither the row nor an orphan packet. A stored packet whose file is missing (a crash between
    the commit and the write) is written again on the next call for the same msg_ref."""
    msg_ref = inbound.get("msg_ref")
    if not msg_ref or not isinstance(msg_ref, str):
        raise Denied("E_VALIDATION", "inbound message needs msg_ref")
    existing = conn.execute("SELECT * FROM inbound_messages WHERE msg_ref = ?", (msg_ref,)).fetchone()
    if existing is not None and existing["packet_path"] and os.path.exists(existing["packet_path"]):
        return existing["packet_path"]
    thread = _thread(conn, inbound.get("thread_key"), inbound.get("thread_id"))
    if thread is None and existing is not None and existing["thread_id"]:
        thread = _thread(conn, thread_id=existing["thread_id"])
    received = inbound.get("received_at") or (existing["received_at"] if existing is not None else None) or canon.now()
    stamp = canon.now()
    company_id = inbound.get("company_id") or (thread["company_id"] if thread is not None else None)
    if existing is None:
        cur = conn.execute(
            "INSERT INTO inbound_messages (msg_ref, channel, thread_id, company_id, from_domain, received_at, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (msg_ref, inbound.get("channel") or "email", thread["id"] if thread is not None else None, company_id,
             (inbound.get("from_domain") or "").lower() or None, received, stamp, stamp))
        inbound_id = cur.lastrowid
    else:
        inbound_id = existing["id"]
    path = os.path.join(packet_dir(), "reply-%d.json" % inbound_id)
    packet = {"inbound_id": inbound_id, "thread_key": thread["thread_key"] if thread is not None else None,
              "channel": inbound.get("channel") or "email", "from_domain": (inbound.get("from_domain") or "").lower(),
              "received_at": received, "subject": _clean(inbound.get("subject") or "", 300),
              "text": _clean(strip_quoted(text or ""), PACKET_TEXT_MAX),
              "our_last_message_excerpt": _our_last_excerpt(conn, thread) if thread is not None else ""}
    conn.execute("UPDATE inbound_messages SET packet_path = ?, updated_at = ? WHERE id = ?", (path, stamp, inbound_id))
    log_event(conn, "reply_packet", inbound_id=inbound_id, thread_key=packet["thread_key"])

    def _write_when_committed(c) -> None:
        row = c.execute("SELECT 1 FROM inbound_messages WHERE id = ? AND msg_ref = ? AND packet_path = ?",
                        (inbound_id, msg_ref, path)).fetchone()
        if row is not None:
            _write_packet_file(path, packet)
    db.defer_write(conn, _write_when_committed)
    return path


def pending(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.packet_path, t.thread_key FROM inbound_messages i LEFT JOIN threads t ON t.id = i.thread_id "
        "WHERE i.status = 'pending' AND i.packet_path IS NOT NULL ORDER BY i.received_at, i.id LIMIT ?",
        (int(limit),)).fetchall()
    return [{"inbound_id": r["id"], "packet_path": r["packet_path"], "thread_key": r["thread_key"]} for r in rows]


def web_checks(conn, limit: int) -> list[dict]:
    """web_ui route: the Gmail searches the replies lane runs in the browser, per open email thread."""
    from .threads import needs_check
    out = []
    for item in needs_check(conn, limit=limit):
        if item["channel"] != "email" or not item.get("query"):
            continue
        out.append({"thread_key": item["thread_key"], "how": "gmail_search", "query": item["query"]})
        if item.get("company_query"):
            out.append({"thread_key": item["thread_key"], "how": "gmail_search", "query": item["company_query"]})
    return out


def _agent_may(agent_id: str | None, command: str) -> bool:
    """Whether acl.json lets this agent run `command` (callers other than agents: True)."""
    if not agent_id:
        return True
    try:
        acl = dep("auth").load_acl()
        cmds = ((acl.get("agents") or {}).get(agent_id) or {}).get("commands") or {}
    except (ImportError, AttributeError, Denied, OSError, ValueError):
        return False
    return command in cmds


def web_lane(conn, agent_id: str | None = None) -> dict:
    """web_ui route: what the replies lane reads in Gmail besides the thread searches, from U9
    mail.audit.web_status: `bounce_check` ({query, threads: [{thread_key, recipient}]} or None), and, when the
    agent may record them with `mail audit --file`, a due `sent_audit` and a requested `history_scan`
    ({purpose, query, ...}). {} when the mail module cannot say (the thread searches still run)."""
    try:
        from .mail import audit
        st = audit.web_status(conn)
    except Exception:   # U9 unavailable or its config unreadable: the lane still runs the thread searches
        return {}
    if not isinstance(st, dict) or st.get("route") == "app_password":
        return {}
    out = {"bounce_check": st.get("bounce_check")}
    if _agent_may(agent_id, "mail audit"):
        if st.get("audit_due"):
            out["sent_audit"] = {"purpose": "audit", "query": st.get("audit_query"),
                                 "last_audit_at": st.get("last_audit_at")}
        if isinstance(st.get("history_scan"), dict):
            out["history_scan"] = dict(st["history_scan"], purpose="history")
    return out


def cleanup_packet(path: str | None) -> bool:
    """Delete a reply packet after its record was committed (only files named inbox/reply-<n>.json)."""
    if not path:
        return False
    real = os.path.realpath(path)
    base = os.path.realpath(packet_dir())
    if os.path.dirname(real) != base or not re.match(r"^reply-[0-9]+\.json$", os.path.basename(real)):
        return False
    try:
        os.unlink(real)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- consequences
def _exclusion_key(kind: str, value: str) -> str | None:
    keys = dep("keys")
    if kind == "email":
        pairs = keys.person_keys(email=value)
        want = ("email",)
    elif kind == "linkedin":
        pairs = keys.person_keys(linkedin_url=value)
        want = ("li_slug", "li_member", "li_legacy", "li_sales")
    else:
        pairs = keys.company_keys(name=value)
        want = ("name",)
    for key, k in pairs:
        if k in want:
            return key
    return pairs[0][0] if pairs else None


def _add_exclusion(conn, kind: str, value: str, source: str, reason: str) -> int | None:
    try:
        key = _exclusion_key(kind, value)
    except (ImportError, NotImplementedError, Denied):
        key = None
    if not key:
        key = "%s:%s" % (kind, value.strip().lower())
    stamp = canon.now()
    conn.execute(
        "INSERT INTO exclusions (type, value_raw, value_key, reason, source, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT (type, value_key) DO UPDATE SET active = 1, "
        "deactivated_at = NULL, deactivated_by = NULL, updated_at = excluded.updated_at",
        (kind, value, key, reason, source, stamp, stamp))
    row = conn.execute("SELECT id FROM exclusions WHERE type = ? AND value_key = ?", (kind, key)).fetchone()
    return row[0] if row else None


def _exclude(conn, kind: str, value: str, source: str, reason: str) -> bool:
    """Add (or reactivate) an exclusion and apply its effects through U1 exclusions.add; when that is not
    available, insert the row here and try exclusions.apply. Returns True when U1 applied the effects."""
    try:
        dep("exclusions").add(conn, kind, value, reason, source)
        return True
    except (ImportError, NotImplementedError, AttributeError):
        pass
    return _apply_exclusion(conn, _add_exclusion(conn, kind, value, source, reason))


def _apply_exclusion(conn, exclusion_id: int | None) -> bool:
    if exclusion_id is None:
        return False
    try:
        dep("exclusions").apply(conn, exclusion_id)
        return True
    except (ImportError, NotImplementedError, AttributeError):
        return False


def _trip(conn, scope: str, reason_code: str, detail: str) -> None:
    """Trip a breaker through U1; if breakers is unavailable, write the open row directly (fail closed)."""
    try:
        dep("breakers").trip(conn, scope, reason_code, detail, None, "system")
        return
    except (ImportError, NotImplementedError, AttributeError):
        pass
    stamp = canon.now()
    conn.execute(
        "INSERT INTO breakers (scope, state, reason_code, detail, tripped_at, requires_human, updated_at) "
        "VALUES (?, 'open', ?, ?, ?, 1, ?) ON CONFLICT (scope) DO UPDATE SET state = 'open', "
        "reason_code = excluded.reason_code, detail = excluded.detail, tripped_at = excluded.tripped_at, "
        "requires_human = 1, updated_at = excluded.updated_at", (scope, reason_code, detail, stamp, stamp))
    conn.execute("INSERT INTO breaker_events (scope, event, reason_code, detail, by, created_at) "
                 "VALUES (?, 'trip', ?, ?, 'system', ?)", (scope, reason_code, detail, stamp))
    enqueue_notification(conn, "breaker:%s:%s" % (scope, stamp[:10]), "high", "alert",
                         "Stopped %s: %s. Nothing else goes out there until you reset it." % (scope, detail))


def _names(conn, thread) -> tuple[str, str]:
    c = conn.execute("SELECT full_name, first_name FROM contacts WHERE id = ?", (thread["contact_id"],)).fetchone() \
        if thread["contact_id"] else None
    co = conn.execute("SELECT display_name FROM companies WHERE id = ?", (thread["company_id"],)).fetchone() \
        if thread["company_id"] else None
    person = (c["full_name"] or c["first_name"]) if c is not None else "someone"
    return person or "someone", co["display_name"] if co is not None else "their company"


def _thread_recipient(conn, thread) -> str | None:
    row = conn.execute("SELECT recipient FROM actions WHERE id = ?", (thread["first_action_id"],)).fetchone()
    return row[0] if row else None


def apply_class(conn, thread, cls: str, received_at: str, summary: str, return_date: str | None,
                reply_id: int | None) -> list[str]:
    """The consequences of one classified reply on its thread (6.1 step 8). Returns effect names."""
    stamp = canon.now()
    effects: list[str] = []
    tid = thread["id"]
    person, company = _names(conn, thread)
    config = load_config()
    if cls != "auto_ack":
        conn.execute("UPDATE threads SET reply_class = ?, reply_at = ?, reply_summary = ?, updated_at = ? WHERE id = ?",
                     (cls, received_at, summary[:300], stamp, tid))
    if cls in HUMAN_CLASSES:
        conn.execute("UPDATE threads SET state = 'replied', needs_human = 1, followup_due_at = NULL, updated_at = ? "
                     "WHERE id = ?", (stamp, tid))
        if thread["company_id"]:
            conn.execute("UPDATE companies SET contact_state = 'active_thread', contact_state_reason = ?, "
                         "updated_at = ? WHERE id = ? AND contact_state IN ('none','contacted')",
                         ("reply:" + thread["thread_key"], stamp, thread["company_id"]))
            effects.append("company_active_thread")
        kind = "positive_reply" if cls in ("positive", "referral_offered") else "alert"
        enqueue_notification(conn, "reply:%s:%s" % (thread["thread_key"], reply_id or received_at), "high", kind,
                             "%s at %s replied (%s): %s" % (person, company, cls.replace("_", " "), summary))
        open_human_task(conn, "review_reply", "Read and answer the reply from %s at %s." % (person, company),
                        thread_id=tid, company_id=thread["company_id"], detail=summary)
        effects += ["thread_replied", "notified_high", "review_task"]
    elif cls in ("negative", "not_hiring"):
        conn.execute("UPDATE threads SET state = 'closed', followup_due_at = NULL, updated_at = ? WHERE id = ?",
                     (stamp, tid))
        if thread["contact_id"]:
            conn.execute("UPDATE contacts SET do_not_contact = 1, dnc_reason = ?, updated_at = ? WHERE id = ?",
                         ("reply_" + cls, stamp, thread["contact_id"]))
        enqueue_notification(conn, "reply:%s:%s" % (thread["thread_key"], reply_id or received_at), "normal", "info",
                             "%s at %s replied (%s): %s" % (person, company, cls.replace("_", " "), summary))
        effects += ["thread_closed", "person_do_not_contact"]
    elif cls in ("opt_out", "complaint"):
        source = "reply_optout" if cls == "opt_out" else "complaint"
        conn.execute("UPDATE threads SET state = 'closed', followup_due_at = NULL, updated_at = ? WHERE id = ?",
                     (stamp, tid))
        c = conn.execute("SELECT * FROM contacts WHERE id = ?", (thread["contact_id"],)).fetchone() \
            if thread["contact_id"] else None
        applied = []
        if c is not None:
            conn.execute("UPDATE contacts SET do_not_contact = 1, dnc_reason = ?, updated_at = ? WHERE id = ?",
                         (source, stamp, c["id"]))
            addr = c["email"] or _thread_recipient(conn, thread)
            if addr and "@" in addr:
                applied.append(_exclude(conn, "email", addr, source, cls))
            if c["linkedin_url"]:
                applied.append(_exclude(conn, "linkedin", c["linkedin_url"], source, cls))
        co = conn.execute("SELECT * FROM companies WHERE id = ?", (thread["company_id"],)).fetchone() \
            if thread["company_id"] else None
        if co is not None:
            ok = _exclude(conn, "company", co["display_name"], source, cls)
            if not ok:   # exclusions.apply unavailable: apply its company effect here (fail closed)
                conn.execute("UPDATE companies SET contact_state = 'do_not_contact', contact_state_reason = ?, "
                             "updated_at = ? WHERE id = ?", (source, stamp, co["id"]))
        effects += ["thread_closed", "person_do_not_contact", "excluded"]
        if c is not None and dep("hooks").on_optout(conn, c["id"]):
            effects.append("finder_data_purged")
        if cls == "complaint":
            since = canon.ts_add(stamp, days=-30)
            n = conn.execute("SELECT count(*) FROM replies WHERE classification = 'complaint' AND received_at >= ?",
                             (since,)).fetchone()[0]
            enqueue_notification(conn, "complaint:%s" % thread["thread_key"], "high", "alert",
                                 "%s at %s marked our email as unwanted. They and the company are excluded now."
                                 % (person, company))
            if n >= 2:
                _trip(conn, "gmail.cold", "complaints_30d", "%d complaints in 30 days" % n)
                effects.append("breaker_gmail_cold")
        else:
            enqueue_notification(conn, "optout:%s" % thread["thread_key"], "normal", "info",
                                 "%s at %s asked not to be contacted. They and the company are excluded now."
                                 % (person, company))
    elif cls == "out_of_office":
        if thread["state"] == "open" and thread["followup_action_id"] is None:
            base = received_at
            if return_date and DATE_RE.match(return_date):
                base = max(received_at, return_date + received_at[10:])
            new_due = add_business_days(base, 2)
            due = thread["followup_due_at"]
            if due is None or new_due > due:
                conn.execute("UPDATE threads SET followup_due_at = ?, updated_at = ? WHERE id = ?",
                             (new_due, stamp, tid))
                effects.append("followup_moved")
    elif cls == "bounce":
        conn.execute("UPDATE threads SET state = 'bounced', followup_due_at = NULL, updated_at = ? WHERE id = ?",
                     (stamp, tid))
        addr = _thread_recipient(conn, thread)
        c = conn.execute("SELECT * FROM contacts WHERE id = ?", (thread["contact_id"],)).fetchone() \
            if thread["contact_id"] else None
        if c is not None:
            conn.execute("UPDATE contacts SET email_invalid = 1, updated_at = ? WHERE id = ?", (stamp, c["id"]))
            dom = recipient_domain(addr or c["email"])
            if c["email_grade"] in ("B", "C") and dom:
                from . import db
                db.meta_set(conn, "no_guess:" + dom, "1", "system")
                effects.append("no_guess_domain")
        effects += ["thread_bounced", "address_invalid"]
        first = conn.execute("SELECT * FROM actions WHERE id = ?", (thread["first_action_id"],)).fetchone() \
            if thread["first_action_id"] else None
        tripped = dep("hooks").on_bounce(conn, dict(first) if first is not None else
                                         {"contact_id": thread["contact_id"], "recipient": addr}, dict(thread))
        if tripped:
            effects.append("finder_bounce_strikes")
        per_24h = cfg("gmail.bounce_stop.per_24h", 2, config)
        try:
            per_24h = int(per_24h)
        except (TypeError, ValueError):
            per_24h = 2
        since = canon.ts_add(stamp, hours=-24)
        n = conn.execute("SELECT count(*) FROM replies WHERE classification = 'bounce' AND received_at >= ?",
                         (since,)).fetchone()[0]
        if n >= max(1, per_24h):
            _trip(conn, "gmail.cold", "bounces_24h", "%d hard bounces in 24 hours" % n)
            effects.append("breaker_gmail_cold")
    log_event(conn, "reply_consequences", thread_key=thread["thread_key"], cls=cls, effects=effects)
    return effects


def _insert_reply(conn, thread_id: int, received_at: str, cls: str, classified_by: str, summary: str,
                  msg_ref: str) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM replies WHERE thread_id = ? AND platform_msg_ref = ?",
                       (thread_id, msg_ref)).fetchone()
    if row is not None:
        return row[0], False
    cur = conn.execute("INSERT INTO replies (thread_id, received_at, classification, classified_by, summary, "
                       "platform_msg_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (thread_id, received_at, cls, classified_by, summary, msg_ref, canon.now()))
    return cur.lastrowid, True


# ---------------------------------------------------------------- agent record (12.13)
def _ts(value, name: str) -> str:
    try:
        return canon.fmt_ts(canon.parse_ts(value))
    except (ValueError, TypeError):
        raise Denied("E_VALIDATION", "%s must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ" % name)


def record(conn, payload: dict, by: str) -> dict:
    """Apply a reply record file (12.13), or an invitation-accepted event
    {"thread_key": "li:P...", "event": "invite_accepted", "observed_at": "<ts>"}."""
    if not isinstance(payload, dict):
        raise Denied("E_SCHEMA", "reply record must be a JSON object")
    if payload.get("event") is not None:
        extra = set(payload) - ACCEPT_KEYS
        if extra or payload.get("event") != "invite_accepted":
            raise Denied("E_SCHEMA", "an event record has only thread_key, event=invite_accepted, observed_at",
                         data={"unknown": sorted(extra)})
        key = payload.get("thread_key")
        if not isinstance(key, str) or not key.startswith("li:") or not THREAD_KEY_RE.match(key):
            raise Denied("E_VALIDATION", "invite_accepted needs a LinkedIn thread_key")
        at = _ts(payload.get("observed_at"), "observed_at")
        mark_invite_accepted(conn, key, at)
        return {"thread_key": key, "event": "invite_accepted", "effects": ["invite_accepted"]}

    extra = set(payload) - RECORD_KEYS
    if extra:
        raise Denied("E_SCHEMA", "unknown keys in reply record", data={"unknown": sorted(extra)})
    for k in ("thread_key", "class", "summary", "received_at"):
        if k not in payload:
            raise Denied("E_SCHEMA", "reply record needs %s" % k)
    key, cls = payload["thread_key"], payload["class"]
    if not isinstance(key, str) or not THREAD_KEY_RE.match(key):
        raise Denied("E_VALIDATION", "bad thread_key")
    if cls not in REPLY_CLASSES:
        raise Denied("E_VALIDATION", "class must be one of %s" % ", ".join(REPLY_CLASSES))
    summary = payload["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise Denied("E_VALIDATION", "summary is required")
    summary = _clean(summary, 10000)
    if len(summary) > 300:
        raise Denied("E_VALIDATION", "summary is longer than 300 characters", data={"length": len(summary)})
    received = _ts(payload["received_at"], "received_at")
    return_date = payload.get("return_date")
    if return_date is not None and (not isinstance(return_date, str) or not DATE_RE.match(return_date)):
        raise Denied("E_VALIDATION", "return_date must be YYYY-MM-DD")
    thread = _thread(conn, key)
    if thread is None:
        raise Denied("E_NOT_FOUND", "no thread %s" % key)
    inbound_id = payload.get("inbound_id")
    stamp = canon.now()
    packet_path = None
    if inbound_id is not None:
        if not isinstance(inbound_id, int) or isinstance(inbound_id, bool):
            raise Denied("E_VALIDATION", "inbound_id must be an integer or null")
        inbound = conn.execute("SELECT * FROM inbound_messages WHERE id = ?", (inbound_id,)).fetchone()
        if inbound is None:
            raise Denied("E_NOT_FOUND", "no inbound message %d" % inbound_id)
        if inbound["thread_id"] is not None and inbound["thread_id"] != thread["id"]:
            raise Denied("E_VALIDATION", "inbound message %d belongs to another thread" % inbound_id)
        msg_ref = inbound["msg_ref"]
        received = inbound["received_at"]
        packet_path = inbound["packet_path"]
    else:
        msg_ref = payload.get("msg_ref")
        if not isinstance(msg_ref, str) or not msg_ref.strip() or len(msg_ref) > 300 or _CTRL_RE.search(msg_ref):
            raise Denied("E_VALIDATION", "msg_ref is required when inbound_id is null")
        inbound = conn.execute("SELECT * FROM inbound_messages WHERE msg_ref = ?", (msg_ref,)).fetchone()
        if inbound is None:
            cur = conn.execute(
                "INSERT INTO inbound_messages (msg_ref, channel, thread_id, company_id, received_at, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'classified', ?, ?)",
                (msg_ref, thread["channel"], thread["id"], thread["company_id"], received, stamp, stamp))
            inbound_id = cur.lastrowid
        else:
            inbound_id = inbound["id"]
    reply_id, new = _insert_reply(conn, thread["id"], received, cls, _by_class(by), summary, msg_ref)
    conn.execute("UPDATE inbound_messages SET status = 'classified', thread_id = COALESCE(thread_id, ?), "
                 "packet_path = NULL, updated_at = ? WHERE id = ?", (thread["id"], stamp, inbound_id))
    effects = apply_class(conn, thread, cls, received, summary, return_date, reply_id) if new else ["already_recorded"]
    log_event(conn, "reply_recorded", thread_key=key, cls=cls, reply_id=reply_id, by=by)
    return {"reply_id": reply_id, "thread_key": key, "class": cls, "effects": effects, "packet_path": packet_path}


# ---------------------------------------------------------------- code classes (U9)
def record_code_class(conn, inbound: dict) -> dict:
    """Store a message the mailer classified by rule and apply its effect. inbound keys: msg_ref,
    code_class, received_at, and optionally channel, thread_key or thread_id, company_id, from_domain,
    return_date (YYYY-MM-DD, out_of_office), job_id (application_confirmation), summary.
    Idempotent on msg_ref."""
    msg_ref = inbound.get("msg_ref")
    cls = inbound.get("code_class")
    if not msg_ref or not isinstance(msg_ref, str):
        raise Denied("E_VALIDATION", "inbound message needs msg_ref")
    if cls not in CODE_CLASSES:
        raise Denied("E_VALIDATION", "code_class must be one of %s" % ", ".join(CODE_CLASSES))
    existing = conn.execute("SELECT id FROM inbound_messages WHERE msg_ref = ?", (msg_ref,)).fetchone()
    if existing is not None:
        return {"inbound_id": existing[0], "effects": ["already_recorded"]}
    received = _ts(inbound.get("received_at") or canon.now(), "received_at")
    thread = _thread(conn, inbound.get("thread_key"), inbound.get("thread_id"))
    stamp = canon.now()
    company_id = inbound.get("company_id") or (thread["company_id"] if thread is not None else None)
    cur = conn.execute(
        "INSERT INTO inbound_messages (msg_ref, channel, thread_id, company_id, from_domain, received_at, code_class, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'classified', ?, ?)",
        (msg_ref, inbound.get("channel") or "email", thread["id"] if thread is not None else None, company_id,
         (inbound.get("from_domain") or "").lower() or None, received, cls, stamp, stamp))
    inbound_id = cur.lastrowid
    effects: list[str] = []
    reply_id = None
    if cls == "application_confirmation":
        app = None
        if inbound.get("job_id"):
            app = conn.execute("SELECT id FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                               (inbound["job_id"],)).fetchone()
        elif company_id:
            app = conn.execute("SELECT id FROM applications WHERE job_id IN (SELECT id FROM jobs WHERE company_id = ?) "
                               "AND confirmation_email_at IS NULL ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
        if app is not None:
            conn.execute("UPDATE applications SET confirmation_email_at = COALESCE(confirmation_email_at, ?), "
                         "updated_at = ? WHERE id = ?", (received, stamp, app[0]))
            effects.append("application_confirmed")
    elif thread is not None:
        summary = _clean(inbound.get("summary") or CODE_SUMMARIES[cls], 300)
        reply_id, new = _insert_reply(conn, thread["id"], received, cls, "code", summary, msg_ref)
        if new:
            effects = apply_class(conn, thread, cls, received, summary, inbound.get("return_date"), reply_id)
    log_event(conn, "reply_code_class", inbound_id=inbound_id, cls=cls, effects=effects)
    return {"inbound_id": inbound_id, "reply_id": reply_id, "thread_key": thread["thread_key"] if thread else None,
            "effects": effects}
