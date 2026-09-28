"""Pacing (design 4.3): the minimum gap since the last write on a platform, enforced by `gate reserve`
(pace_check, E_PACING with retry_after_s) and waited out by `pace wait` (at most 50 s per call).

- Gmail: gmail.ceilings.<tier>.min_gap_minutes plus a jitter in gap_jitter_minutes, drawn once per
  previous send from HMAC(jitter_seed, token), so every process computes the same gap.
- LinkedIn: write_floor (45 s) since the last LinkedIn write, easy_apply_floor (180 s) between Easy Apply
  submissions; `pace wait --kind write` draws 90 to 300 s (log-normal, median 150 s).
- Boards and ATS forms: boards.site_gap_minutes per site, drawn per previous application.
- Dwell: `pace wait --kind dwell` draws from browser.dwell_seconds; the target is stored in the lock
  `pace:<agent_id>:dwell`, which the guard checks before any commit action.
"""
from __future__ import annotations

import math
import random
import time

from . import config as _config, locks
from .canon import now, parse_ts, seconds_between, ts_add, utcnow
from .ceilings import APP_KINDS, EMAIL_KINDS, LIVE, site_name, unit_draw
from .errors import Denied
from .keys import ATS_NAMES

LI_WRITE_KINDS = ("li_invite", "li_message", "li_followup", "inmail", "li_withdraw", "referral_ask", "application")
WAIT_KINDS = ("write", "dwell", "profile_view", "search_page", "easy_apply")
MAX_SLEEP = 50
_rng = random.SystemRandom()


def _last(conn, platforms, kinds):
    q = "SELECT token, reserved_at FROM actions WHERE route <> 'import' AND status IN (%s)" % ",".join("?" * len(LIVE))
    a = list(LIVE)
    if platforms:
        q += " AND platform IN (%s)" % ",".join("?" * len(platforms))
        a += list(platforms)
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        a += list(kinds)
    return conn.execute(q + " ORDER BY reserved_at DESC, id DESC LIMIT 1", a).fetchone()


def _platforms_for_site(platform: str) -> list[str]:
    site = site_name(platform)
    if site == "ats_forms":
        return list(ATS_NAMES) + ["site:" + a for a in ATS_NAMES] + ["ats", "ats_forms", "site:ats_forms"]
    return [platform, "site:" + site, site]


def required_gap(conn, platform: str, kind: str, cfg: dict) -> tuple[str | None, int]:
    """(last write timestamp or None, required gap in seconds) for the next write of kind on platform."""
    p = (platform or "").lower()
    if p == "gmail":
        c = cfg["gmail"]["ceilings"][cfg["gmail"]["tier"]]
        last = _last(conn, ["gmail"], list(EMAIL_KINDS) + ["referral_ask"])
        if not last:
            return None, 0
        lo, hi = c["gap_jitter_minutes"]
        jitter = lo + (hi - lo) * unit_draw(conn, "gap|" + last["token"])
        return last["reserved_at"], int(round((c["min_gap_minutes"] + jitter) * 60))
    if p == "linkedin":
        d = cfg["linkedin"]["delays_sec"]
        if kind == "application":
            last = _last(conn, ["linkedin"], ["application"])
            return (last["reserved_at"], int(d["easy_apply_floor"])) if last else (None, 0)
        last = _last(conn, ["linkedin"], LI_WRITE_KINDS)
        return (last["reserved_at"], int(d["write_floor"])) if last else (None, 0)
    if kind in APP_KINDS:
        last = _last(conn, _platforms_for_site(p), APP_KINDS)
        if not last:
            return None, 0
        lo, hi = cfg["boards"]["site_gap_minutes"]
        return last["reserved_at"], int(round((lo + (hi - lo) * unit_draw(conn, "site|" + last["token"])) * 60))
    return None, 0


def pace_check(conn, platform: str, kind: str, cfg: dict | None = None) -> None:
    """Denied(E_PACING, retry_after) while the gap since the last write on this platform has not passed."""
    cfg = cfg or _config.load(conn)
    last, gap = required_gap(conn, platform, kind, cfg)
    if not last or gap <= 0:
        return
    elapsed = seconds_between(last, now())
    if elapsed < gap:
        raise Denied("E_PACING", "wait %d s before the next %s write" % (gap - elapsed, platform),
                     retry_after=gap - elapsed, data={"last_write_at": last, "gap_s": gap})


def draw(lo: float, hi: float, median: float | None = None) -> float:
    """exp(N(mu, sigma)) clipped to [lo, hi]; about 95% of draws fall inside without clipping."""
    lo, hi = float(lo), float(max(lo, hi))
    if hi <= lo or lo <= 0:
        return lo
    med = median if median else math.sqrt(lo * hi)
    sigma = (math.log(hi) - math.log(lo)) / (2 * 1.96)
    return min(hi, max(lo, _rng.lognormvariate(math.log(med), sigma)))


def _range(cfg: dict, platform: str, kind: str) -> tuple[float, float, float | None]:
    d = cfg["linkedin"]["delays_sec"]
    if kind == "dwell":
        lo, hi = cfg["browser"]["dwell_seconds"]
        return lo, hi, None
    if kind == "profile_view":
        return d["profile_view"][0], d["profile_view"][1], None
    if kind == "search_page":
        return d["search_page"][0], d["search_page"][1], None
    if kind == "easy_apply":
        return d["easy_apply"][0], d["easy_apply"][1], None
    if (platform or "").lower() == "linkedin":
        return d["write"][0], d["write"][1], 150.0
    if (platform or "").lower() == "gmail":
        return 0, 0, None
    lo, hi = cfg["boards"]["site_gap_minutes"]
    return lo * 60.0, hi * 60.0, None


def lock_name(agent_id: str, platform: str, kind: str) -> str:
    return "pace:%s:dwell" % agent_id if kind == "dwell" else "pace:%s:%s:%s" % (agent_id, platform, kind)


def _last_event(conn, agent_id: str, platform: str, kind: str) -> str | None:
    """The event after which a stored target no longer applies (then a new delay is drawn)."""
    if kind == "dwell":
        row = conn.execute("SELECT armed_at FROM actions WHERE agent_id = ? AND status = 'armed' "
                           "ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
        return row[0] if row and row[0] else None
    if kind in ("profile_view", "search_page"):
        metric = "profile_view" if kind == "profile_view" else "job_search_page"
        row = conn.execute("SELECT max(ts) FROM counters WHERE platform = ? AND metric IN (?, 'people_search', "
                           "'content_search') AND kind = 'inc'", (platform, metric)).fetchone()
        return row[0] if row else None
    kinds = ("application",) if kind == "easy_apply" else None
    row = _last(conn, _platforms_for_site(platform) if platform not in ("gmail", "linkedin") else [platform], kinds)
    return row["reserved_at"] if row else None


def pace_plan(conn, platform: str, kind: str, agent_id: str = "system", cfg: dict | None = None) -> dict:
    """Store (or reuse) the pacing target in locks and return {target_at, remaining_s}. Runs inside the
    caller's transaction and never sleeps."""
    if kind not in WAIT_KINDS:
        raise Denied("E_USAGE", "pace kind must be one of %s" % ", ".join(WAIT_KINDS))
    cfg = cfg or _config.load(conn)
    name = lock_name(agent_id, platform, kind)
    row = locks.get(conn, name)
    ts = now()
    event = _last_event(conn, agent_id, platform, kind)
    reuse = row is not None and (event is None or row["acquired_at"] >= event)
    if kind == "dwell" and event is None:
        raise Denied("E_PRECONDITION", "dwell needs an armed token (gate arm first)")
    if reuse:
        target = row["expires_at"]
    else:
        lo, hi, med = _range(cfg, platform, kind)
        delay = draw(lo, hi, med)
        base = event if (kind == "dwell" and event) else ts
        target = ts_add(base, seconds=int(math.ceil(delay)))
        if kind in ("write", "easy_apply"):
            last, gap = required_gap(conn, platform, "application" if kind == "easy_apply" else kind, cfg)
            if last and gap:
                floor_at = ts_add(last, seconds=gap)
                target = max(target, floor_at)
        ttl = max(1, seconds_between(ts, target))
        conn.execute("INSERT INTO locks (name, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, acquired_at = excluded.acquired_at, "
                     "expires_at = excluded.expires_at", (name, agent_id, ts, ts_add(ts, seconds=ttl)))
        target = ts_add(ts, seconds=ttl)
    remaining = max(0, seconds_between(ts, target))
    return {"lock": name, "target_at": target, "remaining_s": remaining}


def pace_wait(conn, platform: str, kind: str, max_s: int = MAX_SLEEP, agent_id: str = "system",
              sleep=time.sleep) -> dict:
    """Plan (own short transaction), then sleep min(remaining, max_s, 50) outside any transaction.
    Returns {slept_s, remaining_s, target_at}; the agent calls again while remaining_s > 0."""
    from . import db
    if conn.in_transaction:
        raise Denied("E_INTERNAL", "pace_wait must not run inside a transaction (it sleeps)")
    with db.tx(conn):
        plan = pace_plan(conn, platform, kind, agent_id)
    wait = min(plan["remaining_s"], max(0, int(max_s)), MAX_SLEEP)
    if wait > 0:
        sleep(wait)
    left = max(0, int((parse_ts(plan["target_at"]) - utcnow()).total_seconds()))
    if sleep is not time.sleep:
        left = max(0, plan["remaining_s"] - wait)
    return {"slept_s": wait, "remaining_s": left, "target_at": plan["target_at"], "lock": plan["lock"]}
