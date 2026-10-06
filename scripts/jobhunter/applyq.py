"""Apply queue: what the applier lane works on, claims with a lease, and hands to the human
(design 1.3.4, 2.5, 6.4, 6.5, 12.10 U6).

Work, in order: browser reconcile tasks for applications (reconcile.work_list, U1), approved application
packages ready to submit, packages or application emails that need a rewrite, then new `eligible` jobs.
Jobs whose route or site mode cannot be automated (apply_route 'human', a site in 'human_queue', 'off' or
'never', Workday, an ATS in meta 'ats_human_queue:<ats>', Easy Apply while LinkedIn Easy Apply writes are
off) are swept to `needs_human` with an apply_manually task by claim(); work_count() counts them so the
lane runs once to sweep them.

Single writer (2.5): eligible -> apply_queued (claim), apply_queued -> eligible (release),
eligible/apply_queued/apply_failed -> needs_human (to_human), needs_human -> eligible (return_from_human, once
no human task for the job is open). All through jobstate.
"""
from __future__ import annotations

from urllib.parse import urlparse

from . import canon, jobstate
from .errors import Denied
from .events import log_event, open_human_task
from .threads import LIVE_SQL, OPEN_DRAFT_SQL, cfg, dep, linkedin_enabled, load_config

LEASE_MINUTES = 30
ANSWER_REASON = "answer_question"   # U4 answers.request_human hands jobs over with this reason
ATS_SOURCES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "bamboohr", "workday",
               "icims", "successfactors", "taleo", "oracle_hcm", "jobvite")
ATS_HOSTS = (("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
             ("smartrecruiters.com", "smartrecruiters"), ("workable.com", "workable"), ("recruitee.com", "recruitee"),
             ("bamboohr.com", "bamboohr"), ("myworkdayjobs.com", "workday"), ("myworkdaysite.com", "workday"),
             ("icims.com", "icims"), ("successfactors.com", "successfactors"), ("successfactors.eu", "successfactors"),
             ("sapsf.com", "successfactors"), ("sapsf.eu", "successfactors"), ("taleo.net", "taleo"),
             ("jobs.jobvite.com", "jobvite"), ("app.jobvite.com", "jobvite"), ("oraclecloud.com", "oracle_hcm"))
BOARD_SITES = {"naukri": "naukri", "instahyre": "instahyre", "foundit": "foundit", "cutshort": "cutshort",
               "hirist": "hirist", "iimjobs": "iimjobs", "wellfound": "wellfound", "yc": "yc",
               "workatastartup": "yc", "linkedin_jobs": "linkedin_jobs", "linkedin_post": "linkedin_posts",
               "indeed": "indeed", "glassdoor": "glassdoor"}
SITE_DEFAULT_APPLY = {"ats_forms": "browser", "workday": "human_queue", "yc": "off", "linkedin_jobs":
                      "via_linkedin_channel", "linkedin_posts": "email_only", "indeed": "never", "glassdoor": "never"}


def _ats_of(job) -> str | None:
    src = (job["source"] or "").lower()
    if src in ATS_SOURCES:
        return src
    for url in (job["apply_url"], job["source_url"]):
        host = (urlparse(url or "").hostname or "").lower()
        for suffix, ats in ATS_HOSTS:
            if host == suffix or host.endswith("." + suffix):
                return ats
    return None


def _site_mode(site: str, config: dict) -> str:
    mode = cfg("boards.sites.%s.apply" % site, None, config)
    if mode is None:
        mode = SITE_DEFAULT_APPLY.get(site, "off")
    return str(mode)


def plan_route(conn, job, config: dict | None = None) -> tuple[str, str]:
    """(needs, reason) for an eligible job: needs is 'package', 'email', 'open_posting' or 'human'."""
    config = load_config() if config is None else config
    route = job["apply_route"]
    if job["human_call"] == "never":
        return "skip", "never_apply"
    if route == "human":
        return "human", "route_human"
    if route == "email":
        if not job["apply_email"]:
            return "human", "email_route_without_address"
        return "email", "apply_by_email"
    if route == "easy_apply":
        writes = cfg("channels.linkedin.writes", {}, config) or {}
        if linkedin_enabled(conn, config) and isinstance(writes, dict) and writes.get("easy_apply") and \
                _site_mode("linkedin_jobs", config) == "via_linkedin_channel":
            return "package", "easy_apply"
        return "human", "easy_apply_off"
    ats = _ats_of(job)
    if route == "ats_form" or (route == "unknown" and ats):
        if ats == "workday":
            mode = _site_mode("workday", config)
        else:
            mode = _site_mode("ats_forms", config)
        flag = conn.execute("SELECT value FROM meta WHERE key = ?", ("ats_human_queue:" + ats,)).fetchone() \
            if ats else None
        if flag is not None and flag[0] == "1":
            return "human", "ats_human_queue"
        if mode == "browser" and ats == "workday" and not _accounts_allowed("workday"):
            # Workday needs a candidate account: only with the owner's ats_accounts consent for workday
            return "human", "workday_account_consent"
        if mode == "browser":
            return ("package", "ats_form") if route == "ats_form" else ("open_posting", "ats_route_unconfirmed")
        return "human", "site_mode_" + mode
    if route == "board_inapp":
        site = BOARD_SITES.get((job["source"] or "").lower())
        if site is None:
            return "human", "unknown_board"
        mode = _site_mode(site, config)
        if mode == "browser":
            return "package", "board_inapp"
        return "human", "site_mode_" + mode
    if route == "unknown":
        if job["apply_url"] or job["source_url"]:
            return "open_posting", "route_unknown"
        return "human", "no_url"
    return "human", "route_" + str(route)


def _accounts_allowed(site: str) -> bool:
    try:
        from . import identity
        return identity.capability_active("ats_accounts", site)
    except Exception:
        return False


def _open_draft(conn, job_id: int, kinds: tuple) -> dict | None:
    row = conn.execute("SELECT draft_uid, kind, status, expires_at FROM drafts WHERE job_id = ? AND kind IN (%s) "
                       "AND status IN %s ORDER BY id DESC LIMIT 1" % (",".join("?" for _ in kinds), OPEN_DRAFT_SQL),
                       (job_id,) + tuple(kinds)).fetchone()
    return dict(row) if row is not None else None


def _base_query(where: str) -> str:
    return ("SELECT j.*, co.company_uid, co.display_name AS company_name, co.contact_state, e.score AS eval_score "
            "FROM jobs j LEFT JOIN companies co ON co.id = j.company_id "
            "LEFT JOIN evaluations e ON e.job_id = j.id WHERE " + where +
            " AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.job_id = j.id AND a.kind IN "
            "('application','application_email') AND a.status IN " + LIVE_SQL + ")"
            " ORDER BY COALESCE(e.score, 0) DESC, j.discovered_at, j.id")


def _skipped(conn, job, stamp: str) -> bool:
    row = conn.execute("SELECT 1 FROM target_skips WHERE until > ? AND target_key IN (?, ?)",
                       (stamp, "job:" + job["job_uid"], "company:" + (job["company_uid"] or "-"))).fetchone()
    return row is not None


def _item(job, needs: str, reason: str, draft: dict | None) -> dict:
    return {"job_uid": job["job_uid"], "route": job["apply_route"], "apply_url": job["apply_url"] or job["source_url"],
            "apply_email": job["apply_email"] if needs == "email" else None, "company": job["company_name"] or
            job["company_name_raw"], "company_uid": job["company_uid"], "title": job["title"],
            "package_draft": draft["draft_uid"] if draft else None, "draft_status": draft["status"] if draft else None,
            "needs": needs, "reason": reason, "score": job["eval_score"]}


def _reconcile_tasks(conn) -> list[dict]:
    try:
        tasks = dep("reconcile").work_list(conn, route="browser", agent_id="jobhunter-applier")
    except (ImportError, NotImplementedError, AttributeError):
        return []
    return [t for t in (tasks or []) if isinstance(t, dict) and t.get("kind") in ("application", None)]


def _scan(conn, limit: int, config: dict) -> dict:
    stamp = canon.now()
    if not cfg("channels.applications.enabled", True, config):
        return {"reconcile": [], "submit": [], "revise": [], "new": [], "human": []}
    free = "(j.claimed_until IS NULL OR j.claimed_until < '%s')" % stamp
    submit, revise, new, human = [], [], [], []
    for job in conn.execute(_base_query("j.status = 'apply_queued' AND " + free)):
        if job["contact_state"] in ("do_not_contact", "active_thread") or _skipped(conn, job, stamp):
            continue
        pkg = _open_draft(conn, job["id"], ("application_package",))
        mail = _open_draft(conn, job["id"], ("application_email",))
        d = pkg or mail
        if d is None:
            needs, reason = plan_route(conn, job, config)
            (human if needs == "human" else new).append(_item(job, needs, reason, None))
        elif d["kind"] == "application_package" and d["status"] == "approved" and \
                (d["expires_at"] is None or d["expires_at"] > stamp):
            submit.append(_item(job, "submit", "package_approved", d))
        elif d["status"] in ("drafted", "lint_failed", "review_failed"):
            revise.append(_item(job, "revise", "draft_" + d["status"], d))
    for job in conn.execute(_base_query("j.status = 'eligible' AND " + free)):
        if len(new) + len(human) >= max(limit * 4, 50):
            break
        if job["contact_state"] in ("do_not_contact", "active_thread") or _skipped(conn, job, stamp):
            continue
        pkg = _open_draft(conn, job["id"], ("application_package",))
        if pkg is not None and pkg["status"] == "approved" and (pkg["expires_at"] is None or pkg["expires_at"] > stamp):
            # an approved package (for example a job back from a solved CAPTCHA) goes straight to submit
            submit.append(_item(job, "submit", "package_approved", pkg))
            continue
        needs, reason = plan_route(conn, job, config)
        if needs == "skip":
            continue
        (human if needs == "human" else new).append(_item(job, needs, reason, None))
    # CAPTCHA-resumed jobs first (the owner just solved the check for them)
    resumed = {r[0] for r in conn.execute("SELECT job_uid FROM jobs WHERE status_reason = 'captcha_resolved'")}
    submit.sort(key=lambda it: 0 if it["job_uid"] in resumed else 1)
    return {"reconcile": _reconcile_tasks(conn), "submit": submit, "revise": revise, "new": new, "human": human}


def work_list(conn, limit: int) -> dict:
    """{reconcile, submit, revise, new, human_queue} for the applier (read only)."""
    config = load_config()
    s = _scan(conn, limit, config)
    return {"reconcile": s["reconcile"][:limit], "submit": s["submit"][:limit], "revise": s["revise"][:limit],
            "new": s["new"][:limit], "human_queue": len(s["human"])}


def work_count(conn) -> int:
    """Items the applier lane has to do (the dispatcher skips the lane at 0)."""
    s = _scan(conn, 50, load_config())
    return len(s["reconcile"]) + len(s["submit"]) + len(s["revise"]) + len(s["new"]) + len(s["human"])


def sweep_human(conn, items: list[dict]) -> int:
    n = 0
    for it in items:
        to_human(conn, it["job_uid"], it["reason"])
        n += 1
    return n


def claim(conn, limit: int, cycle_id: str) -> list[dict]:
    """Claim up to `limit` jobs for this cycle (lease LEASE_MINUTES): approved packages first, then drafts to
    revise, then new eligible jobs (eligible -> apply_queued). Sweeps human-only jobs first. Returns the
    `apply next` items."""
    limit = max(1, min(int(limit), 10))
    config = load_config()
    s = _scan(conn, limit, config)
    swept = sweep_human(conn, s["human"])
    stamp = canon.now()
    until = canon.ts_add(stamp, minutes=LEASE_MINUTES)
    out = []
    for it in (s["submit"] + s["revise"] + s["new"])[:limit]:
        job = conn.execute("SELECT id, status FROM jobs WHERE job_uid = ?", (it["job_uid"],)).fetchone()
        if job["status"] == "eligible":
            jobstate.set_job_status(conn, job["id"], "apply_queued", "claimed", "applyq")
        conn.execute("UPDATE jobs SET claimed_by = ?, claimed_until = ?, updated_at = ? WHERE id = ?",
                     (cycle_id, until, stamp, job["id"]))
        out.append(it)
    log_event(conn, "apply_claimed", cycle_id=cycle_id, jobs=[i["job_uid"] for i in out], swept=swept)
    return out


def release(conn, job_uid: str) -> None:
    """Give a claimed job back: clear the lease; apply_queued -> eligible unless a package or application
    email draft for it is still open (then it stays apply_queued, unclaimed)."""
    job = conn.execute("SELECT id, status FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
    if job is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    stamp = canon.now()
    conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL, updated_at = ? WHERE id = ?",
                 (stamp, job["id"]))
    if job["status"] == "apply_queued" and \
            _open_draft(conn, job["id"], ("application_package", "application_email")) is None:
        jobstate.set_job_status(conn, job["id"], "eligible", "released", "applyq")
    log_event(conn, "apply_released", job_uid=job_uid)


def to_human(conn, job_uid: str, reason: str) -> None:
    """Hand a job to the person: -> needs_human, clear the lease, open an apply_manually task. For reason
    'answer_question' no apply_manually task is opened: the caller (U4 answers.request_human) already opened
    the answer_question task that says what the person has to do, and the job comes back through
    return_from_human once it is answered."""
    job = conn.execute("SELECT j.id, j.status, j.title, j.company_name_raw, co.display_name FROM jobs j "
                       "LEFT JOIN companies co ON co.id = j.company_id WHERE j.job_uid = ?", (job_uid,)).fetchone()
    if job is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    if job["status"] != "needs_human":
        jobstate.set_job_status(conn, job["id"], "needs_human", reason, "applyq")
    conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (job["id"],))
    if reason != ANSWER_REASON:
        company = job["display_name"] or job["company_name_raw"]
        open_human_task(conn, "apply_manually", "Apply by hand: %s at %s." % (job["title"], company),
                        job_id=job["id"], detail=reason)
    log_event(conn, "apply_to_human", job_uid=job_uid, reason=reason)


def return_from_human(conn, job_uid: str, reason: str, by: str = "applyq") -> dict:
    """Give a job the person has unblocked back to the apply queue: needs_human -> eligible (reason is the
    transition reason, for example 'answered'). Only when no human task for the job is open any more,
    apart from the apply_manually task to_human opened for this same hand-over (its detail is the job's
    status_reason, for example the one opened for 'answer_question' before that hand-over stopped opening
    one): that task is closed with resolution 'returned_to_queue'. Any other open task (a form question
    still unanswered, an apply_manually task another writer opened, such as `job set-status` for a CAPTCHA or
    an account wall) keeps the job with the person and nothing changes. A job that is not in needs_human is
    left alone. Callers decide when a hand-over is resolved (U4 answers: only jobs parked for
    'answer_question'). Runs inside the caller's transaction. Returns {job_uid, returned, status,
    open_tasks, closed_tasks}. Denied: E_NOT_FOUND."""
    job = conn.execute("SELECT id, status, status_reason FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
    if job is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    out = {"job_uid": job_uid, "returned": False, "status": job["status"], "open_tasks": [], "closed_tasks": []}
    if job["status"] != "needs_human":
        return out
    related, others = [], []
    for t in conn.execute("SELECT id, task_uid, kind, detail FROM human_tasks WHERE job_id = ? AND done_at IS NULL "
                          "ORDER BY id", (job["id"],)):
        if t["kind"] == "apply_manually" and job["status_reason"] and t["detail"] == job["status_reason"]:
            related.append(t)
        else:
            others.append(t["task_uid"])
    if others:
        out["open_tasks"] = others
        log_event(conn, "apply_return_waits", job_uid=job_uid, reason=reason, by=by, open_tasks=others)
        return out
    stamp = canon.now()
    for t in related:
        conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'returned_to_queue' WHERE id = ?",
                     (stamp, t["id"]))
        out["closed_tasks"].append(t["task_uid"])
    jobstate.set_job_status(conn, job["id"], "eligible", reason, by)
    conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (job["id"],))
    out.update(returned=True, status="eligible")
    log_event(conn, "apply_returned", job_uid=job_uid, reason=reason, by=by, closed_tasks=out["closed_tasks"])
    return out
