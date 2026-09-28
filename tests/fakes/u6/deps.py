"""Fakes of other units' modules that U6 code calls (U1 keys, companies, people, exclusions, config,
breakers, reconcile, gate). They implement just enough behaviour, with fictional data, for the U6 tests.
Installed as sys.modules entries by tests.fakes.u6.install(), so the real modules (when they land) are
not touched and the tests stay deterministic."""
from __future__ import annotations

import copy
import hashlib
import re
import types
from urllib.parse import unquote, urlparse

from jobhunter import canon, hooks, jobstate
from jobhunter.errors import Denied

from . import enrich as enrich_fakes

FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com"}
LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")


# ---------------------------------------------------------------- keys
def _norm_name(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"\b(private limited|pvt ltd|pvt|ltd|limited|inc|llc|corp)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def registrable_domain(host_or_email: str):
    if not host_or_email:
        return None
    host = host_or_email.rsplit("@", 1)[-1].lower().strip(".")
    parts = host.split(".")
    if len(parts) < 2:
        return None
    dom = ".".join(parts[-2:])
    return None if dom in FREEMAIL else dom


def person_keys(email=None, linkedin_url=None, full_name=None, company_uid=None):
    out = []
    if email:
        e = email.strip().lower()
        out.append(("email:" + e, "email"))
        local, dom = e.split("@", 1)
        local = local.split("+", 1)[0]
        if dom in ("gmail.com", "googlemail.com"):
            local, dom = local.replace(".", ""), "gmail.com"
        out.append(("email_norm:%s@%s" % (local, dom), "email_norm"))
    if linkedin_url:
        if "lnkd.in" in linkedin_url:
            raise Denied("E_VALIDATION", "lnkd.in refused")
        path = urlparse(linkedin_url).path
        m = re.match(r"^/in/([^/]+)", path)
        if m and m.group(1).startswith("ACoA"):
            out.append(("li_member:" + m.group(1), "li_member"))
        elif m:
            out.append(("li:" + unquote(m.group(1)).lower(), "li_slug"))
        elif path.startswith("/pub/"):
            out.append(("li_legacy:" + path, "li_legacy"))
        elif path.startswith("/sales/"):
            out.append(("li_sales:" + path.split("/")[3].split(",")[0], "li_sales"))
    if full_name and company_uid:
        toks = [t for t in re.sub(r"[^a-z ]", "", full_name.lower()).split() if len(t) > 1]
        if len(toks) >= 2:
            out.append(("pname:%s.%s@%s" % (toks[0], toks[-1], company_uid), "pname"))
    return out


def company_keys(name=None, domain=None, ats=None, tenant=None):
    out = []
    if name and _norm_name(name):
        out.append(("id:" + _norm_name(name), "name"))
    d = registrable_domain(domain) if domain else None
    if d:
        out.append(("dom:" + d, "dom"))
    return out


def job_key(url, native_ids, source):
    return "url:" + hashlib.sha1(url.encode()).hexdigest(), []


keys = types.ModuleType("jobhunter.keys")
keys.person_keys, keys.company_keys, keys.registrable_domain, keys.job_key = \
    person_keys, company_keys, registrable_domain, job_key


# ---------------------------------------------------------------- companies
def company_resolve(conn, *, name=None, domain=None, ats=None, tenant=None, source="human", create=True):
    ks = company_keys(name=name, domain=domain)
    ids = set()
    for k, _ in ks:
        row = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", (k,)).fetchone()
        if row:
            ids.add(row[0])
    stamp = canon.now()
    if not ids:
        if not create:
            raise Denied("E_NOT_FOUND", "no company")
        cur = conn.execute("INSERT INTO companies (company_uid, display_name, domain, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?)", (canon.new_uid("K"), name or domain, registrable_domain(domain)
                                                      if domain else None, stamp, stamp))
        cid = cur.lastrowid
    else:
        cid = min(ids)
    for k, kind in ks:
        conn.execute("INSERT OR IGNORE INTO company_aliases (alias_key, company_id, kind, source, created_at) "
                     "VALUES (?, ?, ?, 'human', ?)", (k, cid, kind, stamp))
    return cid


companies = types.ModuleType("jobhunter.companies")
companies.resolve = company_resolve


# ---------------------------------------------------------------- people
PEOPLE_COLS = ("full_name", "first_name", "title", "company_id", "role_type", "locale", "email", "email_grade",
               "email_evidence_url", "linkedin_url", "li_slug", "needs_vanity")


def people_resolve(conn, *, keys, fields, create=True):
    ids = set()
    for k, _ in keys:
        row = conn.execute("SELECT contact_id FROM contact_keys WHERE key = ?", (k,)).fetchone()
        if row:
            cid = row[0]
            while True:
                m = conn.execute("SELECT merged_into FROM contacts WHERE id = ?", (cid,)).fetchone()
                if not m or m[0] is None:
                    break
                cid = m[0]
            ids.add(cid)
    stamp = canon.now()
    if not ids:
        cols = [c for c in PEOPLE_COLS if fields.get(c) is not None]
        cur = conn.execute("INSERT INTO contacts (contact_uid, %s created_at, updated_at) VALUES (?, %s ?, ?)"
                           % ("".join(c + ", " for c in cols), "".join("?, " for _ in cols)),
                           [canon.new_uid("P")] + [fields[c] for c in cols] + [stamp, stamp])
        cid = cur.lastrowid
    else:
        cid = min(ids)
        for other in sorted(ids - {cid}):
            dnc = conn.execute("SELECT do_not_contact FROM contacts WHERE id = ?", (other,)).fetchone()[0]
            conn.execute("UPDATE contact_keys SET contact_id = ? WHERE contact_id = ?", (cid, other))
            conn.execute("UPDATE contacts SET merged_into = ? WHERE id = ?", (cid, other))
            if dnc:
                conn.execute("UPDATE contacts SET do_not_contact = 1 WHERE id = ?", (cid,))
    for k, kind in keys:
        conn.execute("INSERT OR IGNORE INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, ?, ?)",
                     (k, cid, kind, stamp))
    return cid


people = types.ModuleType("jobhunter.people")
people.resolve = people_resolve


# ---------------------------------------------------------------- exclusions
def excl_match(conn, **targets):
    unknown = set(targets) - {"company_id", "company_name", "domain", "email", "contact_id", "linkedin_url",
                              "job_id", "job_url"}
    if unknown:
        raise Denied("E_INTERNAL", "unknown exclusion targets: %s" % ", ".join(sorted(unknown)))
    wanted = []
    if targets.get("email"):
        wanted += [("email", person_keys(email=targets["email"])[0][0])]
    if targets.get("linkedin_url"):
        pk = person_keys(linkedin_url=targets["linkedin_url"])
        if pk:
            wanted += [("linkedin", pk[0][0])]
    out = []
    for t, k in wanted:
        row = conn.execute("SELECT type, value_key, reason FROM exclusions WHERE type = ? AND value_key = ? "
                           "AND active = 1", (t, k)).fetchone()
        if row:
            out.append({"type": row[0], "value_key": row[1], "reason": row[2]})
    if targets.get("company_id"):
        row = conn.execute("SELECT contact_state FROM companies WHERE id = ?", (targets["company_id"],)).fetchone()
        if row and row[0] == "do_not_contact":
            out.append({"type": "company", "value_key": "company_id", "reason": "excluded company"})
    return out


APPLIED: list = []


def excl_apply(conn, exclusion_id):
    row = conn.execute("SELECT type, value_key FROM exclusions WHERE id = ?", (exclusion_id,)).fetchone()
    APPLIED.append((row[0], row[1]))
    if row[0] == "company":
        for cid, name in conn.execute("SELECT id, display_name FROM companies").fetchall():
            hit = conn.execute("SELECT 1 FROM company_aliases WHERE alias_key = ? AND company_id = ?",
                               (row[1], cid)).fetchone()
            if hit or row[1] in [k for k, _ in company_keys(name=name)]:
                conn.execute("UPDATE companies SET contact_state = 'do_not_contact' WHERE id = ?", (cid,))


def excl_add(conn, etype, value, reason, source="human"):
    key = {"email": lambda v: person_keys(email=v)[0][0], "linkedin": lambda v: person_keys(linkedin_url=v)[0][0],
           "company": lambda v: company_keys(name=v)[0][0]}[etype](value)
    stamp = canon.now()
    conn.execute("INSERT INTO exclusions (type, value_raw, value_key, reason, source, active, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT (type, value_key) DO UPDATE SET active = 1",
                 (etype, value, key, reason, source, stamp, stamp))
    eid = conn.execute("SELECT id FROM exclusions WHERE type = ? AND value_key = ?", (etype, key)).fetchone()[0]
    excl_apply(conn, eid)
    return {"id": eid, "outcome": "added"}


exclusions = types.ModuleType("jobhunter.exclusions")
exclusions.match, exclusions.apply, exclusions.add = excl_match, excl_apply, excl_add


# ---------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "gmail": {"route": "app_password", "followup_after_business_days": [5, 7], "address_grades_allowed": ["A", "B"],
              "bounce_stop": {"per_24h": 2}},
    "linkedin": {"delays_sec": {"post_accept_message_days": [1, 3], "followup_days": [7, 10]}},
    "channels": {"applications": {"enabled": True}, "email_outreach": {"enabled": True},
                 "linkedin": {"enabled": True, "writes": {"invites": True, "messages": True, "easy_apply": False}}},
    "outreach": {"per_job_max_contacts": 1, "prefer_email_over_linkedin": True, "target_skip_days": 30},
    "boards": {"sites": {"ats_forms": {"apply": "browser"}, "workday": {"apply": "human_queue"},
                         "naukri": {"apply": "off"}, "instahyre": {"apply": "browser"},
                         "linkedin_jobs": {"apply": "via_linkedin_channel"}}},
    "dns": {"doh_url": "https://dns.example.invalid/resolve"},
}
CONFIG: dict = copy.deepcopy(DEFAULT_CONFIG)


def config_load():
    return copy.deepcopy(CONFIG)


def config_set(path: str, value) -> None:
    cur = CONFIG
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def config_reset() -> None:
    CONFIG.clear()
    CONFIG.update(copy.deepcopy(DEFAULT_CONFIG))


config = types.ModuleType("jobhunter.config")
config.load = config_load


# ---------------------------------------------------------------- breakers
TRIPS: list = []


def breaker_trip(conn, scope, reason_code, detail, evidence_path=None, by="system"):
    TRIPS.append((scope, reason_code))
    stamp = canon.now()
    conn.execute("INSERT INTO breakers (scope, state, reason_code, detail, tripped_at, updated_at) VALUES "
                 "(?, 'open', ?, ?, ?, ?) ON CONFLICT (scope) DO UPDATE SET state = 'open'",
                 (scope, reason_code, detail, stamp, stamp))


breakers = types.ModuleType("jobhunter.breakers")
breakers.trip = breaker_trip


# ---------------------------------------------------------------- reconcile
RECONCILE_TASKS: list = []


def reconcile_work_list(conn, route=None, agent_id=None):
    return list(RECONCILE_TASKS)


reconcile = types.ModuleType("jobhunter.reconcile")
reconcile.work_list = reconcile_work_list


# ---------------------------------------------------------------- gate (minimal, for the transcript replay)
def _first_touch(conn, kind, contact_id):
    if kind in ("cold_email", "li_invite", "inmail"):
        return 1
    if kind == "li_message":
        row = conn.execute("SELECT 1 FROM threads WHERE contact_id = ? AND channel = 'linkedin' AND "
                           "state = 'invite_accepted'", (contact_id,)).fetchone()
        return 0 if row else 1
    return 0


def gate_reserve(conn, *, kind, draft_id, precheck_id, platform, agent_id, route, job_id=None, contact_id=None,
                 thread_key=None, cycle_id=None, li_note=0):
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if d is None or d["status"] != "approved":
        raise Denied("E_QC_NOT_APPROVED", "draft is not approved")
    pc = conn.execute("SELECT * FROM prechecks WHERE id = ?", (precheck_id,)).fetchone()
    if pc is None or pc["result"] != "clear" or pc["used_by_action"] is not None:
        raise Denied("E_PRECHECK_STALE", "precheck missing, not clear or used")
    contact_id = contact_id or d["contact_id"]
    seq = None
    if kind in ("li_message", "li_followup", "inmail") or (kind == "li_invite" and li_note):
        seq = 1 + conn.execute("SELECT count(*) FROM actions WHERE contact_id = ? AND li_msg_seq IS NOT NULL AND "
                               "status IN %s" % str(LIVE), (contact_id,)).fetchone()[0]
    stamp = canon.now()
    token = canon.new_token()
    try:
        cur = conn.execute(
            "INSERT INTO actions (token, kind, route, first_touch, li_note, li_msg_seq, platform, agent_id, contact_id, "
            "company_id, job_id, thread_key, recipient, draft_id, approved_sha256, precheck_id, status, reserved_at, "
            "expires_at, cycle_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'reserved', ?, ?, ?, ?, ?)",
            (token, kind, route, _first_touch(conn, kind, contact_id), li_note, seq, platform, agent_id, contact_id,
             d["company_id"], job_id or d["job_id"], thread_key or d["thread_key"], d["recipient"], draft_id,
             d["text_sha256"], precheck_id, stamp, canon.ts_add(stamp, minutes=30), cycle_id, stamp, stamp))
    except Exception as exc:
        from jobhunter.errors import map_sqlite_error
        raise map_sqlite_error(exc)
    conn.execute("UPDATE prechecks SET used_by_action = ? WHERE id = ?", (cur.lastrowid, precheck_id))
    return {"token": token, "expires_at": canon.ts_add(stamp, minutes=30)}


def gate_arm(conn, token, observed_text):
    a = conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (a["draft_id"],)).fetchone()
    if a["kind"] == "application":
        import json
        pkg = json.loads(d["payload_json"])
        obs = json.loads(observed_text)
        approved = canon.canonical_send_text("application", None, None, pkg["fields"], None,
                                             {"filename": pkg.get("resume")})
        observed = canon.canonical_send_text("application", None, None, obs["fields"], None,
                                             {"filename": obs.get("resume_filename_visible")})
    else:
        approved = canon.canonical_send_text(a["kind"], d["subject"], d["body"], None, None)
        text = canon.normalize_text(observed_text)
        if a["kind"] == "inmail" and text.startswith("Subject:"):
            # like gate._observed_canonical: an InMail read-back is "Subject: <s>", a blank line, the body
            head, _sep, body = text.partition("\n\n")
            observed = canon.canonical_send_text("inmail", head[len("Subject:"):].strip(), body, None, None)
        else:
            observed = canon.canonical_send_text(a["kind"], None, observed_text, None, None)
    stamp = canon.now()
    if canon.sha256_text(observed) != canon.sha256_text(approved):
        conn.execute("UPDATE actions SET status = 'failed', fail_reason = 'observed_text_mismatch', updated_at = ? "
                     "WHERE id = ?", (stamp, a["id"]))
        return False
    conn.execute("UPDATE actions SET status = 'armed', armed_at = ?, observed_sha256 = ?, updated_at = ? WHERE id = ?",
                 (stamp, canon.sha256_text(observed), stamp, a["id"]))
    return True


def gate_confirm(conn, token, evidence, *, post_detect_id=None, platform_ref=None, message_id=None):
    a = conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
    if a["status"] != "armed":
        raise Denied("E_PRECONDITION", "not armed")
    stamp = canon.now()
    conn.execute("UPDATE actions SET status = 'sent', sent_at = ?, evidence = ?, message_id = ?, updated_at = ? "
                 "WHERE id = ?", (stamp, evidence, message_id, stamp, a["id"]))
    jobstate.set_draft_status(conn, a["draft_id"], "sent", "confirmed", "gate")
    if a["company_id"]:
        conn.execute("UPDATE companies SET contact_state = 'contacted' WHERE id = ? AND contact_state = 'none'",
                     (a["company_id"],))
    row = dict(conn.execute("SELECT * FROM actions WHERE id = ?", (a["id"],)).fetchone())
    row["platform_ref"] = platform_ref
    hooks.on_confirm(conn, row)
    t = conn.execute("SELECT thread_key, followup_due_at FROM threads WHERE first_action_id = ? OR "
                     "followup_action_id = ? ORDER BY id DESC LIMIT 1", (a["id"], a["id"])).fetchone()
    return {"status": "sent", "thread_key": t[0] if t else None, "followup_due_at": t[1] if t else None}


gate = types.ModuleType("jobhunter.gate")
gate.reserve, gate.arm, gate.confirm = gate_reserve, gate_arm, gate_confirm

MODULES = {"jobhunter.keys": keys, "jobhunter.companies": companies, "jobhunter.people": people,
           "jobhunter.exclusions": exclusions, "jobhunter.config": config, "jobhunter.breakers": breakers,
           "jobhunter.reconcile": reconcile, "jobhunter.gate": gate}
MODULES.update(enrich_fakes.MODULES)


def reset() -> None:
    enrich_fakes.reset()
    config_reset()
    TRIPS.clear()
    APPLIED.clear()
    RECONCILE_TASKS.clear()
