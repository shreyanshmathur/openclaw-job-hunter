"""Outreach queue (design 6.1 steps 1 and 9, 12.10 U6): who the outreach lane may research next, target
skips, and the history-scan import (`actions import`).

Code picks targets before any research is spent, in this order:
  0. accepted LinkedIn invitations whose post-accept message is due (kind post_accept_message)
  a. people named on the posting of an eligible or applied job (hiring manager, post author, founder)
  b. job-post email addresses of those jobs (kind job_email; only when the job is not applied to by email)
  c. recruiters named on a posting
and drops targets that fail exclusions (do-not-contact person or company), company cooldown or agency caps,
active threads, target_skips, the per-job contact limit, an open first-touch draft for the person or the
company, or any existing first touch to the person on any channel. `gate reserve` re-checks everything.

When the email finder (U10, jobhunter.enrich) is installed, each email-capable target carries an `address`
hint for research step 5: have_address, pattern_available (two published addresses prove the domain's
pattern), enrich_possible (`enrich find` may look the person up), enrich_done_no_address (a lookup already
ran without a usable address) or no_domain. Without the finder the key is left out.
"""
from __future__ import annotations

import re

import importlib

from . import canon
from .errors import Denied
from .events import log_event
from .threads import (LIVE_SQL, OPEN_DRAFT_SQL, cfg, dep, due_followups, linkedin_enabled, load_config,
                      recipient_domain)

TARGET_RE = re.compile(r"^(contact|job|company):([JKP][A-Z2-7]{7})$")
SKIP_REASONS = ("no_hook", "no_address", "research_budget", "not_relevant")
POSTING_STATUSES = ("eligible", "apply_queued", "awaiting_approval", "applying", "applied")
RANK = {"hiring_manager": 1, "poster": 1, "founder": 1, "recruiter": 3}
FIRST_TOUCH_DRAFT_KINDS = ("cold_email", "li_invite_note", "inmail", "li_message", "application_email")


def _meta_int(conn, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        return int(row[0]) if row else default
    except (TypeError, ValueError):
        return default


class _Ctx:
    def __init__(self, conn, config: dict):
        self.conn = conn
        self.stamp = canon.now()
        self.email_on = bool(cfg("channels.email_outreach.enabled", True, config))
        self.li_on = linkedin_enabled(conn, config)
        writes = cfg("channels.linkedin.writes", {}, config)
        writes = writes if isinstance(writes, dict) else {}
        self.li_invites = self.li_on and writes.get("invites", True) is not False
        self.li_messages = self.li_on and writes.get("messages", True) is not False
        try:
            self.per_job = max(1, int(cfg("outreach.per_job_max_contacts", 1, config)))
        except (TypeError, ValueError):
            self.per_job = 1
        self.prefer_email = bool(cfg("outreach.prefer_email_over_linkedin", True, config))
        allowed = cfg("gmail.address_grades_allowed", ["A", "B"], config)
        self.grades = allowed if isinstance(allowed, list) else ["A", "B"]
        self.cooldown_days = _meta_int(conn, "company_email_cooldown_days", 365)
        self.li_company_cap = _meta_int(conn, "li_invites_per_company_per_7d", 1)
        self.agency_day = _meta_int(conn, "agency_emails_per_day", 1)
        self._skips = {r[0] for r in conn.execute("SELECT target_key FROM target_skips WHERE until > ?",
                                                  (self.stamp,))}

    def skipped(self, *keys) -> bool:
        return any(k in self._skips for k in keys if k)

    def company_email_blocked(self, company_id, is_agency) -> bool:
        if not company_id:
            return False
        if is_agency:
            since = canon.ts_add(self.stamp, days=-1)
            n = self.conn.execute("SELECT count(*) FROM actions WHERE company_id = ? AND kind IN "
                                  "('cold_email','application_email') AND status IN %s AND reserved_at > ?" % LIVE_SQL,
                                  (company_id, since)).fetchone()[0]
            return n >= self.agency_day
        since = canon.ts_add(self.stamp, days=-self.cooldown_days)
        return self.conn.execute("SELECT 1 FROM actions WHERE company_id = ? AND kind IN ('cold_email',"
                                 "'application_email') AND status IN %s AND reserved_at > ? LIMIT 1" % LIVE_SQL,
                                 (company_id, since)).fetchone() is not None or \
            self.conn.execute("SELECT 1 FROM drafts WHERE company_id = ? AND kind IN ('cold_email','application_email') "
                              "AND status IN %s LIMIT 1" % OPEN_DRAFT_SQL, (company_id,)).fetchone() is not None

    def company_li_blocked(self, company_id) -> bool:
        if not company_id:
            return False
        since = canon.ts_add(self.stamp, days=-7)
        n = self.conn.execute("SELECT count(*) FROM actions WHERE company_id = ? AND (kind IN ('li_invite','inmail') "
                              "OR (kind = 'li_message' AND first_touch = 1)) AND status IN %s AND reserved_at > ?"
                              % LIVE_SQL, (company_id, since)).fetchone()[0]
        return n >= self.li_company_cap

    def person_touched(self, contact_id) -> bool:
        c = self.conn
        return c.execute("SELECT 1 FROM actions WHERE contact_id = ? AND first_touch = 1 AND status IN %s LIMIT 1"
                         % LIVE_SQL, (contact_id,)).fetchone() is not None or \
            c.execute("SELECT 1 FROM threads WHERE contact_id = ? LIMIT 1", (contact_id,)).fetchone() is not None or \
            c.execute("SELECT 1 FROM drafts WHERE contact_id = ? AND kind IN (%s) AND status IN %s LIMIT 1"
                      % (",".join("?" for _ in FIRST_TOUCH_DRAFT_KINDS), OPEN_DRAFT_SQL),
                      (contact_id,) + FIRST_TOUCH_DRAFT_KINDS).fetchone() is not None

    def job_full(self, job_id) -> bool:
        c = self.conn
        touched = c.execute(
            "SELECT count(DISTINCT x.cid) FROM ("
            " SELECT a.contact_id AS cid FROM actions a JOIN job_hiring_team h ON h.contact_id = a.contact_id"
            "  WHERE h.job_id = ? AND a.first_touch = 1 AND a.status IN %s"
            " UNION SELECT a.contact_id FROM actions a WHERE a.job_id = ? AND a.first_touch = 1 AND a.status IN %s"
            "  AND a.kind <> 'application_email'"
            " UNION SELECT d.contact_id FROM drafts d WHERE d.job_id = ? AND d.contact_id IS NOT NULL AND d.kind IN "
            "  ('cold_email','li_invite_note','inmail','li_message') AND d.status IN %s) x"
            % (LIVE_SQL, LIVE_SQL, OPEN_DRAFT_SQL), (job_id, job_id, job_id)).fetchone()[0]
        return touched >= self.per_job


def _post_accept_targets(conn, x: _Ctx) -> list[dict]:
    if not x.li_messages:
        return []
    rows = conn.execute(
        "SELECT t.thread_key, t.followup_due_at, c.id AS cid, c.contact_uid, c.full_name, c.title, co.id AS coid, "
        "co.company_uid, co.contact_state, j.job_uid FROM threads t JOIN contacts c ON c.id = t.contact_id "
        "LEFT JOIN companies co ON co.id = t.company_id LEFT JOIN jobs j ON j.id = t.job_id "
        "WHERE t.channel = 'linkedin' AND t.state = 'invite_accepted' AND t.followup_due_at <= ? "
        "AND c.do_not_contact = 0 AND COALESCE(co.contact_state, 'none') NOT IN ('active_thread','do_not_contact') "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.contact_id = c.id AND d.kind = 'li_message' "
        "  AND d.status IN %s) ORDER BY t.followup_due_at" % OPEN_DRAFT_SQL, (x.stamp,)).fetchall()
    out = []
    for r in rows:
        if x.skipped("contact:" + r["contact_uid"]):
            continue
        out.append({"target_key": "contact:" + r["contact_uid"], "kind": "post_accept_message",
                    "job_uid": r["job_uid"], "contact_uid": r["contact_uid"], "company_uid": r["company_uid"],
                    "thread_key": r["thread_key"], "route": "linkedin",
                    "hint": "accepted your invitation; one message allowed now", "reason": "invite_accepted"})
    return out


def _route_for(x: _Ctx, r) -> str | None:
    usable_email = bool(r["email"]) and not r["email_invalid"] and \
        (r["email_grade"] is None or r["email_grade"] in x.grades)
    li_possible = x.li_invites and bool(r["linkedin_url"] or r["li_slug"] or r["needs_vanity"])
    email_ok = x.email_on and not x.company_email_blocked(r["coid"], r["is_agency"])
    li_ok = li_possible and not x.company_li_blocked(r["coid"])
    if email_ok and usable_email and (x.prefer_email or not li_ok):
        return "email"
    if li_ok:
        return "linkedin"
    if email_ok and not r["email"]:
        return "email"   # research may find a published address; else `outreach skip --reason no_address`
    return None


ADDRESS_HINTS = ("have_address", "pattern_available", "enrich_possible", "enrich_done_no_address", "no_domain")


def _finder_state():
    """jobhunter.enrich.cache.state, or None when the email finder is not installed."""
    try:
        mod = importlib.import_module("jobhunter.enrich.cache")
    except ImportError:
        return None
    return getattr(mod, "state", None)


def _address_hint(conn, x: "_Ctx", r, state_fn, patterns: dict) -> str:
    """One of ADDRESS_HINTS for a person target (read only, no network)."""
    if r["email"] and not r["email_invalid"] and (r["email_grade"] is None or r["email_grade"] in x.grades):
        return "have_address"
    dom = None
    if r["coid"]:
        row = conn.execute("SELECT domain FROM companies WHERE id = ?", (r["coid"],)).fetchone()
        dom = (row[0] or "").strip().lower() if row is not None else None
    if not dom:
        return "no_domain"
    if dom not in patterns:
        from . import emailcheck
        try:
            patterns[dom] = emailcheck.pattern_evidence(conn, dom) is not None
        except Exception:   # a read problem never hides the target; it only loses the hint
            patterns[dom] = False
    if patterns[dom]:
        return "pattern_available"
    st = state_fn(conn, r["cid"])
    if st in ("found", "not_found", "forgotten"):
        return "enrich_done_no_address"
    return "enrich_possible"


def work_list(conn, limit: int) -> list[dict]:
    """Targets for `outreach next` (read only), best first."""
    config = load_config()
    x = _Ctx(conn, config)
    out = _post_accept_targets(conn, x)
    if not (x.email_on or x.li_invites):
        return out[:limit]
    rows = conn.execute(
        "SELECT h.relation, c.id AS cid, c.contact_uid, c.full_name, c.title, c.role_type, c.email, c.email_grade, "
        "c.email_invalid, c.linkedin_url, c.li_slug, c.needs_vanity, j.id AS job_id, j.job_uid, j.title AS job_title, "
        "j.status AS job_status, co.id AS coid, co.company_uid, co.contact_state, co.is_agency "
        "FROM job_hiring_team h JOIN contacts c ON c.id = h.contact_id JOIN jobs j ON j.id = h.job_id "
        "LEFT JOIN companies co ON co.id = COALESCE(c.company_id, j.company_id) "
        "WHERE j.status IN (%s) AND c.merged_into IS NULL AND c.do_not_contact = 0 "
        "AND COALESCE(co.contact_state, 'none') NOT IN ('active_thread','do_not_contact') "
        "ORDER BY j.discovered_at DESC, j.id DESC" % ",".join("?" for _ in POSTING_STATUSES),
        POSTING_STATUSES).fetchall()
    people, seen = [], set()
    state_fn = _finder_state() if x.email_on else None
    patterns: dict = {}
    for r in rows:
        if r["cid"] in seen:
            continue
        seen.add(r["cid"])
        if x.skipped("contact:" + r["contact_uid"], "job:" + r["job_uid"], "company:" + (r["company_uid"] or "")):
            continue
        if x.person_touched(r["cid"]) or x.job_full(r["job_id"]):
            continue
        route = _route_for(x, r)
        if route is None:
            continue
        item = {
            "target_key": "contact:" + r["contact_uid"], "kind": "person", "job_uid": r["job_uid"],
            "contact_uid": r["contact_uid"], "company_uid": r["company_uid"], "thread_key": None, "route": route,
            "hint": "%s named on the posting %s" % (r["relation"].replace("_", " "), r["job_uid"]),
            "reason": "posting_team" if r["relation"] != "recruiter" else "posting_recruiter"}
        if state_fn is not None:
            item["address"] = _address_hint(conn, x, r, state_fn, patterns)
        people.append((RANK.get(r["relation"], 3), item))
    if x.email_on:
        for r in conn.execute(
                "SELECT j.id AS job_id, j.job_uid, j.apply_email, co.id AS coid, co.company_uid, co.is_agency "
                "FROM jobs j LEFT JOIN companies co ON co.id = j.company_id WHERE j.apply_email IS NOT NULL "
                "AND j.apply_route <> 'email' AND j.status IN (%s) "
                "AND COALESCE(co.contact_state, 'none') NOT IN ('active_thread','do_not_contact') "
                "ORDER BY j.discovered_at DESC" % ",".join("?" for _ in POSTING_STATUSES), POSTING_STATUSES):
            addr = r["apply_email"].strip().lower()
            if x.skipped("job:" + r["job_uid"], "company:" + (r["company_uid"] or "")):
                continue
            known = conn.execute("SELECT id, do_not_contact FROM contacts WHERE lower(email) = ?", (addr,)).fetchone()
            if known is not None and (known["do_not_contact"] or x.person_touched(known["id"])):
                continue
            if x.company_email_blocked(r["coid"], r["is_agency"]) or x.job_full(r["job_id"]):
                continue
            item = {"target_key": "job:" + r["job_uid"], "kind": "job_email", "job_uid": r["job_uid"],
                    "contact_uid": None, "company_uid": r["company_uid"], "thread_key": None,
                    "route": "email", "email_domain": recipient_domain(addr),
                    "hint": "address published on the posting %s" % r["job_uid"], "reason": "job_email"}
            if state_fn is not None:
                item["address"] = "have_address"
            people.append((2, item))
    people.sort(key=lambda p: p[0])
    out += [p[1] for p in people]
    return out[:limit]


def work_count(conn) -> int:
    """Outreach lane work: targets plus due follow-ups (the dispatcher skips the lane at 0)."""
    return len(work_list(conn, 200)) + len(due_followups(conn, 200))


def skip(conn, target_key: str, reason: str) -> None:
    """Do not pick this target again for outreach.target_skip_days (at least 30) days."""
    m = TARGET_RE.match(target_key or "")
    if not m:
        raise Denied("E_VALIDATION", "target must be contact:<P...>, job:<J...> or company:<K...>")
    if reason not in SKIP_REASONS:
        raise Denied("E_VALIDATION", "reason must be one of %s" % ", ".join(SKIP_REASONS))
    kind, uid = m.group(1), m.group(2)
    table, col = {"contact": ("contacts", "contact_uid"), "job": ("jobs", "job_uid"),
                  "company": ("companies", "company_uid")}[kind]
    if conn.execute("SELECT 1 FROM %s WHERE %s = ?" % (table, col), (uid,)).fetchone() is None:
        raise Denied("E_NOT_FOUND", "no %s %s" % (kind, uid))
    try:
        days = max(30, int(cfg("outreach.target_skip_days", 30)))
    except (TypeError, ValueError):
        days = 30
    stamp = canon.now()
    until = canon.ts_add(stamp, days=days)
    conn.execute("INSERT INTO target_skips (target_key, reason, drops, until, created_at, updated_at) "
                 "VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT (target_key) DO UPDATE SET reason = excluded.reason, "
                 "drops = target_skips.drops + 1, until = MAX(target_skips.until, excluded.until), "
                 "updated_at = excluded.updated_at", (target_key, reason, until, stamp, stamp))
    log_event(conn, "target_skipped", target_key=target_key, reason=reason, until=until)


# ---------------------------------------------------------------- history import (12.20)
IMPORT_KINDS = ("cold_email", "application_email", "li_invite", "li_message")
IMPORT_KEYS = {"kind", "platform", "recipient", "company_name", "sent_at", "evidence"}
MAX_IMPORT = 200


def import_actions(conn, data: dict, cycle_id: str | None = None) -> dict:
    """Record past sends found by a history scan as `imported` actions (they block dedup slots forever).
    Returns {imported, tokens, already_covered, refused: [{index, code}]}. Runs in the caller's transaction; a
    refused item (already covered by an existing row or rule) never stops the others. Imported history is a
    fact, so the company rules (cooldown, caps) do not refuse it (route 'import' is exempt in schema.sql):
    a second person at an already-emailed company is recorded too and blocks what comes after."""
    if not isinstance(data, dict) or set(data) - {"source", "items"}:
        raise Denied("E_SCHEMA", "history file has only source and items")
    items = data.get("items")
    if not isinstance(items, list):
        raise Denied("E_SCHEMA", "items must be a list")
    if len(items) > MAX_IMPORT:
        raise Denied("E_VALIDATION", "at most %d items per file" % MAX_IMPORT)
    from .errors import map_sqlite_error
    import sqlite3
    keys_mod, people, companies = dep("keys"), dep("people"), dep("companies")
    stamp = canon.now()
    imported, dup, refused = [], 0, []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or set(it) - IMPORT_KEYS:
            raise Denied("E_SCHEMA", "items[%d] has unknown keys" % i)
        kind, rcpt = it.get("kind"), (it.get("recipient") or "").strip()
        if kind not in IMPORT_KINDS:
            raise Denied("E_VALIDATION", "items[%d].kind must be one of %s" % (i, ", ".join(IMPORT_KINDS)))
        try:
            sent_at = canon.fmt_ts(canon.parse_ts(it.get("sent_at")))
        except (ValueError, TypeError):
            raise Denied("E_VALIDATION", "items[%d].sent_at must be a UTC timestamp" % i)
        if sent_at > stamp:
            raise Denied("E_VALIDATION", "items[%d].sent_at is in the future" % i)
        platform = it.get("platform") or ("linkedin" if kind.startswith("li_") else "gmail")
        if not isinstance(platform, str) or not re.match(r"^[a-z0-9_.:-]{2,40}$", platform):
            raise Denied("E_VALIDATION", "items[%d].platform is invalid" % i)
        email = rcpt.lower() if "@" in rcpt else None
        li = rcpt if rcpt.startswith("https://") and "linkedin.com/" in rcpt else None
        if not (email or li):
            raise Denied("E_VALIDATION", "items[%d].recipient must be an email or a LinkedIn profile URL" % i)
        company_id = None
        name = it.get("company_name")
        dom = recipient_domain(email) if email else None
        if (isinstance(name, str) and name.strip()) or dom:
            company_id = companies.resolve(conn, name=name if isinstance(name, str) and name.strip() else None,
                                           domain=dom, source="email" if dom else "human", create=True)
        contact_id = None
        if email or li:
            cuid = conn.execute("SELECT company_uid FROM companies WHERE id = ?", (company_id,)).fetchone() \
                if company_id else None
            pk = keys_mod.person_keys(email=email, linkedin_url=li, company_uid=cuid[0] if cuid else None)
            contact_id = people.resolve(conn, keys=pk, fields={"email": email, "linkedin_url": li,
                                                               "company_id": company_id}, create=True)
        if kind in ("cold_email", "li_invite"):
            first = 1
        elif kind == "application_email":
            rt = conn.execute("SELECT role_type FROM contacts WHERE id = ?", (contact_id,)).fetchone() \
                if contact_id else None
            first = 0 if (contact_id is None or (rt and rt[0] == "role_inbox")) else 1
        elif kind == "li_message":
            first = 0 if conn.execute("SELECT 1 FROM threads WHERE contact_id = ? AND channel = 'linkedin' AND "
                                      "state = 'invite_accepted'", (contact_id,)).fetchone() else 1
        else:
            first = 0
        seq = None
        if kind == "li_message":
            seq = 1 + conn.execute("SELECT count(*) FROM actions WHERE contact_id = ? AND li_msg_seq IS NOT NULL "
                                   "AND status IN %s" % LIVE_SQL, (contact_id,)).fetchone()[0]
            if seq > 2:
                refused.append({"index": i, "code": "E_DUP_LI_TOUCH"})
                continue
        token = canon.new_token()
        evidence = it.get("evidence") if isinstance(it.get("evidence"), str) else None
        try:
            conn.execute(
                "INSERT INTO actions (token, kind, route, first_touch, li_note, li_msg_seq, platform, agent_id, "
                "contact_id, company_id, recipient, status, reserved_at, expires_at, sent_at, resolved_at, cycle_id, "
                "lane, evidence, note, created_at, updated_at) VALUES (?, ?, 'import', ?, 0, ?, ?, NULL, ?, ?, ?, "
                "'imported', ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?)",
                (token, kind, first, seq, platform, contact_id, company_id, email or li, sent_at, sent_at, sent_at,
                 stamp, cycle_id, (evidence or "")[:4000] or None, "history import: %s" % (data.get("source") or "-"),
                 stamp, stamp))
        except sqlite3.DatabaseError as exc:
            d = map_sqlite_error(exc)
            if d.code.startswith("E_DUP") or d.code in ("E_COMPANY_COOLDOWN", "E_COMPANY_APP_CAP",
                                                         "E_COMPANY_LI_CAP", "E_AGENCY_CAP"):
                dup += 1
            refused.append({"index": i, "code": d.code})
            continue
        imported.append(token)
    log_event(conn, "actions_imported", n=len(imported), refused=len(refused), source=data.get("source"))
    return {"imported": len(imported), "tokens": imported, "already_covered": dup, "refused": refused}
