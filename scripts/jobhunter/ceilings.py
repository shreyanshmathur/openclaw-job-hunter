"""Ceilings, warm-up and usage counters (design 4.1 to 4.4).

Writes are counted from `actions` (every live status counts: reserved, armed, sent, failed_after_click,
unknown, imported); reads and gauges from `counters`. Windows: cycle, hour (60 min), day (24 h), week
(7 days), 30d, utc_day (LinkedIn Easy Apply), month (local calendar month), pst_month (LinkedIn searches,
America/Los_Angeles). Daily caps are jittered: floor(cap * u), u in [1 - daily_jitter_pct/100, 1] from
HMAC(jitter_seed, date + platform + kind) (never below 1 for a positive cap). Warm-up caps (4.4),
adaptive LinkedIn clamps and breaker resume clamps (meta 'clamp:<scope>') apply on top of the tier.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import math

from . import config as _config
from .canon import fmt_ts, now, parse_ts, ts_add, utcnow
from .errors import Denied
from .keys import ATS_NAMES

LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")
EMAIL_KINDS = ("cold_email", "followup_email", "application_email")
APP_KINDS = ("application", "application_email")
LI_WRITES = ("li_invite", "li_message", "li_followup", "inmail", "li_withdraw", "referral_ask", "application")
LI_READ_METRICS = ("profile_view", "people_search", "content_search", "job_search_page", "page_view")
WINDOW_S = {"hour": 3600, "day": 86400, "week": 7 * 86400, "30d": 30 * 86400}


class Limit:
    __slots__ = ("window", "limit", "used", "label", "warmup_week", "clamp_reason", "since", "counter", "binding")

    def __init__(self, label, window, limit, counter, warmup_week=None, clamp_reason=None):
        self.label, self.window, self.limit, self.counter = label, window, limit, counter
        self.warmup_week, self.clamp_reason = warmup_week, clamp_reason
        self.used = 0
        self.since = None
        self.binding = None


# ---------------------------------------------------------------- windows
def window_start(window: str, cfg: dict, at: str | None = None) -> str | None:
    t = at or now()
    if window in WINDOW_S:
        return ts_add(t, seconds=-WINDOW_S[window])
    if window == "utc_day":
        return t[:10] + "T00:00:00Z"
    if window in ("month", "pst_month"):
        tz = _config.tzinfo("America/Los_Angeles" if window == "pst_month" else cfg)
        d = parse_ts(t).astimezone(tz)
        first = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return fmt_ts(first.astimezone(_dt.timezone.utc))
    return None


def _window_end(window: str, since: str, cfg: dict) -> str | None:
    if window == "utc_day":
        return ts_add(since, days=1)
    if window in ("month", "pst_month"):
        tz = _config.tzinfo("America/Los_Angeles" if window == "pst_month" else cfg)
        d = parse_ts(since).astimezone(tz)
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return fmt_ts(d.replace(year=y, month=m, day=1).astimezone(_dt.timezone.utc))
    return None


# ---------------------------------------------------------------- counters
def count_writes(conn, platforms=None, kinds=None, since=None, cycle_id=None, extra="", args=()) -> int:
    q = "SELECT count(*) FROM actions WHERE status IN (%s)" % ",".join("?" * len(LIVE))
    a = list(LIVE)
    if platforms:
        q += " AND platform IN (%s)" % ",".join("?" * len(platforms))
        a += list(platforms)
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        a += list(kinds)
    if since:
        q += " AND reserved_at > ?"
        a.append(since)
    if cycle_id:
        q += " AND cycle_id = ?"
        a.append(cycle_id)
    if extra:
        q += " AND " + extra
        a += list(args)
    return conn.execute(q, a).fetchone()[0]


def count_reads(conn, platform, metrics, since=None, cycle_id=None) -> int:
    q = "SELECT COALESCE(sum(n), 0) FROM counters WHERE kind = 'inc' AND platform = ? AND metric IN (%s)" % \
        ",".join("?" * len(metrics))
    a = [platform] + list(metrics)
    if since:
        q += " AND ts > ?"
        a.append(since)
    if cycle_id:
        q += " AND cycle_id = ?"
        a.append(cycle_id)
    return conn.execute(q, a).fetchone()[0]


def gauge(conn, platform: str, metric: str, max_age_days: int = 7) -> int | None:
    row = conn.execute("SELECT n FROM counters WHERE kind = 'gauge' AND platform = ? AND metric = ? AND ts > ? "
                       "ORDER BY ts DESC, id DESC LIMIT 1",
                       (platform, metric, ts_add(now(), days=-max_age_days))).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- warm-up, jitter, clamps
WARMUP_MAX_WEEK = 12          # every ramp is over by then; later weeks need no promotion checks


def warmup_week(conn, platform: str) -> int:
    """Current warm-up week (4.4). From the (re)start week, each full 7 days promotes one week only when the
    week just ended passes the promotion rule (_week_promotable); otherwise the week is held. A restart
    dated in the future (manual-only days after a restriction) holds the restart week until then."""
    lane = "cold" if platform == "gmail" else "all"
    row = conn.execute("SELECT started_at, restarted_at, restart_week FROM warmup WHERE platform = ? AND lane_kind = ?",
                       (platform, lane)).fetchone()
    if row is None:
        return 1
    base = parse_ts(row["restarted_at"] or row["started_at"])
    week = int(row["restart_week"] or 1)
    full = max(0, (utcnow() - base).days) // 7
    for i in range(full):
        if week >= WARMUP_MAX_WEEK:
            break
        start = base + _dt.timedelta(days=7 * i)
        if _week_promotable(conn, platform, week, fmt_ts(start), fmt_ts(start + _dt.timedelta(days=7))):
            week += 1
    return week


def _week_promotable(conn, platform: str, week: int, start: str, end: str) -> bool:
    """4.4: LinkedIn promotes a week only if it had zero stop events; Gmail leaves week 3 (and later weeks)
    only with zero limit or security warnings, a hard-bounce rate under 2% and zero complaints."""
    if platform == "linkedin":
        return conn.execute("SELECT 1 FROM breaker_events WHERE event = 'trip' AND created_at >= ? AND created_at < ? "
                            "AND (scope = 'linkedin' OR scope LIKE 'linkedin.%') LIMIT 1", (start, end)).fetchone() is None
    if platform == "gmail" and week >= 3:
        if conn.execute("SELECT 1 FROM breaker_events WHERE event = 'trip' AND created_at >= ? AND created_at < ? "
                        "AND (scope = 'gmail' OR scope LIKE 'gmail.%') LIMIT 1", (start, end)).fetchone():
            return False
        if conn.execute("SELECT 1 FROM exclusions WHERE source = 'complaint' AND created_at >= ? AND created_at < ? "
                        "LIMIT 1", (start, end)).fetchone():
            return False
        sent, bounced = conn.execute(
            "SELECT count(*), sum(CASE WHEN t.reply_class = 'bounce' THEN 1 ELSE 0 END) FROM actions a LEFT JOIN "
            "threads t ON t.first_action_id = a.id WHERE a.platform = 'gmail' AND a.kind IN ('cold_email',"
            "'application_email') AND a.status = 'sent' AND a.reserved_at >= ? AND a.reserved_at < ?",
            (start, end)).fetchone()
        if sent and (bounced or 0) / float(sent) >= 0.02:
            return False
    return True


def start_warmup(conn, platform: str) -> None:
    lane = "cold" if platform == "gmail" else "all"
    conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES (?, ?, ?, 1) "
                 "ON CONFLICT (platform, lane_kind) DO NOTHING", (platform, lane, now()))


def _seed(conn) -> bytes:
    row = conn.execute("SELECT value FROM meta WHERE key = 'jitter_seed'").fetchone()
    return (row[0] if row and row[0] else "no-seed").encode("utf-8")


def unit_draw(conn, label: str) -> float:
    """Deterministic value in [0, 1) from HMAC(jitter_seed, label)."""
    h = hmac.new(_seed(conn), label.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(h[:8], "big") / float(2 ** 64)


def jittered_day_cap(conn, cfg: dict, platform: str, kind: str, cap: int) -> int:
    if cap <= 0:
        return cap
    pct = cfg["boards"]["daily_jitter_pct"]
    date = _config.local_date(None, cfg)
    u = 1.0 - (pct / 100.0) * unit_draw(conn, "%s|%s|%s" % (date, platform, kind))
    return max(1, int(math.floor(cap * u)))


def clamp(conn, scope: str) -> tuple[float, str | None]:
    """(factor, reason) of the strictest active clamp on the scope: the stored clamp, then the weaker ones
    that outlast it (breakers.set_clamp keeps them under "then")."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", ("clamp:" + scope,)).fetchone()
    if not row:
        return 1.0, None
    try:
        d = json.loads(row[0])
        ts = now()
        best = None
        for e in [d] + list(d.get("then") or []):
            if e.get("until") and e["until"] > ts and (best is None or float(e.get("factor", 1.0)) < best[0]):
                best = (float(e.get("factor", 1.0)), e)
        if best is not None:
            return best[0], "%s x%s until %s" % (scope, best[1].get("factor"), best[1]["until"])
    except (ValueError, TypeError, AttributeError):
        return 0.0, "clamp:%s unreadable" % scope
    return 1.0, None


def _apply_clamps(conn, lim: int, scopes) -> tuple[int, str | None]:
    reasons = []
    for s in scopes:
        f, why = clamp(conn, s)
        if why:
            lim = int(math.floor(lim * f))
            reasons.append(why)
    return lim, "; ".join(reasons) or None


def acceptance(conn, cfg: dict) -> dict:
    ad = cfg["linkedin"]["adaptive"]
    start = ts_add(now(), days=-int(ad["acceptance_window_days"]))
    end = ts_add(now(), days=-7)
    rows = conn.execute("SELECT a.id, t.state FROM actions a LEFT JOIN threads t ON t.first_action_id = a.id "
                        "WHERE a.kind = 'li_invite' AND a.status = 'sent' AND a.reserved_at > ? AND a.reserved_at <= ?",
                        (start, end)).fetchall()
    n = len(rows)
    acc = sum(1 for r in rows if r["state"] not in (None, "invite_pending", "invite_withdrawn"))
    rate = (acc / float(n)) if n else None
    return {"invites_aged": n, "accepted": acc, "rate": rate, "enough": n >= int(ad["min_invites_aged_7d"])}


def _pending_blocked(conn, cfg: dict, stop: int) -> bool:
    rows = conn.execute("SELECT n FROM counters WHERE kind = 'gauge' AND platform = 'linkedin' "
                        "AND metric = 'li_pending_invites' AND ts > ? ORDER BY ts, id",
                        (ts_add(now(), days=-30),)).fetchall()
    blocked = False
    for (n,) in rows:
        if n >= stop:
            blocked = True
        elif n < 0.8 * stop:
            blocked = False
    return blocked


# ---------------------------------------------------------------- limit tables
def site_name(platform: str) -> str:
    p = (platform or "").lower()
    if p.startswith("site:"):
        p = p[5:]
    if p in ATS_NAMES or p in ("ats", "ats_forms"):
        return "ats_forms"
    return p


def _w(platforms, kinds, extra="", args=()):
    return lambda conn, since, cycle: count_writes(conn, platforms, kinds, since, cycle, extra, args)


def write_limits(conn, cfg: dict, platform: str, kind: str, li_note: int = 0) -> list[Limit]:
    out: list[Limit] = []
    p = (platform or "").lower()
    if p == "gmail":
        tier = cfg["gmail"]["tier"]
        c = cfg["gmail"]["ceilings"][tier]
        allk = list(EMAIL_KINDS) + ["referral_ask"]
        gm = ["gmail"]
        out.append(Limit("gmail total", "day", jittered_day_cap(conn, cfg, "gmail", "total", c["total_day"]),
                         _w(gm, allk)))
        out.append(Limit("gmail sends", "hour", c["hour"], _w(gm, allk)))
        out.append(Limit("gmail sends", "cycle", c["cycle"], _w(gm, allk)))
        if kind == "cold_email":
            wk = warmup_week(conn, "gmail")
            base = jittered_day_cap(conn, cfg, "gmail", "cold", c["cold_day"])
            wu = cfg["gmail"]["warmup"]
            ramp = int(wu["week1_cold_day"]) + int(wu["step_per_week"]) * (wk - 1)
            lim = Limit("cold emails", "day", min(base, ramp), _w(gm, ["cold_email"]), warmup_week=wk)
            lim.binding = "warmup" if ramp < base else None
            out.append(lim)
            out.append(Limit("cold emails", "week", c["cold_week"], _w(gm, ["cold_email"])))
        elif kind == "followup_email":
            out.append(Limit("follow-ups", "day", jittered_day_cap(conn, cfg, "gmail", "followup", c["followup_day"]),
                             _w(gm, ["followup_email"])))
        for l in out:
            scopes = ["gmail"] + (["gmail.cold"] if kind == "cold_email" and l.label == "cold emails" else [])
            l.limit, l.clamp_reason = _apply_clamps(conn, l.limit, scopes)
        if kind == "application_email":
            out += _app_global(conn, cfg)
        return out
    if p == "linkedin":
        tier = cfg["linkedin"]["tier"]
        c = cfg["linkedin"]["ceilings"][tier]
        wk = warmup_week(conn, "linkedin")
        ww = cfg["linkedin"]["warmup_weeks"][wk - 1] if wk <= len(cfg["linkedin"]["warmup_weeks"]) else None
        li = ["linkedin"]
        out.append(Limit("LinkedIn writes", "day", jittered_day_cap(conn, cfg, "linkedin", "writes",
                                                                    c["writes_total_day"]), _w(li, LI_WRITES)))
        out.append(Limit("LinkedIn actions", "day", c["actions_total_day"],
                         lambda cn, s, cy: count_writes(cn, li, LI_WRITES, s, cy) +
                         count_reads(cn, "linkedin", LI_READ_METRICS, s, cy)))
        group, scopes = None, ["linkedin"]
        if kind in ("li_invite",):
            group, wkey, kinds, scopes = c["invites"], "invites", ["li_invite"], ["linkedin", "linkedin.invites"]
        elif kind in ("li_message", "li_followup", "referral_ask"):
            group, wkey, kinds = c["messages"], "messages", ["li_message", "li_followup", "referral_ask"]
            scopes = ["linkedin", "linkedin.messages"]
        elif kind == "inmail":
            free = cfg["channels"]["linkedin"]["account_type"] != "premium"
            group, wkey, kinds = {"day": 0 if free else c["inmail"]["day"]}, "inmail", ["inmail"]
            scopes = ["linkedin", "linkedin.messages"]
        elif kind == "application":
            group, wkey, kinds = c["easy_apply"], "easy_apply", ["application"]
            scopes = ["linkedin", "linkedin.easy_apply"]
        elif kind == "li_withdraw":
            group, wkey, kinds = c["withdraw"], None, ["li_withdraw"]
        if group is not None:
            for window in ("cycle", "hour", "day", "week"):
                if window not in group:
                    continue
                lim_v = group[window]
                win = "utc_day" if (window == "day" and kind == "application") else window
                if window == "day" and lim_v > 0:
                    lim_v = jittered_day_cap(conn, cfg, "linkedin", kind, lim_v)
                lim = Limit(kind, win, lim_v, _w(li, kinds))
                if window == "day" and wkey and ww is not None and wkey in ww:
                    lim.warmup_week = wk
                    if int(ww[wkey]) < lim.limit:
                        lim.limit, lim.binding = int(ww[wkey]), "warmup"
                if window == "week" and kind == "li_invite":
                    lim.counter = lambda cn, s, cy: max(count_writes(cn, li, ["li_invite"], s, cy),
                                                        gauge(cn, "linkedin", "li_invites_sent_7d") or 0)
                out.append(lim)
        if kind == "li_invite":
            if li_note and cfg["channels"]["linkedin"]["account_type"] != "premium":
                out.append(Limit("invite notes (free account)", "month", c["notes_free_month"],
                                 _w(li, ["li_invite"], "li_note = 1")))
            stop = int(c["pending_invites_stop"])
            if _pending_blocked(conn, cfg, stop):
                out.append(Limit("pending invites above %d" % stop, "day", 0, lambda cn, s, cy: 0))
            acc = acceptance(conn, cfg)
            ad = cfg["linkedin"]["adaptive"]
            if acc["enough"] and acc["rate"] is not None:
                if acc["rate"] < ad["pause_below"]:
                    out.append(Limit("invite acceptance %.0f%%" % (acc["rate"] * 100), "day", 0, lambda cn, s, cy: 0))
                elif acc["rate"] < ad["half_below"]:
                    for l in out:
                        if l.label == "li_invite":
                            l.limit = int(math.floor(l.limit * 0.5))
                            l.clamp_reason = "acceptance %.0f%% halves invites" % (acc["rate"] * 100)
        for l in out:
            cl, why = _apply_clamps(conn, l.limit, scopes if l.label == kind else ["linkedin"])
            if why:
                l.limit, l.clamp_reason = cl, why
        if kind == "application":
            out += _app_global(conn, cfg)
        return out
    # boards and ATS forms
    if kind in APP_KINDS:
        out += _app_global(conn, cfg)
        site = site_name(p)
        s = cfg["boards"]["sites"].get(site)
        # every spelling of the platform (naukri and site:naukri are one board; greenhouse and site:greenhouse
        # one ATS), so rows stored under either spelling count toward one ceiling
        plats = list(ATS_NAMES) + ["site:" + a for a in ATS_NAMES] + ["ats", "ats_forms", "site:ats_forms"] \
            if site == "ats_forms" else sorted({p, site, "site:" + site})
        if s is None:
            out.append(Limit("unknown site %s" % site, "day", 0, lambda cn, a, b: 0))
            return out
        for window in ("hour", "day", "week"):
            if window in s:
                v = s[window]
                if window == "day":
                    v = jittered_day_cap(conn, cfg, site, "apply", v)
                out.append(Limit("%s applications" % site, window, v, _w(plats, APP_KINDS)))
        if "month_stop" in s:
            out.append(Limit("%s applications" % site, "month", s["month_stop"], _w(plats, APP_KINDS)))
        for l in out:
            l.limit, l.clamp_reason = _apply_clamps(conn, l.limit, ["site:" + site])
    return out


def _app_global(conn, cfg: dict) -> list[Limit]:
    g = cfg["boards"]["global"]
    return [Limit("all applications", "day", jittered_day_cap(conn, cfg, "boards", "apps", g["apps_day"]),
                  _w(None, APP_KINDS)),
            Limit("all applications", "week", g["apps_week"], _w(None, APP_KINDS)),
            Limit("all applications", "hour", g["apps_hour"], _w(None, APP_KINDS))]


def read_limits(conn, cfg: dict, platform: str, metric: str) -> list[Limit]:
    out: list[Limit] = []
    p = (platform or "").lower()
    if p == "linkedin":
        c = cfg["linkedin"]["ceilings"][cfg["linkedin"]["tier"]]
        wk = warmup_week(conn, "linkedin")
        ww = cfg["linkedin"]["warmup_weeks"][wk - 1] if wk <= len(cfg["linkedin"]["warmup_weeks"]) else None

        def r(metrics):
            return lambda cn, s, cy: count_reads(cn, "linkedin", metrics, s, cy)
        spec = {"profile_view": ("profile_views", ["profile_view"], ("cycle", "hour", "day", "week")),
                "people_search": ("people_search", ["people_search"], ("day", "month")),
                "content_search": ("content_search", ["content_search"], ("day", "week")),
                "job_search_page": ("job_search_pages", ["job_search_page"], ("day",))}.get(metric)
        if spec:
            key, metrics, windows = spec
            for window in windows:
                if window not in c[key]:
                    continue
                win = "pst_month" if (window == "month") else window
                lim = Limit(key, win, c[key][window], r(metrics))
                if window == "day" and ww is not None and key in ww and int(ww[key]) < lim.limit:
                    lim.limit, lim.binding, lim.warmup_week = int(ww[key]), "warmup", wk
                out.append(lim)
        out.append(Limit("LinkedIn actions", "day", c["actions_total_day"],
                         lambda cn, s, cy: count_writes(cn, ["linkedin"], LI_WRITES, s, cy) +
                         count_reads(cn, "linkedin", LI_READ_METRICS, s, cy)))
        for l in out:
            l.limit, l.clamp_reason = _apply_clamps(conn, l.limit, ["linkedin"] +
                                                    (["linkedin.search"] if metric in ("people_search",
                                                                                       "content_search") else []))
        return out
    site = site_name(p)
    s = cfg["boards"]["sites"].get(site)
    if s and "pages_day" in s and metric in ("page_view", "job_search_page"):
        name = p if p.startswith("site:") else "site:" + site
        out.append(Limit("%s pages" % site, "day", s["pages_day"],
                         lambda cn, a, b: count_reads(cn, name, ["page_view", "job_search_page"], a, b) +
                         (count_reads(cn, site, ["page_view", "job_search_page"], a, b) if name != site else 0)))
    return out


# ---------------------------------------------------------------- evaluate
def _evaluate(conn, cfg: dict, limits: list[Limit], cycle_id: str | None) -> list[Limit]:
    for l in limits:
        if l.window == "cycle":
            l.since = None
            l.used = l.counter(conn, None, cycle_id) if cycle_id else 0
        else:
            l.since = window_start(l.window, cfg)
            l.used = l.counter(conn, l.since, None)
    return limits


def _retry_after(conn, cfg: dict, l: Limit, platform: str, n: int = 1) -> int | None:
    if l.limit <= 0 or l.window == "cycle":
        return None
    end = _window_end(l.window, l.since, cfg) if l.since else None
    if end:
        return max(1, int((parse_ts(end) - utcnow()).total_seconds()))
    secs = WINDOW_S.get(l.window)
    if not secs:
        return None
    over = l.used + n - l.limit
    row = conn.execute("SELECT reserved_at FROM actions WHERE status IN (%s) AND platform = ? AND reserved_at > ? "
                       "ORDER BY reserved_at LIMIT 1 OFFSET ?" % ",".join("?" * len(LIVE)),
                       list(LIVE) + [platform, l.since, max(0, over - 1)]).fetchone()
    if row is None:
        return secs
    return max(1, int((parse_ts(ts_add(row[0], seconds=secs)) - utcnow()).total_seconds()))


def check(conn, platform: str, kind: str, cycle_id: str | None = None, li_note: int = 0, cfg: dict | None = None) -> None:
    """Denied(E_CEILING | E_WARMUP) when one more write of kind on platform would pass any window."""
    cfg = cfg or _config.load(conn)
    for l in _evaluate(conn, cfg, write_limits(conn, cfg, platform, kind, li_note), cycle_id):
        if l.used + 1 > l.limit:
            code = "E_WARMUP" if l.binding == "warmup" else "E_CEILING"
            raise Denied(code, "%s: %d of %d used in this %s" % (l.label, l.used, l.limit, l.window.replace("_", " ")),
                         retry_after=_retry_after(conn, cfg, l, platform),
                         data={"window": l.window, "used": l.used, "limit": l.limit, "warmup_week": l.warmup_week,
                               "clamp_reason": l.clamp_reason})


def remaining(conn, platform: str, kind: str, cycle_id: str | None = None, cfg: dict | None = None) -> dict:
    """{cycle, hour, day, week, month}: the smallest remaining count per window."""
    cfg = cfg or _config.load(conn)
    out: dict = {}
    for l in _evaluate(conn, cfg, write_limits(conn, cfg, platform, kind), cycle_id):
        key = {"utc_day": "day", "pst_month": "month"}.get(l.window, l.window)
        rem = max(0, l.limit - l.used)
        out[key] = rem if key not in out else min(out[key], rem)
    return out


def budget(conn, platform: str | None = None, kind: str | None = None, at: str | None = None,
           cycle_id: str | None = None) -> dict:
    """Rows for `budget`: [{platform, kind, window, used, limit, remaining, warmup_week, clamp_reason}]."""
    cfg = _config.load(conn)
    pairs = []
    if platform and kind:
        pairs = [(platform, kind)]
    else:
        std = [("gmail", "cold_email"), ("gmail", "followup_email"), ("gmail", "application_email"),
               ("linkedin", "li_invite"), ("linkedin", "li_message"), ("linkedin", "application"),
               ("linkedin", "profile_view"), ("linkedin", "people_search"), ("greenhouse", "application")]
        pairs = [(p, k) for p, k in std if (platform is None or p == platform) and (kind is None or k == kind)]
        if platform and not pairs:
            pairs = [(platform, "application")]
    rows = []
    seen = set()
    for p, k in pairs:
        limits = read_limits(conn, cfg, p, k) if k in LI_READ_METRICS else write_limits(conn, cfg, p, k)
        for l in _evaluate(conn, cfg, limits, cycle_id):
            sig = (l.label, l.window, p if not l.label.startswith("all ") else "*")
            if sig in seen:
                continue
            seen.add(sig)
            rows.append({"platform": p, "kind": k, "item": l.label, "window": l.window, "used": l.used,
                         "limit": l.limit, "remaining": max(0, l.limit - l.used), "warmup_week": l.warmup_week,
                         "clamp_reason": l.clamp_reason})
    return {"rows": rows, "next_allowed_at": None, "tiers": {"gmail": cfg["gmail"]["tier"],
                                                             "linkedin": cfg["linkedin"]["tier"]},
            "warmup_weeks": {"gmail": warmup_week(conn, "gmail"), "linkedin": warmup_week(conn, "linkedin")},
            "eligibility": {"gmail": tier_eligibility(conn, "gmail", cfg),
                            "linkedin": tier_eligibility(conn, "linkedin", cfg)}}


# ---------------------------------------------------------------- usage
def usage_add(conn, platform: str, metric: str, n: int = 1, cycle_id: str | None = None) -> dict:
    """Count a read (page view, search). Denied(E_CEILING) when it would pass a read ceiling."""
    if n < 1 or n > 1000:
        raise Denied("E_VALIDATION", "n must be 1 to 1000")
    cfg = _config.load(conn)
    limits = _evaluate(conn, cfg, read_limits(conn, cfg, platform, metric), cycle_id)
    for l in limits:
        if l.used + n > l.limit:
            code = "E_WARMUP" if l.binding == "warmup" else "E_CEILING"
            end = _window_end(l.window, l.since, cfg) if l.since else None
            if end:
                retry = max(1, int((parse_ts(end) - utcnow()).total_seconds()))
            else:
                retry = WINDOW_S.get(l.window)
            raise Denied(code, "%s: %d of %d used in this %s" % (l.label, l.used, l.limit, l.window),
                         retry_after=retry, data={"used": l.used, "limit": l.limit, "window": l.window})
    conn.execute("INSERT INTO counters (ts, platform, metric, n, kind, cycle_id) VALUES (?, ?, ?, ?, 'inc', ?)",
                 (now(), platform, metric, n, cycle_id))
    tight = min(limits, key=lambda l: l.limit - l.used) if limits else None
    return {"used": (tight.used + n) if tight else None, "limit": tight.limit if tight else None,
            "window": tight.window if tight else None}


def usage_gauge(conn, platform: str, metric: str, value: int, cycle_id: str | None = None) -> dict:
    if value < 0:
        raise Denied("E_VALIDATION", "gauge values are non-negative")
    conn.execute("INSERT INTO counters (ts, platform, metric, n, kind, cycle_id) VALUES (?, ?, ?, ?, 'gauge', ?)",
                 (now(), platform, metric, int(value), cycle_id))
    return {"platform": platform, "metric": metric, "value": int(value)}


# ---------------------------------------------------------------- tier eligibility (4.4)
def tier_eligibility(conn, platform: str, cfg: dict | None = None) -> dict:
    cfg = cfg or _config.load(conn)
    reasons = []
    lane = "cold" if platform == "gmail" else "all"
    row = conn.execute("SELECT started_at FROM warmup WHERE platform = ? AND lane_kind = ?", (platform, lane)).fetchone()
    days = (utcnow() - parse_ts(row[0])).days if row else 0
    if days < 28:
        reasons.append("needs 28 days on conservative (%d so far)" % days)
    since = ts_add(now(), days=-28)
    stops = conn.execute("SELECT count(*) FROM breaker_events WHERE event = 'trip' AND created_at > ? AND "
                         "(scope = ? OR scope LIKE ? OR scope = 'global')", (since, platform, platform + ".%")).fetchone()[0]
    if stops:
        reasons.append("%d stop events in the last 28 days" % stops)
    if platform == "linkedin":
        acc = acceptance(conn, cfg)
        if not acc["enough"] or (acc["rate"] or 0) < 0.40:
            reasons.append("acceptance must be 40%% or more on 20 or more invites aged 7 days (now %s of %d)"
                           % ("%.0f%%" % (acc["rate"] * 100) if acc["rate"] is not None else "n/a", acc["invites_aged"]))
        age = conn.execute("SELECT value FROM meta WHERE key = 'linkedin_account_age_years'").fetchone()
        try:
            if age is None or float(age[0]) <= 1:
                reasons.append("the account must be older than 1 year")
        except ValueError:
            reasons.append("the account age is unknown")
    else:
        sent = conn.execute("SELECT count(*) FROM actions WHERE platform = 'gmail' AND status = 'sent' "
                            "AND reserved_at > ?", (since,)).fetchone()[0]
        bounced = conn.execute("SELECT count(*) FROM threads WHERE channel = 'email' AND state = 'bounced' "
                               "AND created_at > ?", (since,)).fetchone()[0]
        if sent and bounced / float(sent) >= 0.02:
            reasons.append("hard-bounce rate %.1f%% is 2%% or more" % (100.0 * bounced / sent))
        complaints = conn.execute("SELECT count(*) FROM exclusions WHERE source = 'complaint'").fetchone()[0]
        if complaints:
            reasons.append("complaints on record")
    return {"eligible": not reasons, "reasons": reasons}
