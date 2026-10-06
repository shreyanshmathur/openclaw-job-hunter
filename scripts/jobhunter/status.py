"""`status`, `inbox`, the Sheet dashboard and the Limits and settings tab (design 3.4, 7.3, 1.5).

Everything here is read-only. Other units' modules (config, ceilings, profile) are optional: when one is
missing or still a stub the value falls back to what the database and the files show, so `status` always
answers (it is the command a person runs when something is wrong).

Also home of the small shared helpers of U5: effective config lookup, the person's time zone, local day
bounds and the activity counters used by the Sheet, the digest and the dashboard.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import re
import sqlite3

from . import canon, paths
from . import sheets_labels as L
from .errors import Denied

try:  # Python 3.9 has zoneinfo; tz data may still be missing on a host
    import zoneinfo as _zoneinfo
except ImportError:  # pragma: no cover
    _zoneinfo = None

SENT_LIKE = ("sent",)
HEARTBEAT_FRESH_S = 600
LANES = ("scout", "evaluator", "applier", "outreach", "replies", "api", "mailer")

# ---------------------------------------------------------------- config
DEFAULTS = {
    "timezone": "auto",
    "sheets": {"enabled": True, "person_name_style": "first_last_initial", "skipped_tab_days": 30,
               "max_ops_per_post": 400, "store_message_text": True, "resync_overlap_minutes": 10},
    "owner": {"notify": {"channel": "whatsapp", "to": "", "desktop": True, "quiet_hours": ["22:00", "08:00"]}},
    "gmail": {"route": "web_ui"},
    "approval": {"mode": "inherit"},
}


def _optional(module: str):
    try:
        return importlib.import_module(module)
    except ImportError:
        return None


def load_config() -> dict:
    """Effective config from jobhunter.config.load() (U1) when available, else private/config.json, else {}."""
    mod = _optional("jobhunter.config")
    if mod is not None and hasattr(mod, "load"):
        try:
            data = mod.load()
            if isinstance(data, dict):
                return data
        except Exception:  # a broken config must never break status, the Sheet or notifications
            pass
    path = os.path.join(paths.private_dir(), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cfg(config: dict | None, dotted: str, default=None):
    """config['a']['b'] for 'a.b', falling back to DEFAULTS and then to `default`."""
    for src in (config or {}, DEFAULTS):
        cur = src
        ok = True
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


# ---------------------------------------------------------------- time zone and local days
def system_tz_name() -> str:
    tz = os.environ.get("TZ", "")
    if tz and "/" in tz and not tz.startswith("/"):
        return tz
    try:
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            return real.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "Etc/UTC"


def tz_name(config: dict | None = None) -> str:
    """IANA name of the person's zone; 'auto' is the system zone (U1 config.resolve_timezone when present)."""
    name = cfg(config, "timezone", "auto")
    mod = _optional("jobhunter.config")
    if mod is not None and hasattr(mod, "resolve_timezone"):
        try:
            got = mod.resolve_timezone(name)
            if isinstance(got, str) and got:
                return got
        except Exception:
            pass
    if not isinstance(name, str) or not name or name == "auto":
        return system_tz_name()
    return name


def tzinfo(config: dict | None = None):
    name = tz_name(config)
    if _zoneinfo is not None:
        try:
            return _zoneinfo.ZoneInfo(name)
        except Exception:
            pass
    return _dt.timezone.utc


def _parse(ts: str | None) -> _dt.datetime | None:
    """Our UTC timestamps, plus ISO forms with fractions or offsets that other writers may use."""
    if not ts:
        return None
    s = str(ts).strip()
    try:
        return canon.parse_ts(s)
    except ValueError:
        pass
    m = re.match(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?$", s)
    if m:
        d = _dt.datetime.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")
        off = m.group(4)
        if off and off != "Z":
            sign = 1 if off[0] == "+" else -1
            hh, mm = int(off[1:3]), int(off[-2:])
            return (d - sign * _dt.timedelta(hours=hh, minutes=mm)).replace(tzinfo=_dt.timezone.utc)
        return d.replace(tzinfo=_dt.timezone.utc)
    if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", s):
        return _dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    return None


def parse_any(ts: str | None) -> _dt.datetime | None:
    return _parse(ts)


def local_date(ts: str | None, tz) -> str | None:
    d = _parse(ts)
    return d.astimezone(tz).strftime("%Y-%m-%d") if d else None


def day_bounds(date_str: str, tz) -> tuple[str, str]:
    """UTC [start, end) timestamps of one local calendar day."""
    d = _dt.datetime.strptime(date_str, "%Y-%m-%d")
    start = d.replace(tzinfo=tz)
    end = (d + _dt.timedelta(days=1)).replace(tzinfo=tz)
    return canon.fmt_ts(start), canon.fmt_ts(end)


def fmt_local(ts: str | None, tz, with_time: bool = True) -> str:
    """'27 Sep 2026 10:42' (or '27 Sep 2026') in the person's time zone; '' for nothing."""
    d = _parse(ts)
    if not d:
        return ""
    loc = d.astimezone(tz)
    s = "%d %s %d" % (loc.day, loc.strftime("%b"), loc.year)
    return s + loc.strftime(" %H:%M") if with_time else s


def fmt_day_month(ts: str | None, tz) -> str:
    d = _parse(ts)
    if not d:
        return ""
    loc = d.astimezone(tz)
    return "%d %s" % (loc.day, loc.strftime("%b"))


def today_local(tz) -> str:
    return canon.utcnow().astimezone(tz).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- activity counts
_METRIC_SQL = {
    "jobs_found": "SELECT count(*) FROM jobs WHERE discovered_at >= ? AND discovered_at < ?",
    "passed_filters": "SELECT count(*) FROM jobs WHERE discovered_at >= ? AND discovered_at < ? "
                      "AND status NOT IN ('new','prefilter_rejected','duplicate','excluded')",
    "evaluated": "SELECT count(*) FROM evaluations WHERE stage = 'llm' AND evaluated_at >= ? AND evaluated_at < ?",
    "good_fits": "SELECT count(*) FROM evaluations WHERE stage = 'llm' AND verdict = 'apply' "
                 "AND evaluated_at >= ? AND evaluated_at < ?",
    "applications": "SELECT count(*) FROM actions WHERE kind IN ('application','application_email') "
                    "AND status = 'sent' AND sent_at >= ? AND sent_at < ?",
    "emails": "SELECT count(*) FROM actions WHERE kind IN ('cold_email','referral_ask') AND status = 'sent' "
              "AND sent_at >= ? AND sent_at < ?",
    "li_invites": "SELECT count(*) FROM actions WHERE kind = 'li_invite' AND status = 'sent' "
                  "AND sent_at >= ? AND sent_at < ?",
    "li_messages": "SELECT count(*) FROM actions WHERE kind IN ('li_message','inmail') AND status = 'sent' "
                   "AND sent_at >= ? AND sent_at < ?",
    "follow_ups": "SELECT count(*) FROM actions WHERE kind IN ('followup_email','li_followup') AND status = 'sent' "
                  "AND sent_at >= ? AND sent_at < ?",
    "replies": "SELECT count(*) FROM replies WHERE received_at >= ? AND received_at < ? "
               "AND classification NOT IN ('auto_ack','out_of_office','bounce')",
    "positive": "SELECT count(*) FROM replies WHERE received_at >= ? AND received_at < ? "
                "AND classification IN ('positive','referral_offered')",
    "interviews": "SELECT count(*) FROM applications WHERE outcome IN ('screening_call','interview','offer') "
                  "AND outcome_at >= ? AND outcome_at < ?",
    "drafts": "SELECT count(*) FROM drafts WHERE created_at >= ? AND created_at < ? "
              "AND kind NOT IN ('form_answer','cover_note')",
    "first_pass": "SELECT count(*) FROM qc_results WHERE stage = 'review' AND attempt = 1 AND human_edit_no = 0 "
                  "AND passed = 1 AND created_at >= ? AND created_at < ?",
    "dropped": "SELECT count(*) FROM drafts WHERE status = 'dropped_qc' AND updated_at >= ? AND updated_at < ?",
    "stops": "SELECT count(*) FROM breaker_events WHERE event = 'trip' AND created_at >= ? AND created_at < ?",
    "cycles": "SELECT count(*) FROM cycles WHERE started_at >= ? AND started_at < ?",
}
METRICS = tuple(_METRIC_SQL)
METRIC_LABELS = {
    "jobs_found": "Jobs found", "passed_filters": "Passed filters", "evaluated": "Evaluated", "good_fits": "Good fits",
    "applications": "Applications", "emails": "Emails", "li_invites": "LinkedIn invites",
    "li_messages": "LinkedIn messages", "follow_ups": "Follow-ups", "replies": "Replies",
    "positive": "Positive replies", "interviews": "Interviews", "drafts": "Drafts",
    "first_pass": "Passed QC first try", "dropped": "Dropped by QC", "stops": "Stops", "cycles": "Cycles run",
}


def count_activity(conn, start: str, end: str, metrics=METRICS) -> dict:
    """{metric: int} for UTC [start, end)."""
    return {m: int(conn.execute(_METRIC_SQL[m], (start, end)).fetchone()[0] or 0) for m in metrics}


# ---------------------------------------------------------------- state pieces
def _one(conn, sql: str, args=()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0] or 0) if row else 0


def open_breakers(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM breakers WHERE state = 'open' ORDER BY tripped_at"):
        out.append({"scope": r["scope"], "area": L.scope_label(r["scope"]), "reason_code": r["reason_code"],
                    "reason": L.reason_sentence(r["reason_code"], None), "detail": L.clip(r["detail"], 300),
                    "since": r["tripped_at"], "until": r["min_cooldown_until"],
                    "requires_human": bool(r["requires_human"]),
                    "todo": L.todo_sentence(r["scope"], r["reason_code"], bool(r["requires_human"]))})
    return out


def is_paused() -> bool:
    return os.path.exists(paths.paused_file())


def agent_state(conn) -> str:
    """'Running', 'Paused' or 'Stopped: LinkedIn, Gmail' (open breakers other than pause scopes)."""
    if is_paused():
        return "Paused"
    brk = open_breakers(conn)
    stops = [b["area"] for b in brk if not str(b["scope"]).startswith(("pause:", "api:"))]
    if any(b["scope"] == "global" for b in brk):
        return "Stopped: everything"
    if stops:
        return "Stopped: " + ", ".join(stops)
    paused = [b["area"] for b in brk if str(b["scope"]).startswith("pause:")]
    if paused:
        return "Running (partly paused)"
    return "Running"


def guard_heartbeat() -> dict:
    path = os.path.join(paths.guard_dir(), "heartbeat.json")
    out = {"present": False, "fresh": False, "age_s": None, "install_id_ok": None, "version": None}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        return out
    out["present"] = True
    out["version"] = hb.get("version")
    beat = _parse(hb.get("beat_at"))
    if beat:
        out["age_s"] = int((canon.utcnow() - beat).total_seconds())
    try:
        out["install_id_ok"] = hb.get("install_id") == paths.home().get("install_id")
    except Denied:
        out["install_id_ok"] = None
    out["fresh"] = bool(out["age_s"] is not None and out["age_s"] <= HEARTBEAT_FRESH_S and out["install_id_ok"])
    return out


def undelivered(conn, priority: str | None = "high", limit: int = 20) -> list[dict]:
    sql = "SELECT id, priority, kind, text, created_at, attempts, last_error FROM notifications " \
          "WHERE delivered_at IS NULL AND suppressed = 0"
    args: list = []
    if priority:
        sql += " AND priority = ?"
        args.append(priority)
    sql += " ORDER BY created_at LIMIT ?"
    args.append(limit)
    return [{"id": r["id"], "priority": r["priority"], "kind": r["kind"], "text": L.clip(r["text"], 400),
             "created_at": r["created_at"], "attempts": r["attempts"], "last_error": r["last_error"]}
            for r in conn.execute(sql, args)]


def undelivered_high_count(conn) -> int:
    return _one(conn, "SELECT count(*) FROM notifications WHERE delivered_at IS NULL AND suppressed = 0 "
                      "AND priority = 'high'")


LIMIT_ITEMS = (
    ("gmail", "cold_email", "Gmail", "Cold emails"),
    ("gmail", "followup_email", "Gmail", "Follow-up emails"),
    ("linkedin", "li_invite", "LinkedIn", "Invites"),
    ("linkedin", "li_message", "LinkedIn", "Messages"),
    ("boards", "application", "Job sites", "Applications"),
)
# the ceilings.budget row that is each kind's own daily cap (others are shared totals such as "gmail total"
# or "LinkedIn writes", which count every kind on the platform)
_OWN_DAY_ITEM = {"cold_email": "cold emails", "followup_email": "follow-ups", "application": "all applications"}
_DAY_WINDOWS = ("day", "utc_day")


def _remaining(r: dict) -> float:
    rem = r.get("remaining")
    if isinstance(rem, (int, float)):
        return rem
    used, limit = r.get("used"), r.get("limit")
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
        return limit - used
    return float("inf")


def pick_day_row(rows: list, kind: str) -> dict | None:
    """The budget row to show as `kind` "today": the kind's own day limit, else the day row with the least
    remaining, else the first row."""
    rows = [r for r in rows if isinstance(r, dict)]
    day = [r for r in rows if r.get("window") in _DAY_WINDOWS]
    own = _OWN_DAY_ITEM.get(kind, kind)
    mine = [r for r in day if r.get("item") == own]
    if mine:
        return min(mine, key=_remaining)
    if day:
        return min(day, key=_remaining)
    return rows[0] if rows else None


_KIND_SQL = {
    "cold_email": ("cold_email", "referral_ask"), "followup_email": ("followup_email",),
    "li_invite": ("li_invite",), "li_message": ("li_message", "inmail", "li_followup"),
    "application": ("application", "application_email"),
}


def limits_today(conn) -> list[dict]:
    """[{area, item, used, limit, platform, kind}] for the main daily ceilings. Uses ceilings.budget (U1) when
    available; otherwise counts the ledger for the last 24 hours and leaves the limit empty."""
    mod = _optional("jobhunter.ceilings")
    since = canon.ts_add(canon.now(), hours=-24)
    out = []
    for platform, kind, area, item in LIMIT_ITEMS:
        used, limit = None, None
        if mod is not None and hasattr(mod, "budget"):
            try:
                b = mod.budget(conn, platform, kind)
                row = None
                if isinstance(b, dict) and isinstance(b.get("rows"), list):
                    row = pick_day_row(b["rows"], kind)
                elif isinstance(b, dict):
                    row = b
                if row is not None:
                    used, limit = row.get("used"), row.get("limit")
            except (NotImplementedError, Denied, TypeError, ValueError, KeyError):
                pass
        if used is None:
            kinds = _KIND_SQL[kind]
            sql = "SELECT count(*) FROM actions WHERE kind IN (%s) AND status IN (%s) AND reserved_at > ?" % (
                ",".join("?" for _ in kinds), ",".join("?" for _ in L.LIVE_STATUSES))
            args = list(kinds) + list(L.LIVE_STATUSES) + [since]
            if platform != "boards":
                sql += " AND platform = ?"
                args.append(platform)
            used = _one(conn, sql, args)
        out.append({"platform": platform, "kind": kind, "area": area, "item": item + " today",
                    "used": int(used or 0), "limit": int(limit) if isinstance(limit, (int, float)) else None})
    return out


def queues(conn) -> dict:
    return {
        "approvals_waiting": _one(conn, "SELECT count(*) FROM drafts WHERE status = 'awaiting_approval'"),
        "jobs_to_evaluate": _one(conn, "SELECT count(*) FROM jobs WHERE status = 'eval_queued'"),
        "jobs_to_apply": _one(conn, "SELECT count(*) FROM jobs WHERE status IN ('eligible','apply_queued')"),
        "jobs_needing_you": _one(conn, "SELECT count(*) FROM jobs WHERE status = 'needs_human'"),
        "human_tasks": _one(conn, "SELECT count(*) FROM human_tasks WHERE done_at IS NULL"),
        "replies_to_classify": _one(conn, "SELECT count(*) FROM inbound_messages WHERE status = 'pending'"),
        "unconfirmed_sends": _one(conn, "SELECT count(*) FROM actions WHERE status = 'unknown'"),
        "qc_reviews_queued": _one(conn, "SELECT count(*) FROM qc_jobs WHERE state IN ('queued','running')"),
        "notifications_pending": _one(conn, "SELECT count(*) FROM notifications WHERE delivered_at IS NULL "
                                            "AND suppressed = 0"),
    }


def last_cycles(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT c.* FROM cycles c JOIN (SELECT lane, max(started_at) AS m FROM cycles "
                          "GROUP BY lane) x ON x.lane = c.lane AND x.m = c.started_at"):
        out[r["lane"]] = {"cycle_id": r["cycle_id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                          "status": r["status"]}
    return out


def sheet_state(conn) -> dict:
    row = conn.execute("SELECT * FROM sheet_state WHERE tab = '_all'").fetchone()
    connected = False
    try:
        sheets = importlib.import_module("jobhunter.sheets")
        connected = sheets.is_connected()
    except Exception:
        connected = False
    return {"connected": connected, "last_sync_at": row["last_push_at"] if row else None,
            "last_full_at": row["last_full_at"] if row else None, "last_error": row["last_error"] if row else None}


def dispatch_today(conn, tz) -> list[dict]:
    day = today_local(tz)
    return [{"lane": r["lane"], "slot_at": r["slot_at"], "status": r["status"], "reason": r["reason"]}
            for r in conn.execute("SELECT * FROM dispatch_slots WHERE local_date = ? ORDER BY slot_at", (day,))]


def _meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def approval_mode(conn, config: dict | None) -> str:
    if cfg(config, "approval.mode", "inherit") == "human":
        return "human"
    return "auto" if _meta(conn, "approval_mode") == "auto" else "human"


def linkedin_on(conn, config: dict | None) -> bool:
    return (_meta(conn, "channel_linkedin_enabled") == "1" and _meta(conn, "linkedin_tos_ack") == "1"
            and bool(cfg(config, "channels.linkedin.enabled", True)))


# ---------------------------------------------------------------- email finder budget (U10, optional)
ENRICH_BUDGET_MODULE = "jobhunter.enrich.budget"
ENRICH_DOC = "docs/EMAIL-FINDER.md"
_FAR_FUTURE = "9999"


def _import_module(module: str):
    """The module, or None when it is missing or fails to import. Other units' modules are optional here: a
    broken one never breaks status, the Sheet or the digest."""
    try:
        return importlib.import_module(module)
    except Exception:
        return None


def _import_attr(module: str, name: str):
    mod = _import_module(module)
    return getattr(mod, name, None) if mod is not None else None


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def fmt_credits(v) -> str:
    """12.0 -> '12', 12.5 -> '12.5', 0.25 -> '0.25'."""
    return ("%.2f" % _num(v)).rstrip("0").rstrip(".") or "0"


def _enrich_used(conn, since: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM enrich_calls WHERE started_at > ? LIMIT 1", (since,)).fetchone() is not None
    except Exception:   # the email finder's tables are not installed
        return False


def _enrich_found(conn, since: str) -> int | None:
    try:
        return _one(conn, "SELECT count(*) FROM enrich_requests WHERE status = 'found' "
                          "AND COALESCE(finished_at, updated_at) > ?", (since,))
    except Exception:
        return None


def email_finder(conn, config: dict | None = None, summary_fn=None) -> dict | None:
    """The optional email finder's budget, from `jobhunter.enrich.budget.summary(conn)` (U10), in the shape
    `status`, the digest and the Limits and settings tab show. None when the feature is not installed or its
    summary fails. Read-only. While the finder is off and unused for 31 days the summary is not called (it
    asks the key store about every service), and the view just says it is off."""
    fn = summary_fn if summary_fn is not None else _import_attr(ENRICH_BUDGET_MODULE, "summary")
    if not callable(fn):
        return None
    now = canon.now()
    since = canon.ts_add(now, days=-31)
    off = {"enabled": False, "paused": False, "breaker": None, "providers": [], "credits_left": 0.0,
           "budget_31d": 0.0, "spent_31d": 0.0, "found_31d": _enrich_found(conn, since), "lookups_24h": 0,
           "unsent_found": 0, "stops": []}
    if summary_fn is None and not cfg(config, "enrich.enabled", False) and not _enrich_used(conn, since):
        return off
    try:
        raw = fn(conn)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    providers, stops = [], []
    left_total = budget_total = spent_total = 0.0
    items = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    for p, r in items.items():
        if not isinstance(r, dict):
            continue
        b31, s31 = _num(r.get("budget_31d")), _num(r.get("spent_31d"))
        left = max(b31 - s31, 0.0)
        if isinstance(r.get("budget_lifetime"), (int, float)):
            left = min(left, max(_num(r.get("budget_lifetime")) - _num(r.get("spent_lifetime")), 0.0))
        exhausted = r.get("exhausted_until")
        exhausted = exhausted if isinstance(exhausted, str) and exhausted > now else None
        item = {"provider": str(p), "name": L.enrich_provider_label(p), "enabled": bool(r.get("enabled")),
                "key": bool(r.get("key")), "spent_31d": s31, "budget_31d": b31, "credits_left": left,
                "spent_24h": _num(r.get("spent_24h")), "day_credits": _num(r.get("day_credits")),
                "requests_24h": int(_num(r.get("requests_24h"))), "day_requests": int(_num(r.get("day_requests"))),
                "exhausted_until": exhausted, "breaker": r.get("breaker") or None}
        spent_total += s31
        if item["enabled"] and item["key"]:   # credits of a service without a key cannot be used
            left_total += left
            budget_total += b31
        if item["breaker"]:
            stops.append(item["name"])
        providers.append(item)
    if raw.get("breaker"):
        stops.insert(0, "Email finder")
    return {"enabled": bool(raw.get("enabled")), "paused": bool(raw.get("paused")),
            "breaker": raw.get("breaker") or None, "providers": providers, "credits_left": left_total,
            "budget_31d": budget_total, "spent_31d": spent_total, "found_31d": _enrich_found(conn, since),
            "lookups_24h": int(_num(raw.get("lookups_24h"))), "unsent_found": int(_num(raw.get("unsent_found"))),
            "stops": stops}


def finder_active(v: dict) -> bool:
    return bool(v["enabled"] or v["spent_31d"] or v["stops"])


def email_finder_line(v: dict | None) -> str | None:
    """One line for `status` and the digest: found in 31 days, free credits left, stops."""
    if v is None:
        return None
    if not finder_active(v):
        return "Email finder: off (optional, see %s)" % ENRICH_DOC
    state = "paused by you" if v["paused"] else ("on" if v["enabled"] else "off")
    parts = []
    if v["found_31d"] is not None:
        parts.append("%d addresses found in 31 days" % v["found_31d"])
    if v["enabled"] and not any(p["enabled"] and p["key"] for p in v["providers"]):
        parts.append("no service connected yet (./jobhunter enrich connect <service>)")
    else:
        parts.append("%s of %s free credits left (31 days)" % (fmt_credits(v["credits_left"]),
                                                              fmt_credits(v["budget_31d"])))
    if v["stops"]:
        parts.append("stopped: " + ", ".join(v["stops"]))
    return "Email finder: %s; %s" % (state, "; ".join(parts))


def _finder_rows(v: dict | None, tz) -> list[list]:
    section = ["Email finder (optional)", "", "", "", "section"]
    if v is None:
        return []
    what = "Looks up work email addresses with your own free API keys. Off by default; see %s." % ENRICH_DOC
    if not finder_active(v):
        return [section, ["Email finder", "Off", "", what, "muted"]]
    what += (" Each service stops before its free credits run out. Used today: lookups in the last 24 hours here, "
             "credits against the daily cap on each service's row.")
    state = "Paused" if v["paused"] else ("On" if v["enabled"] else "Off")
    tone = "bad" if v["breaker"] else ("wait" if v["paused"] else ("good" if v["enabled"] else "muted"))
    if v["breaker"]:
        state = "Stopped"
        what += " " + L.todo_sentence("enrich", v["breaker"])
    rows = [section, ["Email finder", state, "%d lookups" % v["lookups_24h"], what, tone]]
    if v["found_31d"] is not None:
        rows.append(["Addresses found (31 days)", str(v["found_31d"]), "",
                     "Found addresses still pass QC, your limits and the no-duplicates check before any email. "
                     "%d found addresses are not emailed yet." % v["unsent_found"]])
    for p in v["providers"]:
        if not (p["enabled"] or p["key"] or p["spent_31d"] or p["breaker"]):
            continue
        notes = []
        if not p["enabled"]:
            notes.append("Turned off in your settings.")
        notes.append("Key saved." if p["key"] else "No key yet: ./jobhunter enrich connect %s." % p["provider"])
        tone = "good" if p["enabled"] and p["key"] else "muted"
        if p["exhausted_until"]:
            tone = "wait"
            notes.append("The service said the free credits are used up" + (
                "." if p["exhausted_until"].startswith(_FAR_FUTURE) else
                "; skipped until %s." % fmt_local(p["exhausted_until"], tz)))
        if p["breaker"]:
            tone = "bad"
            notes.append("Stopped: %s. %s" % (L.reason_sentence(p["breaker"]),
                                               L.todo_sentence("enrich:" + p["provider"], p["breaker"])))
        rows.append(["Email finder: %s" % p["name"],
                     "%s of %s credits left (31 days)" % (fmt_credits(p["credits_left"]), fmt_credits(p["budget_31d"])),
                     "%s of %s credits" % (fmt_credits(p["spent_24h"]), fmt_credits(p["day_credits"])),
                     " ".join(notes), tone])
    return rows


# ---------------------------------------------------------------- browser consent per site (U1, read only)
# U1's consent API, tried in order: (module, read-only function names). A function may take the connection
# (first parameter named conn) or nothing; one that needs anything else is skipped. Nothing here writes:
# granting and revoking are the owner's commands (./jobhunter init, ./jobhunter browser consent|forget).
CONSENT_APIS = (
    ("jobhunter.consent", ("summary", "status", "sites", "list_sites", "load")),
    ("jobhunter.identity", ("consent_summary", "consent_status", "consent_view", "list_consents", "consents",
                            "load_consent")),
)
# every read-only name above, for an API object handed in directly (tests)
CONSENT_READERS = tuple(dict.fromkeys(n for _mod, names in CONSENT_APIS for n in names))
CONSENT_MAX_BYTES = 1024 * 1024
_CONSENT_META_KEYS = ("chrome_profile", "chrome_profile_name", "profile", "version", "schema", "schema_version",
                      "updated_at", "path")
_STATE_WORDS = {
    "granted": ("granted", "active", "allowed", "yes", "consented", "ok", "on", "true"),
    "revoked": ("revoked", "withdrawn", "forgotten"),
    "not_granted": ("not_granted", "denied", "declined", "no", "not_asked", "none", "off", "false", "missing",
                    "absent", "never"),
    "needs_you": ("needs_you", "expired", "needs_login", "login_required", "logged_out", "mismatch", "checkpoint",
                  "needs_reconsent", "login_failed", "session_expired"),
}
_STATE_OF_WORD = {w: k for k, ws in _STATE_WORDS.items() for w in ws}


def _consent_word(v: str) -> str:
    return _STATE_OF_WORD.get(v.strip().lower().replace("-", "_").replace(" ", "_"), "unknown")


def _consent_state(row: dict) -> str:
    state = None
    for k in ("state", "status", "consent"):
        v = row.get(k)
        if isinstance(v, bool):
            state = "granted" if v else "not_granted"
            break
        if isinstance(v, str) and v.strip():
            state = _consent_word(v)
            break
    if state is None:
        for k in ("active", "granted", "allowed"):
            v = row.get(k)
            if isinstance(v, bool):
                state = "granted" if v else ("revoked" if row.get("revoked_at") else "not_granted")
                break
    if state is None:
        state = "revoked" if row.get("revoked_at") else ("granted" if row.get("granted_at") else "not_granted")
    if state == "granted" and (row.get("login_ok") is False or (
            isinstance(row.get("login_check"), str) and _consent_word(row["login_check"]) == "needs_you")):
        state = "needs_you"
    return state


def _call_readonly(fn, conn):
    import inspect
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        params = []
    if params and params[0].name in ("conn", "db", "connection"):
        if any(p.default is p.empty for p in params[1:]):
            raise TypeError("needs arguments")
        if conn is not None:
            return fn(conn)
        if params[0].default is params[0].empty:
            raise TypeError("needs a connection")
        return fn()
    if any(p.default is p.empty for p in params):
        raise TypeError("needs arguments")
    return fn()


def _consent_rows_of(raw) -> tuple[list[dict], str | None] | None:
    profile = None
    rows = raw
    if isinstance(raw, dict):
        profile = raw.get("chrome_profile_name") or raw.get("chrome_profile") or raw.get("profile")
        for k in ("sites", "rows", "consents"):
            if k in raw:
                rows = raw[k]
                break
        else:
            rows = {k: v for k, v in raw.items() if k not in _CONSENT_META_KEYS}
    items = []
    if isinstance(rows, dict):
        for k, v in rows.items():
            if isinstance(v, dict):
                items.append(dict(v, site=v.get("site") or k))
            elif isinstance(v, (bool, str)):
                items.append({"site": k, "state": v})
    elif isinstance(rows, (list, tuple)):
        for v in rows:
            if isinstance(v, dict):
                items.append(dict(v))
            elif isinstance(v, str):       # a plain list of names: the allowed sites
                items.append({"site": v, "state": "granted"})
    else:
        return None
    return items, (str(profile) if isinstance(profile, str) and profile else None)


def _text(v) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def normalize_consent(raw, known=None) -> dict | None:
    """{chrome_profile, several_profiles, sites: [{site, label, state, chrome_profile, since, method}]} from
    whatever list or mapping the consent API returns (or the rows of private/consent.json). Every site the
    consent step asks about is listed; a site without a row is 'not_granted' (the default is No). A row's
    `chrome_profile` is the Chrome profile's display name when the row has one, else its folder name; the
    top-level one is the profile every allowed site came from (None when they came from several)."""
    got = _consent_rows_of(raw)
    if got is None:
        return None
    items, profile = got
    by_key: dict[str, dict] = {}
    for row in items:
        site = row.get("site") or row.get("name") or row.get("domain")
        if not isinstance(site, str) or not site.strip():
            continue
        key = L.consent_site_key(site)
        state = _consent_state(row)
        if state == "revoked":
            since = _text(row.get("revoked_at"))
        elif state == "not_granted":
            since = _text(row.get("declined_at"))
        else:
            since = _text(row.get("granted_at")) or _text(row.get("updated_at"))
        chrome = _text(row.get("chrome_profile_name")) or _text(row.get("chrome_profile")) or _text(row.get("profile"))
        entry = {"site": key, "label": L.consent_site_label(key), "state": state,
                 "chrome_profile": L.clip(chrome, 60) if chrome else None, "since": since,
                 "method": _text(row.get("method"))}
        prev = by_key.get(key)
        if prev is None or str(entry["since"] or "") >= str(prev["since"] or ""):   # the latest decision wins
            by_key[key] = entry
    order = [L.consent_site_key(x) for x in (known if isinstance(known, (list, tuple)) and known
                                             else L.CONSENT_SITES)]
    sites = []
    for key in order:
        if key in by_key:
            sites.append(by_key.pop(key))
        elif key not in [s["site"] for s in sites]:
            sites.append({"site": key, "label": L.consent_site_label(key), "state": "not_granted",
                          "chrome_profile": None, "since": None, "method": None})
    sites.extend(sorted(by_key.values(), key=lambda e: e["label"].lower()))
    used = sorted({s["chrome_profile"] for s in sites if s["state"] in ("granted", "needs_you")
                   and s["chrome_profile"] and s["method"] not in ("manual_login", "manual")})
    several = len(used) > 1
    if several:
        main = None
    else:
        main = (L.clip(profile, 60) if profile else None) or (used[0] if used else None)
    return {"chrome_profile": main, "several_profiles": several, "sites": sites}


CONSENT_UNREADABLE = {"chrome_profile": None, "sites": [], "error": True}


def read_consent_file():
    """private/consent.json read directly (never written), for when U1's consent API is not installed. A
    missing file means nothing is allowed yet. Like the gate, anything it cannot trust (a symlink, a file of
    another user or one that others can write, a file that is not valid) raises ValueError, shown as Unknown
    and never as allowed. None when this install has no consent file path at all."""
    where = getattr(paths, "consent_file", None)
    if not callable(where):
        return None
    try:
        fd = os.open(where(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"sites": {}}
    except OSError as exc:
        raise ValueError("consent file unreadable: %s" % exc.__class__.__name__)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_size > CONSENT_MAX_BYTES:
            raise ValueError("consent file is not private")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            doc = json.load(fh)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(doc, dict) or not isinstance(doc.get("sites"), dict):
        raise ValueError("consent file has no sites")
    return doc


def browser_consent(conn=None, api=None) -> dict | None:
    """Per-site consent to use the person's Chrome logins, read through U1's consent API (never written here);
    while that API is not installed, private/consent.json is read directly. `api` (tests) is the only source
    when given. None when there is no consent at all in this install (nothing is shown); CONSENT_UNREADABLE
    when it could not be read (shown as Unknown, never as allowed)."""
    if api is not None:
        sources = [(api, CONSENT_READERS)]
    else:
        sources = [(m, names) for m, names in ((_import_module(mod), names) for mod, names in CONSENT_APIS)
                   if m is not None]
    for mod, names in sources:
        for name in names:
            fn = getattr(mod, name, None)
            if not callable(fn):
                continue
            try:
                raw = _call_readonly(fn, conn)
            except TypeError:
                continue
            except Exception:
                return dict(CONSENT_UNREADABLE)
            view = normalize_consent(raw, getattr(mod, "SITES", None))
            if view is not None:
                return view
    if api is not None:
        return dict(CONSENT_UNREADABLE)
    try:
        doc = read_consent_file()
    except (ValueError, OSError):
        return dict(CONSENT_UNREADABLE)
    return None if doc is None else normalize_consent(doc)


CONSENT_ALLOW_CMD = "./jobhunter browser consent"
CONSENT_FORGET_CMD = "./jobhunter browser forget"


def consent_line(v: dict | None) -> str | None:
    """One line for `status` and the digest (None when this install has no browser consent at all)."""
    if v is None:
        return None
    if v.get("error"):
        return "Browser sites: could not read which sites you allowed (%s shows and sets them)" % CONSENT_ALLOW_CMD
    allowed = [s["label"] for s in v["sites"] if s["state"] == "granted"]
    s = "Browser sites allowed: " + (", ".join(allowed) if allowed else "none yet (%s)" % CONSENT_ALLOW_CMD)
    if allowed and v.get("several_profiles"):
        s += " (from several Chrome profiles)"
    elif allowed and v["chrome_profile"]:
        s += ' (Chrome profile "%s")' % v["chrome_profile"]
    needs = [x["label"] for x in v["sites"] if x["state"] in ("needs_you", "unknown")]
    if needs:
        s += "; needs you: %s (%s)" % (", ".join(needs), CONSENT_ALLOW_CMD)
    revoked = [x["label"] for x in v["sites"] if x["state"] == "revoked"]
    if revoked:
        s += "; taken back: " + ", ".join(revoked)
    return s + ("; every other site is off" if allowed else "")


def _consent_rows(v: dict | None, tz) -> list[list]:
    """The Browser sites block of the Limits and settings tab: one intro row, then one row per site."""
    if v is None:
        return []
    rows = [["Browser sites (your Chrome logins)", "", "", "", "section"]]
    if v.get("error"):
        return rows + [["Browser sites", "Unknown", "",
                        "Could not read which sites you allowed, so the agent uses none of them. Run %s to "
                        "check." % CONSENT_ALLOW_CMD, "wait"]]
    main = v.get("chrome_profile")
    shown = "Several (see each site)" if v.get("several_profiles") else (main or "Not chosen")
    rows.append(["Chrome profile", shown, "",
                 "The agent uses a site's login only after you said yes to that site; every site is No until "
                 "then. Only the allowed sites' cookies are copied into the agent's own browser. Your normal "
                 "Chrome is never used. Allow or check: %s" % CONSENT_ALLOW_CMD, "muted"])
    for s in v["sites"]:
        st = s["state"]
        when = fmt_local(s["since"], tz) if s["since"] else ""
        on = (" on " + when) if when else ""
        if st == "granted":
            what = "Allowed%s." % on
            if s["method"] in ("manual_login", "manual"):
                what += " You logged in by hand in the agent's browser."
            elif s["chrome_profile"] and v.get("several_profiles"):    # else the one named at the top
                what += ' Copied from Chrome profile "%s".' % s["chrome_profile"]
            what += " To take it back: %s %s" % (CONSENT_FORGET_CMD, s["site"])
        elif st == "revoked":
            what = "You took this back%s. The agent does not use this site. To allow it again: %s" % (
                on, CONSENT_ALLOW_CMD)
        elif st == "needs_you":
            what = "Allowed, but the login needs you (it expired or shows another account). Check it: %s" % (
                CONSENT_ALLOW_CMD)
        elif st == "unknown":
            what = "Could not read the answer for this site, so the agent does not use it. Check: %s" % (
                CONSENT_ALLOW_CMD)
        else:
            what = "The agent does not use this site."
        rows.append([s["label"], L.CONSENT_STATE_LABEL.get(st, "Unknown"), "", what,
                     L.CONSENT_STATE_TONE.get(st, "wait")])
    return rows


# ---------------------------------------------------------------- status
def build_status(conn, config: dict | None = None) -> dict:
    config = load_config() if config is None else config
    tz = tzinfo(config)
    try:
        home = paths.home()
    except Denied:
        home = {}
    return {
        "state": agent_state(conn),
        "paused": is_paused(),
        "db_path": paths.db_path(),
        "install_id": _meta(conn, "install_id"),
        "home_install_id": home.get("install_id"),
        "schema_version": _meta(conn, "schema_version"),
        "timezone": tz_name(config),
        "approval_mode": approval_mode(conn, config),
        "linkedin_enabled": linkedin_on(conn, config),
        "guard": guard_heartbeat(),
        "breakers": open_breakers(conn),
        "usage": limits_today(conn),
        "queues": queues(conn),
        "last_cycles": last_cycles(conn),
        "sheet": sheet_state(conn),
        "notifications": {"undelivered_high": undelivered_high_count(conn), "items": undelivered(conn, "high", 10)},
        "dispatch": dispatch_today(conn, tz),
        "browser_sites": browser_consent(conn),
        "email_finder": email_finder(conn, config),
        "captcha_tasks": captcha_tasks(conn),
        "env_warnings": paths.env_warnings(),
        "now": canon.now(),
    }


def captcha_tasks(conn) -> list[dict]:
    """Open CAPTCHA hand-offs (FEATURES-OTP-ACCOUNTS-CAPTCHA 4.2), soonest deadline first, with minutes left."""
    try:
        rows = conn.execute("SELECT t.code, t.platform, t.site, t.deadline_at, j.job_uid, j.title, "
                            "COALESCE(co.display_name, j.company_name_raw) AS company FROM captcha_tasks t "
                            "JOIN jobs j ON j.id = t.job_id LEFT JOIN companies co ON co.id = j.company_id "
                            "WHERE t.status = 'open' ORDER BY t.deadline_at").fetchall()
    except sqlite3.DatabaseError:
        return []
    now = canon.now()
    out = []
    for r in rows:
        try:
            left = max(0, int((canon.parse_ts(r["deadline_at"]) - canon.parse_ts(now)).total_seconds() // 60))
        except ValueError:
            left = None
        out.append({"code": r["code"], "job_uid": r["job_uid"], "company": r["company"] or "", "title": r["title"],
                    "site": L.platform_label(r["platform"]) if r["platform"] != "host" else r["site"][5:],
                    "deadline_at": r["deadline_at"], "minutes_left": left})
    return out


def captcha_line(t: dict) -> str:
    return ("captcha %s %s / %s (%s) %s min left: solve it in the agent window, then ./jobhunter continue %s"
            % (t["code"], t["company"], t["title"], t["site"], t["minutes_left"], t["code"]))


def render_status_text(d: dict, tz=None) -> str:
    tz = tz or _dt.timezone.utc
    lines = ["Job Hunter: %s" % d["state"]]
    g = d["guard"]
    if not g["present"]:
        lines.append("Safety plugin: not running (run ./jobhunter doctor)")
    elif not g["fresh"]:
        lines.append("Safety plugin: last seen %s seconds ago (run ./jobhunter doctor)" % g["age_s"])
    else:
        lines.append("Safety plugin: running")
    lines.append("Approval mode: %s" % ("you approve every message" if d["approval_mode"] == "human"
                                        else "QC approves automatically"))
    for b in d["breakers"]:
        s = "Stopped: %s. %s. Since %s." % (b["area"], b["reason"], fmt_local(b["since"], tz))
        if b["until"]:
            s += " Earliest reset %s." % fmt_local(b["until"], tz)
        lines.append(s)
        lines.append("  What to do: " + b["todo"])
    for t in d.get("captcha_tasks") or []:
        lines.append(captcha_line(t))
    q = d["queues"]
    lines.append("Waiting for you: %d approvals, %d tasks, %d jobs to apply to yourself"
                 % (q["approvals_waiting"], q["human_tasks"], q["jobs_needing_you"]))
    lines.append("Queues: %d jobs to evaluate, %d to apply, %d replies to read, %d sends being checked"
                 % (q["jobs_to_evaluate"], q["jobs_to_apply"], q["replies_to_classify"], q["unconfirmed_sends"]))
    used = ["%s %s: %d%s" % (u["area"], u["item"].replace(" today", "").lower(), u["used"],
                             (" of %d" % u["limit"]) if u["limit"] is not None else "") for u in d["usage"]]
    lines.append("Used in the last 24 hours: " + "; ".join(used))
    if d["last_cycles"]:
        parts = ["%s %s (%s)" % (lane, fmt_local(c["started_at"], tz), c["status"])
                 for lane, c in sorted(d["last_cycles"].items())]
        lines.append("Last cycles: " + "; ".join(parts))
    sh = d["sheet"]
    if not sh["connected"]:
        lines.append("Google Sheet: not connected (./jobhunter sheet connect)")
    else:
        s = "Google Sheet: last sync %s" % (fmt_local(sh["last_sync_at"], tz) or "never")
        if sh["last_error"]:
            s += "; last error: %s" % L.clip(sh["last_error"], 160)
        lines.append(s)
    n = d["notifications"]["undelivered_high"]
    if n:
        lines.append("%d important messages could not be delivered to your chat (./jobhunter inbox)" % n)
    sites = consent_line(d.get("browser_sites"))
    if sites:
        lines.append(sites)
    finder = email_finder_line(d.get("email_finder"))
    if finder:
        lines.append(finder)
    planned = [s for s in d["dispatch"] if s["status"] == "planned"]
    if planned:
        lines.append("Planned today: " + ", ".join("%s at %s" % (s["lane"], fmt_local(s["slot_at"], tz)[-5:])
                                                   for s in planned[:8]))
    lines.append("Database: %s (install %s)" % (d["db_path"], d["install_id"]))
    for w in d["env_warnings"]:
        lines.append("Warning: " + w)
    return "\n".join(lines)


# ---------------------------------------------------------------- inbox
def pending_approvals(conn, config: dict | None = None) -> list[dict]:
    style = cfg(config, "sheets.person_name_style", "first_last_initial")
    out = []
    for r in conn.execute(
            "SELECT d.id, d.draft_uid, d.kind, d.subject, d.body, d.expires_at, c.full_name, c.first_name, c.title, "
            "c.role_type, c.email, co.display_name AS company, j.company_name_raw, j.title AS job_title, "
            "(SELECT code FROM approval_codes a WHERE a.draft_id = d.id AND a.closed_at IS NULL) AS code "
            "FROM drafts d LEFT JOIN contacts c ON c.id = d.contact_id "
            "LEFT JOIN companies co ON co.id = COALESCE(d.company_id, c.company_id) "
            "LEFT JOIN jobs j ON j.id = d.job_id WHERE d.status = 'awaiting_approval' ORDER BY d.created_at"):
        out.append({"code": r["code"], "draft_uid": r["draft_uid"], "what": L.DRAFT_KIND.get(r["kind"], r["kind"]),
                    "to": recipient_label(r, style), "company": r["company"] or r["company_name_raw"] or "",
                    "subject": r["subject"] or L.first_line(r["body"]) or (r["job_title"] or ""),
                    "expires_at": r["expires_at"]})
    return out


def recipient_label(r, style: str) -> str:
    """'Meera N. (Head of Analytics)', 'careers@ (role inbox)' or 'Company form'."""
    keys = r.keys() if hasattr(r, "keys") else r
    role_type = r["role_type"] if "role_type" in keys else None
    email = r["email"] if "email" in keys else None
    if role_type == "role_inbox" and email:
        return "%s@ (role inbox)" % str(email).split("@", 1)[0]
    name = L.person_name(r["full_name"], r["first_name"], style) if "full_name" in keys else ""
    if name:
        title = r["title"] if "title" in keys else None
        return "%s (%s)" % (name, title) if title else name
    return "Company form"


def build_inbox(conn, config: dict | None = None) -> dict:
    config = load_config() if config is None else config
    questions, tasks = [], []
    for r in conn.execute("SELECT task_uid, kind, question, detail, created_at FROM human_tasks "
                          "WHERE done_at IS NULL ORDER BY created_at"):
        item = {"task_uid": r["task_uid"], "kind": r["kind"], "question": r["question"] or "",
                "created_at": r["created_at"]}
        (questions if r["kind"] == "answer_question" else tasks).append(item)
    tasks.sort(key=lambda t: 0 if t["kind"] == "captcha" else 1)       # CAPTCHAs first: they have a deadline
    return {"approvals": pending_approvals(conn, config), "questions": questions, "tasks": tasks,
            "captcha_tasks": captcha_tasks(conn), "breakers": open_breakers(conn),
            "undelivered_high": undelivered(conn, "high", 20)}


TASK_LABELS = {
    "apply_manually": "Apply yourself", "answer_question": "Answer a question", "review_reply": "Read a reply",
    "resolve_unknown": "Check whether something was sent", "confirm_not_sent": "Confirm something was not sent",
    "reset_breaker": "Reset a stop", "confirm_profile": "Confirm your profile", "relax_gate": "Decide on a filter",
    "relogin": "Log in again", "confirm_company_merge": "Confirm two companies are the same",
    "confirm_agency": "Confirm a recruiting agency", "review_audit_mismatch": "Review an unrecorded send",
    "connect_mail": "Connect your email", "suggest_auto": "Consider automatic approval",
    "captcha": "Solve a CAPTCHA",
}


def render_inbox_text(d: dict, tz=None) -> str:
    tz = tz or _dt.timezone.utc
    lines = [captcha_line(t) for t in d.get("captcha_tasks") or []]
    if d["approvals"]:
        lines.append("Waiting for your approval (%d):" % len(d["approvals"]))
        for a in d["approvals"]:
            lines.append("  %s  %s to %s, %s: %s (expires %s)" % (a["code"] or a["draft_uid"], a["what"], a["to"],
                                                                a["company"], L.clip(a["subject"], 60),
                                                                fmt_local(a["expires_at"], tz) or "not set"))
        lines.append("  Reply /jh approve CODE or /jh skip CODE, or use the Approvals tab.")
    if d["questions"]:
        lines.append("Questions for you (%d):" % len(d["questions"]))
        for q in d["questions"]:
            lines.append("  %s: %s" % (q["task_uid"], L.clip(q["question"], 200)))
    if d["tasks"]:
        lines.append("Things to do (%d):" % len(d["tasks"]))
        for t in d["tasks"]:
            lines.append("  %s: %s. %s" % (t["task_uid"], TASK_LABELS.get(t["kind"], t["kind"]),
                                          L.clip(t["question"], 200)))
    for b in d["breakers"]:
        lines.append("Stopped: %s. %s. What to do: %s" % (b["area"], b["reason"], b["todo"]))
    if d["undelivered_high"]:
        lines.append("Important messages that did not reach your chat (%d):" % len(d["undelivered_high"]))
        for n in d["undelivered_high"]:
            lines.append("  %s: %s" % (fmt_local(n["created_at"], tz), L.clip(n["text"], 200)))
    return "\n".join(lines) if lines else "Nothing is waiting for you."


# ---------------------------------------------------------------- dashboard and settings (Sheet render)
def _skip_reasons(conn, since: str, limit: int = 6) -> list[list]:
    rows = conn.execute(
        "SELECT status_reason, count(*) AS n FROM jobs WHERE status IN ('prefilter_rejected','rejected') "
        "AND updated_at >= ? GROUP BY status_reason ORDER BY n DESC LIMIT ?", (since, limit)).fetchall()
    return [[L.prefilter_sentence(r["status_reason"]), int(r["n"])] for r in rows]


def dashboard(conn, config: dict | None = None, limits: list | None = None) -> dict:
    """The `render.dashboard` payload of a sync request (shape read by renderDashboard_ in Code.gs)."""
    config = load_config() if config is None else config
    tz = tzinfo(config)
    now = canon.now()
    day = today_local(tz)
    today_start, _ = day_bounds(day, tz)
    end = canon.ts_add(now, seconds=1)
    windows = [("Today", today_start), ("7 days", canon.ts_add(now, days=-7)),
               ("30 days", canon.ts_add(now, days=-30)), ("All time", "0000")]
    act_metrics = ("jobs_found", "passed_filters", "good_fits", "applications", "emails", "follow_ups",
                   "li_invites", "li_messages", "replies", "positive", "interviews")
    counts = [count_activity(conn, start, end, act_metrics) for _, start in windows]
    activity = {"columns": [w[0] for w in windows],
                "rows": [[METRIC_LABELS[m]] + [c[m] for c in counts] for m in act_metrics]}
    week, total = counts[1], counts[3]
    q = queues(conn)
    headline = [
        {"label": "Jobs found (7 days)", "value": week["jobs_found"]},
        {"label": "Applications (7 days)", "value": week["applications"]},
        {"label": "Emails and messages (7 days)",
         "value": week["emails"] + week["li_invites"] + week["li_messages"] + week["follow_ups"]},
        {"label": "Replies (7 days)", "value": week["replies"]},
        {"label": "Waiting for you", "value": q["approvals_waiting"] + q["human_tasks"]},
        {"label": "Interviews and offers (all time)", "value": total["interviews"]},
    ]
    evaluated_total = count_activity(conn, "0000", end, ("evaluated",))["evaluated"]
    funnel = [["Jobs found", total["jobs_found"]], ["Passed your filters", total["passed_filters"]],
              ["Evaluated", evaluated_total], ["Good fits", total["good_fits"]],
              ["Applied", total["applications"]], ["Replies", total["replies"]],
              ["Interviews and offers", total["interviews"]]]
    limits = [{"area": u["area"], "item": u["item"], "used": u["used"], "limit": u["limit"]}
              for u in (limits_today(conn) if limits is None else limits) if u["limit"] is not None]
    safety = []
    if is_paused():
        safety.append({"area": "Everything", "state": "Paused", "since": "", "reason": "You paused the agent",
                       "todo": L.TODO_BY_REASON["paused"]})
    for b in open_breakers(conn):
        safety.append({"area": b["area"], "state": "Stopped", "since": fmt_local(b["since"], tz),
                       "reason": L.reason_sentence(b["reason_code"], b["detail"]), "todo": b["todo"]})
    undelivered_n = undelivered_high_count(conn)
    attention = [
        {"item": "Approvals waiting", "count": q["approvals_waiting"], "where": "Approvals tab"},
        {"item": "Jobs to apply to yourself", "count": q["jobs_needing_you"], "where": "Jobs tab, status Needs you"},
        {"item": "Questions and tasks", "count": q["human_tasks"], "where": "./jobhunter inbox"},
        {"item": "Sends being checked", "count": q["unconfirmed_sends"], "where": "nothing to do yet"},
        {"item": "Messages not delivered to your chat", "count": undelivered_n, "where": "./jobhunter inbox"},
    ]
    attention = [a for a in attention if a["count"]]
    trend_metrics = ("jobs_found", "applications", "emails", "replies")
    series = {m: [] for m in trend_metrics}
    base = _dt.datetime.strptime(day, "%Y-%m-%d")
    for i in range(13, -1, -1):
        d = (base - _dt.timedelta(days=i)).strftime("%Y-%m-%d")
        s, e = day_bounds(d, tz)
        c = count_activity(conn, s, e, trend_metrics)
        for m in trend_metrics:
            series[m].append(c[m])
    trend = {"series": [{"label": METRIC_LABELS[m], "values": series[m]} for m in trend_metrics]}
    return {"agent_state": agent_state(conn), "undelivered_high": undelivered_n, "updated_at": now,
            "headline": headline, "activity": activity, "funnel": funnel, "limits": limits, "safety": safety,
            "attention": attention, "skip_reasons": _skip_reasons(conn, canon.ts_add(now, days=-7)), "trend": trend}


def _warmup_week(conn, platform: str) -> int | None:
    row = conn.execute("SELECT started_at, restarted_at, restart_week FROM warmup WHERE platform = ? "
                       "ORDER BY started_at LIMIT 1", (platform,)).fetchone()
    if not row:
        return None
    start = _parse(row["restarted_at"] or row["started_at"])
    if not start:
        return None
    days = (canon.utcnow() - start).days
    return int(row["restart_week"] or 1) + max(days, 0) // 7


ROUTE_LABEL = {"web_ui": "Browser (web_ui)", "app_password": "App password (app_password)"}
_UNSET = object()


def settings_rows(conn, config: dict | None = None, limits: list | None = None, *, finder=_UNSET,
                  consent=_UNSET) -> list[list]:
    """Rows of the Limits and settings tab: [Setting, Value, Used today, What it means], grouped under
    heading rows. A fifth item is a style for Code.gs: 'section' (a heading across the tab) or the colour of
    the Value cell ('good', 'wait', 'bad', 'muted'). The CSV export keeps the first four. `finder` and
    `consent` are the email_finder() and browser_consent() views (computed here when not given)."""
    config = load_config() if config is None else config
    tz = tzinfo(config)
    finder = email_finder(conn, config) if finder is _UNSET else finder
    consent = browser_consent(conn) if consent is _UNSET else consent
    mode = approval_mode(conn, config)
    route = str(cfg(config, "gmail.route", "web_ui"))
    rows = [
        ["How the agent works", "", "", "", "section"],
        ["Approval mode", "You approve" if mode == "human" else "Automatic after QC", "",
         "human: every message waits for your OK. auto: messages that pass the quality check are sent."],
        ["Email route", ROUTE_LABEL.get(route, route), "",
         "web_ui (default): the agent writes in Gmail in its own browser, with the Gmail login you allowed. "
         "app_password (optional): the agent's code sends email itself with a Google app password."],
    ]
    if route == "app_password":
        rows.append(["Email connected", fmt_local(_meta(conn, "mail_connected_at"), tz) or "No", "",
                     "Run ./jobhunter mail connect to connect or reconnect."])
    else:
        gm = next((x for x in (consent or {}).get("sites", []) if x["site"] == "gmail"), None)
        st = gm["state"] if gm else ("unknown" if consent is None or consent.get("error") else "not_granted")
        rows.append(["Email connected", "Gmail " + L.CONSENT_STATE_LABEL.get(st, "Unknown").lower(), "",
                     "The browser route needs your OK for Gmail (see Browser sites below). Run ./jobhunter init "
                     "to give or check it.", L.CONSENT_STATE_TONE.get(st, "wait")])
    rows += [
        ["LinkedIn actions", "On" if linkedin_on(conn, config) else "Off", "",
         "Off by default. LinkedIn does not allow automation; turning it on is your decision."],
        ["Gmail tier", _meta(conn, "tier_gmail", "conservative"), "", "How high the email limits are."],
        ["LinkedIn tier", _meta(conn, "tier_linkedin", "conservative"), "", "How high the LinkedIn limits are."],
    ]
    for platform in ("gmail", "linkedin"):
        wk = _warmup_week(conn, platform)
        if wk is not None:
            rows.append(["%s warm-up week" % platform.capitalize(), str(wk), "",
                         "New accounts start slowly and speed up week by week."])
    g = guard_heartbeat()
    rows.append(["Safety plugin", "Running" if g["fresh"] else ("Not running" if not g["present"] else "Stale"), "",
                 "Blocks any action the agent is not allowed to take.", "good" if g["fresh"] else "bad"])
    rows.append(["Time zone", tz_name(config), "", "Dates in this sheet use this time zone."])
    rows.append(["Daily limits (last 24 hours)", "", "", "", "section"])
    for u in (limits_today(conn) if limits is None else limits):
        full = u["limit"] is not None and u["used"] >= u["limit"]
        rows.append(["%s: %s" % (u["area"], u["item"].replace(" today", "").lower()),
                     "" if u["limit"] is None else str(u["limit"]), str(u["used"]),
                     "Most allowed in 24 hours; the agent stops at this number."] + (["wait"] if full else []))
    rows += _consent_rows(consent, tz)
    rows += capability_rows(conn, config)
    rows += _finder_rows(finder, tz)
    return rows


CAPABILITY_STATE = {"granted": ("Granted", "good"), "declined": ("Declined", "muted"), "revoked": ("Taken back", "muted"),
                    "not_granted": ("Not asked", "muted"), "unavailable": ("Unavailable: Gmail not allowed", "wait")}


def capability_rows(conn, config: dict | None = None) -> list[list]:
    """The 'Email codes and site accounts' section: one row per site and capability, and the used-today counts
    (FEATURES-OTP-ACCOUNTS-CAPTCHA 4.1). Read only; nothing secret."""
    try:
        from . import identity
        summary = identity.capability_summary(conn, config)
    except Exception:
        return []
    rows = [["Email codes and site accounts", "", "", "", "section"]]
    what = {"email_codes": "may read verification codes and sign-in links this site emails you",
            "ats_accounts": "may create and use an account on this site with your address"}
    for cap in ("email_codes", "ats_accounts"):
        for r in summary.get(cap) or []:
            label, tone = CAPABILITY_STATE.get(r["state"], (r["state"], "muted"))
            if r["state"] == "unavailable" and (cfg(config, "gmail.route", "web_ui") == "app_password"):
                label = "Unavailable: email not connected"
            rows.append(["%s: %s" % (r["label"], "email codes" if cap == "email_codes" else "site accounts"), label,
                         "", "The agent %s. Change it with ./jobhunter browser consent." % what[cap], tone])
    since = canon.ts_add(canon.now(), days=-1)
    try:
        codes = _one(conn, "SELECT count(*) FROM code_uses WHERE used_at > ?", (since,))
        new = _one(conn, "SELECT count(*) FROM ats_accounts WHERE created_at > ?", (since,))
        handoffs = _one(conn, "SELECT count(*) FROM captcha_tasks WHERE opened_at > ?", (since,))
        open_n = _one(conn, "SELECT count(*) FROM captcha_tasks WHERE status = 'open'")
    except sqlite3.DatabaseError:
        return rows
    for label, used, key, why in (
            ("Email codes used (24 h)", codes, "otp.max_uses_day", "Codes and sign-in links used for all sites."),
            ("New site accounts (24 h)", new, "accounts.max_new_day", "Accounts the agent created."),
            ("CAPTCHA hand-offs (24 h)", handoffs, "captcha.max_tasks_day", "CAPTCHAs handed to you to solve."),
            ("Open CAPTCHA tasks", open_n, "captcha.max_open", "Waiting for /jh continue <code>.")):
        limit = cfg(config, key, None)
        rows.append([label, "%d of %s" % (used, limit), str(used), why] +
                    (["wait"] if limit is not None and used >= int(limit) else []))
    return rows
