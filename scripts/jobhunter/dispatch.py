"""Dispatcher (design 1.2): the command job `jobhunter:dispatch` runs `jh.py dispatch tick` every 10 minutes.

plan_day(conn, local_date): per lane, `cycles_per_day` slot times drawn with random.SystemRandom inside the
lane's window on its days, at least min_spacing_minutes apart; each slot is dropped (status skipped,
reason random_skip) with skip_probability; browser-lane slots stay 35 minutes apart across lanes.

tick(conn): plans the local day on its first tick, then for every due planned slot: paused, global or lane
breaker, or outside hours -> skipped; no work -> skipped (no_work); browser lease or a running cycle of the
lane -> left planned unless more than 45 minutes late (missed); guard heartbeat stale or without identity
proof version 2 -> skipped (guard_missing, high alert); the lane's cron job differs from its install manifest
spec (ocrun.verify_job, CLI route 6.3.1) -> skipped (cron_drift, audit event and high alert); else
`openclaw cron run <job-id>` -> triggered. tick manages its own transactions (it calls openclaw between them).
"""
from __future__ import annotations

import datetime as _dt
import random

from . import breakers, config as _config, db, gate, locks, ocrun
from .canon import fmt_ts, now, seconds_between, ts_add
from .errors import Denied
from .events import enqueue_notification, log_event

LANES = ("scout", "evaluator", "applier", "outreach", "replies")
BROWSER_LANES = ("scout", "applier", "outreach", "replies")
BROWSER_GAP_S = 35 * 60
MISSED_AFTER_S = 45 * 60
JOB_KEYS = {"scout": "jobhunter:scout", "evaluator": "jobhunter:evaluate", "applier": "jobhunter:apply",
            "outreach": "jobhunter:outreach", "replies": "jobhunter:replies"}
LANE_SCOPES = {"scout": [], "evaluator": [], "applier": ["pause:applications"],
               "outreach": [], "replies": []}
_rng = random.SystemRandom()


def _count(module: str, func: str):
    import importlib
    mod = importlib.import_module("jobhunter." + module)
    return getattr(mod, func)


def default_work_count(conn, lane: str) -> int:
    """How much work a lane has (0 means skip the slot). Missing modules count as no work."""
    try:
        if lane == "evaluator":
            return conn.execute("SELECT count(*) FROM jobs WHERE status = 'eval_queued'").fetchone()[0]
        if lane == "applier":
            return int(_count("applyq", "work_count")(conn))
        if lane == "outreach":
            return int(_count("outreach", "work_count")(conn))
        if lane == "replies":
            n = int(_count("threads", "needs_check_count")(conn))
            from . import reconcile
            return n + len(reconcile.work_list(conn, "browser", "jobhunter-outreach"))
        if lane == "scout":
            return int(_count("searches", "due_count")(conn))
    except (ImportError, AttributeError):
        return 0
    return 0


WORK_COUNT = default_work_count   # tests replace this


def plan_day(conn, local_date: str, cfg: dict | None = None) -> list[dict]:
    """Draw and insert the day's slots (idempotent per lane and date). Runs inside the caller's tx."""
    cfg = cfg or _config.load(conn)
    tz = _config.tzinfo(cfg)
    day = _dt.datetime.strptime(local_date, "%Y-%m-%d").replace(tzinfo=tz)
    weekday = day.isoweekday()
    taken_browser: list[_dt.datetime] = []
    for r in conn.execute("SELECT lane, slot_at FROM dispatch_slots WHERE local_date = ? AND status <> 'skipped'",
                          (local_date,)):
        if r["lane"] in BROWSER_LANES:
            taken_browser.append(_dt.datetime.strptime(r["slot_at"], "%Y-%m-%dT%H:%M:%SZ")
                                 .replace(tzinfo=_dt.timezone.utc))
    out = []
    for lane in LANES:
        spec = cfg["dispatch"]["lanes"][lane]
        if conn.execute("SELECT 1 FROM dispatch_slots WHERE local_date = ? AND lane = ?", (local_date, lane)).fetchone():
            continue
        if weekday not in spec["days"]:
            continue
        lo, hi = spec["cycles_per_day"]
        n = _rng.randint(int(min(lo, hi)), int(max(lo, hi)))
        w0, w1 = _config.hhmm(spec["window"][0]), _config.hhmm(spec["window"][1])
        spacing = int(spec["min_spacing_minutes"]) * 60
        chosen: list[_dt.datetime] = []
        for _ in range(n):
            for _attempt in range(60):
                minute = _rng.randint(w0, max(w0, w1))
                cand = (day + _dt.timedelta(minutes=minute)).astimezone(_dt.timezone.utc)
                if any(abs((cand - c).total_seconds()) < spacing for c in chosen):
                    continue
                if lane in BROWSER_LANES and any(abs((cand - c).total_seconds()) < BROWSER_GAP_S
                                                 for c in taken_browser):
                    continue
                chosen.append(cand)
                if lane in BROWSER_LANES:
                    taken_browser.append(cand)
                break
        for cand in sorted(chosen):
            skip = _rng.random() < float(spec["skip_probability"])
            slot_at = fmt_ts(cand)
            conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status, reason) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT (lane, slot_at) DO NOTHING",
                         (local_date, lane, slot_at, "skipped" if skip else "planned", "random_skip" if skip else None))
            out.append({"lane": lane, "slot_at": slot_at, "status": "skipped" if skip else "planned"})
    return out


NUDGE_META = "dispatch_nudge:applier"


def _nudge(conn, cfg: dict) -> dict | None:
    """After the owner continued a CAPTCHA (meta dispatch_nudge:applier): one extra applier slot now, when inside
    the lane's window and days and under the lane's hard cycles-per-day maximum; the nudge is used up either way.
    Runs inside the caller's tx."""
    from . import hardmax
    at = db.meta_get(conn, NUDGE_META)
    if not at:
        return None
    db.meta_delete(conn, NUDGE_META)
    spec = cfg["dispatch"]["lanes"]["applier"]
    d = _config.now_local(cfg)
    if d.isoweekday() not in spec["days"] or not _config.in_window(d, spec["window"]):
        return None
    local_date = _config.local_date(None, cfg)
    n = conn.execute("SELECT count(*) FROM dispatch_slots WHERE local_date = ? AND lane = 'applier' AND status IN "
                     "('planned','triggered')", (local_date,)).fetchone()[0]
    if n >= int(hardmax.LANE_CYCLES_MAX["applier"][1]):
        return None
    slot_at = now()
    conn.execute("INSERT INTO dispatch_slots (local_date, lane, slot_at, status, reason) VALUES (?, 'applier', ?, "
                 "'planned', 'captcha_resume') ON CONFLICT (lane, slot_at) DO NOTHING", (local_date, slot_at))
    log_event(conn, "dispatch_nudge", lane="applier", slot_at=slot_at)
    return {"lane": "applier", "slot_at": slot_at, "status": "planned", "reason": "captcha_resume"}


def _lane_blocked(conn, cfg: dict, lane: str) -> str | None:
    if breakers.is_paused():
        return "paused"
    scopes = breakers.open_scopes(conn)
    if "global" in scopes:
        return "breaker_global"
    for s in LANE_SCOPES[lane]:
        if s in scopes:
            return "breaker_" + s
    if lane == "outreach" and "gmail" in scopes and "linkedin" in scopes:
        return "breaker_gmail_linkedin"
    if lane in BROWSER_LANES:
        ah = cfg["active_hours"]
        d = _config.now_local(cfg)
        if d.isoweekday() not in ah["browser_days"] or not _config.in_window(d, ah["browser_window"]):
            return "outside_hours"
    return None


def tick(conn, work_count=None, cron_run=None, verify_job=None) -> dict:
    """One dispatcher tick (see module doc). Returns {triggered: [...], skipped: [...], planned}.
    `verify_job(key, listing)` (default ocrun.verify_job) raises Denied(E_CRON_DRIFT) for a drifted job."""
    work_count = work_count or WORK_COUNT
    cron_run = cron_run or ocrun.cron_run
    verify_job = verify_job or ocrun.verify_job
    with db.tx(conn):
        cfg = _config.load(conn)
        breakers.close_expired(conn)
        planned = []
        if cfg["dispatch"]["mode"] == "dispatcher":
            today = _config.local_date(None, cfg)
            planned = plan_day(conn, today, cfg)
        if cfg["dispatch"]["mode"] == "dispatcher":
            nudged = _nudge(conn, cfg)
            if nudged:
                planned = list(planned) + [nudged]
        from . import otp
        otp.expire_waiting(conn, cfg)
        due = conn.execute("SELECT * FROM dispatch_slots WHERE status = 'planned' AND slot_at <= ? ORDER BY slot_at",
                           (now(),)).fetchall()
    try:
        from . import captcha
        captcha.expire_all(conn)
    except Exception as exc:   # a CAPTCHA timeout must never stop the dispatcher
        log_event(conn, "captcha_expire_failed", error="%s: %s" % (type(exc).__name__, exc))
    triggered, skipped, left = [], [], []
    if cfg["dispatch"]["mode"] != "dispatcher":
        return {"triggered": [], "skipped": [], "planned": [], "mode": cfg["dispatch"]["mode"]}
    job_ids = ocrun.cron_job_ids()
    failures = []
    listing = {"doc": None}

    def drift_of(lane: str):
        """None when the lane's job matches its spec, else (reason, data) (one cron list per tick)."""
        try:
            if listing["doc"] is None and verify_job is ocrun.verify_job:
                res = ocrun.cron_list()
                if not res["ok"]:
                    return "cron_list_failed", {"error": res.get("error")}
                listing["doc"] = res["doc"]
            verify_job(JOB_KEYS[lane], listing["doc"])
        except Denied as d:
            if d.code == "E_CRON_DRIFT":
                return "cron_drift", {"fields": (d.data or {}).get("fields") or []}
            return "cron_list_failed", {"error": d.message}
        return None

    for slot in due:
        lane = slot["lane"]
        reason = None
        with db.tx(conn):
            late = seconds_between(slot["slot_at"], now())
            reason = _lane_blocked(conn, cfg, lane)
            if reason is None and work_count(conn, lane) <= 0:
                reason = "no_work"
            busy = False
            if reason is None:
                running = conn.execute("SELECT 1 FROM cycles WHERE lane = ? AND status = 'running' AND started_at > ?",
                                       (lane, ts_add(now(), hours=-1))).fetchone()
                busy = bool(running) or (lane in BROWSER_LANES and locks.is_held(conn, "browser"))
                if busy:
                    if late > MISSED_AFTER_S:
                        conn.execute("UPDATE dispatch_slots SET status = 'missed', reason = 'busy' WHERE id = ?",
                                     (slot["id"],))
                        skipped.append({"lane": lane, "slot_at": slot["slot_at"], "reason": "missed"})
                    else:
                        left.append({"lane": lane, "slot_at": slot["slot_at"]})
                    continue
            if reason is None:
                hb = gate.guard_heartbeat()
                if not hb["fresh"] or hb.get("proof_version") != 2:
                    reason = "guard_missing"
                    text = ("The jobhunter-guard plugin is not running: no agent cycle starts until it is back "
                            "(openclaw plugins list).") if not hb["fresh"] else \
                        ("The jobhunter-guard plugin is an old version without identity proof version 2: no agent "
                         "cycle starts until ./install.sh has run.")
                    enqueue_notification(conn, "guard_missing:%s" % now()[:13], "high", "alert", text)
            if reason is None and JOB_KEYS[lane] not in job_ids:
                reason = "job_id_unknown"
            if reason is not None:
                conn.execute("UPDATE dispatch_slots SET status = 'skipped', reason = ? WHERE id = ?", (reason, slot["id"]))
                skipped.append({"lane": lane, "slot_at": slot["slot_at"], "reason": reason})
                continue
        drift = drift_of(lane)          # openclaw cron list, outside any transaction
        if drift is not None:
            reason, data = drift
            with db.tx(conn):
                conn.execute("UPDATE dispatch_slots SET status = 'skipped', reason = ? WHERE id = ?", (reason, slot["id"]))
                log_event(conn, "cron_drift" if reason == "cron_drift" else "dispatch_failed", lane=lane,
                          key=JOB_KEYS[lane], **data)
                if reason == "cron_drift":
                    enqueue_notification(conn, "cron_drift:%s:%s" % (lane, now()[:10]), "high", "alert",
                                         "The %s automation was changed outside Job Hunter, so it does not start. "
                                         "Run ./install.sh again to repair it." % lane)
            skipped.append({"lane": lane, "slot_at": slot["slot_at"], "reason": reason})
            continue
        res = cron_run(job_ids[JOB_KEYS[lane]])
        with db.tx(conn):
            if res.get("ok"):
                conn.execute("UPDATE dispatch_slots SET status = 'triggered', triggered_at = ? WHERE id = ?",
                             (now(), slot["id"]))
                triggered.append({"lane": lane, "slot_at": slot["slot_at"], "job_id": job_ids[JOB_KEYS[lane]]})
                log_event(conn, "dispatch_triggered", lane=lane, slot_at=slot["slot_at"])
            else:
                failures.append({"lane": lane, "error": res.get("error")})
                log_event(conn, "dispatch_failed", lane=lane, error=res.get("error"))
    return {"triggered": triggered, "skipped": skipped, "waiting": left, "planned": planned, "failures": failures}


def slots(conn, local_date: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT lane, slot_at, status, reason, triggered_at, cycle_id FROM "
                                          "dispatch_slots WHERE local_date = ? ORDER BY slot_at", (local_date,))]
