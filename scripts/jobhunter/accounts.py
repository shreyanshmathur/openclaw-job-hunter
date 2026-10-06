"""ATS accounts and the code-owned browser steps (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.1, 2.3, 2.8, 2.9) [U1].

- Platforms and hosts: ATS_PLATFORMS, classify_host() (the guard's host classes from openclaw/guard-hosts.json, so
  code and guard judge a tab the same way), tenant_key() (one account per tenant).
- Passwords: generate_password() (secrets.choice, never the model), kept only in the store of secretstore.py.
- Terms: classify_checkbox() (standard_terms, unusual, other) and the create-step policy.
- Compound steps: prepare() runs the common refusals of 2.1 (kill switch and breakers, the proven applier in its
  running cycle holding the browser lease, the guard heartbeat, the claimed job with an approved package, the
  token, the capability consent, active hours, the tab on the job's host, the page stop scan), then create(),
  signin() and status() do their one step. Nothing here prints, logs, stores or returns a password, a code or a
  link; outcomes carry ids, reason codes and site names only.
"""
from __future__ import annotations

import functools
import json
import os
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from . import db, paths
from .canon import new_uid, now, ts_add
from .errors import Denied
from .events import enqueue_notification, log_event, open_human_task

ATS_PLATFORMS = ("workday", "icims", "successfactors", "taleo", "greenhouse", "lever", "ashby", "smartrecruiters",
                 "oracle_hcm", "jobvite")
PLATFORM_LABEL = {"workday": "Workday", "icims": "iCIMS", "successfactors": "SAP SuccessFactors", "taleo": "Oracle Taleo",
                  "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
                  "oracle_hcm": "Oracle Cloud HCM", "jobvite": "Jobvite"}
NO_ACCOUNT_PLATFORMS = ("lever", "ashby", "jobvite")
GLOBAL_TENANT_PLATFORMS = ("smartrecruiters", "greenhouse")
HOSTS_FILE = os.path.join(paths.REPO, "openclaw", "guard-hosts.json")
LEASE = "browser"
ACCOUNT_STATUSES = ("creating", "pending_verify", "active", "failed", "locked", "forgotten")
LIVE_ACCOUNT = ("creating", "pending_verify", "active", "locked")

PW_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
PW_LOWER = "abcdefghijkmnopqrstuvwxyz"
PW_DIGITS = "23456789"
PW_SPECIAL = "!#%+-=?@^_*"
PW_FORBIDDEN = set("'\"` \\$&<>")

STANDARD_RE = re.compile(r"(privacy (policy|notice|statement)|terms (of use|and conditions|of service)|"
                         r"data (protection|privacy|processing)|candidate (privacy|consent|data)|"
                         r"i (have read|agree|accept|acknowledge|consent).{0,80}(privacy|terms|data|policy))", re.I)
UNUSUAL_RE = re.compile(r"marketing|newsletter|promotion|job alert|talent (community|network|pool)|future "
                        r"(opportunities|roles)|text message|sms|whatsapp|call me|background (check|screening)|credit|"
                        r"drug|criminal|reference check|paid|fee|payment|subscription|share .{0,40}"
                        r"(third|partner|affiliate)|sell", re.I)

PW_REJECTED_RE = re.compile(r"password.{0,80}(must|should|requirement|invalid|too short|too long|at least|not allowed|"
                            r"special character)", re.I)
EXISTS_RE = re.compile(r"already (exists|registered|in use|have an account)|account with this email", re.I)
CHECK_EMAIL_RE = re.compile(r"check your (email|inbox)|we (sent|emailed) (you )?(a |an )?(verification |activation )?"
                            r"(link|email)|verify your email", re.I)
SIGNIN_FAIL_RE = re.compile(r"(incorrect|invalid|wrong) (email|password|user ?name|credentials)|sign.?in failed|"
                            r"could not sign you in|password (is )?incorrect", re.I)

# test hook: a fixed password source (tests only); production always uses secrets.choice
_password_hook = None


# ---------------------------------------------------------------- hosts (guard-hosts.json, read only)
@functools.lru_cache(maxsize=1)
def _hosts() -> dict:
    with open(HOSTS_FILE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    never = doc.get("never") or {}
    plats = {}
    for key, p in (doc.get("platforms") or {}).items():
        plats[key] = {"scope": p.get("scope"), "hosts": [h.lower() for h in p.get("hosts") or []],
                      "patterns": [re.compile(x, re.I) for x in p.get("host_patterns") or []]}
    return {"never_hosts": [h.lower() for h in never.get("hosts") or []],
            "never_urls": [re.compile(x, re.I) for x in never.get("url_patterns") or []], "platforms": plats}


def host_of(url: str | None) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def classify_host(host: str, url: str | None = None) -> dict:
    """{host, never, platform, scope} by the guard's rules: a listed domain covers its subdomains; a platform's
    host_patterns are tested after the listed hosts; never hosts and never URL patterns win."""
    h = (host or "").lower().rstrip(".")
    d = _hosts()
    never = any(h == n or h.endswith("." + n) for n in d["never_hosts"])
    if url and not never:
        never = any(rx.search(url) for rx in d["never_urls"])
    plat = None
    for key, p in d["platforms"].items():
        if any(h == x or h.endswith("." + x) for x in p["hosts"]):
            plat = key
            break
    if plat is None:
        for key, p in d["platforms"].items():
            if any(rx.search(h) for rx in p["patterns"]):
                plat = key
                break
    scope = d["platforms"][plat]["scope"] if plat else None
    return {"host": h, "never": never, "platform": plat, "scope": scope}


def job_platform(job) -> tuple:
    """(platform key or None, site, host, apply URL) of a job: its ATS from the source or the apply URL host, else
    its company portal host (site host:<host>)."""
    url = job["apply_url"] or job["source_url"] or ""
    host = host_of(url)
    cls = classify_host(host, url)
    src = (job["source"] or "").lower()
    plat = cls["platform"] if cls["platform"] in ATS_PLATFORMS else (src if src in ATS_PLATFORMS else None)
    if plat:
        return plat, plat, host, url
    from . import identity
    return None, identity.capability_site_of(None, host), host, url


def tenant_key(platform: str | None, url: str) -> str:
    """One account per tenant (2.9). Denied(E_VALIDATION tenant_unknown) for an unknown shape and
    E_PRECONDITION no_accounts_on_platform for platforms without candidate accounts."""
    host = host_of(url)
    if platform is None:
        if not host:
            raise Denied("E_VALIDATION", "the page has no host", data={"reason": "tenant_unknown"})
        return host
    if platform in NO_ACCOUNT_PLATFORMS:
        raise Denied("E_PRECONDITION", "%s applications need no account" % PLATFORM_LABEL[platform],
                     data={"reason": "no_accounts_on_platform"})
    if platform in GLOBAL_TENANT_PLATFORMS:
        return "global"
    if platform == "workday":
        m = re.match(r"^([a-z0-9-]+)\.(wd[0-9]+)\.myworkday(jobs|site)?\.com$", host)
        if not m:
            raise Denied("E_VALIDATION", "not a Workday tenant host", data={"reason": "tenant_unknown"})
        return "%s.%s" % (m.group(1), m.group(2))
    if platform in ("icims", "taleo"):
        if not host:
            raise Denied("E_VALIDATION", "no host", data={"reason": "tenant_unknown"})
        return host
    if platform == "successfactors":
        q = parse_qs(urlsplit(url).query)
        comp = (q.get("company") or [""])[0].lower()
        if not host or not re.match(r"^[a-z0-9_-]{1,60}$", comp):
            raise Denied("E_VALIDATION", "the SuccessFactors link names no company", data={"reason": "tenant_unknown"})
        return "%s|company=%s" % (host, comp)
    if platform == "oracle_hcm":
        m = re.search(r"/sites/([A-Za-z0-9_]{1,40})(/|$)", urlsplit(url).path)
        if not host or not m:
            raise Denied("E_VALIDATION", "the Oracle link names no site", data={"reason": "tenant_unknown"})
        return "%s|site=%s" % (host, m.group(1).lower())
    raise Denied("E_VALIDATION", "unknown tenant shape", data={"reason": "tenant_unknown"})


# ---------------------------------------------------------------- passwords
def set_password_hook(fn) -> None:
    """Tests only: fn(length, alternative) -> str replaces the random source (the result is still checked)."""
    global _password_hook
    _password_hook = fn


def password_ok(pw: str, length: int, email: str = "", alternative: bool = False) -> bool:
    if len(pw) < (16 if alternative else max(16, length)) or any(c in PW_FORBIDDEN for c in pw):
        return False
    for i in range(len(pw) - 2):
        if pw[i] == pw[i + 1] == pw[i + 2]:
            return False
    local = (email or "").split("@")[0].lower()
    if local and len(local) >= 3 and local in pw.lower():
        return False
    up = sum(c.isupper() for c in pw)
    lo = sum(c.islower() for c in pw)
    dg = sum(c.isdigit() for c in pw)
    sp = sum(c in PW_SPECIAL for c in pw)
    if alternative:
        return up >= 2 and lo >= 2 and dg >= 2 and pw.count("-") == 1 and all(c.isalnum() or c == "-" for c in pw)
    return up >= 2 and lo >= 2 and dg >= 2 and sp >= 2 and all(c.isalnum() or c in PW_SPECIAL for c in pw)


def generate_password(length: int = 20, email: str = "", alternative: bool = False):
    """A strong random password (secretstore.Secret). Default policy: upper, lower, digits and !#%+-=?@^_*, at
    least two of each, length >= 16; alternative (after a site rejected the first): 16 letters and digits plus one
    '-'. Never printed, logged or put in an exception."""
    from .secretstore import Secret
    n = 16 if alternative else max(16, int(length or 20))
    for _ in range(200):
        if _password_hook is not None:
            pw = _password_hook(n, alternative)
        elif alternative:
            pool = PW_UPPER + PW_LOWER + PW_DIGITS
            body = [secrets.choice(PW_UPPER) for _ in range(2)] + [secrets.choice(PW_LOWER) for _ in range(2)] + \
                [secrets.choice(PW_DIGITS) for _ in range(2)] + [secrets.choice(pool) for _ in range(n - 7)]
            secrets.SystemRandom().shuffle(body)
            pw = "".join(body[:8]) + "-" + "".join(body[8:])
        else:
            pool = PW_UPPER + PW_LOWER + PW_DIGITS + PW_SPECIAL
            body = [secrets.choice(PW_UPPER) for _ in range(2)] + [secrets.choice(PW_LOWER) for _ in range(2)] + \
                [secrets.choice(PW_DIGITS) for _ in range(2)] + [secrets.choice(PW_SPECIAL) for _ in range(2)] + \
                [secrets.choice(pool) for _ in range(n - 8)]
            secrets.SystemRandom().shuffle(body)
            pw = "".join(body)
        if password_ok(pw, n, email, alternative):
            return Secret(pw)
    raise Denied("E_INTERNAL", "no password met the policy")


# ---------------------------------------------------------------- terms
def classify_checkbox(label: str) -> str:
    t = " ".join(str(label or "").split())
    if UNUSUAL_RE.search(t):
        return "unusual"
    if STANDARD_RE.search(t):
        return "standard_terms"
    return "other"


def terms_plan(boxes: list) -> list:
    """[{label, required, class, action, field}] for every checkbox of the account form: a required
    standard_terms box is ticked; an optional box stays unticked; a required unusual or other box refuses."""
    out = []
    for b in boxes:
        label = (b.get("label") or b.get("name") or "")[:300]
        cls = classify_checkbox(label)
        req = bool(b.get("required"))
        if req and cls == "standard_terms":
            action = "ticked"
        elif req:
            action = "refused"
        else:
            action = "left_unticked"
        out.append({"label": label, "required": req, "class": cls, "action": action, "field": b})
    return out


# ---------------------------------------------------------------- ledger
def account_for(conn, platform: str | None, tenant: str):
    return conn.execute("SELECT * FROM ats_accounts WHERE platform = ? AND tenant = ? AND status IN "
                        "('creating','pending_verify','active','locked') ORDER BY id DESC LIMIT 1",
                        (platform or "host", tenant)).fetchone()


def accounts_list(conn) -> list:
    return [{"account_uid": r["account_uid"], "platform": r["platform"], "host": r["host"], "tenant": r["tenant"],
             "email": r["email"], "status": r["status"], "created_at": r["created_at"],
             "last_used_at": r["last_used_at"]}
            for r in conn.execute("SELECT * FROM ats_accounts ORDER BY id")]


def forget(conn, host: str, by: str) -> dict:
    """Every non-forgotten account on host (or a subdomain of it): delete the stored password, mark it forgotten.
    The account on the site itself still exists. Runs inside the caller's tx."""
    from . import secretstore
    h = str(host or "").strip().lower().rstrip(".")
    if not re.match(r"^[a-z0-9.-]{3,100}$", h):
        raise Denied("E_VALIDATION", "give the site's host, for example kestrel.wd5.myworkdayjobs.com")
    rows = [r for r in conn.execute("SELECT * FROM ats_accounts WHERE status <> 'forgotten'")
            if r["host"] == h or r["host"].endswith("." + h)]
    if not rows:
        raise Denied("E_NOT_FOUND", "no site account on %s; ./jobhunter accounts list" % h)
    ts = now()
    gone, deleted = [], True
    for r in rows:
        try:
            ok = secretstore.delete(r["host"], r["email"], store=r["store"])
        except secretstore.StoreError:
            ok = False
            deleted = False
        conn.execute("UPDATE ats_accounts SET status = 'forgotten', forgotten_at = ?, updated_at = ? WHERE id = ?",
                     (ts, ts, r["id"]))
        gone.append(r["account_uid"])
        log_event(conn, "ats_account_forgotten", account_uid=r["account_uid"], host=r["host"], by=by,
                  store_deleted=ok)
    return {"forgotten": gone, "keychain_deleted": deleted,
            "reminder": "the account on %s still exists; delete it on the site if you want it gone" % h}


# ---------------------------------------------------------------- the step context and common refusals (2.1)
class Step:
    """What one compound step works on (no secret in it)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self) -> str:
        return "Step(job=%s, site=%s, tab=%s)" % (getattr(self, "job_uid", None), getattr(self, "site", None),
                                                getattr(self, "tab_id", None))


def _job_row(conn, job_uid: str):
    row = conn.execute("SELECT j.*, co.display_name AS company_name, co.id AS company_row FROM jobs j "
                       "LEFT JOIN companies co ON co.id = j.company_id WHERE j.job_uid = ?", (job_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    return row


def company_domains(conn, company_id) -> list:
    if not company_id:
        return []
    from . import companies
    try:
        ids = companies.group(conn, company_id)
    except Exception:
        ids = [company_id]
    q = ",".join("?" * len(ids))
    out = []
    for (d,) in conn.execute("SELECT domain FROM companies WHERE id IN (%s) AND domain IS NOT NULL" % q, ids):
        if d and d.lower() not in out:
            out.append(d.lower())
    for (k,) in conn.execute("SELECT alias_key FROM company_aliases WHERE company_id IN (%s) AND kind = 'dom'" % q,
                             ids):
        d = k[4:].lower()
        if d and d not in out:
            out.append(d)
    return out


def check_caller(conn, agent_id: str, cycle_id: str | None):
    """2.1 rule 2: the proven applier in its running applier cycle holding the browser lease; the guard heartbeat
    fresh with proof version 2. Returns the cycle row."""
    from . import gate, locks
    if agent_id != "jobhunter-applier":
        raise Denied("E_CALLER_NOT_ALLOWED", "only the applier runs code-owned browser steps")
    cycle = gate.agent_cycle(conn, agent_id, cycle_id)
    if cycle["lane"] != "applier":
        raise Denied("E_CALLER_NOT_ALLOWED", "code-owned browser steps run only in an applier cycle")
    lease = locks.get(conn, LEASE)
    if lease is None or lease["holder"] != cycle["cycle_id"] or lease["expires_at"] <= now():
        raise Denied("E_CALLER_NOT_ALLOWED", "this cycle does not hold the browser lease (lock renew)")
    hb = gate.guard_heartbeat()
    if not hb["fresh"] or hb.get("proof_version") != 2:
        raise Denied("E_GUARD_MISSING", "the jobhunter-guard heartbeat is missing, stale or too old", data=hb)
    return cycle


def check_job(conn, job_uid: str, cycle_id: str, agent_id: str, token: str | None):
    """2.1 rule 3: the job is claimed by this cycle, apply_queued or applying, with an approved unexpired
    application_package draft; a token must be this agent's live application token for the job."""
    job = _job_row(conn, job_uid)
    ts = now()
    if job["claimed_by"] != cycle_id or not job["claimed_until"] or job["claimed_until"] <= ts:
        raise Denied("E_PRECONDITION", "the job is not claimed by this cycle (apply next)", data={"reason": "not_claimed"})
    if job["status"] not in ("apply_queued", "applying"):
        raise Denied("E_PRECONDITION", "the job is %s" % job["status"], data={"reason": "job_status"})
    pkg = conn.execute("SELECT * FROM drafts WHERE job_id = ? AND kind = 'application_package' AND status = 'approved' "
                       "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC LIMIT 1", (job["id"], ts)).fetchone()
    if pkg is None:
        raise Denied("E_PRECONDITION", "the job has no approved application package", data={"reason": "no_package"})
    action = None
    if token:
        from . import gate
        action = gate.action_by_token(conn, token)
        if action["agent_id"] != agent_id or action["kind"] != "application" or action["job_id"] != job["id"] or \
                action["status"] not in ("reserved", "armed"):
            raise Denied("E_PRECONDITION", "the token is not this job's open application token",
                         data={"reason": "token"})
    return job, pkg, action


def check_hours(conn, cfg: dict) -> None:
    from . import gate
    if gate.hours_ok(conn, cfg, "application", "ats"):
        return
    from . import config as _config
    wait = _config.seconds_until(lambda t: gate.hours_ok(conn, cfg, "application", "ats", at=t))
    raise Denied("E_OUTSIDE_HOURS", "outside the browser hours", retry_after=wait)


def retry_after(oldest: str | None, window_s: int) -> int | None:
    if not oldest:
        return None
    from .canon import seconds_between
    return max(1, window_s - seconds_between(oldest, now()))


def check_breakers(conn, platform: str | None) -> None:
    from . import breakers
    scopes = ["global", "ats", "pause:applications"]
    if platform:
        scopes.append("ats:" + platform)
    breakers.check_breakers(conn, scopes)


def open_captcha_for(conn, job_id: int, site: str):
    return conn.execute("SELECT * FROM captcha_tasks WHERE status = 'open' AND (job_id = ? OR site = ?) LIMIT 1",
                        (job_id, site)).fetchone()


def prepare(conn, *, agent_id: str, cycle_id: str | None, job_uid: str, tab_id: str, token: str | None,
            capabilities: tuple, cfg: dict | None = None, caps_check=None) -> Step:
    """The common refusals of every compound step, in the order of 2.1 (first failure wins), up to and including
    the CDP and host checks (rules 7 and 8). The page stop scan (rule 9) is scan() on the open session."""
    from . import breakers, config as _config, identity
    cfg = cfg or _config.load(conn)
    if breakers.is_paused():
        raise Denied("E_PAUSED", "the job hunter is paused (state/PAUSED)")
    job0 = _job_row(conn, job_uid)
    platform, site, job_host, url = job_platform(job0)
    check_breakers(conn, platform)
    cycle = check_caller(conn, agent_id, cycle_id)
    job, pkg, action = check_job(conn, job_uid, cycle["cycle_id"], agent_id, token)
    for cap in capabilities:
        identity.require_capability(conn, cap, site, cfg=cfg)
    check_hours(conn, cfg)
    if caps_check is not None:
        caps_check(conn, cfg, site, platform)
    if open_captcha_for(conn, job["id"], site) is not None:
        raise Denied("E_STOP_DETECTED", "a CAPTCHA on this site waits for you; nothing more happens there until you "
                     "solve it", data={"reason": "captcha_open"})
    return Step(agent_id=agent_id, cycle_id=cycle["cycle_id"], job=job, job_uid=job_uid, job_id=job["id"],
                package=pkg, action=action, token=token, platform=platform, site=site, job_host=job_host,
                apply_url=url, tab_id=tab_id, cfg=cfg, company=job["company_name"] or job["company_name_raw"],
                title=job["title"])


def open_tab(step: Step):
    """2.1 rules 7 and 8: CDP reachable, the tab exists, its host is the job's platform or the consented portal.
    Returns (session, target)."""
    from . import cdp
    t = cdp.find_target(step.tab_id)
    if t is None:
        raise Denied("E_ROUTE_UNAVAILABLE", "the tab %s is not open in the agent browser" % step.tab_id,
                     data={"reason": "tab_not_found"})
    host = host_of(t["url"])
    cls = classify_host(host, t["url"])
    ok = not cls["never"] and (
        (step.platform is not None and cls["platform"] == step.platform) or
        (step.platform is None and step.site.startswith("host:") and
         (host == step.site[5:] or host.endswith("." + step.site[5:]))))
    if not ok:
        raise Denied("E_VALIDATION", "the tab is not on the job's site", data={"reason": "wrong_host"})
    step.host = host
    step.tab_url = t["url"]
    try:
        step.tenant = tenant_key(step.platform, t["url"])
    except Denied:
        step.tenant = None
    return cdp.connect(step.tab_id), t


def scan(conn, step: Step, session, when: str) -> dict:
    """2.1 rule 9 on the session's page: a tripping signature records the detection, trips its breaker (kept
    after the refusal) and raises E_STOP_DETECTED; a CAPTCHA opens the owner's task and returns
    {captcha: {...}}; a job-level stop the consent does not cover raises E_STOP_DETECTED; else {verdict: clear}."""
    from . import detect, pagefill
    page = session.page_text()
    cap = pagefill.captcha_state(session)
    res = detect.scan_page(conn, page, source="code", platform=step.platform or "ats", job_id=step.job_id,
                           token=step.token, agent_id=step.agent_id, tab_id=step.tab_id, cycle_id=step.cycle_id,
                           captcha_visible=bool(cap.get("visible")), site=step.site)
    res["page"] = page
    return res


def step_log(conn, step: Step, kind: str, outcome: str, detail: str | None = None, request_id=None, account_id=None,
             captcha_id=None) -> None:
    conn.execute("INSERT INTO code_steps (step, platform, site, host, job_id, action_id, request_id, account_id, "
                 "captcha_id, outcome, detail, agent_id, cycle_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (kind, step.platform or "host", step.site, getattr(step, "host", None) or step.job_host, step.job_id,
                  step.action["id"] if step.action is not None else None, request_id, account_id, captcha_id, outcome,
                  (detail or None) and detail[:200], step.agent_id, step.cycle_id, now()))


def token_line(token: str | None, cls: str, action: str, host: str | None, name: str | None) -> None:
    """One line in state/guard/<token>.jsonl for a code step (the guard counts class commit lines)."""
    if not token:
        return
    from . import gate
    gate.append_code_line(token, cls=cls, action=action, host=host, name=name)


# ---------------------------------------------------------------- caps and breakers
def new_accounts_caps(conn, cfg: dict, site: str, platform) -> None:
    a = cfg["accounts"]
    ts = now()
    day = conn.execute("SELECT count(*), min(created_at) FROM ats_accounts WHERE created_at > ?",
                       (ts_add(ts, days=-1),)).fetchone()
    if day[0] >= int(a["max_new_day"]):
        raise Denied("E_CEILING", "new site accounts: %d in 24 hours (limit %d)" % (day[0], int(a["max_new_day"])),
                     retry_after=retry_after(day[1], 86400), data={"window": "day"})
    week = conn.execute("SELECT count(*), min(created_at) FROM ats_accounts WHERE created_at > ?",
                        (ts_add(ts, days=-7),)).fetchone()
    if week[0] >= int(a["max_new_week"]):
        raise Denied("E_CEILING", "new site accounts: %d in 7 days (limit %d)" % (week[0], int(a["max_new_week"])),
                     retry_after=retry_after(week[1], 7 * 86400), data={"window": "week"})


def account_failure(conn, step: Step, cfg: dict) -> None:
    """Count a failed create or sign-in; at accounts.failure_breaker_24h per site trip ats:<platform>."""
    n = conn.execute("SELECT count(*) FROM code_steps WHERE site = ? AND step IN ('account_create','account_signin') "
                     "AND outcome IN ('rejected','error') AND at > ?", (step.site, ts_add(now(), days=-1))).fetchone()[0]
    if step.platform and n >= int(cfg["accounts"]["failure_breaker_24h"]):
        from . import breakers
        breakers.trip(conn, "ats:" + step.platform, "account_failures",
                      "%d failed account steps on %s in 24 hours" % (n, step.site), by="code", cycle_id=step.cycle_id)


# ---------------------------------------------------------------- account status
def status(conn, *, job_uid: str, cfg: dict | None = None) -> dict:
    from . import config as _config, identity
    cfg = cfg or _config.load(conn)
    job = _job_row(conn, job_uid)
    platform, site, host, url = job_platform(job)
    try:
        tenant = tenant_key(platform, url)
    except Denied:
        tenant = None
    consent = {}
    for cap in identity.CAPABILITIES:
        if identity.capability_active(cap, site, cfg=cfg):
            consent[cap] = "unavailable" if identity.capability_prerequisite(conn, cap, cfg) else "granted"
        else:
            row = ((identity.load_consent().get("capabilities") or {}).get(cap) or {}).get(site) or {}
            consent[cap] = {"declined": "declined", "revoked": "revoked"}.get(row.get("status"), "not_granted")
    acct = account_for(conn, platform, tenant) if tenant else None
    ts = now()
    new_today = conn.execute("SELECT count(*) FROM ats_accounts WHERE created_at > ?", (ts_add(ts, days=-1),)).fetchone()[0]
    codes_today = conn.execute("SELECT count(*) FROM code_uses WHERE used_at > ?", (ts_add(ts, days=-1),)).fetchone()[0]
    if acct is not None and acct["status"] in ("pending_verify", "active"):
        nxt = "signin" if consent["ats_accounts"] == "granted" else "needs_human"
    elif consent["ats_accounts"] == "granted" and tenant and platform not in NO_ACCOUNT_PLATFORMS:
        nxt = "create"
    else:
        nxt = "needs_human"
    return {"platform": platform, "site": site, "tenant": tenant, "consent": consent,
            "account": ({"account_uid": acct["account_uid"], "status": acct["status"], "created_at": acct["created_at"],
                         "last_used_at": acct["last_used_at"]} if acct is not None else None),
            "caps": {"new_today": new_today, "new_limit": int(cfg["accounts"]["max_new_day"]),
                     "codes_today": codes_today, "codes_limit": int(cfg["otp"]["max_uses_day"])},
            "next": nxt}


# ---------------------------------------------------------------- page helpers
def _wait_change(session, before: dict, timeout_s: float = 20.0) -> dict:
    from . import cdp

    def changed():
        p = session.page_text()
        return p if (p["url"] != before.get("url") or p["text"] != before.get("text")) else None
    return cdp.wait(changed, timeout_s, 0.5) or session.page_text()


def _owner_email(cfg: dict) -> str:
    from . import identity
    e = str((cfg.get("owner") or {}).get("gmail_address") or "").strip().lower()
    if not e or e in identity.PLACEHOLDERS:
        raise Denied("E_CONFIG_INVALID", "set owner.gmail_address in private/config.json")
    return e


def _to_owner(conn, step: Step, reason: str, detail: str | None = None, sentence: str | None = None) -> str | None:
    """The job goes to the owner (needs_human with reason), with an apply_manually task."""
    from . import jobstate
    job = conn.execute("SELECT id, status FROM jobs WHERE id = ?", (step.job_id,)).fetchone()
    if job["status"] not in ("needs_human",):
        try:
            jobstate.set_job_status(conn, step.job_id, "needs_human", reason, "code")
        except Denied:
            return None
    else:
        conn.execute("UPDATE jobs SET status_reason = ? WHERE id = ?", (reason, step.job_id))
    conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (step.job_id,))
    q = sentence or "Apply by hand: %s at %s." % (step.title, step.company)
    return open_human_task(conn, "apply_manually", q, job_id=step.job_id, detail=detail or reason)


def _release_token_for_owner(conn, step: Step, why: str) -> None:
    """A reserved token is freed (not_attempted) when the job goes to the owner before any commit."""
    if step.action is None:
        return
    from . import gate
    row = gate.action_by_token(conn, step.token)
    if row["status"] == "reserved" and not gate.commit_count(step.token):
        ts = now()
        conn.execute("UPDATE actions SET status = 'failed', fail_reason = 'not_attempted', resolved_at = ?, "
                     "evidence = ?, updated_at = ? WHERE id = ?", (ts, why[:200], ts, row["id"]))


# ---------------------------------------------------------------- account create (2.3 step 5)
def create(conn, *, agent_id: str, cycle_id: str | None, job_uid: str, tab_id: str, token: str | None) -> dict:
    from . import identity, otp, pagefill, secretstore
    step = prepare(conn, agent_id=agent_id, cycle_id=cycle_id, job_uid=job_uid, tab_id=tab_id, token=token,
                   capabilities=("ats_accounts",), caps_check=new_accounts_caps)
    cfg = step.cfg
    email = _owner_email(cfg)
    if step.platform in NO_ACCOUNT_PLATFORMS:
        raise Denied("E_PRECONDITION", "%s applications need no account" % PLATFORM_LABEL[step.platform],
                     data={"reason": "no_accounts_on_platform"})
    session, _t = open_tab(step)
    with session:
        if step.tenant is None:
            tenant_key(step.platform, step.tab_url)             # raises the precise refusal
        acct = account_for(conn, step.platform, step.tenant)
        if acct is not None:
            raise Denied("E_ALREADY_DONE", "an account for this site already exists", data={"next": "signin",
                                                                                           "account_uid": acct["account_uid"]})
        with db.tx(conn):
            pre = scan(conn, step, session, "before")
        if pre.get("captcha"):
            return {"outcome": "captcha", "captcha_code": pre["captcha"]["code"], "account_uid": None, "terms": []}
        form = pagefill.read_form(session)
        af = pagefill.find_account_form(form)
        if af["button"] is None:
            raise Denied("E_PRECONDITION", "the account form has no create button", data={"reason": "form_not_recognized"})
        plan = terms_plan(af["checkboxes"])
        public_terms = [{k: v for k, v in t.items() if k != "field"} for t in plan]
        refused = [t for t in plan if t["action"] == "refused"]
        if refused:
            with db.tx(conn):
                for t in plan:
                    conn.execute("INSERT INTO account_terms (account_id, site, host, label, required, class, action, at) "
                                 "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)", (step.site, step.host, t["label"],
                                                                       1 if t["required"] else 0, t["class"],
                                                                       t["action"], now()))
                step_log(conn, step, "account_create", "refused", "terms_unusual")
                _release_token_for_owner(conn, step, "terms_unusual")
                task = _to_owner(conn, step, "account_terms", detail="terms: " + refused[0]["label"][:250],
                                 sentence="The account form on %s needs a box I may not tick: \"%s\". Apply by hand: "
                                          "%s at %s." % (step.host, refused[0]["label"][:120], step.title, step.company))
                log_event(conn, "ats_account_terms_refused", site=step.site, host=step.host, job_uid=job_uid)
            return {"outcome": "rejected", "reason": "terms_unusual", "account_uid": None, "terms": public_terms,
                    "human_task": task}
        # d. the ledger row and the password (the store is read back and compared in memory)
        store = secretstore.backend(cfg)
        pw = generate_password(int(cfg["accounts"]["password_length"]), email)
        uid = new_uid("N")
        try:
            with db.tx(conn):
                ts = now()
                cur = conn.execute(
                    "INSERT INTO ats_accounts (account_uid, platform, site, host, tenant, email, store, secret_ref, status, "
                    "created_at, created_job_id, created_cycle_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                    "'creating', ?, ?, ?, ?)", (uid, step.platform or "host", step.site, step.host, step.tenant, email,
                                                store, secretstore.ref(step.host, email), ts, step.job_id, step.cycle_id,
                                                ts))
                acct_id = cur.lastrowid
                secretstore.put(step.host, email, pw, store=store)
                back = secretstore.get(step.host, email, store=store)
                if back is None or back != pw:
                    raise secretstore.StoreError("unavailable", "the stored password did not read back")
                req = None
                if identity.capability_active("email_codes", step.site, cfg=cfg) and \
                        not identity.capability_prerequisite(conn, "email_codes", cfg):
                    req = otp.open_request(conn, step=step, purpose="account_verify", want="either", account_id=acct_id)
        except secretstore.StoreError:
            raise Denied("E_ROUTE_UNAVAILABLE", "the password store is not available (unlock the Keychain and run the "
                         "job again)", data={"reason": "keychain_unavailable"})
        # f. type, tick, verify, click
        outcome, detail = _create_on_page(conn, step, session, af, plan, email, pw, store)
        if outcome == "password_rejected":
            pw = generate_password(16, email, alternative=True)
            try:
                secretstore.put(step.host, email, pw, store=store)
            except secretstore.StoreError:
                outcome, detail = "error", "keychain_unavailable"
            else:
                form = pagefill.read_form(session)
                try:
                    af = pagefill.find_account_form(form)
                    outcome, detail = _create_on_page(conn, step, session, af, plan, email, pw, store)
                except Denied:
                    outcome, detail = "rejected", "form_not_recognized"
                if outcome == "password_rejected":
                    outcome, detail = "rejected", "password_rejected"
        del pw
        return _record_create(conn, step, session, uid, acct_id, outcome, detail, plan, public_terms, req)


def _create_on_page(conn, step: Step, session, af: dict, plan: list, email: str, pw, store: str) -> tuple:
    """Fill, tick, verify, click once, wait, classify. Returns (outcome, detail)."""
    from . import pagefill
    pagefill.fill(session, af["email"], email)
    for f in af["passwords"]:
        pagefill.fill(session, f, pw, masked=True)
    for t in plan:
        if t["action"] == "ticked" and not t["field"].get("checked"):
            session.click_at(t["field"]["x"], t["field"]["y"])
    form = pagefill.read_form(session)
    for f in af["passwords"]:
        cur = pagefill.field_by_index(form, f["i"])
        if cur is None or cur.get("masked") is not True:
            for g in af["passwords"]:
                pagefill.clear(session, g)
            raise Denied("E_PRECONDITION", "a password field is not masked", data={"reason": "fill_not_verified"})
    for t in plan:
        if t["action"] == "ticked":
            cur = pagefill.field_by_index(form, t["field"]["i"])
            if cur is None or not cur.get("checked"):
                raise Denied("E_PRECONDITION", "the terms box did not take", data={"reason": "fill_not_verified"})
    before = session.page_text()
    button = af["button"]
    token_line(step.token, "fill", "account_create", step.host, button.get("name"))
    pagefill.press_button(session, button)
    after = _wait_change(session, before)
    form2 = pagefill.read_form(session)
    text = after["text"]
    if EXISTS_RE.search(text):
        return "exists", "exists"
    if PW_REJECTED_RE.search(text) and any(f.get("type") == "password" for f in form2.get("fields") or []):
        return "password_rejected", "password_rejected"
    if pagefill.code_field_present(form2):
        return "verify_code", None
    if CHECK_EMAIL_RE.search(text):
        return "verify_link", None
    if after["url"] == before["url"] and after["text"] == before["text"]:
        return "unknown", "no_change"
    return "created", None


def _record_create(conn, step: Step, session, uid: str, acct_id: int, outcome: str, detail, plan, public_terms,
                   req) -> dict:
    from . import otp
    cfg = step.cfg
    with db.tx(conn):
        post = None
        if outcome not in ("error",):
            post = scan(conn, step, session, "after")
        ts = now()
        for t in plan:
            conn.execute("INSERT INTO account_terms (account_id, site, host, label, required, class, action, at) VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?)", (acct_id, step.site, step.host, t["label"], 1 if t["required"] else 0,
                                                      t["class"], t["action"], ts))
        captcha = post.get("captcha") if post else None
        if captcha:
            outcome = "captcha"
        status_map = {"created": "pending_verify", "verify_code": "pending_verify", "verify_link": "pending_verify",
                      "captcha": "pending_verify", "exists": "failed", "rejected": "failed", "unknown": "pending_verify",
                      "error": "failed"}
        new_status = status_map.get(outcome, "failed")
        reason = detail if new_status == "failed" else None
        conn.execute("UPDATE ats_accounts SET status = ?, reason = ?, failures = failures + ?, updated_at = ? WHERE id = ?",
                     (new_status, reason, 1 if new_status == "failed" else 0, ts, acct_id))
        step_out = {"created": "ok", "verify_code": "ok", "verify_link": "ok", "captcha": "captcha",
                    "exists": "rejected", "rejected": "rejected", "unknown": "unknown", "error": "error"}[outcome]
        step_log(conn, step, "account_create", step_out, detail or outcome, account_id=acct_id,
                 request_id=req["id"] if req else None)
        out = {"account_uid": uid, "outcome": outcome, "terms": public_terms}
        if outcome in ("verify_code", "verify_link") and req is not None:
            out["request_uid"] = req["request_uid"]
        elif req is not None:
            otp.cancel_request(conn, req["id"], "superseded")
        if outcome == "captcha":
            out["captcha_code"] = captcha["code"]
        if outcome in ("exists", "rejected", "error"):
            account_failure(conn, step, cfg)
            if outcome == "exists":
                sentence = ("An account for your address already exists on %s; sign in once yourself in the agent's "
                            "window or reset the password. Then apply by hand: %s at %s." % (step.host, step.title,
                                                                                             step.company))
                _release_token_for_owner(conn, step, "account_exists")
                out["human_task"] = _to_owner(conn, step, "account_required", detail="account_exists", sentence=sentence)
        if new_status != "failed":
            enqueue_notification(conn, "account:%s" % uid, "low", "info",
                                 "Created an account on %s for %s, %s (with your address). Remove it any time: "
                                 "./jobhunter accounts forget %s" % (step.host, step.company, step.title, step.host))
            log_event(conn, "ats_account_created", site=step.site, host=step.host, job_uid=step.job_uid,
                      account_uid=uid, outcome=outcome)
    return out


# ---------------------------------------------------------------- account sign-in
def signin(conn, *, agent_id: str, cycle_id: str | None, job_uid: str, tab_id: str, token: str | None) -> dict:
    from . import identity, otp, pagefill, secretstore
    step = prepare(conn, agent_id=agent_id, cycle_id=cycle_id, job_uid=job_uid, tab_id=tab_id, token=token,
                   capabilities=("ats_accounts",))
    cfg = step.cfg
    session, _t = open_tab(step)
    with session:
        if step.tenant is None:
            tenant_key(step.platform, step.tab_url)
        acct = account_for(conn, step.platform, step.tenant)
        if acct is None or acct["status"] not in ("pending_verify", "active"):
            raise Denied("E_PRECONDITION", "no account for this site yet", data={"reason": "no_account", "next": "create"})
        with db.tx(conn):
            pre = scan(conn, step, session, "before")
        if pre.get("captcha"):
            return {"account_uid": acct["account_uid"], "outcome": "captcha", "captcha_code": pre["captcha"]["code"]}
        sf = pagefill.find_signin_form(pagefill.read_form(session))
        if sf["button"] is None:
            raise Denied("E_PRECONDITION", "the sign-in form has no button", data={"reason": "form_not_recognized"})
        try:
            pw = secretstore.get(acct["host"], acct["email"], store=acct["store"])
        except secretstore.StoreError:
            pw = None
        if pw is None:
            raise Denied("E_ROUTE_UNAVAILABLE", "the stored password is not available (unlock the Keychain)",
                         data={"reason": "keychain_unavailable"})
        req = None
        if identity.capability_active("email_codes", step.site, cfg=cfg) and \
                not identity.capability_prerequisite(conn, "email_codes", cfg):
            with db.tx(conn):
                req = otp.open_request(conn, step=step, purpose="signin", want="either", account_id=acct["id"])
        pagefill.fill(session, sf["email"], acct["email"])
        pagefill.fill(session, sf["password"], pw, masked=True)
        del pw
        before = session.page_text()
        token_line(step.token, "fill", "account_signin", step.host, sf["button"].get("name"))
        pagefill.press_button(session, sf["button"])
        after = _wait_change(session, before)
        form2 = pagefill.read_form(session)
        if SIGNIN_FAIL_RE.search(after["text"]):
            outcome = "rejected"
        elif pagefill.code_field_present(form2) or CHECK_EMAIL_RE.search(after["text"]):
            outcome = "code_needed"
        elif any(f.get("type") == "password" for f in form2.get("fields") or []) and after["url"] == before["url"]:
            outcome = "rejected"
        else:
            outcome = "signed_in"
        with db.tx(conn):
            post = scan(conn, step, session, "after")
            if post.get("captcha"):
                outcome = "captcha"
            ts = now()
            out = {"account_uid": acct["account_uid"], "outcome": outcome}
            if outcome == "signed_in":
                conn.execute("UPDATE ats_accounts SET status = 'active', verified_at = COALESCE(verified_at, ?), "
                             "last_used_at = ?, updated_at = ? WHERE id = ?", (ts, ts, ts, acct["id"]))
            elif outcome == "rejected":
                conn.execute("UPDATE ats_accounts SET failures = failures + 1, updated_at = ? WHERE id = ?",
                             (ts, acct["id"]))
            if outcome == "code_needed" and req is not None:
                out["request_uid"] = req["request_uid"]
            elif req is not None:
                otp.cancel_request(conn, req["id"], "superseded")
            if outcome == "captcha":
                out["captcha_code"] = post["captcha"]["code"]
            step_log(conn, step, "account_signin", {"signed_in": "ok", "code_needed": "ok", "rejected": "rejected",
                                                    "captcha": "captcha"}[outcome], outcome, account_id=acct["id"],
                     request_id=req["id"] if req else None)
            if outcome == "rejected":
                account_failure(conn, step, cfg)
            log_event(conn, "ats_account_signin", site=step.site, host=step.host, job_uid=step.job_uid, outcome=outcome)
        return out
