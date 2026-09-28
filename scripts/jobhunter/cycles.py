"""Agent cycles: preflight, lease renewal and cycle end (design 1.3, 1.1.2).

preflight(conn, lane, agent_id) decides go / no-go in this order: kill switch, global breaker, guard
heartbeat, clock, active hours and display (browser lanes), profile confirmed (evaluator, applier,
outreach), the owner's per-site browser consent (browser lanes, identity.lane_consent), planned slot
(cron_schedule fallback), browser lease. It also expires stale tokens, applies a changed config file, imports
a changed exclusions file and trips the breaker of a site whose consent was revoked (identity.sync_consent).
Once a browser cycle holds the lease, close_stale() puts every token its agent left reserved or armed in an
earlier cycle to unknown and ends that cycle as error: a crash between the click and confirm never leaves a
live token for the next cycle (design 0, principle 2).
The result is returned (never raised) so the cycle row is committed; the command turns go: false into its
exit code.
"""
from __future__ import annotations

import os
import platform as _platform
import subprocess

from . import breakers, ceilings, config as _config, gate, locks
from .canon import new_cycle_id, now, seconds_between, ts_add
from .errors import Denied
from .events import log_event

LANE_AGENTS = {"scout": "jobhunter-scout", "evaluator": "jobhunter-evaluator", "applier": "jobhunter-applier",
               "outreach": "jobhunter-outreach", "replies": "jobhunter-outreach"}
BROWSER_LANES = ("scout", "applier", "outreach", "replies")
PROFILE_LANES = ("evaluator", "applier", "outreach")
LEASE = "browser"


def _dep(name: str):
    import importlib
    return importlib.import_module("jobhunter." + name)


def profile_confirmed(conn) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'profile_version'").fetchone()
    if not row or not row[0]:
        return False
    try:
        st = _dep("profile").status()
        return bool(st.get("confirmed"))
    except ImportError:
        return True
    except Exception:
        return False


def battery_low(cfg: dict) -> bool:
    """macOS: on battery power and below active_hours.battery_floor_pct."""
    if _platform.system() != "Darwin":
        return False
    try:
        out = subprocess.run(["pmset", "-g", "batt"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=5).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return False
    if "Battery Power" not in out:
        return False
    import re
    m = re.search(r"(\d+)%", out)
    return bool(m) and int(m.group(1)) < int(cfg["active_hours"]["battery_floor_pct"])


def has_display(env: dict) -> bool:
    if _platform.system() == "Darwin":
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def clock_check(conn, cfg: dict) -> str | None:
    """Trips 'global' (clock_skew) when the clock jumped backwards; returns the reason or None."""
    from . import db
    ts = now()
    row = conn.execute("SELECT value FROM meta WHERE key = 'max_seen_ts'").fetchone()
    seen = row[0] if row else None
    back = int(cfg["clock"]["max_backward_minutes"]) * 60
    if seen:
        try:
            if seconds_between(ts, seen) > back:
                breakers.trip(conn, "global", "clock_skew", "the clock went back from %s to %s" % (seen, ts))
                return "clock_backward"
        except ValueError:
            pass
    if not seen or ts > seen:
        db.meta_set(conn, "max_seen_ts", ts, "system")
    return None


LINKEDIN_LANES = ("outreach", "replies")
# A LinkedIn cycle is a cycle (any lane) that did something on LinkedIn: a page check, a counted read or a
# write carries its cycle id.
_LI_ACTIVE = ("(EXISTS (SELECT 1 FROM actions a WHERE a.platform = 'linkedin' AND a.reserved_at >= c.started_at "
              "AND a.cycle_id = c.cycle_id) OR "
              "EXISTS (SELECT 1 FROM counters k WHERE k.platform = 'linkedin' AND k.ts >= c.started_at "
              "AND k.cycle_id = c.cycle_id) OR "
              "EXISTS (SELECT 1 FROM detections d WHERE d.platform = 'linkedin' AND d.created_at >= c.started_at "
              "AND d.cycle_id = c.cycle_id))")


def linkedin_window(conn, cfg: dict, lane: str, cycle_id: str | None = None) -> dict:
    """Whether a cycle may write on LinkedIn now (4.3): inside the day window, at least
    between_cycles_min[0] minutes after the previous LinkedIn cycle ended, and fewer than cycles_day LinkedIn
    cycles in the 24 hours before. Counts are taken at the start of `cycle_id` (default now) and leave that
    cycle out; gate.reserve enforces the result for every LinkedIn write."""
    enabled = bool(cfg["channels"]["linkedin"]["enabled"])
    out = {"enabled": enabled, "ok": False}
    if not enabled or lane not in LINKEDIN_LANES + ("scout", "applier"):
        return out
    d = _config.now_local(cfg)
    start, end = gate.linkedin_day_window(conn, cfg, d.strftime("%Y-%m-%d"))
    in_win = gate.hours_ok(conn, cfg, "li_message", "linkedin")
    lo = int(cfg["linkedin"]["delays_sec"]["between_cycles_min"][0]) * 60
    ref = now()
    if cycle_id:
        row = conn.execute("SELECT started_at FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        if row is not None:
            ref = row[0]
    base = ("FROM cycles c WHERE c.status <> 'no_go' AND c.cycle_id IS NOT ? AND c.started_at <= ? AND "
            "c.started_at > ? AND %s" % _LI_ACTIVE)
    last = conn.execute("SELECT max(COALESCE(c.ended_at, c.started_at)) " + base,
                        (cycle_id, ref, ts_add(ref, days=-2))).fetchone()[0]
    idle_s = None if last is None else int(seconds_between(last, ref))
    idle_ok = idle_s is None or idle_s >= lo
    tier = cfg["linkedin"]["tier"]
    cmax = cfg["linkedin"]["ceilings"][tier]["cycles_day"]
    today = conn.execute("SELECT count(*) " + base, (cycle_id, ref, ts_add(ref, days=-1))).fetchone()[0]
    out.update(start=start, end=end, in_window=in_win, idle_ok=idle_ok, idle_s=idle_s, idle_min_s=lo,
               cycles_today=today, cycles_max=cmax, ok=bool(in_win and idle_ok and today < cmax))
    return out


def _work(conn, lane: str, agent_id: str, cycle_id: str) -> dict:
    try:
        if lane == "scout":
            s = _dep("searches")
            return {"searches": s.due(conn, cycle_id)}
        if lane == "evaluator":
            n = conn.execute("SELECT count(*) FROM jobs WHERE status = 'eval_queued'").fetchone()[0]
            return {"eval_queued": n}
        if lane == "applier":
            return {"applyq": _dep("applyq").work_list(conn, 10)}
        if lane == "outreach":
            return {"targets": _dep("outreach").work_list(conn, 10),
                    "reconcile": _dep("reconcile").work_list(conn, "browser", agent_id)}
        if lane == "replies":
            return {"threads": _dep("threads").needs_check_count(conn),
                    "reconcile": _dep("reconcile").work_list(conn, "browser", agent_id)}
    except ImportError as exc:
        return {"unavailable": str(exc)}
    except Denied as d:
        return {"error": d.code, "message": d.message}
    return {}


def _planned_slot(conn, lane: str):
    return conn.execute("SELECT * FROM dispatch_slots WHERE lane = ? AND status IN ('planned','triggered') AND "
                        "slot_at <= ? AND slot_at > ? AND cycle_id IS NULL ORDER BY slot_at DESC LIMIT 1",
                        (lane, now(), ts_add(now(), minutes=-60))).fetchone()


def preflight(conn, lane: str, agent_id: str, env: dict | None = None) -> dict:
    """{go, code, cycle_id, reasons, budget, work, identity_required, linkedin_window, searches}. Runs inside
    the caller's transaction."""
    from . import exclusions
    env = env if env is not None else dict(os.environ)
    if lane not in LANE_AGENTS:
        raise Denied("E_USAGE", "unknown lane %r" % lane)
    if agent_id != LANE_AGENTS[lane] and agent_id not in ("system", "human"):
        raise Denied("E_CALLER_NOT_ALLOWED", "lane %s belongs to %s" % (lane, LANE_AGENTS[lane]))
    ts = now()
    cycle_id = new_cycle_id(ts)
    gate.expire(conn)
    cfg_row = conn.execute("SELECT value FROM meta WHERE key = 'config_sha256'").fetchone()
    if (cfg_row[0] if cfg_row else None) != _config.file_sha256():
        _config.apply(conn)
    if exclusions.file_changed(conn):
        exclusions.import_csv(conn, None, False, None)
    from . import companies, identity
    if companies.aliases_changed(conn):
        companies.import_aliases(conn)
    identity.sync_consent(conn)
    cfg = _config.load(conn)
    consent = identity.lane_consent(cfg, lane) if lane in BROWSER_LANES else None
    reasons: list[str] = []
    code = None
    retry = None
    if breakers.is_paused():
        code, reasons = "E_PAUSED", ["paused"]
    elif "global" in breakers.open_scopes(conn):
        code, reasons = "E_BREAKER_OPEN", ["global breaker open"]
    if code is None:
        hb = gate.guard_heartbeat()
        if not hb["fresh"]:
            code, reasons = "E_GUARD_MISSING", ["guard heartbeat missing or stale"]
    if code is None and clock_check(conn, cfg):
        code, reasons = "E_CLOCK_SKEW", ["clock jumped backwards"]
    if code is None and lane in BROWSER_LANES:
        ah = cfg["active_hours"]
        d = _config.now_local(cfg)
        if d.isoweekday() not in ah["browser_days"] or not _config.in_window(d, ah["browser_window"]):
            code, reasons = "E_OUTSIDE_HOURS", ["outside the browser hours"]
            tz = _config.tzinfo(cfg)
            retry = _config.seconds_until(lambda t: t.astimezone(tz).isoweekday() in ah["browser_days"] and
                                          _config.in_window(t.astimezone(tz), ah["browser_window"]))
        elif cfg["browser"]["require_display"] and not has_display(env):
            code, reasons = "E_ROUTE_UNAVAILABLE", ["headless_host"]
        elif battery_low(cfg):
            code, reasons = "E_ROUTE_UNAVAILABLE", ["battery"]
    if code is None and lane in PROFILE_LANES and not profile_confirmed(conn):
        code, reasons = "E_PROFILE_UNCONFIRMED", ["confirm your profile first (profile interview)"]
    if code is None and lane == "applier" and "pause:applications" in breakers.open_scopes(conn):
        code, reasons = "E_BREAKER_OPEN", ["applications are paused"]
    if code is None and consent is not None and consent["sites"] and not consent["allowed"] \
            and not consent["public_work"]:
        # every login site of this lane lacks the owner's consent: nothing to do without using one
        code, reasons = "E_CONSENT_MISSING", ["no browser consent for %s (./jobhunter browser consent)"
                                              % ", ".join(consent["missing"])]
    slot = _planned_slot(conn, lane)
    if code is None and cfg["dispatch"]["mode"] == "cron_schedule" and slot is None:
        code, reasons = "E_OUTSIDE_HOURS", ["not_planned_slot"]
    stale = None
    if code is None and lane in BROWSER_LANES:
        if not locks.acquire(conn, LEASE, cycle_id, int(cfg["browser"]["lease_minutes"]) * 60):
            code, reasons = "E_LOCKED", ["another browser cycle holds the lease"]
        else:
            stale = close_stale(conn, LANE_AGENTS[lane], cycle_id)
    status = "running" if code is None else "no_go"
    conn.execute("INSERT INTO cycles (cycle_id, lane, agent_id, slot_id, started_at, ended_at, status) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (cycle_id, lane, agent_id, slot["id"] if slot is not None else None, ts,
                                                  ts if code else None, status))
    if slot is not None and code is None:
        conn.execute("UPDATE dispatch_slots SET cycle_id = ? WHERE id = ?", (cycle_id, slot["id"]))
    log_event(conn, "preflight", lane=lane, cycle_id=cycle_id, go=code is None, code=code)
    out = {"go": code is None, "code": code or "OK", "cycle_id": cycle_id, "reasons": reasons, "retry_after_s": retry}
    if consent is not None:
        # the login sites this lane may use now; gate reserve, usage add and identity check refuse the others
        out["consent"] = {"allowed": consent["allowed"], "missing": consent["missing"]}
    if stale and stale["tokens"]:
        out["stale"] = stale
    if code is not None:
        return out
    platforms = {"scout": ["linkedin"], "evaluator": [], "applier": ["greenhouse", "gmail"],
                 "outreach": ["gmail", "linkedin"], "replies": ["linkedin"]}[lane]
    rows = []
    for p in platforms:
        try:
            rows += ceilings.budget(conn, p, None, cycle_id=cycle_id)["rows"]
        except Denied:
            pass
    ident = []
    # only lanes whose agent may run `identity check` (acl.json: applier, outreach); the scout only reads
    if lane in ("outreach", "replies") and cfg["channels"]["linkedin"]["enabled"]:
        ident.append("linkedin")
    if lane in ("applier", "outreach", "replies") and cfg["gmail"]["route"] == "web_ui":
        ident.append("gmail")
    if consent is not None:
        ident = [p for p in ident if p in consent["allowed"]]
    out.update(budget=rows, work=_work(conn, lane, agent_id, cycle_id), identity_required=ident,
               linkedin_window=linkedin_window(conn, cfg, lane, cycle_id=cycle_id),
               open_breakers=[s for s in breakers.open_scopes(conn) if s != "global"])
    if lane == "scout":
        out["searches"] = out["work"].get("searches", [])
    return out


def renew(conn, cycle_id: str, agent_id: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no cycle %s" % cycle_id)
    if agent_id and row["agent_id"] != agent_id:
        raise Denied("E_CALLER_NOT_ALLOWED", "the cycle belongs to %s" % row["agent_id"])
    if row["status"] != "running":
        raise Denied("E_LOCKED", "the cycle is %s" % row["status"])
    cfg = _config.load(conn)
    ttl = int(cfg["browser"]["lease_minutes"]) * 60
    if not locks.renew(conn, LEASE, cycle_id, ttl):
        raise Denied("E_LOCKED", "this cycle does not hold the browser lease")
    return {"expires_at": ts_add(now(), seconds=ttl)}


def end(conn, cycle_id: str, summary: dict | None, agent_id: str | None = None) -> dict:
    """Close a cycle: release its leases and claims, put open tokens to unknown, remove staged files."""
    row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no cycle %s" % cycle_id)
    if agent_id and row["agent_id"] not in (agent_id, None):
        raise Denied("E_CALLER_NOT_ALLOWED", "the cycle belongs to %s" % row["agent_id"])
    ts = now()
    import json
    released = _release(conn, cycle_id, "cycle ended with the token open")
    status = "ok" if row["status"] == "running" else row["status"]
    conn.execute("UPDATE cycles SET ended_at = COALESCE(ended_at, ?), status = ?, summary_json = ? WHERE cycle_id = ?",
                 (ts, status, json.dumps(summary, sort_keys=True)[:8000] if summary is not None else None, cycle_id))
    log_event(conn, "cycle_end", cycle_id=cycle_id, status=status, released=released)
    return {"reply": "NO_REPLY", "cycle_id": cycle_id, "status": status, "released": released}


def _release(conn, cycle_id: str, token_note: str) -> dict:
    """Release what a cycle holds: its locks, its job claims, its open tokens (to unknown) and staged files."""
    ts = now()
    released = {"locks": locks.release_holder(conn, cycle_id), "jobs": 0, "tokens": 0, "staged": 0}
    from . import jobstate
    for j in conn.execute("SELECT id, status FROM jobs WHERE claimed_by = ?", (cycle_id,)).fetchall():
        back = {"evaluating": "eval_queued", "apply_queued": "eligible"}.get(j["status"])
        if back:
            jobstate.set_job_status(conn, j["id"], back, "cycle_end", "cycles.end")
        conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL, updated_at = ? WHERE id = ?", (ts, j["id"]))
        released["jobs"] += 1
    for (tok,) in conn.execute("SELECT token FROM actions WHERE cycle_id = ? AND status IN ('reserved','armed')",
                               (cycle_id,)).fetchall():
        gate.mark_unknown(conn, tok, note=token_note)
        released["tokens"] += 1
    for sf in conn.execute("SELECT s.token, s.path FROM staged_files s JOIN actions a ON a.token = s.token "
                           "WHERE a.cycle_id = ? AND s.removed_at IS NULL", (cycle_id,)).fetchall():
        try:
            _dep("resume").unstage(conn, sf["token"])
        except Exception:
            try:
                os.unlink(sf["path"])
            except OSError:
                pass
            conn.execute("UPDATE staged_files SET removed_at = ? WHERE token = ?", (ts, sf["token"]))
        released["staged"] += 1
    return released


def close_stale(conn, agent_id: str, cycle_id: str) -> dict:
    """Run by preflight once the new cycle holds the browser lease, so no other browser cycle of this agent is
    alive. A reserved or armed token the agent still holds belongs to a cycle that died between reserve and
    confirm (a crash, a lapsed lease): it becomes unknown (reconcile decides; it can never be sent again under
    the old token, and the guard finds no open token to allow a second Send), and that cycle ends as error with
    its claims and staged files released (design 0 principle 2, 2.3.3)."""
    rows = conn.execute("SELECT token, cycle_id FROM actions WHERE agent_id = ? AND status IN ('reserved','armed') "
                        "AND cycle_id IS NOT ? ORDER BY id", (agent_id, cycle_id)).fetchall()
    out = {"tokens": [], "cycles": []}
    if not rows:
        return out
    ts = now()
    done: set = set()
    for r in rows:
        old = r["cycle_id"]
        note = "stale_cycle: cycle %s ended without closing the token; cycle %s started" % (old or "(none)", cycle_id)
        if old and old not in done:
            done.add(old)
            _release(conn, old, note)
            cur = conn.execute("UPDATE cycles SET ended_at = COALESCE(ended_at, ?), status = CASE WHEN status = "
                               "'running' THEN 'error' ELSE status END WHERE cycle_id = ?", (ts, old))
            if cur.rowcount:
                out["cycles"].append(old)
        st = conn.execute("SELECT status FROM actions WHERE token = ?", (r["token"],)).fetchone()[0]
        if st in ("reserved", "armed"):
            gate.mark_unknown(conn, r["token"], note=note)
        out["tokens"].append(r["token"])
    log_event(conn, "stale_tokens_closed", cycle_id=cycle_id, agent_id=agent_id, tokens=out["tokens"],
              stale_cycles=out["cycles"])
    return out
