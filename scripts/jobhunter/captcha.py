"""CAPTCHA hand-off to the owner (FEATURES-OTP-ACCOUNTS-CAPTCHA 3) [U1].

There is no automated solving of any kind here or anywhere in the package: no solver service, no audio path, no
change to the browser's automation flags, no click inside a CAPTCHA frame. A CAPTCHA on an application form
pauses only that job's action and asks the owner:

- open_task(): refuse (job to the owner as before) when captcha.handoff is false, at captcha.max_open or
  captcha.max_tasks_day, or when this open would reach captcha.repeat_breaker_per_site_day for the site (then
  ats:<platform> trips, reason captcha_repeat); else release or settle the token by the gate rules
  (gate.release_for_captcha), job needs_human (captcha_wait), a `captcha` human task, a captcha_tasks row with a
  fresh 4-character code and a deadline, and a high chat alert. finish_open() (after the commit, best effort)
  takes a screenshot of the tab over CDP and attaches it to that alert.
- continue_task() (owner only: /jh continue <code> or ./jobhunter continue <code>): a read-only check over CDP that
  the tab is still there, on the task's site, with no CAPTCHA and no stop on it; then the job goes back to the
  apply queue (captcha_resolved) and the dispatcher plans an extra applier slot soon (meta dispatch_nudge:applier).
- expire(): tasks past their deadline time out; the job is closed (captcha_timeout) and the tab closed.
"""
from __future__ import annotations

import os
import secrets

from . import db, paths
from .canon import now, ts_add
from .errors import Denied
from .events import enqueue_notification, log_event, open_human_task

NUDGE_META = "dispatch_nudge:applier"


def _cfg(conn):
    from . import config
    return config.load(conn)


def _new_code(conn) -> str:
    from .approvals import CODE_ALPHABET
    since = ts_add(now(), days=-30)
    for _ in range(400):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        if conn.execute("SELECT 1 FROM approval_codes WHERE code = ?", (code,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM captcha_tasks WHERE code = ? AND (status = 'open' OR opened_at > ?)",
                        (code, since)).fetchone():
            continue
        return code
    raise Denied("E_INTERNAL", "no free CAPTCHA code")


def _local_hhmm(cfg: dict, ts: str) -> str:
    from . import config
    try:
        return config.local_dt(ts, cfg).strftime("%H:%M")
    except Exception:
        return ts[11:16] + " UTC"


def _job(conn, job_id: int):
    return conn.execute("SELECT j.*, co.display_name AS company_name FROM jobs j LEFT JOIN companies co ON "
                        "co.id = j.company_id WHERE j.id = ?", (job_id,)).fetchone()


def task_view(row) -> dict:
    return {"id": row["id"], "captcha_code": row["code"], "code": row["code"], "task_uid": row["task_uid"],
            "platform": row["platform"], "site": row["site"], "status": row["status"],
            "opened_at": row["opened_at"], "deadline_at": row["deadline_at"], "token_outcome": row["token_outcome"]}


def _to_owner_as_before(conn, job_id: int, why: str) -> None:
    from . import jobstate
    job = _job(conn, job_id)
    if job is None:
        return
    if job["status"] in ("apply_queued", "applying", "eligible", "apply_failed"):
        try:
            jobstate.set_job_status(conn, job_id, "needs_human", "captcha_visible", "captcha")
        except Denied:
            pass
    conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (job_id,))
    open_human_task(conn, "apply_manually", "Apply by hand: %s at %s (a CAPTCHA showed: %s)."
                    % (job["title"], job["company_name"] or job["company_name_raw"], why), job_id=job_id,
                    detail="captcha_visible")


def open_task(conn, *, job_id: int, tab_id: str | None, token: str | None, opened_by: str, url: str | None = None,
              title: str | None = None, cycle_id: str | None = None) -> dict | None:
    """Open (or return) the CAPTCHA task of a job (3.2). Inside the caller's tx. None: no task (refused; the job
    went to the owner as before)."""
    from . import accounts, gate, jobstate
    from .otp import hmac_of
    cfg = _cfg(conn)
    c = cfg["captcha"]
    row = conn.execute("SELECT * FROM captcha_tasks WHERE job_id = ? AND status = 'open'", (job_id,)).fetchone()
    if row is not None:
        return task_view(row)
    job = _job(conn, job_id)
    if job is None:
        raise Denied("E_NOT_FOUND", "no job for the CAPTCHA")
    platform, site, host, _url = accounts.job_platform(job)
    page_host = accounts.host_of(url) or host
    ts = now()
    since = ts_add(ts, days=-1)
    reason = None
    if not c.get("handoff", True):
        reason = "hand-off is off"
    elif conn.execute("SELECT count(*) FROM captcha_tasks WHERE status = 'open'").fetchone()[0] >= int(c["max_open"]):
        reason = "too many open CAPTCHA tasks"
    elif conn.execute("SELECT count(*) FROM captcha_tasks WHERE opened_at > ?", (since,)).fetchone()[0] >= \
            int(c["max_tasks_day"]):
        reason = "too many CAPTCHA tasks today"
    else:
        n_site = conn.execute("SELECT count(*) FROM captcha_tasks WHERE site = ? AND opened_at > ?",
                              (site, since)).fetchone()[0]
        if n_site + 1 >= int(c["repeat_breaker_per_site_day"]):
            reason = "CAPTCHAs repeat on this site"
            if platform:
                from . import breakers
                breakers.trip(conn, "ats:" + platform, "captcha_repeat",
                              "%d CAPTCHAs on %s within 24 hours" % (n_site + 1, site), by="captcha",
                              cycle_id=cycle_id)
    if reason is not None:
        if token:
            try:
                gate.release_for_captcha(conn, token)
            except Denied:
                pass
        _to_owner_as_before(conn, job_id, reason)
        log_event(conn, "captcha_refused", job_id=job_id, site=site, reason=reason)
        return None
    token_outcome = "none"
    if token:
        token_outcome = gate.release_for_captcha(conn, token)
    job = _job(conn, job_id)
    if job["status"] != "needs_human":
        jobstate.set_job_status(conn, job_id, "needs_human", "captcha_wait", "captcha")
    else:
        conn.execute("UPDATE jobs SET status_reason = 'captcha_wait' WHERE id = ?", (job_id,))
    conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (job_id,))
    code = _new_code(conn)
    company = job["company_name"] or job["company_name_raw"]
    q = ("Solve the CAPTCHA for %s at %s in the agent's browser window, then reply /jh continue %s"
         % (job["title"], company, code))
    task_uid = open_human_task(conn, "captcha", q, job_id=job_id, detail="captcha:" + code)
    deadline = ts_add(ts, minutes=int(c["timeout_minutes"]))
    cur = conn.execute(
        "INSERT INTO captcha_tasks (code, task_uid, platform, site, host, job_id, action_id, token_outcome, tab_id, "
        "url_hmac, screenshot_path, status, opened_by, opened_at, deadline_at, cycle_id, created_at, updated_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open', ?, ?, ?, ?, ?, ?)",
        (code, task_uid, platform or "host", site, page_host or "-", job_id,
         _action_id(conn, token), token_outcome, tab_id, hmac_of(conn, url) if url else None, opened_by, ts, deadline,
         cycle_id, ts, ts))
    tid = cur.lastrowid
    label = accounts.PLATFORM_LABEL.get(platform, page_host) if platform else page_host
    lines = ["CAPTCHA on %s: %s, %s." % (label, company, job["title"]),
             "Solve it in the agent's browser window (the jobhunter Chrome window%s), then reply: /jh continue %s"
             % (', tab "%s"' % " ".join(str(title).split())[:80] if title else "", code),
             "Or in a terminal: ./jobhunter continue %s" % code,
             "It waits until %s; after that the job is skipped." % _local_hhmm(cfg, deadline)]
    if token_outcome == "unknown":
        lines.insert(2, "If the page still shows the Submit button after you solve it, click it yourself, then send "
                        "/jh continue %s" % code)
    enqueue_notification(conn, "captcha:%s" % code, "high", "alert", "\n".join(lines))
    log_event(conn, "captcha_opened", captcha_code=code, job_id=job_id, site=site, opened_by=opened_by,
              token_outcome=token_outcome)
    return task_view(conn.execute("SELECT * FROM captcha_tasks WHERE id = ?", (tid,)).fetchone())


def _action_id(conn, token):
    if not token:
        return None
    r = conn.execute("SELECT id FROM actions WHERE token = ?", (token,)).fetchone()
    return r[0] if r else None


def screenshot_dir() -> str:
    return os.path.join(paths.state_dir(), "captcha")


def finish_open(conn, task: dict | None) -> dict | None:
    """After the commit (best effort, about 10 s): a screenshot of the task's tab, attached to its chat alert."""
    if not task or not task.get("id"):
        return task
    row = conn.execute("SELECT * FROM captcha_tasks WHERE id = ?", (task["id"],)).fetchone()
    if row is None or row["screenshot_path"] or not row["tab_id"]:
        return task
    cfg = _cfg(conn)
    if not cfg["captcha"].get("screenshot", True):
        return task
    from . import cdp
    path = os.path.join(screenshot_dir(), "%d.png" % row["id"])
    outcome = "ok"
    try:
        with cdp.connect(row["tab_id"], budget_s=10) as s:
            s.screenshot(path)
    except (Denied, cdp.CdpError, OSError):
        outcome, path = "error", None
    with db.tx(conn):
        if path:
            conn.execute("UPDATE captcha_tasks SET screenshot_path = ?, updated_at = ? WHERE id = ?",
                         (path, now(), row["id"]))
            conn.execute("UPDATE notifications SET media_path = ? WHERE dedupe_key = ? AND delivered_at IS NULL",
                         (path, "captcha:%s" % row["code"]))
        conn.execute("INSERT INTO code_steps (step, platform, site, host, job_id, captcha_id, outcome, detail, at) "
                     "VALUES ('captcha_screenshot', ?, ?, ?, ?, ?, ?, NULL, ?)",
                     (row["platform"], row["site"], row["host"], row["job_id"], row["id"], outcome, now()))
    return dict(task, screenshot=bool(path))


def handoff_from_detect(conn, p: dict, source: str, cycle_id, *, job_id=None, token=None, agent_id=None,
                        tab_id=None) -> dict | None:
    """3.1: the job of a CAPTCHA page (the token's job; else the given job; else the job claimed by the agent's
    running cycle whose apply host is the page host) and its task. None when no job is found."""
    from . import accounts, gate
    tok = token or p.get("token")
    jid = job_id
    if tok and jid is None:
        r = conn.execute("SELECT job_id FROM actions WHERE token = ? AND kind = 'application'", (tok,)).fetchone()
        jid = r[0] if r else None
    agent = agent_id or p.get("agent")
    if jid is None and agent:
        cyc = cycle_id or gate.running_cycle_id(conn, agent)
        host = accounts.host_of(p.get("url"))
        if cyc and host:
            for j in conn.execute("SELECT * FROM jobs WHERE claimed_by = ? AND status IN ('apply_queued','applying')",
                                  (cyc,)).fetchall():
                jh = accounts.host_of(j["apply_url"] or j["source_url"])
                if jh == host or (jh and host.endswith("." + jh)) or (host and jh.endswith("." + host)):
                    jid = j["id"]
                    break
    if jid is None:
        return None
    if tok is None and agent:
        r = conn.execute("SELECT token FROM actions WHERE agent_id = ? AND job_id = ? AND kind = 'application' AND "
                         "status IN ('reserved','armed')", (agent, jid)).fetchone()
        tok = r[0] if r else None
    by = {"guard": "guard", "agent": "agent"}.get(source, "code")
    return open_task(conn, job_id=jid, tab_id=tab_id or p.get("tab_id"), token=tok, opened_by=by, url=p.get("url"),
                     title=p.get("title"), cycle_id=cycle_id)


# ---------------------------------------------------------------- continue (owner only)
def _check(conn, row) -> dict:
    """The read-only check over CDP: {tab_found, host_ok, captcha_gone, stop}. Raises E_STOP_DETECTED (and
    cancels the task, the trip kept) for a tripping signature on the page."""
    from . import accounts, cdp, detect, pagefill
    check = {"tab_found": False, "host_ok": False, "captcha_gone": False, "stop": None}
    if not row["tab_id"]:
        return check
    try:
        t = cdp.find_target(row["tab_id"])
    except Denied:
        t = None
    if t is None:
        return check
    check["tab_found"] = True
    host = accounts.host_of(t["url"])
    cls = accounts.classify_host(host, t["url"])
    if row["site"].startswith("host:"):
        check["host_ok"] = not cls["never"] and (host == row["site"][5:] or host.endswith("." + row["site"][5:]))
    else:
        check["host_ok"] = not cls["never"] and cls["platform"] == row["platform"]
    if not check["host_ok"]:
        return check
    try:
        with cdp.connect(row["tab_id"], budget_s=20) as s:
            page = s.page_text()
            state = pagefill.captcha_state(s)
    except (Denied, cdp.CdpError):
        check["tab_found"] = False
        return check
    payload = {"platform": row["platform"] if row["platform"] != "host" else "ats", "url": page["url"],
               "title": page["title"], "http_status": None, "text": page["text"][:detect.MAX_TEXT]}
    verdict, sig = detect.match(detect.validate_payload(payload, "code"))
    if sig is not None and verdict == "stop" and sig.get("trip"):
        check["stop"] = sig.get("id")

        def _keep(c, pl=payload, tid=row["id"]):
            detect.detect(c, pl, "code", row["cycle_id"], open_captcha=False)
            c.execute("UPDATE captcha_tasks SET status = 'cancelled', resolved_at = ?, resolved_by = 'code', "
                      "check_detail = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                      (now(), "stop:%s" % sig.get("id"), now(), tid))
        db.defer_write(conn, _keep)
        raise Denied("E_STOP_DETECTED", "the page now shows a stop (%s); the site is stopped" % sig.get("id"),
                     data={"check": check})
    captcha_sig = sig is not None and verdict == "stop" and sig.get("handoff") == "captcha"
    check["captcha_gone"] = not state.get("visible") and not captcha_sig
    if sig is not None and verdict == "stop" and not captcha_sig and not sig.get("capability"):
        check["stop"] = sig.get("id")
    return check


def continue_task(conn, code: str, by: str) -> dict:
    """3.3: owner only. Read-only check, then the job goes back to the apply queue."""
    from . import applyq
    code = str(code or "").strip().upper()
    row = conn.execute("SELECT * FROM captcha_tasks WHERE code = ? AND status = 'open'", (code,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no open CAPTCHA with code %s; ./jobhunter captcha list" % code)
    with db.tx(conn):
        check = _check(conn, row)
        ok = check["tab_found"] and check["host_ok"] and check["captcha_gone"] and not check["stop"]
        detail = ",".join("%s=%s" % (k, v) for k, v in sorted(check.items()))[:200]
        conn.execute("INSERT INTO code_steps (step, platform, site, host, job_id, captcha_id, outcome, detail, at) "
                     "VALUES ('captcha_check', ?, ?, ?, ?, ?, ?, ?, ?)", (row["platform"], row["site"], row["host"],
                                                                          row["job_id"], row["id"],
                                                                          "ok" if ok else "refused", detail, now()))
    if not ok:
        raise Denied("E_PRECONDITION", "The CAPTCHA still shows (or the tab is gone). Solve it in the agent's window "
                     "and send /jh continue %s again." % code, data={"captcha_code": code, "check": check})
    ts = now()
    with db.tx(conn):
        conn.execute("UPDATE captcha_tasks SET status = 'resolved', resolved_at = ?, resolved_by = ?, check_detail = ?, "
                     "updated_at = ? WHERE id = ?", (ts, by, detail, ts, row["id"]))
        conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'captcha_solved' WHERE task_uid = ? AND "
                     "done_at IS NULL", (ts, row["task_uid"]))
        job = conn.execute("SELECT job_uid, status FROM jobs WHERE id = ?", (row["job_id"],)).fetchone()
        resumed = False
        if row["token_outcome"] != "unknown":
            res = applyq.return_from_human(conn, job["job_uid"], "captcha_resolved", by=by)
            resumed = bool(res.get("returned"))
            if resumed:
                db.meta_set(conn, NUDGE_META, ts, "system")
        log_event(conn, "captcha_resolved", captcha_code=code, job_uid=job["job_uid"], by=by, resumed=resumed)
    out = {"captcha_code": code, "job_uid": job["job_uid"], "check": check, "resumed": resumed,
           "next_cycle_at": ts_add(ts, minutes=10) if resumed else None}
    if row["token_outcome"] == "unknown":
        out["reconcile"] = True
    return out


# ---------------------------------------------------------------- timeout
def expire(conn) -> list:
    """Open tasks past their deadline: timed_out, job closed (captcha_timeout), human task done, a normal
    notification. Inside the caller's tx. Returns [{id, code, tab_id, platform, site}] for close_tabs()."""
    from . import jobstate
    out = []
    ts = now()
    for row in conn.execute("SELECT * FROM captcha_tasks WHERE status = 'open' AND deadline_at <= ?", (ts,)).fetchall():
        conn.execute("UPDATE captcha_tasks SET status = 'timed_out', resolved_at = ?, resolved_by = 'timeout', "
                     "updated_at = ? WHERE id = ?", (ts, ts, row["id"]))
        conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'timed_out' WHERE task_uid = ? AND done_at IS NULL",
                     (ts, row["task_uid"]))
        job = _job(conn, row["job_id"])
        if job is not None and job["status"] == "needs_human" and row["token_outcome"] != "unknown":
            jobstate.set_job_status(conn, row["job_id"], "closed", "captcha_timeout", "captcha")
        company = (job["company_name"] or job["company_name_raw"]) if job is not None else "the company"
        hours = max(1, int(round(_cfg(conn)["captcha"]["timeout_minutes"] / 60.0)))
        enqueue_notification(conn, "captcha_timeout:%s:%s" % (row["code"], ts), "normal", "info",
                             "The CAPTCHA for %s was not solved in %d hour%s; the job is skipped."
                             % (company, hours, "" if hours == 1 else "s"))
        log_event(conn, "captcha_timed_out", captcha_code=row["code"], job_id=row["job_id"])
        out.append({"id": row["id"], "code": row["code"], "tab_id": row["tab_id"], "platform": row["platform"],
                    "site": row["site"], "host": row["host"], "job_id": row["job_id"]})
    return out


def close_tabs(conn, items: list) -> list:
    """Best effort after expire(): close a timed-out task's tab when it still shows the task's site."""
    from . import accounts, cdp
    closed = []
    for it in items:
        if not it.get("tab_id"):
            continue
        try:
            t = cdp.find_target(it["tab_id"])
        except Denied:
            t = None
        if t is None:
            continue
        host = accounts.host_of(t["url"])
        cls = accounts.classify_host(host, t["url"])
        same = cls["platform"] == it["platform"] if it["platform"] != "host" else \
            (host == it["site"][5:] or host.endswith("." + it["site"][5:]))
        if not same:
            continue
        ok = False
        try:
            ok = cdp.close_tab(it["tab_id"])
        except Denied:
            ok = False
        with db.tx(conn):
            conn.execute("INSERT INTO code_steps (step, platform, site, host, job_id, captcha_id, outcome, detail, at) "
                         "VALUES ('tab_close', ?, ?, ?, ?, ?, ?, 'captcha_timeout', ?)",
                         (it["platform"], it["site"], it["host"], it["job_id"], it["id"], "ok" if ok else "error",
                          now()))
        if ok:
            closed.append(it["code"])
    return closed


def expire_all(conn) -> dict:
    """captcha expire (dispatch tick, housekeeping): the DB part in one transaction, then the tabs."""
    with db.tx(conn):
        items = expire(conn)
    closed = close_tabs(conn, items)
    return {"timed_out": [i["code"] for i in items], "tabs_closed": closed}


def prune_screenshots(conn) -> int:
    """Delete screenshots older than captcha.screenshot_retention_days (inside the caller's tx)."""
    days = int(_cfg(conn)["captcha"]["screenshot_retention_days"])
    n = 0
    for row in conn.execute("SELECT id, screenshot_path FROM captcha_tasks WHERE screenshot_path IS NOT NULL AND "
                            "opened_at < ?", (ts_add(now(), days=-days),)).fetchall():
        try:
            os.unlink(row["screenshot_path"])
        except OSError:
            pass
        conn.execute("UPDATE captcha_tasks SET screenshot_path = NULL WHERE id = ?", (row["id"],))
        conn.execute("UPDATE notifications SET media_path = NULL WHERE media_path = ?", (row["screenshot_path"],))
        n += 1
    return n


# ---------------------------------------------------------------- views
def status(conn, job_uid: str | None = None) -> list:
    q = ("SELECT t.*, j.job_uid FROM captcha_tasks t JOIN jobs j ON j.id = t.job_id WHERE (t.status = 'open' OR "
         "t.opened_at > ?)")
    args = [ts_add(now(), days=-7)]
    if job_uid:
        q += " AND j.job_uid = ?"
        args.append(job_uid)
    return [{"captcha_code": r["code"], "job_uid": r["job_uid"], "platform": r["platform"], "site": r["site"],
             "status": r["status"], "opened_at": r["opened_at"], "deadline_at": r["deadline_at"]}
            for r in conn.execute(q + " ORDER BY t.status = 'open' DESC, t.opened_at DESC", args)]


def open_tasks(conn) -> list:
    """Open tasks with company and role, for status and the Sheet."""
    out = []
    for r in conn.execute("SELECT t.*, j.job_uid, j.title, COALESCE(co.display_name, j.company_name_raw) AS company "
                          "FROM captcha_tasks t JOIN jobs j ON j.id = t.job_id LEFT JOIN companies co ON "
                          "co.id = j.company_id WHERE t.status = 'open' ORDER BY t.deadline_at"):
        out.append({"captcha_code": r["code"], "job_uid": r["job_uid"], "company": r["company"], "title": r["title"],
                    "platform": r["platform"], "site": r["site"], "deadline_at": r["deadline_at"]})
    return out
