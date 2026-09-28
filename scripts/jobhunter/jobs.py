"""Job ingest, aliases, the human call and job reads (design 1.3, 2.2, 2.4.1, 6.2, 12.1, 12.10).

    results = ingest(conn, payload, cycle_id, discovered_via)      # inside the caller's db.tx()
    add_alias(conn, job_uid, url)
    set_human_call(conn, job_uid, "apply_anyway" | "never", by)

`ingest` is the single path by which a listing becomes a `jobs` row, for the scout's `job add --file` (browser),
the API lane (`sources fetch`) and humans. Per item: canonical key and aliases from `keys.job_key` (U1), key lookup
(an existing key is a duplicate or adds aliases), `companies.resolve` (U1), exclusions, the 45-day fingerprint
duplicate rule, the pre-filter (`prefilter.check`) and finally `eval_queued` (or `new` while the API lane still has
to fetch the JD). Every status write goes through `jobstate.set_job_status` (2.5). Nothing here commits.

Dependencies owned by other units (`keys`, `companies`, `people`, `exclusions`, `config`, `profile`) are imported
lazily through `dep()`, so this module imports without them and tests can substitute fakes.
"""
from __future__ import annotations

import importlib
import json
import re
import urllib.parse

from . import canon, jobstate, prefilter
from .errors import Denied
from .events import log_event, open_human_task

ATS_SOURCES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "bamboohr", "workday")
HUMAN_ONLY_SOURCES = ("glassdoor", "indeed")
APPLY_ROUTES = ("ats_form", "board_inapp", "easy_apply", "email", "human", "unknown")
WORK_MODES = ("remote", "hybrid", "onsite", "unknown")
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship", "temporary", "unknown")
RELATIONS = ("hiring_manager", "recruiter", "poster", "founder")
NATIVE_ID_KEYS = ("board_job_id", "ats_job_id", "post_id")
ITEM_KEYS = ("source_url", "apply_url", "redirect_urls", "company", "company_domain", "title", "location",
             "work_mode", "remote_scope", "employment_type", "posted_at", "years", "salary", "apply_route_hint",
             "apply_email", "jd_text", "hiring_team", "native_ids")
API_ITEM_KEYS = ("tenant", "can_apply")          # accepted only from the API lane (discovered_via = "api")
JD_MAX = 60000
MAX_ITEMS = 200
FP_WINDOW_DAYS = 45
_LIVE_ACTIONS = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")
KEY_KINDS = ("ats", "board", "post", "url")
HUMAN_CALLS = ("apply_anyway", "never")
AGENT_NEEDS_HUMAN_REASONS = ("captcha_visible", "account_required", "sensitive_field", "unsupported_form",
                             "answer_missing")

_SOURCE_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_NATIVE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------- dependencies
def dep(name: str):
    """jobhunter.<name>, imported at call time (U1 and U4 modules; fakes in tests)."""
    try:
        return importlib.import_module("jobhunter." + name)
    except ImportError as exc:
        raise Denied("E_INTERNAL", "jobhunter.%s is not available: %s" % (name, exc))


def load_config() -> dict:
    cfg = dep("config").load()
    return cfg if isinstance(cfg, dict) else {}


def load_profile(required: bool = False) -> dict:
    """The confirmed profile (U4 `profile.load_confirmed`). Unconfirmed: {} for the pre-filter (rules that need
    a profile value are skipped), or E_PROFILE_UNCONFIRMED when `required`."""
    try:
        prof = dep("profile").load_confirmed()
    except Denied as d:
        if d.code == "E_PROFILE_UNCONFIRMED" and not required:
            return {}
        raise
    if required and (not isinstance(prof, dict) or prof.get("confirmed") is False
                     or not prefilter.profile_values(prof)):
        raise Denied("E_PROFILE_UNCONFIRMED", "the profile is not confirmed yet; run ./jobhunter profile")
    return prof if isinstance(prof, dict) else {}


def profile_version(conn, prof: dict | None) -> str:
    if isinstance(prof, dict) and isinstance(prof.get("profile_version"), str) and prof.get("profile_version"):
        return prof["profile_version"]
    row = conn.execute("SELECT value FROM meta WHERE key = 'profile_version'").fetchone()
    return row[0] if row and row[0] is not None else ""


# ---------------------------------------------------------------- small helpers
def key_kind(key: str) -> str:
    kind = key.split(":", 1)[0]
    return kind if kind in KEY_KINDS else "alias"


def _rank(key: str) -> int:
    return {"ats": 0, "board": 1, "post": 2, "url": 3}.get(key.split(":", 1)[0], 4)


def is_url(v, https_only: bool = False) -> bool:
    if not isinstance(v, str) or len(v) > 2048 or any(c.isspace() for c in v):
        return False
    try:
        parts = urllib.parse.urlsplit(v)
    except ValueError:
        return False
    schemes = ("https",) if https_only else ("https", "http")
    return parts.scheme in schemes and bool(parts.netloc)


_ATS_TENANT_RES = (
    ("greenhouse", re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed/)([A-Za-z0-9_-]+)/")),
    ("greenhouse", re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/job_app\?.*\bfor=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"^https?://jobs\.(eu\.)?lever\.co/([A-Za-z0-9_.-]+)/")),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/([^/?#]+)/")),
    ("workday", re.compile(r"^https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/")),
    ("smartrecruiters", re.compile(r"^https?://jobs\.smartrecruiters\.com/([^/?#]+)/")),
    ("workable", re.compile(r"^https?://apply\.workable\.com/([^/?#]+)/")),
    ("recruitee", re.compile(r"^https?://([a-z0-9-]+)\.recruitee\.com/")),
    ("bamboohr", re.compile(r"^https?://([a-z0-9-]+)\.bamboohr\.com/")),
)


def ats_tenant_from_url(url: str | None) -> tuple[str, str, str] | None:
    """(ats, fetch_tenant, company_tenant) for a hosted ATS URL, else None. fetch_tenant is what `sources fetch`
    polls (Workday 'tenant.wdN/site', Lever EU 'eu:site'); company_tenant is the plain tenant for the company key."""
    if not isinstance(url, str):
        return None
    for ats, rx in _ATS_TENANT_RES:
        m = rx.match(url)
        if not m:
            continue
        if ats == "lever":
            site = m.group(2)
            return ats, ("eu:" + site if m.group(1) else site), site
        if ats == "workday":
            return ats, "%s.%s/%s" % (m.group(1), m.group(2), m.group(3)), m.group(1)
        t = m.group(1)
        if t.lower() in ("www", "api", "app", "careers"):
            continue
        return ats, t, t
    return None


def _first_place(location: str | None) -> str | None:
    if not location:
        return None
    first = re.split(r"\s*[/;|]\s*|\s+or\s+", str(location))[0]
    first = first.split(",")[0].strip(" ()")
    return first or None


def _infer_mode(mode: str | None, location: str | None) -> str:
    if mode in WORK_MODES and mode != "unknown":
        return mode
    loc = (location or "").lower()
    if re.search(r"\bhybrid\b", loc):
        return "hybrid"
    if re.search(r"\b(remote|work from home|wfh|anywhere)\b", loc):
        return "remote"
    return "unknown"


# ---------------------------------------------------------------- validation (12.1)
def _norm_mode(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"[^a-z]", "", str(v).lower())
    s = {"onsite": "onsite", "office": "onsite", "inoffice": "onsite", "remote": "remote", "hybrid": "hybrid",
         "unknown": "unknown", "": "unknown"}.get(s)
    return s


def _norm_etype(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"[^a-z]", "_", str(v).lower()).strip("_")
    s = {"fulltime": "full_time", "full_time": "full_time", "permanent": "full_time", "parttime": "part_time",
         "part_time": "part_time", "contract": "contract", "contractor": "contract", "freelance": "contract",
         "temporary": "temporary", "temp": "temporary", "intern": "internship", "internship": "internship",
         "unknown": "unknown", "": "unknown"}.get(s.replace("__", "_"))
    return s


def _date_or_none(v):
    if v is None or v == "":
        return None
    if isinstance(v, str) and _DATE_RE.match(v.strip()):
        return v.strip()[:10]
    return False


def _str(v, max_len: int, required: bool = False):
    if v is None:
        return None if not required else False
    if not isinstance(v, str):
        return False
    v = " ".join(v.split()) if max_len <= 400 else v
    if required and not v.strip():
        return False
    return v[:max_len]


def validate_payload(payload, discovered_via: str) -> tuple[str, list[dict]]:
    """Check a 12.1 ingest file. Returns (source, cleaned items); raises E_SCHEMA listing every problem.
    Unknown keys are refused; API-only keys (tenant, can_apply) are accepted only from the API lane."""
    if not isinstance(payload, dict):
        raise Denied("E_SCHEMA", "the ingest file must be a JSON object with source and jobs")
    unknown = sorted(set(payload) - {"source", "discovered_via", "jobs"})
    if unknown:
        raise Denied("E_SCHEMA", "unknown top-level keys: %s" % ", ".join(unknown))
    source = payload.get("source")
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise Denied("E_SCHEMA", "source must be a site id like 'naukri' or 'linkedin_post'")
    dv = payload.get("discovered_via")
    if dv is not None and dv not in ("api", "browser", "human"):
        raise Denied("E_SCHEMA", "discovered_via must be api, browser or human")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise Denied("E_SCHEMA", "jobs must be a non-empty list")
    if len(jobs) > MAX_ITEMS:
        raise Denied("E_SCHEMA", "at most %d jobs per file" % MAX_ITEMS)
    allowed = set(ITEM_KEYS) | (set(API_ITEM_KEYS) if discovered_via == "api" else set())
    errors: list[dict] = []
    out: list[dict] = []
    for idx, raw in enumerate(jobs):
        def err(field, msg):
            errors.append({"input_index": idx, "field": field, "error": msg})
        if not isinstance(raw, dict):
            err("", "each job must be an object")
            continue
        bad = sorted(set(raw) - allowed)
        if bad:
            err(",".join(bad), "unknown keys")
        it: dict = {}
        if not is_url(raw.get("source_url")):
            err("source_url", "required http(s) URL of the page you visited")
        it["source_url"] = raw.get("source_url")
        au = raw.get("apply_url")
        if au is not None and not is_url(au):
            if isinstance(au, str) and au.lower().startswith("mailto:"):
                addr = urllib.parse.unquote(au[7:].split("?")[0]).strip()
                if _EMAIL_RE.match(addr) and not raw.get("apply_email"):
                    raw = dict(raw, apply_email=addr)
                au = None
            else:
                err("apply_url", "must be null or an http(s) URL")
        it["apply_url"] = au
        rus = raw.get("redirect_urls") or []
        if not isinstance(rus, list) or len(rus) > 10 or not all(is_url(u) for u in rus):
            err("redirect_urls", "must be a list of at most 10 http(s) URLs")
            rus = []
        it["redirect_urls"] = rus
        for field, mx, req in (("company", 200, True), ("title", 300, True), ("location", 300, False),
                               ("remote_scope", 200, False)):
            v = _str(raw.get(field), mx, req)
            if v is False:
                err(field, "required text" if req else "must be text or null")
            it[field] = v if v is not False else None
        dom = raw.get("company_domain")
        if dom is not None:
            dom = str(dom).strip().lower()
            if dom.startswith("www."):
                dom = dom[4:]
            if not _DOMAIN_RE.match(dom):
                err("company_domain", "must be a bare domain like kestrel.example")
                dom = None
        it["company_domain"] = dom
        mode = _norm_mode(raw.get("work_mode"))
        if raw.get("work_mode") is not None and mode is None:
            err("work_mode", "must be remote, hybrid, onsite or unknown")
        it["work_mode"] = mode
        et = _norm_etype(raw.get("employment_type"))
        if raw.get("employment_type") is not None and et is None:
            err("employment_type", "must be one of %s" % ", ".join(EMPLOYMENT_TYPES))
        it["employment_type"] = et
        pa = _date_or_none(raw.get("posted_at"))
        if pa is False:
            err("posted_at", "must be YYYY-MM-DD or null")
            pa = None
        it["posted_at"] = pa
        years = raw.get("years")
        it["years"] = {"min": None, "max": None}
        if years is not None:
            if not isinstance(years, dict) or set(years) - {"min", "max"}:
                err("years", "must be {min, max}")
            else:
                for k in ("min", "max"):
                    v = years.get(k)
                    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 60):
                        err("years." + k, "must be a number from 0 to 60 or null")
                    else:
                        it["years"][k] = v
        sal = raw.get("salary")
        it["salary"] = {"min": None, "max": None, "currency": None, "period": None}
        if sal is not None:
            if not isinstance(sal, dict) or set(sal) - {"min", "max", "currency", "period"}:
                err("salary", "must be {min, max, currency, period}")
            else:
                for k in ("min", "max"):
                    v = sal.get(k)
                    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                        err("salary." + k, "must be a positive number or null")
                    else:
                        it["salary"][k] = v
                cur = sal.get("currency")
                if cur is not None and (not isinstance(cur, str) or not re.match(r"^[A-Za-z]{3}$", cur)):
                    err("salary.currency", "must be a 3-letter currency code or null")
                else:
                    it["salary"]["currency"] = cur.upper() if cur else None
                per = sal.get("period")
                if per is not None and per not in ("year", "month", "week", "day", "hour"):
                    err("salary.period", "must be year, month, week, day or hour")
                else:
                    it["salary"]["period"] = per
        hint = raw.get("apply_route_hint")
        if hint is not None and hint not in APPLY_ROUTES:
            err("apply_route_hint", "must be one of %s" % ", ".join(APPLY_ROUTES))
            hint = None
        it["apply_route_hint"] = hint
        em = raw.get("apply_email")
        if em is not None:
            if not isinstance(em, str) or not _EMAIL_RE.match(em.strip()):
                err("apply_email", "must be an email address or null")
                em = None
            else:
                em = em.strip().lower()
        it["apply_email"] = em
        jd = raw.get("jd_text")
        if jd is not None and not isinstance(jd, str):
            err("jd_text", "must be text or null")
            jd = None
        it["jd_text"] = jd[:JD_MAX] if jd and jd.strip() else None
        it["jd_truncated"] = bool(jd and len(jd) > JD_MAX)
        team = raw.get("hiring_team") or []
        clean_team = []
        if not isinstance(team, list) or len(team) > 10:
            err("hiring_team", "must be a list of at most 10 people")
            team = []
        for p in team:
            if not isinstance(p, dict) or set(p) - {"name", "title", "linkedin_url", "relation"}:
                err("hiring_team", "each person is {name, title, linkedin_url, relation}")
                continue
            if p.get("relation") not in RELATIONS:
                err("hiring_team.relation", "must be one of %s" % ", ".join(RELATIONS))
                continue
            li = p.get("linkedin_url")
            if li is not None and not is_url(li, https_only=True):
                err("hiring_team.linkedin_url", "must be an https URL or null")
                continue
            name = _str(p.get("name"), 120)
            title = _str(p.get("title"), 200)
            if name is False or title is False or (not name and not li):
                err("hiring_team", "a person needs a name or a linkedin_url")
                continue
            clean_team.append({"name": name, "title": title, "linkedin_url": li, "relation": p["relation"]})
        it["hiring_team"] = clean_team
        nat = raw.get("native_ids") or {}
        if not isinstance(nat, dict) or set(nat) - set(NATIVE_ID_KEYS):
            err("native_ids", "keys must be among %s" % ", ".join(NATIVE_ID_KEYS))
            nat = {}
        clean_nat = {}
        for k, v in nat.items():
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (str, int)) or not _NATIVE_RE.match(str(v)):
                err("native_ids." + k, "must be a plain id")
                continue
            clean_nat[k] = str(v)
        it["native_ids"] = clean_nat
        if discovered_via == "api":
            ten = raw.get("tenant")
            if ten is not None and not (isinstance(ten, dict) and ten.get("ats") in ATS_SOURCES
                                        and isinstance(ten.get("tenant"), str) and ten.get("tenant")):
                err("tenant", "must be {ats, tenant}")
                ten = None
            it["tenant"] = ten
            ca = raw.get("can_apply")
            it["can_apply"] = ca if isinstance(ca, bool) else None
        else:
            it["tenant"] = None
            it["can_apply"] = None
        out.append(it)
    if errors:
        raise Denied("E_SCHEMA", "the ingest file has %d problem(s); fix them and add the file again" % len(errors),
                     data={"errors": errors[:40]})
    return source, out


# ---------------------------------------------------------------- keys
def derive_keys(item: dict, source: str) -> list[str]:
    """All job keys of one item, best first (ats > board > post > url). The source URL with its native ids gives
    the canonical key and its aliases; apply and redirect URLs add only strong (ats, board, post) keys, so a
    generic careers page never ties two jobs together. Denied(E_INVENTED_KEY|E_VALIDATION) from keys.job_key."""
    keys = dep("keys")
    ck, aliases = keys.job_key(item["source_url"], item.get("native_ids") or {}, source)
    found = [ck] + [a for a in (aliases or []) if a]
    for u in [item.get("apply_url")] + list(item.get("redirect_urls") or []):
        if not u:
            continue
        try:
            k2, a2 = keys.job_key(u, {}, source)
        except Denied:
            continue
        for k in [k2] + list(a2 or []):
            if k and _rank(k) < 3:
                found.append(k)
    seen, ordered = set(), []
    for k in found:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    ordered.sort(key=_rank)   # stable: keeps the source URL's own key first within a rank
    return ordered


def lookup_keys(conn, keys: list[str]) -> dict:
    if not keys:
        return {}
    rows = conn.execute("SELECT key, job_id FROM job_keys WHERE key IN (%s)" % ",".join("?" for _ in keys),
                        keys).fetchall()
    return {r[0]: r[1] for r in rows}


def _insert_key(conn, key: str, job_id: int) -> bool:
    cur = conn.execute("INSERT INTO job_keys (key, job_id, kind, created_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (key) DO NOTHING", (key, job_id, key_kind(key), canon.now()))
    return cur.rowcount == 1


# ---------------------------------------------------------------- ingest
def ingest(conn, payload: dict, cycle_id: str | None, discovered_via: str, *, profile: dict | None = None,
           config: dict | None = None, fetch_jd_later: bool = False) -> list[dict]:
    """Ingest a 12.1 payload inside the caller's transaction. Returns one result per input item:
    {input_index, job_uid, outcome: new|duplicate|alias_added|excluded|prefilter_rejected|error, reason_code,
    duplicate_of}. Validation and key problems refuse the whole file (E_SCHEMA, E_INVENTED_KEY) before anything is
    written, except on the API lane where a listing whose key cannot be derived is reported as `error`.
    fetch_jd_later (API lane): a survivor without JD text stays `new` until `attach_jd` stores the JD."""
    if discovered_via not in ("api", "browser", "human"):
        raise Denied("E_VALIDATION", "discovered_via must be api, browser or human")
    source, items = validate_payload(payload, discovered_via)
    cfg = config if config is not None else load_config()
    prof = profile if profile is not None else load_profile()
    prepared: list = []
    key_errors: list[dict] = []
    for idx, it in enumerate(items):
        try:
            prepared.append(derive_keys(it, source))
        except Denied as d:
            if discovered_via == "api":
                prepared.append(d)
            else:
                key_errors.append({"input_index": idx, "code": d.code, "error": d.message})
    if key_errors:
        code = "E_INVENTED_KEY" if any(e["code"] == "E_INVENTED_KEY" for e in key_errors) else "E_VALIDATION"
        raise Denied(code, "job keys could not be derived for %d item(s); use the real page URL and ids"
                     % len(key_errors), data={"errors": key_errors[:40]})
    pv = profile_version(conn, prof)
    results = []
    for idx, (it, keys_or_err) in enumerate(zip(items, prepared)):
        if isinstance(keys_or_err, Denied):
            results.append({"input_index": idx, "job_uid": None, "outcome": "error",
                            "reason_code": keys_or_err.code.lower(), "duplicate_of": None})
            continue
        res = _ingest_one(conn, idx, it, keys_or_err, source, discovered_via, cycle_id, cfg, prof, pv,
                          fetch_jd_later)
        results.append(res)
        log_event(conn, "job_ingested", job_uid=res["job_uid"], outcome=res["outcome"], source=source,
                  reason_code=res["reason_code"], cycle_id=cycle_id)
    return results


def _uid_of(conn, job_id: int | None) -> str | None:
    if job_id is None:
        return None
    row = conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row[0] if row else None


def _original_uid(conn, job_id: int) -> str | None:
    row = conn.execute("SELECT job_uid, duplicate_of FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    if row[1] is not None:
        return _uid_of(conn, row[1]) or row[0]
    return row[0]


def store_jd(conn, job_id: int, text: str) -> bool:
    """Insert the JD text when the job has none (the first stored text wins). True when stored."""
    if not text or not text.strip():
        return False
    text = text[:JD_MAX]
    cur = conn.execute("INSERT INTO job_texts (job_id, jd_text, jd_sha256, fetched_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (job_id) DO NOTHING", (job_id, text, canon.sha256_text(text), canon.now()))
    return cur.rowcount == 1


def jd_text(conn, job_id: int) -> str:
    row = conn.execute("SELECT jd_text FROM job_texts WHERE job_id = ?", (job_id,)).fetchone()
    return row[0] if row else ""


def _apply_route(it: dict, source: str, keys: list[str]) -> str:
    if source in HUMAN_ONLY_SOURCES:
        return "human"
    if source == "linkedin_post":
        return "email" if it.get("apply_email") else "human"
    if it.get("apply_route_hint"):
        return it["apply_route_hint"]
    if it.get("apply_email") and not it.get("apply_url"):
        return "email"
    if any(k.startswith("ats:workday:") for k in keys):
        return "human"
    if any(k.startswith("ats:") for k in keys):
        return "ats_form"
    return "unknown"


def _company(conn, it: dict, source: str, discovered_via: str) -> int:
    """companies.resolve with the plain ATS tenant (U1 also records it in ats_tenants). When the polled form of
    the tenant differs (Workday 'tenant.wdN/site', Lever EU 'eu:site') that form is recorded too, so the API lane
    can poll the board. The apply email's registrable domain is a company key too (2.4.2: a recipient address), so
    the job's company and the company that owns that domain become one and the company cooldown covers the
    application email (_join_mail_domain)."""
    ten = it.get("tenant")
    ats = tenant = fetch = None
    if isinstance(ten, dict):
        ats, fetch = ten["ats"], ten["tenant"]
        parsed = ats_tenant_from_url(it["source_url"])
        tenant = parsed[2] if parsed and parsed[0] == ats else fetch.split(".")[0].split(":")[-1]
    else:
        for u in [it["source_url"], it.get("apply_url")] + list(it.get("redirect_urls") or []):
            parsed = ats_tenant_from_url(u)
            if parsed:
                ats, fetch, tenant = parsed
                break
    domain = it.get("company_domain")
    csource = "ats" if (discovered_via == "api" and source in ATS_SOURCES) else "job_board"
    comp, kmod = dep("companies"), dep("keys")
    mail = kmod.company_domain(it["apply_email"]) if it.get("apply_email") else None
    if mail and domain and kmod.company_domain(domain) == mail:
        mail = None
    ident = {"name": it["company"], "ats": ats, "tenant": tenant, "source": csource}
    cid = None
    if mail and not domain and comp.resolve(conn, domain=None, create=False, **ident) is None:
        # a company not seen yet: key it by the apply email's domain in the same call, so it joins the company
        # that already owns that domain instead of being created and merged into it afterwards
        try:
            cid = comp.resolve(conn, domain=mail, create=True, **ident)
        except Denied as d:
            if d.code != "E_COMPANY_AMBIGUOUS":
                raise
            # the domain's own keys match companies marked distinct; resolve opened the human task
        mail = None
    if cid is None:
        cid = comp.resolve(conn, domain=domain, create=True, **ident)
    if mail:
        cid = _join_mail_domain(conn, cid, mail, ident)
    if ats and fetch and fetch != tenant:
        conn.execute("INSERT INTO ats_tenants (ats, tenant, company_id, active, validated_at, source) VALUES "
                     "(?, ?, ?, 1, NULL, 'harvest') ON CONFLICT (ats, tenant) DO NOTHING", (ats, fetch, cid))
    return cid


def _join_mail_domain(conn, cid: int, mail: str, ident: dict) -> int:
    """Key the job's company by its apply email's registrable domain too (2.4.2 lists a recipient address as a
    domain key source), so the company that owns that domain merges with it and the company cooldown covers the
    application email. Skipped when a human marked the two companies distinct (companies split); the gate still
    counts that domain's company for the cooldown. Returns the surviving company id."""
    comp = dep("companies")
    me = comp.survivor(conn, cid)
    for key, _kind in dep("keys").company_keys(domain=mail):
        row = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", (key,)).fetchone()
        other = comp.survivor(conn, row[0]) if row else None
        if other is not None and other != me and conn.execute(
                "SELECT 1 FROM company_distinct WHERE a_id = ? AND b_id = ?", (min(me, other), max(me, other))
        ).fetchone():
            return me
    try:
        # create=False: when nothing but company_domain keyed this company (a name with no usable key and no
        # tenant), no key here finds it, and a second company must not be made for the mail domain alone
        comp.resolve(conn, name=ident["name"], domain=mail, ats=ident["ats"], tenant=ident["tenant"],
                     source="email", create=False)
    except Denied as d:
        if d.code != "E_COMPANY_AMBIGUOUS":
            raise
    return comp.survivor(conn, me)


def _exclusion_reason(conn, company_id: int | None, it: dict, keys: list[str]) -> str | None:
    rows = conn.execute("SELECT 1 FROM exclusions WHERE active = 1 AND type = 'job_url' AND value_key IN (%s)"
                        % ",".join("?" for _ in keys), keys).fetchone() if keys else None
    if rows:
        return "excluded_job_url"
    if company_id is not None:
        st = conn.execute("SELECT contact_state FROM companies WHERE id = ?", (company_id,)).fetchone()
        if st and st[0] == "do_not_contact":
            return "excluded_company"
    match = dep("exclusions").match
    hits = list(match(conn, company_id=company_id, company_name=it.get("company"), domain=it.get("company_domain"),
                      email=it.get("apply_email"), job_url=it.get("source_url")) or [])
    for u in [it.get("apply_url")] + list(it.get("redirect_urls") or []):
        if u:
            try:
                hits += list(match(conn, job_url=u) or [])
            except Denied as d:
                if d.code not in ("E_VALIDATION", "E_INVENTED_KEY"):
                    raise
    for h in hits:
        t = h.get("type") if isinstance(h, dict) else None
        if t == "job_url":
            return "excluded_job_url"
    return "excluded_company" if hits else None


def _record_prefilter(conn, job_id: int, code: str, sentence: str, pv: str) -> None:
    ts = canon.now()
    conn.execute(
        "INSERT INTO evaluations (job_id, stage, score, verdict, reason_code, reason_text, gates_failed, "
        "scorecard_json, clamped, profile_version, model, evaluated_at, updated_at) "
        "VALUES (?, 'prefilter', NULL, 'skip', ?, ?, ?, NULL, 0, ?, NULL, ?, ?) "
        "ON CONFLICT (job_id) DO UPDATE SET stage = 'prefilter', score = NULL, verdict = 'skip', "
        "reason_code = excluded.reason_code, reason_text = excluded.reason_text, gates_failed = excluded.gates_failed, "
        "scorecard_json = NULL, clamped = 0, profile_version = excluded.profile_version, model = NULL, "
        "evaluated_at = excluded.evaluated_at, updated_at = excluded.updated_at",
        (job_id, code, sentence or prefilter.reason_sentence(code), "[]", pv, ts, ts))


def job_filter_view(conn, job_id: int) -> dict:
    """The pre-filter view of a stored job (row, JD text and the company's agency flag)."""
    row = conn.execute("SELECT j.*, c.is_agency AS is_agency FROM jobs j LEFT JOIN companies c "
                       "ON c.id = j.company_id WHERE j.id = ?", (job_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no job with id %r" % job_id)
    view = dict(row)
    view["jd_text"] = jd_text(conn, job_id)
    view["company"] = view.get("company_name_raw")
    return view


def _hiring_team(conn, job_id: int, company_id: int, company_uid: str, team: list[dict]) -> int:
    if not team:
        return 0
    keys = dep("keys")
    people = dep("people")
    added = 0
    for p in team:
        try:
            pkeys = keys.person_keys(linkedin_url=p.get("linkedin_url"), full_name=p.get("name"),
                                     company_uid=company_uid)
        except Denied:
            continue
        if not pkeys:
            continue
        role = {"hiring_manager": "hiring_manager", "recruiter": "recruiter", "founder": "founder"}.get(
            p["relation"], "other")
        fields = {"full_name": p.get("name"), "title": p.get("title"), "company_id": company_id,
                  "role_type": role, "linkedin_url": p.get("linkedin_url")}
        try:
            contact_id = people.resolve(conn, keys=pkeys, fields=fields, create=True)
        except Denied:
            continue
        cur = conn.execute("INSERT INTO job_hiring_team (job_id, contact_id, relation) VALUES (?, ?, ?) "
                           "ON CONFLICT (job_id, contact_id) DO NOTHING", (job_id, contact_id, p["relation"]))
        added += cur.rowcount
    return added


def _root_id(conn, job_id: int) -> int:
    """The original of a job recorded as a duplicate (follows duplicate_of; a cycle stops the walk)."""
    seen = set()
    while job_id not in seen:
        seen.add(job_id)
        row = conn.execute("SELECT duplicate_of FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row[0] is None:
            break
        job_id = row[0]
    return job_id


def _has_application(conn, job_id: int) -> bool:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is not None and row[0] in ("applying", "applied"):
        return True
    return conn.execute("SELECT 1 FROM actions WHERE job_id = ? AND kind IN ('application','application_email') "
                        "AND status IN (%s) LIMIT 1" % ",".join("?" for _ in _LIVE_ACTIONS),
                        (job_id,) + _LIVE_ACTIONS).fetchone() is not None


def _link_jobs(conn, job_ids: list, by: str) -> tuple:
    """One listing's keys matched several job rows (for example a board page whose apply URL is an ATS posting
    stored earlier under a company name that resolved elsewhere): they are one posting. The survivor is the row
    that already has an application, else the oldest. Every other row gets duplicate_of = survivor (rows that
    pointed at it follow) and leaves the pipeline: `new` becomes `duplicate`, an evaluation claim is released and
    any status that may close is `closed`; applied or applying rows keep their status. Returns (survivor, linked)."""
    roots = sorted({_root_id(conn, j) for j in job_ids})
    if len(roots) == 1:
        return roots[0], []
    survivor = next((j for j in roots if _has_application(conn, j)), roots[0])
    linked = []
    for j in roots:
        if j == survivor:
            continue
        conn.execute("UPDATE jobs SET duplicate_of = ? WHERE id = ? OR duplicate_of = ?", (survivor, j, j))
        status = conn.execute("SELECT status FROM jobs WHERE id = ?", (j,)).fetchone()[0]
        if status == "new":
            jobstate.set_job_status(conn, j, "duplicate", "shared_key", by)
        else:
            if status == "evaluating":
                conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (j,))
                jobstate.set_job_status(conn, j, "eval_queued", "duplicate", by)
                status = "eval_queued"
            if status != "closed" and jobstate.job_transition_allowed(status, "closed"):
                jobstate.set_job_status(conn, j, "closed", "duplicate", by)
        linked.append(j)
    log_event(conn, "job_linked", job_uid=_uid_of(conn, survivor), duplicates=[_uid_of(conn, j) for j in linked],
              by=by)
    return survivor, linked


def _ingest_one(conn, idx, it, keys, source, discovered_via, cycle_id, cfg, prof, pv, fetch_jd_later) -> dict:
    res = {"input_index": idx, "job_uid": None, "outcome": None, "reason_code": None, "duplicate_of": None}
    existing = lookup_keys(conn, keys)
    if existing:
        matched = sorted(set(existing.values()))
        linked: list = []
        if len(matched) > 1:
            target, linked = _link_jobs(conn, matched, "jobs.ingest")
        else:
            target = matched[0]
        new_keys = [k for k in keys if k not in existing]
        for k in new_keys:
            _insert_key(conn, k, target)
        row = conn.execute("SELECT job_uid, status, human_call FROM jobs WHERE id = ?", (target,)).fetchone()
        if it.get("jd_text") and store_jd(conn, target, it["jd_text"]) and row["status"] == "new" \
                and row["human_call"] != "never":
            _decide_after_jd(conn, target, prof, cfg, pv)
        res.update(job_uid=row["job_uid"], outcome="alias_added" if new_keys else "duplicate",
                   reason_code="shared_key" if linked else None, duplicate_of=_original_uid(conn, target))
        return res
    try:
        company_id = _company(conn, it, source, discovered_via)
    except Denied as d:
        if d.code in ("E_COMPANY_AMBIGUOUS", "E_VALIDATION"):
            res.update(outcome="error", reason_code=d.code.lower())
            return res
        raise
    crow = conn.execute("SELECT company_uid, is_agency FROM companies WHERE id = ?", (company_id,)).fetchone()
    if crow is None:
        raise Denied("E_INTERNAL", "companies.resolve returned an unknown id")
    kmod = dep("keys")
    title = it["title"]
    place = _first_place(it.get("location"))
    ncity = kmod.norm_city(place) if place else None
    mode = _infer_mode(it.get("work_mode"), it.get("location"))
    ts = canon.now()
    uid = canon.new_uid("J")
    cur = conn.execute(
        "INSERT INTO jobs (job_uid, canonical_key, fingerprint, source, discovered_via, source_url, apply_url, "
        "apply_route, apply_email, company_id, company_name_raw, title, norm_title, role_key, location_raw, "
        "norm_city, work_mode, remote_scope, employment_type, years_min, years_max, salary_min, salary_max, "
        "salary_currency, salary_period, posted_at, discovered_at, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
        (uid, keys[0], kmod.fingerprint(crow["company_uid"], title, ncity), source, discovered_via,
         it["source_url"], it.get("apply_url"), _apply_route(it, source, keys), it.get("apply_email"), company_id,
         it["company"], title, kmod.norm_title(title), kmod.role_key(title), it.get("location"), ncity, mode,
         it.get("remote_scope"), it.get("employment_type"), it["years"]["min"], it["years"]["max"],
         it["salary"]["min"], it["salary"]["max"], it["salary"]["currency"], it["salary"]["period"],
         it.get("posted_at"), ts, ts, ts))
    job_id = cur.lastrowid
    for k in keys:
        _insert_key(conn, k, job_id)
    if it.get("jd_text"):
        store_jd(conn, job_id, it["jd_text"])
    _hiring_team(conn, job_id, company_id, crow["company_uid"], it.get("hiring_team") or [])
    res["job_uid"] = uid
    # 1. exclusions
    ex = _exclusion_reason(conn, company_id, it, keys)
    if ex:
        jobstate.set_job_status(conn, job_id, "excluded", ex, "jobs.ingest")
        _record_prefilter(conn, job_id, ex, prefilter.reason_sentence(ex), pv)
        res.update(outcome="excluded", reason_code=ex)
        return res
    # 2. the same job from another source (fingerprint within 45 days)
    fp = conn.execute("SELECT fingerprint FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    dup = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint = ? AND id <> ? AND status <> 'duplicate' "
        "AND discovered_at > ? ORDER BY id LIMIT 1",
        (fp, job_id, canon.ts_add(ts, days=-FP_WINDOW_DAYS))).fetchone()
    if dup:
        original = _root_id(conn, dup["id"])    # a row linked by shared keys points at its survivor
        conn.execute("UPDATE jobs SET duplicate_of = ? WHERE id = ?", (original, job_id))
        jobstate.set_job_status(conn, job_id, "duplicate", "fingerprint", "jobs.ingest")
        res.update(outcome="duplicate", reason_code="fingerprint", duplicate_of=_uid_of(conn, original))
        return res
    # 3. pre-filter (6.2)
    view = dict(it)
    view["norm_city"] = ncity
    view["work_mode"] = mode
    view["is_agency"] = bool(crow["is_agency"])
    ok, code, sentence = prefilter.explain(view, prof, cfg)
    if not ok:
        jobstate.set_job_status(conn, job_id, "prefilter_rejected", code, "jobs.ingest")
        _record_prefilter(conn, job_id, code, sentence, pv)
        res.update(outcome="prefilter_rejected", reason_code=code)
        return res
    # 4. evaluation queue, or wait for the JD fetch (API lane)
    if it.get("jd_text") or not fetch_jd_later:
        jobstate.set_job_status(conn, job_id, "eval_queued", "prefilter_passed", "jobs.ingest")
    res["outcome"] = "new"
    return res


def _decide_after_jd(conn, job_id: int, prof: dict, cfg: dict, pv: str) -> str:
    """For a job left `new` by the API lane: run the pre-filter again with the JD and queue or reject it."""
    ok, code, sentence = prefilter.explain(job_filter_view(conn, job_id), prof, cfg)
    if not ok:
        jobstate.set_job_status(conn, job_id, "prefilter_rejected", code, "jobs.attach_jd")
        _record_prefilter(conn, job_id, code, sentence, pv)
        return "prefilter_rejected"
    jobstate.set_job_status(conn, job_id, "eval_queued", "prefilter_passed", "jobs.attach_jd")
    return "eval_queued"


def attach_jd(conn, job_id: int, text: str | None, *, can_apply: bool | None = None, posted_at: str | None = None,
              profile: dict | None = None, config: dict | None = None) -> str:
    """Store a fetched JD (API lane detail stage) and move a waiting `new` job on. Returns the job status."""
    row = conn.execute("SELECT status, human_call, posted_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no job with id %r" % job_id)
    if posted_at and not row["posted_at"] and _DATE_RE.match(posted_at):
        conn.execute("UPDATE jobs SET posted_at = ? WHERE id = ?", (posted_at[:10], job_id))
    stored = store_jd(conn, job_id, text or "")
    if row["status"] != "new" or row["human_call"] == "never":
        return row["status"]
    prof = profile if profile is not None else load_profile()
    cfg = config if config is not None else load_config()
    pv = profile_version(conn, prof)
    if can_apply is False:
        jobstate.set_job_status(conn, job_id, "prefilter_rejected", "stale", "jobs.attach_jd")
        _record_prefilter(conn, job_id, "stale", "The posting no longer accepts applications", pv)
        return "prefilter_rejected"
    if not stored and not jd_text(conn, job_id):
        return "new"
    return _decide_after_jd(conn, job_id, prof, cfg, pv)


# ---------------------------------------------------------------- aliases, human call, status
def get_job(conn, job_uid: str):
    row = conn.execute("SELECT * FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    return row


def add_alias(conn, job_uid: str, url: str) -> dict:
    """Record another URL of a known job (for example the ATS page a board redirected to). Every key of the URL
    is added to the job; a key that already belongs to another job is refused with E_DUP_JOB (exit 3)."""
    if not is_url(url, https_only=True):
        raise Denied("E_VALIDATION", "the alias must be an https URL")
    job = get_job(conn, job_uid)
    ck, aliases = dep("keys").job_key(url, {}, job["source"])
    keys = [ck] + [a for a in (aliases or []) if a]
    existing = lookup_keys(conn, keys)
    for k, jid in existing.items():
        if jid != job["id"]:
            other = _uid_of(conn, jid)
            raise Denied("E_DUP_JOB", "this URL belongs to another job %s" % other,
                         data={"duplicate_of": other, "key": k})
    added = [k for k in keys if k not in existing and _insert_key(conn, k, job["id"])]
    log_event(conn, "job_alias_added", job_uid=job_uid, keys=added)
    return {"job_uid": job_uid, "key": ck, "kind": key_kind(ck), "added": added}


def set_human_call(conn, job_uid: str, call: str, by: str) -> None:
    """The person's call on a job (Sheet "Your call", /jh, CLI). apply_anyway moves a borderline, rejected,
    pre-filter-rejected or closed job to `eligible` (every send gate still applies); never records the call and
    closes the job where the status graph allows it. Exclusions always win (E_EXCLUDED); a job recorded as a
    duplicate of another (fingerprint or shared keys) refuses apply_anyway with E_DUP_JOB."""
    if call not in HUMAN_CALLS:
        raise Denied("E_VALIDATION", "call must be apply_anyway or never")
    job = get_job(conn, job_uid)
    status = job["status"]
    if call == "apply_anyway":
        if status == "excluded":
            raise Denied("E_EXCLUDED", "the job or its company is on your exclusion list")
        if status == "duplicate" or job["duplicate_of"] is not None:
            dup = _uid_of(conn, job["duplicate_of"])
            raise Denied("E_DUP_JOB", "this job is a duplicate of %s; decide on that one" % dup,
                         data={"duplicate_of": dup})
    conn.execute("UPDATE jobs SET human_call = ? WHERE id = ?", (call, job["id"]))
    if call == "apply_anyway" and status in ("borderline", "rejected", "prefilter_rejected", "closed"):
        jobstate.set_job_status(conn, job["id"], "eligible", "human_apply_anyway", by)
    elif call == "never":
        if status == "evaluating":
            conn.execute("UPDATE jobs SET claimed_by = NULL, claimed_until = NULL WHERE id = ?", (job["id"],))
            jobstate.set_job_status(conn, job["id"], "eval_queued", "human_never", by)
            status = "eval_queued"
        if jobstate.job_transition_allowed(status, "closed") and status != "closed":
            jobstate.set_job_status(conn, job["id"], "closed", "human_never", by)
    log_event(conn, "job_human_call", job_uid=job_uid, call=call, by=by, status_before=status)


def set_status(conn, job_uid: str, status: str, reason: str, by: str, *, agent: bool = False) -> dict:
    """`job set-status`: needs_human (applier or human) or closed (human). needs_human opens an apply_manually
    task for the person."""
    if status not in ("needs_human", "closed"):
        raise Denied("E_VALIDATION", "status must be needs_human or closed")
    if not isinstance(reason, str) or not re.match(r"^[a-z][a-z0-9_]{1,40}$", reason):
        raise Denied("E_VALIDATION", "reason must be a short code like account_required")
    if agent and (status != "needs_human" or reason not in AGENT_NEEDS_HUMAN_REASONS):
        raise Denied("E_CALLER_NOT_ALLOWED", "agents may only set needs_human with a listed reason")
    job = get_job(conn, job_uid)
    jobstate.set_job_status(conn, job["id"], status, reason, by)
    task = None
    if status == "needs_human":
        task = open_human_task(conn, "apply_manually",
                               "Apply to %s at %s yourself (%s)" % (job["title"], job["company_name_raw"],
                                                                    reason.replace("_", " ")),
                               job_id=job["id"], detail=job["source_url"])
    return {"job_uid": job_uid, "status": status, "reason": reason, "task_uid": task}


# ---------------------------------------------------------------- reads
def show(conn, job_uid: str, jd_max: int = 12000) -> dict:
    job = get_job(conn, job_uid)
    out = {k: job[k] for k in job.keys() if k not in ("id", "company_id", "duplicate_of", "claimed_by",
                                                      "claimed_until")}
    comp = conn.execute("SELECT company_uid, display_name, domain, is_agency, contact_state FROM companies "
                        "WHERE id = ?", (job["company_id"],)).fetchone() if job["company_id"] else None
    out["company"] = dict(comp) if comp else None
    out["duplicate_of"] = _uid_of(conn, job["duplicate_of"])
    out["keys"] = [{"key": r[0], "kind": r[1]} for r in
                   conn.execute("SELECT key, kind FROM job_keys WHERE job_id = ? ORDER BY created_at, key",
                                (job["id"],))]
    ev = conn.execute("SELECT stage, score, verdict, reason_code, reason_text, gates_failed, clamped, "
                      "profile_version, model, evaluated_at FROM evaluations WHERE job_id = ?",
                      (job["id"],)).fetchone()
    if ev:
        ev = dict(ev)
        try:
            ev["gates_failed"] = json.loads(ev["gates_failed"] or "[]")
        except ValueError:
            pass
    out["evaluation"] = ev
    out["actions"] = [dict(r) for r in conn.execute(
        "SELECT token, kind, status, platform, reserved_at, sent_at FROM actions WHERE job_id = ? ORDER BY id",
        (job["id"],))]
    out["hiring_team"] = [dict(r) for r in conn.execute(
        "SELECT c.contact_uid, c.full_name, c.title, h.relation FROM job_hiring_team h JOIN contacts c "
        "ON c.id = h.contact_id WHERE h.job_id = ?", (job["id"],))]
    text = jd_text(conn, job["id"])
    out["jd_chars"] = len(text)
    out["jd_text"] = text[:jd_max]
    out["jd_truncated"] = len(text) > jd_max
    return out


def list_jobs(conn, status: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 500))
    sql = ("SELECT j.job_uid, j.status, j.status_reason, j.title, j.company_name_raw AS company, j.location_raw "
           "AS location, j.work_mode, j.source, j.discovered_at, e.score, e.verdict FROM jobs j LEFT JOIN "
           "evaluations e ON e.job_id = j.id")
    args: list = []
    if status:
        if status not in jobstate.JOB_STATUSES:
            raise Denied("E_VALIDATION", "unknown job status %r" % status)
        sql += " WHERE j.status = ?"
        args.append(status)
    sql += " ORDER BY j.discovered_at DESC, j.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]
