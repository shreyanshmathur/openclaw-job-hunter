"""Sent-folder audit and history import over IMAP (design 4.5 nightly audit, 8 step 12).

sent_since(conn, days): Sent messages of the last `days` days that went to a person or company the job hunter
knows (a stored contact address or a company domain) and match no ledger action: not our Message-ID, no live
action to that address or company within 3 days, and not part of a conversation the person took over (a
thread in state replied, or a company in active_thread). Mail to anyone else is none of the audit's business
and is not reported. U1's audit_run trips `global` (audit_mismatch) on any result.

import_history(conn, days): records every address the person wrote to in the last `days` days as an
`imported` action (gate.import_actions), so person and company rules apply from day one. Bulk messages (more
than 10 recipients), no-reply style addresses and the person's own address are skipped.

web_ui route (the default; no IMAP unless the person also connected an app password):
- The replies lane reads the Sent folder in the browser once a day (web_status says when it is due and which
  search to run) and records what it saw with `mail audit --file` (record_web_read, purpose `audit`). The
  audit rule above runs over those rows at once (a mismatch runs U1's audit, which trips `global`), and
  sent_since() re-applies it to the stored rows at the nightly audit, so a message the person has since
  recorded is no longer reported. With no browser read in the last 36 hours sent_since() raises
  E_PRECONDITION, which U1's audit reports as an error: a missing audit never looks like a clean one.
- `mail import-history` records a request (request_web_history); the replies lane reads the Sent folder of
  those days in the browser and records it with purpose `history`, which imports it exactly as the IMAP
  import does. A read that says complete closes the request.
- web_status also hands the replies lane the delivery-failure search and the recently emailed threads with
  their recipients (bounce_check), so a failure notice seen in the browser can be recorded on its thread.
The browser reads live in state/mail-web.json (mode 600), never in the Sheet.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile

from .. import canon, db, gate, keys, paths
from ..errors import Denied
from . import browser_lane, is_connected, is_web_route, imap_client, owner_address, valid_address
from .fetch import gmail_date

LEDGER_WINDOW_DAYS = 3
MAX_AUDIT_MESSAGES = 500
BULK_RECIPIENTS = 10
IMPORT_CHUNK = 200
NOREPLY_RE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|mailer-daemon|postmaster|bounce[s]?)"
                        r"([+._-].*)?@", re.I)
LIVE_SQL = "('reserved','armed','sent','failed_after_click','unknown','imported')"
WEB_FILE = "mail-web.json"
WEB_AUDIT_DAYS = 2
WEB_AUDIT_EVERY_H = 20          # the replies lane reads Sent again after this many hours
WEB_AUDIT_MAX_AGE_H = 36        # older browser reads do not count for the nightly audit
WEB_READ_MAX_AGE_H = 6          # observed_at of a recorded read
WEB_MAX_MESSAGES = 300
WEB_HISTORY_MAX_DAYS = 365
WEB_READ_KEYS = {"purpose", "observed_at", "query", "complete", "messages"}
WEB_MSG_KEYS = {"date", "to", "cc", "subject", "url"}
WEB_SOURCE = "gmail_web_history"
WEB_BOUNCE_DAYS = 7             # threads whose first message is this recent are watched for failure notices
WEB_BOUNCE_SEARCH_DAYS = 3
WEB_BOUNCE_THREADS = 50
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _known_address(conn, addr: str) -> tuple[bool, int | None]:
    """(known, company_id): the address is a stored contact, or its domain is a company domain."""
    norm = keys.normalize_email(addr)
    row = conn.execute("SELECT c.id, c.company_id FROM contacts c WHERE lower(c.email) = ? OR c.id IN "
                       "(SELECT contact_id FROM contact_keys WHERE key IN (?, ?)) LIMIT 1",
                       (addr, "email:" + addr, "email_norm:" + norm)).fetchone()
    if row is not None:
        return True, row["company_id"]
    dom = keys.company_domain(addr)
    if dom:
        r = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", ("dom:" + dom,)).fetchone()
        if r is not None:
            return True, r[0]
        r = conn.execute("SELECT id FROM companies WHERE lower(domain) = ? AND merged_into IS NULL LIMIT 1",
                         (dom,)).fetchone()
        if r is not None:
            return True, r[0]
    return False, None


def _human_conversation(conn, addr: str, company_id) -> bool:
    if company_id is not None:
        r = conn.execute("SELECT contact_state FROM companies WHERE id = ?", (company_id,)).fetchone()
        if r is not None and r[0] == "active_thread":
            return True
    r = conn.execute("SELECT 1 FROM threads t JOIN actions a ON a.id = t.first_action_id WHERE a.recipient = ? "
                     "AND (t.state = 'replied' OR t.needs_human = 1)", (addr,)).fetchone()
    return r is not None


def _ledgered(conn, addr: str, company_id, date: str) -> bool:
    lo = canon.ts_add(date, days=-LEDGER_WINDOW_DAYS)
    hi = canon.ts_add(date, days=LEDGER_WINDOW_DAYS)
    r = conn.execute("SELECT 1 FROM actions WHERE status IN %s AND platform = 'gmail' AND recipient = ? "
                     "AND COALESCE(sent_at, reserved_at) BETWEEN ? AND ? LIMIT 1" % LIVE_SQL, (addr, lo, hi)).fetchone()
    if r is not None:
        return True
    if company_id is not None:
        r = conn.execute("SELECT 1 FROM actions WHERE status IN %s AND platform = 'gmail' AND company_id = ? "
                         "AND COALESCE(sent_at, reserved_at) BETWEEN ? AND ? LIMIT 1" % LIVE_SQL,
                         (company_id, lo, hi)).fetchone()
        if r is not None:
            return True
    return False


def unledgered(conn, headers: list[dict], owner: str) -> list[dict]:
    """The audit rule over parsed Sent headers (imap.parse_headers dicts)."""
    owner = (owner or "").lower()
    mids = [h["message_id"] for h in headers if h.get("message_id")]
    ours = set()
    for i in range(0, len(mids), 400):
        chunk = mids[i:i + 400]
        ours |= {r[0] for r in conn.execute("SELECT message_id FROM actions WHERE message_id IN (%s)"
                                            % ",".join("?" * len(chunk)), chunk)}
    out = []
    for h in headers:
        if h.get("message_id") and h["message_id"] in ours:
            continue
        date = h.get("date") or canon.now()
        for addr in sorted(set((h.get("to") or []) + (h.get("cc") or [])) - {owner}):
            known, company_id = _known_address(conn, addr)
            if not known:
                continue
            if _human_conversation(conn, addr, company_id) or _ledgered(conn, addr, company_id, date):
                continue
            out.append({"date": date, "to": addr, "subject": (h.get("subject") or "")[:120],
                        "message_id": h.get("message_id"), "company_id": company_id})
    return out


def _sent_headers(imap, days: int, limit: int | None = None) -> list[dict]:
    since = canon.ts_add(canon.now(), days=-int(days))
    uids = imap.search("in:sent after:%s" % gmail_date(since))
    if limit:
        uids = uids[-limit:]
    return imap.fetch_headers(uids)


def sent_since(conn, days: int, imap=None) -> list[dict]:
    """Unledgered Sent messages (see module doc). Over IMAP when mail is connected (either route); on the
    web_ui route without a connection, from the latest browser read (E_PRECONDITION when there is none in
    the last 36 hours); [] on the app_password route before `mail connect`. Only reads the database, so it
    is safe inside the caller's transaction; the IMAP session is opened and closed here."""
    from .. import config as _config
    if imap is None and not is_connected(conn):
        cfg = _config.load(conn)
        if is_web_route(cfg):
            return web_sent_since(conn, days, cfg)
        return []
    owner = owner_address(_config.load(conn))
    own = imap is None
    if own:
        imap = imap_client()
        imap.open()
    try:
        heads = _sent_headers(imap, days, MAX_AUDIT_MESSAGES)
    finally:
        if own:
            imap.close()
    return unledgered(conn, heads, owner)


def import_items(headers: list[dict], owner: str) -> list[dict]:
    """actions import items (12.20 shape) for Sent headers, oldest first."""
    owner = (owner or "").lower()
    items = []
    for h in sorted(headers, key=lambda x: x.get("date") or ""):
        rcpts = [a for a in (h.get("to") or []) if a and a != owner]
        allr = set(rcpts) | set(h.get("cc") or [])
        if not rcpts or len(allr) > BULK_RECIPIENTS:
            continue
        for addr in rcpts:
            if NOREPLY_RE.match(addr) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
                continue
            items.append({"kind": "cold_email", "platform": "gmail", "recipient": addr,
                          "sent_at": h.get("date") or canon.now(),
                          "evidence": "Gmail Sent %s: %s" % ((h.get("date") or "")[:10], (h.get("subject") or "")[:80])})
    return items


def import_history(conn, days: int, imap=None) -> dict:
    """`mail import-history --days <n>` (human). Manages its own transactions."""
    if int(days) < 1 or int(days) > 3650:
        raise Denied("E_VALIDATION", "--days must be between 1 and 3650")
    from .. import config as _config
    owner = owner_address(_config.load(conn))
    own = imap is None
    if own:
        imap = imap_client()
        imap.open()
    try:
        heads = _sent_headers(imap, days)
    finally:
        if own:
            imap.close()
    mids = {r[0] for r in conn.execute("SELECT message_id FROM actions WHERE message_id IS NOT NULL")}
    heads = [h for h in heads if not (h.get("message_id") and h["message_id"] in mids)]
    items = import_items(heads, owner)
    imported = skipped = 0
    for i in range(0, len(items), IMPORT_CHUNK):
        chunk = items[i:i + IMPORT_CHUNK]
        with db.tx(conn):
            res = gate.import_actions(conn, chunk, "gmail_imap_history")
        imported += len(res.get("imported") or [])
        skipped += len(res.get("skipped") or [])
    with db.tx(conn):
        db.log_event(conn, "mail_history_imported", days=int(days), messages=len(heads), imported=imported,
                     skipped=skipped)
    return {"messages": len(heads), "addresses": len(items), "imported": imported, "skipped": skipped}


# ---------------------------------------------------------------- web_ui route: browser reads of the Sent folder
def _web_path() -> str:
    return os.path.join(paths.state_dir(), WEB_FILE)


def load_web() -> dict:
    try:
        with open(_web_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_web(data: dict) -> None:
    """Atomic write, mode 600 (the file holds addresses and subjects)."""
    d = paths.state_dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mail-web-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, _web_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _owner_or_blank(cfg: dict) -> str:
    try:
        return owner_address(cfg)
    except Denied:
        return ""


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return canon.seconds_between(ts, canon.now()) / 3600.0
    except ValueError:
        return None


def web_status(conn, cfg: dict | None = None) -> dict:
    """What the browser lane owes on the web_ui route: {route, handled_by, audit_due, audit_query, last_audit_at,
    history_scan: None | {days, query, requested_at}, bounce_check: None | {query, threads}, imap_connected}.
    With the optional IMAP connection the audit runs over IMAP and audit_due is false."""
    from .. import config as _config
    cfg = cfg if cfg is not None else _config.load(conn)
    data = load_web()
    last = (data.get("audit") or {}).get("observed_at")
    connected = is_connected(conn)
    age = _hours_since(last)
    out = dict(browser_lane("audit"))
    out.update({"audit_due": (not connected) and (age is None or age >= WEB_AUDIT_EVERY_H),
                "audit_query": "in:sent newer_than:%dd" % WEB_AUDIT_DAYS, "last_audit_at": last,
                "history_scan": None, "bounce_check": None, "imap_connected": connected})
    if not is_web_route(cfg):
        out.update(route="app_password", handled_by="code_imap", audit_due=False,
                   message="gmail.route is app_password: code reads the Sent folder over IMAP")
        return out
    out["bounce_check"] = web_bounce_check(conn)
    req = data.get("history_request")
    if isinstance(req, dict) and req.get("days"):
        since = canon.ts_add(req.get("requested_at") or canon.now(), days=-int(req["days"]))
        out["history_scan"] = {"days": int(req["days"]), "requested_at": req.get("requested_at"),
                               "query": "in:sent after:%s" % gmail_date(since)}
    return out


def web_bounce_check(conn) -> dict | None:
    """The delivery-failure search the replies lane runs in the browser, with the email threads whose first
    message went out in the last WEB_BOUNCE_DAYS days (a failure notice names the address; the thread with that
    recipient is the one to record `bounce` on). None when nothing went out recently (as the IMAP fetch)."""
    from .fetch import open_threads
    since = canon.ts_add(canon.now(), days=-WEB_BOUNCE_DAYS)
    threads = [{"thread_key": t["thread_key"], "recipient": t["recipient"]} for t in open_threads(conn)
               if t["recipient"] and (t["first_sent_at"] or "") >= since][-WEB_BOUNCE_THREADS:]
    if not threads:
        return None
    return {"query": "{from:mailer-daemon from:postmaster} newer_than:%dd" % WEB_BOUNCE_SEARCH_DAYS,
            "threads": threads}


def request_web_history(conn, days: int) -> dict:
    """`mail import-history` on the web_ui route without IMAP: ask the replies lane for a Sent-folder read."""
    days = int(days)
    if days < 1 or days > WEB_HISTORY_MAX_DAYS:
        raise Denied("E_VALIDATION", "on the web_ui route --days must be between 1 and %d" % WEB_HISTORY_MAX_DAYS)
    with db.file_lock():
        data = load_web()
        data["history_request"] = {"days": days, "requested_at": canon.now()}
        _save_web(data)
    with db.tx(conn):
        db.log_event(conn, "mail_web_history_requested", days=days)
    return web_status(conn)


def _web_date(v, i: int) -> str:
    if isinstance(v, str) and _DATE_ONLY_RE.match(v.strip()):
        v = v.strip() + "T12:00:00Z"
    try:
        return canon.fmt_ts(canon.parse_ts(v))
    except ValueError:
        raise Denied("E_VALIDATION", "messages[%d].date must be a UTC timestamp or YYYY-MM-DD" % i)


def _web_addrs(v, i: int, name: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or len(v) > 50:
        raise Denied("E_VALIDATION", "messages[%d].%s must be a list of addresses" % (i, name))
    out = []
    for a in v:
        a = a.strip().lower() if isinstance(a, str) else ""
        if not valid_address(a):
            raise Denied("E_VALIDATION", "messages[%d].%s holds %r, which is not an email address (open the "
                         "message and read the address, not the display name)" % (i, name, a[:80]))
        out.append(a)
    return out


def parse_web_read(payload) -> dict:
    """Validate a browser read of the Sent folder (the `mail audit --file` contract) and return
    {purpose, observed_at, complete, headers}; headers have the imap.parse_headers shape used above."""
    if not isinstance(payload, dict):
        raise Denied("E_SCHEMA", "the Sent read must be a JSON object")
    extra = set(payload) - WEB_READ_KEYS
    if extra:
        raise Denied("E_SCHEMA", "unknown keys: %s" % ", ".join(sorted(extra)))
    purpose = payload.get("purpose", "audit")
    if purpose not in ("audit", "history"):
        raise Denied("E_VALIDATION", "purpose must be audit or history")
    try:
        observed = canon.fmt_ts(canon.parse_ts(payload.get("observed_at")))
        age = canon.seconds_between(observed, canon.now())
    except ValueError:
        raise Denied("E_VALIDATION", "observed_at must be a UTC timestamp")
    if age > WEB_READ_MAX_AGE_H * 3600 or age < -300:
        raise Denied("E_VALIDATION", "observed_at is not recent")
    complete = payload.get("complete", True)
    if not isinstance(complete, bool):
        raise Denied("E_VALIDATION", "complete must be true or false")
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        raise Denied("E_SCHEMA", "messages must be a list (empty when the search found nothing)")
    if len(msgs) > WEB_MAX_MESSAGES:
        raise Denied("E_VALIDATION", "at most %d messages per file; record the rest in another file"
                     % WEB_MAX_MESSAGES)
    heads = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise Denied("E_SCHEMA", "messages[%d] must be an object" % i)
        extra = set(m) - WEB_MSG_KEYS
        if extra:
            raise Denied("E_SCHEMA", "messages[%d]: unknown keys %s" % (i, ", ".join(sorted(extra))))
        to = _web_addrs(m.get("to"), i, "to")
        if not to:
            raise Denied("E_VALIDATION", "messages[%d].to is required" % i)
        subject = m.get("subject") or ""
        url = m.get("url")
        if not isinstance(subject, str) or (url is not None and not (isinstance(url, str) and
                                                                     url.startswith("https://mail.google.com/"))):
            raise Denied("E_VALIDATION", "messages[%d]: subject must be text and url a mail.google.com link" % i)
        heads.append({"message_id": None, "to": to, "cc": _web_addrs(m.get("cc"), i, "cc"),
                      "date": _web_date(m.get("date"), i), "subject": subject.strip()[:200], "url": url})
    return {"purpose": purpose, "observed_at": observed, "complete": complete, "headers": heads}


def _in_window(heads: list[dict], days: int) -> list[dict]:
    since = canon.ts_add(canon.now(), days=-int(days))
    return [h for h in heads if (h.get("date") or "") >= since]


def web_sent_since(conn, days: int, cfg: dict | None = None) -> list[dict]:
    """The audit rule over the latest browser read of the Sent folder (web_ui route without IMAP)."""
    from .. import config as _config
    cfg = cfg if cfg is not None else _config.load(conn)
    rec = load_web().get("audit") or {}
    age = _hours_since(rec.get("observed_at"))
    if age is None or age > WEB_AUDIT_MAX_AGE_H:
        lane = browser_lane("audit")
        raise Denied("E_PRECONDITION", "%s; no browser read of the Sent folder in the last %d hours (last: %s)"
                     % (lane["message"], WEB_AUDIT_MAX_AGE_H, rec.get("observed_at") or "never"), data=lane)
    return unledgered(conn, _in_window(rec.get("headers") or [], days), _owner_or_blank(cfg))


def record_web_read(conn, payload, days: int = WEB_AUDIT_DAYS) -> dict:
    """`mail audit --file` (web_ui route). purpose audit: store the rows and apply the audit rule now; any
    unledgered row runs U1's audit at once (it trips `global`). purpose history (only while the owner's
    request is pending): import the rows as the IMAP history import does. Manages its own transactions."""
    from .. import config as _config
    cfg = _config.load(conn)
    if not is_web_route(cfg):
        raise Denied("E_ROUTE_UNAVAILABLE", "gmail.route is app_password: code reads the Sent folder over IMAP")
    read = parse_web_read(payload)
    owner = _owner_or_blank(cfg)
    heads = read["headers"]
    if read["purpose"] == "history":
        if not isinstance(load_web().get("history_request"), dict):
            raise Denied("E_PRECONDITION", "no Sent history read is pending; the owner asks for one with "
                         "./jobhunter mail import-history --days <n>")
        items = import_items(heads, owner)
        imported = skipped = 0
        for i in range(0, len(items), IMPORT_CHUNK):
            with db.tx(conn):
                res = gate.import_actions(conn, items[i:i + IMPORT_CHUNK], WEB_SOURCE)
            imported += len(res.get("imported") or [])
            skipped += len(res.get("skipped") or [])
        with db.file_lock():
            data = load_web()
            if read["complete"]:
                data.pop("history_request", None)
            data["history_last"] = {"observed_at": read["observed_at"], "messages": len(heads),
                                    "imported": imported, "complete": read["complete"]}
            _save_web(data)
        with db.tx(conn):
            db.log_event(conn, "mail_history_imported", source=WEB_SOURCE, messages=len(heads), imported=imported,
                         skipped=skipped)
        return {"purpose": "history", "messages": len(heads), "addresses": len(items), "imported": imported,
                "skipped": skipped, "complete": read["complete"]}
    with db.file_lock():
        data = load_web()
        data["audit"] = {"observed_at": read["observed_at"], "recorded_at": canon.now(),
                         "complete": read["complete"], "headers": heads}
        _save_web(data)
    rows = unledgered(conn, _in_window(heads, days), owner)
    out = {"purpose": "audit", "messages": len(heads), "unledgered": rows, "complete": read["complete"],
           "tripped": False}
    if rows:
        from .. import audit as core_audit
        res = core_audit.run(conn, days, sent_since=lambda _c, _d: rows)
        out["tripped"] = bool(res.get("mismatches"))
    else:
        with db.tx(conn):
            db.log_event(conn, "mail_web_audit", messages=len(heads), unledgered=0)
    return out
