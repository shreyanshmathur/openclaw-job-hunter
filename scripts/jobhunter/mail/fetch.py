"""Inbound mail over IMAP (design 1.3.6, 1.3.7, 4.5 and minors 9 and 22).

fetch(conn, imap) looks at, in this order of priority per message:
  1. security emails: Google ("critical security alert", "unusual activity", "verify it's you", account
     disabled or suspended) trip the `gmail` breaker; LinkedIn security, verification, restriction or
     "action required" emails trip `linkedin` (only when LinkedIn is enabled);
  2. delivery failures (DSN) that name one of our Message-IDs: a hard bounce is recorded as code class
     `bounce` on that thread (U6 applies the consequences); delays are ignored;
  3. application confirmation emails for recent applications (subject or text such as "thank you for
     applying"), recorded as `application_confirmation` (and an `unknown` application found this way is
     reconciled to sent);
  4. replies to open email threads (from the recipient's address or the company's domain, since the first
     send): code pre-rules classify out-of-office and automatic acknowledgements; everything else becomes
     a reply packet for the replies lane (replies.write_packet, U6).
Every message handled is stored in inbound_messages (msg_ref = gm:<X-GM-MSGID hex>), so it is handled once.
IMAP work happens outside transactions; each message is recorded in its own short transaction.

On the web_ui route (the default) fetch() does nothing and says so (`handled_by: browser_lane`): the replies lane
reads replies and delivery failures in Gmail in the browser and records them with `reply record`, so reading the
same messages over IMAP too would record them twice under two references.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re

from .. import breakers, canon, companies, db, keys
from ..errors import Denied
from ..events import log_event
from . import MAILER_AGENT, MailError, browser_lane, is_web_route
from .imap import text_of

THREAD_MAX_AGE_DAYS = 60
APP_WINDOW_DAYS = 14
MAX_NEW = 50
MAX_TEXT_FETCH = 30
TERMS_PER_QUERY = 16
FIRST_LOOKBACK_DAYS = 3
OVERLAP_DAYS = 2

BOUNCE_FROM_RE = re.compile(r"^(mailer-daemon|postmaster|mail-daemon)@", re.I)
BOUNCE_SUBJECT_RE = re.compile(r"delivery status notification|undeliver(able|ed)|delivery (has )?failed|"
                               r"mail delivery (failed|subsystem)|returned mail|failure notice|"
                               r"address not found|message not delivered|delivery failure", re.I)
SOFT_BOUNCE_RE = re.compile(r"\bdelay(ed)?\b|will (keep|continue) trying|delivery incomplete|"
                            r"action:\s*delayed|status:\s*4\.\d", re.I)
HARD_BOUNCE_RE = re.compile(r"action:\s*failed|status:\s*5\.\d|\b55[0-4]\b|address not found|"
                            r"(user|mailbox|recipient|address) (unknown|not found|unavailable|does ?n[o']t exist)|"
                            r"no such (user|mailbox)|wasn'?t delivered|couldn'?t be delivered|"
                            r"permanent(ly)? (error|failure)", re.I)
OUR_MID_RE = re.compile(r"<?(T[A-Z2-7]{11})@jobhunter\.invalid>?")
FINAL_RCPT_RE = re.compile(r"(?:final|original)-recipient:\s*rfc822;\s*<?([^\s<>]+@[^\s<>]+)>?", re.I)
OOO_SUBJECT_RE = re.compile(r"^\s*(\[?auto(matic)?[- ]?(reply|response)\]?|out of (the )?office|ooo\b|"
                            r"away from (the |my )?office|on (annual |parental |maternity |paternity )?leave|"
                            r"vacation|holiday)", re.I)
OOO_TEXT_RE = re.compile(r"out of (the )?office|on (annual |parental |maternity |paternity |sick )?leave|"
                         r"on vacation|on holiday|away (from (the |my )?(office|desk)|until)|"
                         r"limited access to (my )?e-?mail|(back|return(ing)?) (in the office )?on|"
                         r"will be back", re.I)
ACK_SUBJECT_RE = re.compile(r"^\s*(auto(matic)?[- ]?(reply|response)|we('ve| have) received your|"
                            r"thank(s| you) for (contacting|your (e-?mail|message|note|enquiry|inquiry))|"
                            r"(message|e-?mail) received|received:)", re.I)
ACK_TEXT_RE = re.compile(r"this is an automat(ed|ic) (reply|response|message)|do not reply to this|"
                         r"we('ve| have) received your (e-?mail|message)|will (get back|respond|reply) to you "
                         r"(as soon as|shortly|within)", re.I)
CONFIRM_RE = re.compile(r"thank(s| you) for (applying|your application|your interest in)|"
                        r"application (received|submitted|confirmation|was submitted)|"
                        r"we('ve| have) (successfully )?received your application|"
                        r"your application (to|for|at|has been (received|submitted))|"
                        r"successfully (applied|submitted)|confirm(ing|ation of) your application", re.I)
REJECT_RE = re.compile(r"unfortunately|regret to|not (be )?(moving|move) forward|other candidates|"
                       r"not (been )?selected|decided (not )?to|position has been filled", re.I)
GOOGLE_SEC_DOMAIN = "accounts.google.com"
NOREPLY_RE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|news(letter)?|marketing|info|"
                        r"hello|updates?)([+._-][^@]*)?@", re.I)
GOOGLE_SEC_RE = re.compile(r"critical security alert|suspicious (sign-?in|activity)|unusual activity|"
                           r"verify it'?s you|(account|access) (has been |was )?(disabled|suspended|locked)", re.I)
LI_SEC_RE = re.compile(r"security|verif|restrict|unusual|action required", re.I)
CONFIRM_QUERY = ('{subject:application subject:applying subject:applied "thank you for applying" '
                 '"thanks for applying" "received your application"}')
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
_DATE_ISO_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_DATE_MD_RE = re.compile(r"\b([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?(?:,? (20\d\d))?\b")
_DATE_DM_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)? (?:of )?([A-Za-z]{3,9})\.?(?:,? (20\d\d))?\b")
_RETURN_KEY_RE = re.compile(r"(until|till|back (in the office )?on|return(ing)? (to the office )?on|"
                            r"back|returning|return)\b", re.I)


# ---------------------------------------------------------------- pure pre-rules
def gmail_date(ts: str) -> str:
    return ts[:10].replace("-", "/")


def is_bounce(h: dict) -> bool:
    if BOUNCE_FROM_RE.match(h.get("from_addr") or ""):
        return True
    ctype = h.get("content_type") or ""
    if "multipart/report" in ctype and "delivery-status" in ctype:
        return True
    return bool(BOUNCE_SUBJECT_RE.search(h.get("subject") or "")) and not h.get("in_reply_to")


def bounce_info(raw_text: str) -> dict:
    """{hard, token, recipient} from the text of a delivery failure notice."""
    text = raw_text or ""
    m = OUR_MID_RE.search(text)
    rc = FINAL_RCPT_RE.search(text)
    hard = bool(HARD_BOUNCE_RE.search(text)) and not (SOFT_BOUNCE_RE.search(text) and
                                                     not re.search(r"action:\s*failed|status:\s*5\.", text, re.I))
    return {"hard": hard, "token": m.group(1) if m else None, "recipient": rc.group(1).lower() if rc else None}


def is_auto(h: dict) -> bool:
    auto = h.get("auto_submitted") or ""
    return bool((auto and auto != "no") or h.get("x_autoreply") or
                h.get("precedence") in ("auto_reply", "auto-reply", "bulk", "junk"))


def return_date(text: str, received_at: str | None = None) -> str | None:
    """Best-effort return date from an out-of-office text (YYYY-MM-DD) or None."""
    if not text:
        return None
    base = canon.parse_ts(received_at) if received_at else canon.utcnow()
    for km in _RETURN_KEY_RE.finditer(text):
        window = text[km.end():km.end() + 60]
        d = _first_date(window, base)
        if d:
            return d
    return None


def _mk(y: int, mo: int, d: int, base: _dt.datetime, explicit_year: bool) -> str | None:
    try:
        cand = _dt.date(y, mo, d)
    except ValueError:
        return None
    if not explicit_year and cand < base.date() - _dt.timedelta(days=1):
        try:
            cand = _dt.date(y + 1, mo, d)
        except ValueError:
            return None
    if cand < base.date() - _dt.timedelta(days=1) or cand > base.date() + _dt.timedelta(days=370):
        return None
    return cand.isoformat()


def _first_date(window: str, base: _dt.datetime) -> str | None:
    best = None
    for rx in (_DATE_ISO_RE, _DATE_MD_RE, _DATE_DM_RE):
        m = rx.search(window)
        if not m:
            continue
        if rx is _DATE_ISO_RE:
            r = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)), base, True)
        elif rx is _DATE_MD_RE:
            mo = MONTHS.get(m.group(1).lower())
            r = _mk(int(m.group(3) or base.year), mo, int(m.group(2)), base, bool(m.group(3))) if mo else None
        else:
            mo = MONTHS.get(m.group(2).lower())
            r = _mk(int(m.group(3) or base.year), mo, int(m.group(1)), base, bool(m.group(3))) if mo else None
        if r and (best is None or m.start() < best[0]):
            best = (m.start(), r)
    return best[1] if best else None


def is_out_of_office(h: dict, text: str) -> bool:
    subj = h.get("subject") or ""
    if OOO_SUBJECT_RE.search(subj) and (OOO_TEXT_RE.search(text or "") or re.search(r"out of (the )?office|ooo\b",
                                                                                     subj, re.I)):
        return True
    return is_auto(h) and bool(OOO_TEXT_RE.search(text or ""))


def is_confirmation(subject: str, text: str = "") -> bool:
    blob = "%s\n%s" % (subject or "", text or "")
    return bool(CONFIRM_RE.search(blob)) and not REJECT_RE.search(blob)


def is_auto_ack(h: dict, text: str) -> bool:
    if REJECT_RE.search(text or "") and not is_auto(h):
        return False
    if ACK_SUBJECT_RE.search(h.get("subject") or ""):
        return True
    return is_auto(h) and bool(ACK_TEXT_RE.search(text or "") or not (text or "").strip())


def classify_reply(h: dict, text: str, application: bool = False) -> tuple[str | None, dict]:
    """Code pre-rules for a message in one of our threads: (code_class or None, extra)."""
    if is_out_of_office(h, text):
        return "out_of_office", {"return_date": return_date(text, h.get("date"))}
    if application and is_confirmation(h.get("subject") or "", text):
        return "application_confirmation", {}
    if is_auto_ack(h, text):
        return "auto_ack", {}
    return None, {}


# ---------------------------------------------------------------- database helpers
def _known_refs(conn, refs: list[str]) -> set:
    out = set()
    refs = [r for r in refs if r]
    for i in range(0, len(refs), 400):
        chunk = refs[i:i + 400]
        out |= {r[0] for r in conn.execute("SELECT msg_ref FROM inbound_messages WHERE msg_ref IN (%s)"
                                           % ",".join("?" * len(chunk)), chunk)}
    return out


def record_ignored(conn, h: dict, company_id=None, thread_id=None, why: str = "") -> None:
    ts = canon.now()
    conn.execute("INSERT INTO inbound_messages (msg_ref, channel, thread_id, company_id, from_domain, received_at, "
                 "code_class, status, created_at, updated_at) VALUES (?, 'email', ?, ?, ?, ?, NULL, 'ignored', ?, ?) "
                 "ON CONFLICT (msg_ref) DO NOTHING",
                 (h["msg_ref"], thread_id, company_id, _domain(h.get("from_addr")), h.get("date") or ts, ts, ts))
    if why:
        log_event(conn, "mail_ignored", msg_ref=h["msg_ref"], why=why)


def _domain(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].strip().lower() or None


def open_threads(conn) -> list[dict]:
    """Email threads the fetch watches: open or followed up, first send in the last 60 days."""
    since = canon.ts_add(canon.now(), days=-THREAD_MAX_AGE_DAYS)
    rows = conn.execute(
        "SELECT t.id, t.thread_key, t.company_id, t.job_id, t.first_message_id, t.state, t.followup_action_id, "
        "a.recipient AS recipient, COALESCE(a.sent_at, a.reserved_at) AS first_sent_at, a.message_id AS first_mid, "
        "a.kind AS first_kind, co.domain AS company_domain, fa.message_id AS followup_mid "
        "FROM threads t JOIN actions a ON a.id = t.first_action_id "
        "LEFT JOIN companies co ON co.id = t.company_id LEFT JOIN actions fa ON fa.id = t.followup_action_id "
        "WHERE t.channel = 'email' AND t.state IN ('open','followed_up') "
        "AND COALESCE(t.last_outbound_at, t.created_at) >= ? ORDER BY t.id", (since,)).fetchall()
    out = []
    for r in rows:
        rec = (r["recipient"] or "").strip().lower()
        dom = keys.company_domain(r["company_domain"]) if r["company_domain"] else None
        dom = dom or keys.company_domain(rec)
        mids = {m for m in (r["first_message_id"], r["first_mid"], r["followup_mid"]) if m}
        out.append({"id": r["id"], "thread_key": r["thread_key"], "company_id": r["company_id"], "job_id": r["job_id"],
                    "recipient": rec or None, "domain": dom, "first_sent_at": r["first_sent_at"],
                    "message_ids": mids, "application": r["first_kind"] == "application_email"})
    return out


def recent_applications(conn, limit: int = 20) -> list[dict]:
    since = canon.ts_add(canon.now(), days=-APP_WINDOW_DAYS)
    rows = conn.execute(
        "SELECT a.id, a.token, a.kind, a.status, a.company_id, a.job_id, "
        "COALESCE(a.armed_at, a.reserved_at) AS at FROM actions a LEFT JOIN applications ap ON ap.action_id = a.id "
        "WHERE a.kind IN ('application','application_email') AND a.status IN ('sent','unknown','armed') "
        "AND a.company_id IS NOT NULL AND COALESCE(a.armed_at, a.reserved_at) >= ? "
        "AND (ap.id IS NULL OR ap.confirmation_email_at IS NULL) ORDER BY a.id DESC LIMIT ?",
        (since, int(limit))).fetchall()
    return [dict(r) for r in rows]


def _match_thread(h: dict, threads: list[dict]) -> tuple[dict | None, str | None]:
    """(thread, how): how is 'reference' (In-Reply-To or References name our message), 'address' (from the
    person we wrote to) or 'domain' (someone else at the company), each only after our first send."""
    refs = set(h.get("in_reply_to") or []) | set(h.get("references") or [])
    for t in threads:
        if refs & t["message_ids"]:
            return t, "reference"
    frm = (h.get("from_addr") or "").lower()
    date = h.get("date") or canon.now()

    def after_first(t):
        try:
            return canon.seconds_between(t["first_sent_at"], date) >= -600
        except (TypeError, ValueError):
            return False
    same = [t for t in threads if t["recipient"] and t["recipient"] == frm and after_first(t)]
    if same:
        return same[-1], "address"
    dom = keys.company_domain(frm)
    if dom:
        same = [t for t in threads if t["domain"] == dom and after_first(t)]
        if same:
            return same[-1], "domain"
    return None, None


def is_bulk(h: dict) -> bool:
    """Newsletters, notifications and no-reply senders: never a reply from a person."""
    return bool(h.get("list_id") or h.get("list_unsubscribe") or
                h.get("precedence") in ("bulk", "list", "junk") or NOREPLY_RE.match(h.get("from_addr") or ""))


# ---------------------------------------------------------------- searches
def _batches(terms: list[str], n: int = TERMS_PER_QUERY):
    for i in range(0, len(terms), n):
        yield terms[i:i + n]


def _thread_queries(threads: list[dict]) -> list[str]:
    by_term: dict[str, str] = {}
    for t in threads:
        first = t["first_sent_at"] or canon.now()
        for term in ([("from:" + t["recipient"])] if t["recipient"] else []) + \
                    ([("from:" + t["domain"])] if t["domain"] else []):
            if term not in by_term or first < by_term[term]:
                by_term[term] = first
    items = sorted(by_term.items(), key=lambda kv: kv[1])
    out = []
    for batch in _batches(items):
        since = min(v for _k, v in batch)
        out.append("after:%s -in:sent {%s}" % (gmail_date(canon.ts_add(since, days=-1)),
                                              " ".join(k for k, _v in batch)))
    return out


def _since(conn, since: str | None) -> str:
    if since:
        return canon.ts_add(since, days=-OVERLAP_DAYS)
    last = db.meta_get(conn, "mail_fetch_last_at")
    if last:
        try:
            return canon.ts_add(last, days=-OVERLAP_DAYS)
        except ValueError:
            pass
    return canon.ts_add(canon.now(), days=-FIRST_LOOKBACK_DAYS)


def _linkedin_enabled(conn) -> bool:
    return (db.meta_get(conn, "channel_linkedin_enabled") or "0") == "1"


# ---------------------------------------------------------------- fetch
def fetch(conn, imap, since: str | None = None, owner_addr: str | None = None) -> dict:
    """One fetch pass (see module doc). Manages its own short transactions (IMAP never runs inside one).
    Returns {fetched, replies_classified, packets_written, confirmations_seen, bounces, ignored, tripped}; on the
    web_ui route the same counts at 0 plus the browser_lane() keys, and IMAP is not touched."""
    from .. import config as _config
    from .. import replies
    stats = {"fetched": 0, "replies_classified": 0, "packets_written": 0, "confirmations_seen": 0, "bounces": 0,
             "ignored": 0, "tripped": [], "errors": []}
    if is_web_route(_config.load(conn)):
        stats.update(browser_lane("fetch"))
        return stats
    owner = (owner_addr or getattr(imap, "account", "") or "").lower()
    start = _since(conn, since)
    threads = open_threads(conn)
    apps = recent_applications(conn)
    ctx: dict[str, dict] = {}     # uid -> {"kinds": set(), "apps": [..]}

    def add(uids, kind, app=None):
        for u in uids:
            c = ctx.setdefault(u, {"kinds": set(), "apps": []})
            c["kinds"].add(kind)
            if app is not None:
                c["apps"].append(app)

    # 1. security emails
    add(imap.search('after:%s from:(%s) {"critical security alert" "suspicious" "unusual activity" '
                    '"verify it\'s you" "disabled" "suspended"}' % (gmail_date(start), GOOGLE_SEC_DOMAIN)), "google_sec")
    if _linkedin_enabled(conn):
        add(imap.search('after:%s from:(linkedin.com) {subject:security subject:verify subject:verification '
                        'subject:restricted subject:unusual "action required"}' % gmail_date(start)), "li_sec")
    # 2. delivery failures (only when we sent something recently)
    if conn.execute("SELECT 1 FROM actions WHERE route = 'mailer' AND status IN ('sent','unknown','armed') "
                    "AND reserved_at >= ?", (canon.ts_add(canon.now(), days=-7),)).fetchone():
        add(imap.search("after:%s {from:mailer-daemon from:postmaster}" % gmail_date(start)), "bounce")
    # 3. application confirmations
    for app in apps:
        q = companies.precheck_query(conn, app["company_id"])
        if not q or q == "()":
            continue
        add(imap.search("after:%s %s %s" % (gmail_date(canon.ts_add(app["at"], days=-1)), q, CONFIRM_QUERY)),
            "confirm", app)
    # 4. thread replies
    for q in _thread_queries(threads):
        add(imap.search(q), "thread")
    if not ctx:
        _stamp(conn)
        return stats
    uids = sorted(ctx, key=int)
    heads = imap.fetch_headers(uids[-(MAX_NEW * 3):])
    known = _known_refs(conn, [h["msg_ref"] for h in heads])
    fresh = [h for h in heads if h["msg_ref"] and h["msg_ref"] not in known
             and (h.get("from_addr") or "").lower() != owner][:MAX_NEW]
    stats["fetched"] = len(fresh)
    texts_left = [MAX_TEXT_FETCH]

    def text(h):
        if texts_left[0] <= 0:
            return ""
        texts_left[0] -= 1
        return imap.fetch_text(h["uid"], 4000)

    for h in fresh:
        kinds = ctx[str(h["uid"])]["kinds"]
        try:
            if "google_sec" in kinds or "li_sec" in kinds:
                _security(conn, h, kinds, stats)
            elif "bounce" in kinds and is_bounce(h):
                _bounce(conn, imap, h, stats, replies)
            elif "confirm" in kinds and _confirmation(conn, h, ctx[str(h["uid"])]["apps"], text, stats, replies):
                pass
            elif "thread" in kinds:
                _reply(conn, h, threads, text, stats, replies)
            else:
                with db.tx(conn):
                    record_ignored(conn, h, why="not ours")
                stats["ignored"] += 1
        except MailError:
            raise
        except Denied as d:
            stats["errors"].append({"msg_ref": h["msg_ref"], "code": d.code, "message": d.message})
    _stamp(conn)
    return stats


def _stamp(conn) -> None:
    with db.tx(conn):
        db.meta_set(conn, "mail_fetch_last_at", canon.now(), "system")


def _security(conn, h: dict, kinds: set, stats: dict) -> None:
    subj = h.get("subject") or ""
    with db.tx(conn):
        if "google_sec" in kinds and (h.get("from_addr") or "").lower().endswith("@" + GOOGLE_SEC_DOMAIN) and \
                GOOGLE_SEC_RE.search(subj):
            breakers.trip(conn, "gmail", "gmail_security", "Google security email: %s" % subj[:200], by="mailer")
            stats["tripped"].append("gmail")
        elif "li_sec" in kinds and (h.get("from_addr") or "").lower().endswith("linkedin.com") and LI_SEC_RE.search(subj):
            breakers.trip(conn, "linkedin", "li_security_email", "LinkedIn security email: %s" % subj[:200], by="mailer")
            stats["tripped"].append("linkedin")
        record_ignored(conn, h, why="security email")
    stats["ignored"] += 1


def _bounce(conn, imap, h: dict, stats: dict, replies) -> None:
    msg = imap.fetch_message(h["uid"], 65536)
    raw = ""
    if msg is not None:
        try:
            raw = msg.as_string()
        except Exception:
            raw = text_of(msg, 20000)
    info = bounce_info(raw)
    thread_key = None
    if info["token"]:
        row = conn.execute("SELECT thread_key, kind, token FROM actions WHERE token = ?", (info["token"],)).fetchone()
        if row is not None:
            thread_key = row["thread_key"] or ("em:" + row["token"])
    if thread_key is None and info["recipient"]:
        row = conn.execute("SELECT t.thread_key FROM threads t JOIN actions a ON a.id = t.first_action_id "
                           "WHERE t.channel = 'email' AND a.recipient = ? ORDER BY t.id DESC LIMIT 1",
                           (info["recipient"],)).fetchone()
        thread_key = row[0] if row else None
    with db.tx(conn):
        if not info["hard"] or thread_key is None or \
                conn.execute("SELECT 1 FROM threads WHERE thread_key = ?", (thread_key,)).fetchone() is None:
            record_ignored(conn, h, why="soft bounce" if not info["hard"] else "bounce for an unknown message")
            stats["ignored"] += 1
            return
        replies.record_code_class(conn, {"msg_ref": h["msg_ref"], "code_class": "bounce",
                                         "received_at": h.get("date") or canon.now(), "thread_key": thread_key,
                                         "from_domain": _domain(h.get("from_addr")),
                                         "summary": "Delivery failed%s." % (" to " + info["recipient"]
                                                                            if info["recipient"] else "")})
    stats["bounces"] += 1
    stats["replies_classified"] += 1


def _confirmation(conn, h: dict, apps: list, text, stats: dict, replies) -> bool:
    date = h.get("date") or canon.now()
    cands = [a for a in apps if canon.seconds_between(a["at"], date) >= -600]
    if not cands:
        return False
    if not is_confirmation(h.get("subject") or "", text(h)):   # the text too: "Your application" can be a no
        return False
    app = cands[0]
    with db.tx(conn):
        replies.record_code_class(conn, {"msg_ref": h["msg_ref"], "code_class": "application_confirmation",
                                         "received_at": date, "company_id": app["company_id"],
                                         "job_id": app["job_id"], "from_domain": _domain(h.get("from_addr"))})
        if app["status"] == "unknown":
            from .. import reconcile
            try:
                reconcile.record_check(conn, app["token"], "inbox_confirmation", "found",
                                       "confirmation email on %s: %s" % (date, (h.get("subject") or "")[:200]),
                                       by=MAILER_AGENT)
            except Denied:
                pass
    stats["confirmations_seen"] += 1
    return True


def _reply(conn, h: dict, threads: list, text, stats: dict, replies) -> None:
    t, how = _match_thread(h, threads)
    if t is None or (how == "domain" and is_bulk(h)):
        with db.tx(conn):
            record_ignored(conn, h, company_id=t["company_id"] if t else None,
                           why="no matching thread" if t is None else "bulk mail from the company domain")
        stats["ignored"] += 1
        return
    body = text(h)
    cls, extra = classify_reply(h, body, application=t["application"])
    with db.tx(conn):
        if cls is not None:
            payload = {"msg_ref": h["msg_ref"], "code_class": cls, "received_at": h.get("date") or canon.now(),
                       "thread_key": t["thread_key"], "company_id": t["company_id"],
                       "from_domain": _domain(h.get("from_addr"))}
            if cls == "out_of_office" and extra.get("return_date"):
                payload["return_date"] = extra["return_date"]
            if cls == "application_confirmation":
                payload["job_id"] = t["job_id"]
                stats["confirmations_seen"] += 1
            replies.record_code_class(conn, payload)
            stats["replies_classified"] += 1
        else:
            replies.write_packet(conn, {"msg_ref": h["msg_ref"], "thread_key": t["thread_key"], "channel": "email",
                                        "company_id": t["company_id"], "from_domain": _domain(h.get("from_addr")),
                                        "received_at": h.get("date") or canon.now(),
                                        "subject": h.get("subject") or ""}, body)
            stats["packets_written"] += 1
