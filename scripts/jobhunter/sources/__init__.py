"""API discovery lane (design 1.3.1): documented public job APIs polled by code, no model, no login.

    summary = run_fetch(conn, lane="api")           # `sources fetch --lane api` (manages its own transactions)
    rows    = list_sources(conn)                     # `sources list`
    info    = add_tenant(conn, "greenhouse", "kestrelcommerce", company="Kestrel Commerce")

Per due (source, tenant): fetch the list (one request per second per host, ETag/Last-Modified where the source
supports it), ingest every listing through `jobs.ingest` (keys, company, exclusions, pre-filter), then fetch the JD
detail for survivors only and queue them for the evaluator. Failures back off exponentially; a 429 or three
consecutive errors trip the `api:<source>` breaker (4.5); a 404 for an ATS tenant deactivates that tenant. The
first response's HTTP Date is compared with the local clock (more than `clock.max_skew_minutes` trips `global`).
Instahyre and Foundit internal endpoints are never called by code (they are opt-in browser sites).
"""
from __future__ import annotations

import csv
import importlib
import json
import os
import time
from dataclasses import dataclass

from .. import canon, db, jobs, paths, prefilter
from ..errors import Denied
from ..events import log_event
from .http import HttpClient, HttpError, NotFound, NotModified, RateLimited, date_skew_seconds


@dataclass(frozen=True)
class Spec:
    id: str            # config key under sources.api
    module: str        # module in this package
    kind: str          # 'ats' (needs tenants) or 'board'
    poll: str          # key under sources.poll_hours
    job_source: str    # jobs.source value


SPECS = (
    Spec("greenhouse", "greenhouse", "ats", "ats", "greenhouse"),
    Spec("lever", "lever", "ats", "ats", "lever"),
    Spec("ashby", "ashby", "ats", "ats", "ashby"),
    Spec("smartrecruiters", "smartrecruiters", "ats", "ats", "smartrecruiters"),
    Spec("workable", "workable", "ats", "ats", "workable"),
    Spec("recruitee", "recruitee", "ats", "ats", "recruitee"),
    Spec("bamboohr", "bamboohr", "ats", "ats", "bamboohr"),
    Spec("workday", "workday", "ats", "ats", "workday"),
    Spec("yc", "yc", "board", "yc", "yc"),
    Spec("himalayas", "himalayas", "board", "remote_boards", "himalayas"),
    Spec("remoteok", "remoteok", "board", "remote_boards", "remoteok"),
    Spec("remotive", "remotive", "board", "remote_boards", "remotive"),
    Spec("weworkremotely", "wwr", "board", "remote_boards", "weworkremotely"),
    Spec("workingnomads", "workingnomads", "board", "remote_boards", "workingnomads"),
    Spec("hn_whoishiring", "hn", "board", "hn", "hn"),
    Spec("jobicy", "jobicy", "board", "remote_boards", "jobicy"),
    Spec("serpapi_google_jobs", "serpapi", "board", "serpapi", "serpapi"),
)
BY_ID = {s.id: s for s in SPECS}
ATS_IDS = tuple(s.id for s in SPECS if s.kind == "ats")
DEFAULT_ENABLED = {s.id: s.id not in ("jobicy", "serpapi_google_jobs") for s in SPECS}
POLL_DEFAULT = {"ats": 12, "remote_boards": 6, "yc": 24, "hn": 24, "serpapi": 24}
POLL_MIN = {"ats": 6, "remote_boards": 3, "yc": 24, "hn": 24, "serpapi": 24}
REMOTIVE_MAX_DAY = 4
MAX_ERRORS = 3
DETAIL_LIMIT = 80
HARVEST_LIMIT = 20
DEFAULT_MAX_SECONDS = 780
DEFAULT_UA = "openclaw-job-hunter/2.0 (personal job search)"
COUNTRY_NAMES = {"IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
                 "SG": "Singapore", "DE": "Germany", "NL": "Netherlands", "AE": "United Arab Emirates"}


def module(spec: Spec):
    return importlib.import_module("%s.%s" % (__name__, spec.module))


def _cfg(cfg, *path, default=None):
    return prefilter._cfg(cfg, *path, default=default)


def enabled(spec: Spec, cfg: dict) -> bool:
    v = _cfg(cfg, "sources", "api", spec.id, default=DEFAULT_ENABLED[spec.id])
    return v is True


def poll_hours(spec: Spec, cfg: dict) -> int:
    v = _cfg(cfg, "sources", "poll_hours", spec.poll, default=POLL_DEFAULT[spec.poll])
    v = v if isinstance(v, int) and not isinstance(v, bool) else POLL_DEFAULT[spec.poll]
    return max(v, POLL_MIN[spec.poll])


def _secrets() -> dict:
    try:
        with open(os.path.join(paths.private_dir(), "secrets.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build_ctx(prof: dict, cfg: dict) -> dict:
    """What a source may use from the profile: role titles (Workday and SerpApi search text), countries (YC,
    Himalayas and Jobicy location filters), cities and whether remote work is wanted. Never personal facts."""
    vals = prefilter.profile_values(prof)
    queries = []
    for fam in vals.get("role_families") or []:
        if isinstance(fam, dict):
            for t in (fam.get("titles") or [])[:2]:
                if isinstance(t, str) and t.strip() and t not in queries:
                    queries.append(" ".join(t.split()))
    countries = sorted(prefilter.person_countries(vals))
    loc = vals.get("locations") if isinstance(vals.get("locations"), dict) else {}
    cities = [c for c in (loc.get("cities") or []) if isinstance(c, str) and c.lower() != "remote"]
    modes = [str(m).lower() for m in (loc.get("work_modes") or [])]
    remote = "remote" in modes or any(isinstance(c, str) and c.lower() == "remote" for c in (loc.get("cities") or []))
    return {"queries": queries[:4], "countries": countries,
            "country_names": [COUNTRY_NAMES[c] for c in countries if c in COUNTRY_NAMES],
            "places": cities[:2], "remote": remote}


# ---------------------------------------------------------------- state
def _state(conn, source: str, tenant: str):
    return conn.execute("SELECT * FROM sources_state WHERE source = ? AND tenant = ?", (source, tenant)).fetchone()


def _save_state(conn, source: str, tenant: str, **cols) -> None:
    row = _state(conn, source, tenant)
    if row is None:
        conn.execute("INSERT INTO sources_state (source, tenant) VALUES (?, ?)", (source, tenant))
    if cols:
        conn.execute("UPDATE sources_state SET %s WHERE source = ? AND tenant = ?" % ", ".join(
            "%s = ?" % k for k in cols), list(cols.values()) + [source, tenant])


def _backoff_hours(errors: int, poll: int) -> float:
    return min(float(poll) * 4, 0.5 * (2 ** max(0, errors - 1)))


def sync_targets(conn, cfg: dict) -> int:
    """Add the tenants of private/targets.csv (company,ats,tenant) to ats_tenants. Never deactivates."""
    rel = _cfg(cfg, "sources", "targets_file", default="private/targets.csv")
    path = rel if os.path.isabs(str(rel)) else os.path.join(paths.root(), str(rel))
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return 0
    added = 0
    for row in csv.DictReader(lines):
        ats = (row.get("ats") or "").strip().lower()
        tenant = (row.get("tenant") or "").strip()
        company = (row.get("company") or "").strip() or None
        if ats not in ATS_IDS or not tenant:
            continue
        mod = module(BY_ID[ats])
        if not mod.TENANT_RE.match(tenant):
            continue
        cur = conn.execute("INSERT INTO ats_tenants (ats, tenant, company_id, active, validated_at, source) "
                           "VALUES (?, ?, NULL, 1, NULL, 'targets_file') ON CONFLICT (ats, tenant) DO NOTHING",
                           (ats, tenant))
        if cur.rowcount:
            added += 1
            if company:
                _link_company(conn, ats, tenant, company)
    return added


def _company_tenant(ats: str, tenant: str) -> str:
    if ats == "workday":
        return tenant.split(".")[0]
    if ats == "lever" and tenant.startswith("eu:"):
        return tenant[3:]
    return tenant


def _link_company(conn, ats: str, tenant: str, name: str | None) -> int | None:
    try:
        cid = jobs.dep("companies").resolve(conn, name=name or _company_tenant(ats, tenant), domain=None, ats=ats,
                                            tenant=_company_tenant(ats, tenant), source="ats", create=True)
    except Denied:
        return None
    conn.execute("UPDATE ats_tenants SET company_id = ? WHERE ats = ? AND tenant = ?", (cid, ats, tenant))
    return cid


def _tenants(conn, ats: str) -> list[str]:
    """Active tenants in the form this source polls. Rows in another form (companies.resolve records the plain
    tenant, for example 'kestrel' for a Workday board polled as 'kestrel.wd5/External') are skipped."""
    rx = module(BY_ID[ats]).TENANT_RE
    return [r[0] for r in conn.execute("SELECT tenant FROM ats_tenants WHERE ats = ? AND active = 1 ORDER BY tenant",
                                       (ats,)) if rx.match(r[0] or "")]


def _breaker_open(conn, scope: str) -> bool:
    try:
        jobs.dep("breakers").check_breakers(conn, [scope])
    except Denied:
        return True
    return False


def plan(conn, cfg: dict, source: str | None = None, tenant: str | None = None,
         limit: int | None = None) -> list[tuple[Spec, str]]:
    """Due (source, tenant) pairs, least recently fetched first. Naming a source (or tenant) skips the due test
    and the enabled switch, not the breaker."""
    if source is not None and source not in BY_ID:
        raise Denied("E_USAGE", "unknown source %r; one of %s" % (source, ", ".join(BY_ID)))
    now = canon.now()
    out = []
    for spec in SPECS:
        if source is not None and spec.id != source:
            continue
        if source is None and not enabled(spec, cfg):
            continue
        tenants = _tenants(conn, spec.id) if spec.kind == "ats" else [""]
        if tenant is not None:
            tenants = [t for t in tenants if t == tenant] or ([tenant] if spec.kind == "ats" else [])
        for t in tenants:
            st = _state(conn, spec.id, t)
            nxt = st["next_fetch_at"] if st is not None else None
            if source is None and nxt and nxt > now:
                continue
            out.append((nxt or "", spec, t))
    out.sort(key=lambda x: (x[0], x[1].id, x[2]))
    pairs = [(s, t) for _, s, t in out]
    if limit is not None:
        pairs = pairs[:max(0, int(limit))]
    return pairs


def _requests_24h(conn, source: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(n), 0) FROM counters WHERE platform = ? AND metric = 'api_request' "
                       "AND ts > ?", ("api:" + source, canon.ts_add(canon.now(), hours=-24))).fetchone()
    return int(row[0] or 0)


def _count_requests(conn, source: str, n: int) -> None:
    if n > 0:
        conn.execute("INSERT INTO counters (ts, platform, metric, n, kind, cycle_id) VALUES (?, ?, 'api_request', "
                     "?, 'inc', NULL)", (canon.now(), "api:" + source, n))


def make_client(cfg: dict) -> HttpClient:
    ua = _cfg(cfg, "sources", "user_agent", default=DEFAULT_UA)
    if not isinstance(ua, str) or "openclaw-job-hunter" not in ua:
        ua = DEFAULT_UA
    gap = _cfg(cfg, "sources", "per_host_min_interval_ms", default=1000)
    gap = max(1000, gap) if isinstance(gap, int) else 1000
    return HttpClient(ua, min_interval_ms=gap)


# ---------------------------------------------------------------- the run
def _valid_items(items: list, job_source: str) -> tuple[list, int]:
    ok, bad = [], 0
    for it in items:
        try:
            jobs.validate_payload({"source": job_source, "jobs": [it]}, "api")
        except Denied:
            bad += 1
            continue
        ok.append(it)
    return ok, bad


def _harvest(conn, items: list, cfg: dict, budget: list) -> int:
    """ATS tenants seen in apply links of board listings become polled tenants (source 'harvest')."""
    n = 0
    for it in items:
        if budget[0] <= 0:
            break
        for u in [it.get("apply_url"), it.get("source_url")] + list(it.get("redirect_urls") or []):
            parsed = jobs.ats_tenant_from_url(u)
            if not parsed or not enabled(BY_ID[parsed[0]], cfg):
                continue
            if not module(BY_ID[parsed[0]]).TENANT_RE.match(parsed[1]):
                continue
            cur = conn.execute("INSERT INTO ats_tenants (ats, tenant, company_id, active, validated_at, source) "
                               "VALUES (?, ?, NULL, 1, NULL, 'harvest') ON CONFLICT (ats, tenant) DO NOTHING",
                               parsed[:2])
            if cur.rowcount:
                n += 1
                budget[0] -= 1
            break
    return n


def run_fetch(conn, *, lane: str = "api", source: str | None = None, tenant: str | None = None,
              limit: int | None = None, client=None, config: dict | None = None, profile: dict | None = None,
              max_seconds: int = DEFAULT_MAX_SECONDS, clock=time.monotonic) -> dict:
    """One API discovery run. Returns the summary of 3.4: fetched, new, duplicates, prefilter_rejected,
    eval_queued, errors (plus sources, not_modified, jd_fetched, tenants_harvested). Denied(E_PAUSED |
    E_BREAKER_OPEN) when the install is paused or the global breaker is open; E_CLOCK_SKEW after tripping
    `global` when the server clock disagrees with ours."""
    if lane != "api":
        raise Denied("E_USAGE", "only --lane api is fetched by code; browser sites belong to the scout")
    if os.path.exists(paths.paused_file()):
        raise Denied("E_PAUSED", "the job hunter is paused")
    cfg = config if config is not None else jobs.load_config()
    prof = profile if profile is not None else jobs.load_profile()
    with db.tx(conn):
        jobs.dep("breakers").check_breakers(conn, ["global"])
        sync_targets(conn, cfg)
    if not isinstance(prof, dict) or prof.get("confirmed") is False or not prefilter.profile_values(prof):
        # without confirmed preferences the pre-filter cannot filter; fetching now would flood the evaluator
        return {"fetched": 0, "new": 0, "duplicates": 0, "prefilter_rejected": 0, "eval_queued": 0, "excluded": 0,
                "errors": [], "sources": 0, "not_modified": 0, "jd_fetched": 0, "tenants_harvested": 0,
                "stopped_early": False, "ok_sources": 0, "skipped": "profile_unconfirmed"}
    client = client or make_client(cfg)
    ctx_base = build_ctx(prof, cfg)
    if source == "serpapi_google_jobs" or (source is None and enabled(BY_ID["serpapi_google_jobs"], cfg)):
        ctx_base["secrets"] = {"serpapi_key": _secrets().get("serpapi_key")}   # only this key, never the rest
    start = clock()
    summary = {"fetched": 0, "new": 0, "duplicates": 0, "prefilter_rejected": 0, "eval_queued": 0,
               "excluded": 0, "errors": [], "sources": 0, "not_modified": 0, "jd_fetched": 0,
               "tenants_harvested": 0, "stopped_early": False}
    tripped: set = set()
    harvest_budget = [HARVEST_LIMIT]
    remotive_cap = _cfg(cfg, "sources", "remotive_max_calls_day", default=REMOTIVE_MAX_DAY)
    remotive_cap = min(REMOTIVE_MAX_DAY, remotive_cap) if isinstance(remotive_cap, int) else REMOTIVE_MAX_DAY
    succeeded = 0
    clock_checked = False
    for spec, ten in plan(conn, cfg, source, tenant, limit):
        if clock() - start > max_seconds:
            summary["stopped_early"] = True
            break
        if spec.id in tripped or _breaker_open(conn, "api:" + spec.id):
            continue
        if spec.id == "remotive" and _requests_24h(conn, "remotive") >= remotive_cap:
            continue
        mod = module(spec)
        st = _state(conn, spec.id, ten)
        ctx = dict(ctx_base)
        if getattr(mod, "USES_VALIDATORS", False) and st is not None:
            ctx["etag"], ctx["last_modified"] = st["etag"], st["last_modified"]
        before = client.requests
        poll = poll_hours(spec, cfg)
        now = canon.now()
        try:
            items = mod.fetch_list(client, ten, ctx)
        except NotModified:
            with db.tx(conn):
                _count_requests(conn, spec.id, client.requests - before)
                _save_state(conn, spec.id, ten, last_fetch_at=now, next_fetch_at=canon.ts_add(now, hours=poll),
                            last_status=304, consecutive_errors=0)
                if ten:
                    _save_state(conn, spec.id, "", consecutive_errors=0)
            summary["not_modified"] += 1
            succeeded += 1
            continue
        except NotFound as exc:
            with db.tx(conn):
                _count_requests(conn, spec.id, client.requests - before)
                _save_state(conn, spec.id, ten, last_fetch_at=now, next_fetch_at=canon.ts_add(now, hours=poll * 4),
                            last_status=exc.status or 404)
                if spec.kind == "ats" and ten:
                    conn.execute("UPDATE ats_tenants SET active = 0 WHERE ats = ? AND tenant = ?", (spec.id, ten))
                    log_event(conn, "ats_tenant_deactivated", ats=spec.id, tenant=ten, status=exc.status)
            summary["errors"].append({"source": spec.id, "tenant": ten, "error": "not_found"})
            continue
        except RateLimited as exc:
            with db.tx(conn):
                _count_requests(conn, spec.id, client.requests - before)
                _save_state(conn, spec.id, ten, last_fetch_at=now, last_status=exc.status,
                            next_fetch_at=canon.ts_add(now, hours=poll))
                jobs.dep("breakers").trip(conn, "api:" + spec.id, "http_%d" % exc.status,
                                          "rate limited by %s" % spec.id)
            tripped.add(spec.id)
            summary["errors"].append({"source": spec.id, "tenant": ten, "error": "rate_limited"})
            continue
        except (HttpError, ValueError, KeyError, TypeError, AttributeError) as exc:
            status = getattr(exc, "status", None)
            with db.tx(conn):
                _count_requests(conn, spec.id, client.requests - before)
                n = ((st["consecutive_errors"] if st is not None else 0) or 0) + 1
                _save_state(conn, spec.id, ten, last_fetch_at=now, last_status=status if status is not None else -1,
                            consecutive_errors=n, next_fetch_at=canon.ts_add(now, hours=_backoff_hours(n, poll)))
                if ten:
                    src = _state(conn, spec.id, "")
                    sn = ((src["consecutive_errors"] if src is not None else 0) or 0) + 1
                    _save_state(conn, spec.id, "", consecutive_errors=sn)
                else:
                    sn = n
                if sn >= MAX_ERRORS:
                    jobs.dep("breakers").trip(conn, "api:" + spec.id, "consecutive_errors",
                                              "%d consecutive errors from %s: %s" % (sn, spec.id, str(exc)[:200]))
                    tripped.add(spec.id)
            summary["errors"].append({"source": spec.id, "tenant": ten, "error": type(exc).__name__,
                                      "detail": str(exc)[:200]})
            continue
        if not clock_checked and client.first_date:
            clock_checked = True
            skew = date_skew_seconds(client.first_date, canon.utcnow())
            lim = _cfg(cfg, "clock", "max_skew_minutes", default=10)
            lim = lim if isinstance(lim, int) and lim > 0 else 10
            if skew is not None and skew > lim * 60:
                with db.tx(conn):
                    jobs.dep("breakers").trip(conn, "global", "clock_skew",
                                              "server clock differs from ours by %d seconds" % skew)
                raise Denied("E_CLOCK_SKEW", "the computer clock is off by %d seconds; fix it, then reset the "
                             "global breaker" % skew, data=summary)
        succeeded += 1
        items, bad = _valid_items(items, spec.job_source)
        summary["fetched"] += len(items) + bad
        if bad:
            summary["errors"].append({"source": spec.id, "tenant": ten, "error": "invalid_listings", "n": bad})
        with db.tx(conn):
            _count_requests(conn, spec.id, client.requests - before)
            for i in range(0, len(items), jobs.MAX_ITEMS):
                chunk = items[i:i + jobs.MAX_ITEMS]
                results = jobs.ingest(conn, {"source": spec.job_source, "jobs": chunk}, None, "api", profile=prof,
                                      config=cfg, fetch_jd_later=bool(getattr(mod, "NEEDS_DETAIL", False)))
                _tally(conn, summary, results)
            if spec.kind == "board":
                summary["tenants_harvested"] += _harvest(conn, items, cfg, harvest_budget)
            validators = client.validators if getattr(mod, "USES_VALIDATORS", False) else (None, None)
            _save_state(conn, spec.id, ten, last_fetch_at=now, next_fetch_at=canon.ts_add(now, hours=poll),
                        last_status=200, consecutive_errors=0, items_last=len(items),
                        etag=validators[0], last_modified=validators[1])
            if ten:
                _save_state(conn, spec.id, "", consecutive_errors=0)
                conn.execute("UPDATE ats_tenants SET validated_at = COALESCE(validated_at, ?) WHERE ats = ? AND "
                             "tenant = ?", (now, spec.id, ten))
        summary["sources"] += 1
    _detail_stage(conn, client, cfg, prof, summary, tripped, source, start, max_seconds, clock)
    with db.tx(conn):
        from .. import evaluate
        evaluate.feasibility_check(conn, cfg)
        log_event(conn, "sources_fetch", **{k: v for k, v in summary.items() if k != "errors"},
                  errors=len(summary["errors"]))
    summary["ok_sources"] = succeeded
    return summary


def _tally(conn, summary: dict, results: list) -> None:
    for r in results:
        out = r["outcome"]
        if out == "new":
            summary["new"] += 1
            st = conn.execute("SELECT status FROM jobs WHERE job_uid = ?", (r["job_uid"],)).fetchone()
            if st and st[0] == "eval_queued":
                summary["eval_queued"] += 1
        elif out in ("duplicate", "alias_added"):
            summary["duplicates"] += 1
        elif out == "prefilter_rejected":
            summary["prefilter_rejected"] += 1
        elif out == "excluded":
            summary["excluded"] += 1


def _detail_stage(conn, client, cfg, prof, summary, tripped, source, start, max_seconds, clock) -> None:
    """Fetch the JD of survivors (and of any API job still missing one) for sources that need a detail call."""
    max_age = _cfg(cfg, "sources", "max_age_days", default=30)
    max_age = max_age if isinstance(max_age, int) and max_age > 0 else 30
    since = canon.ts_add(canon.now(), days=-max_age)
    budget = DETAIL_LIMIT
    for spec in SPECS:
        mod = module(spec)
        if not getattr(mod, "NEEDS_DETAIL", False) or spec.id in tripped:
            continue
        if source is not None and spec.id != source:
            continue
        if source is None and not enabled(spec, cfg):
            continue
        if _breaker_open(conn, "api:" + spec.id):
            continue
        rows = conn.execute(
            "SELECT j.id, j.job_uid, j.source_url, j.status FROM jobs j LEFT JOIN job_texts t ON t.job_id = j.id "
            "WHERE j.source = ? AND j.discovered_via = 'api' AND t.job_id IS NULL AND j.status IN ('new', "
            "'eval_queued', 'eligible') AND (j.human_call IS NULL OR j.human_call <> 'never') AND "
            "j.discovered_at >= ? ORDER BY j.discovered_at DESC, j.id DESC LIMIT ?",
            (spec.job_source, since, budget)).fetchall()
        for row in rows:
            if budget <= 0 or clock() - start > max_seconds:
                summary["stopped_early"] = summary["stopped_early"] or clock() - start > max_seconds
                return
            budget -= 1
            before = client.requests
            try:
                detail = mod.fetch_detail(client, dict(row))
            except NotFound:
                detail = {"jd_text": None, "can_apply": False}
            except RateLimited as exc:
                with db.tx(conn):
                    _count_requests(conn, spec.id, client.requests - before)
                    jobs.dep("breakers").trip(conn, "api:" + spec.id, "http_%d" % exc.status,
                                              "rate limited by %s (detail)" % spec.id)
                tripped.add(spec.id)
                summary["errors"].append({"source": spec.id, "error": "rate_limited"})
                break
            except (HttpError, ValueError, KeyError, TypeError, AttributeError) as exc:
                summary["errors"].append({"source": spec.id, "job_uid": row["job_uid"], "error": type(exc).__name__})
                continue
            if not detail:
                continue
            with db.tx(conn):
                _count_requests(conn, spec.id, client.requests - before)
                before_status = row["status"]
                status = jobs.attach_jd(conn, row["id"], detail.get("jd_text"), can_apply=detail.get("can_apply"),
                                        posted_at=detail.get("posted_at"), profile=prof, config=cfg)
            summary["jd_fetched"] += 1
            if before_status == "new":
                if status == "eval_queued":
                    summary["eval_queued"] += 1
                elif status == "prefilter_rejected":
                    summary["prefilter_rejected"] += 1


# ---------------------------------------------------------------- list and add-tenant
def list_sources(conn, cfg: dict | None = None, profile: dict | None = None) -> list[dict]:
    cfg = cfg if cfg is not None else jobs.load_config()
    now = canon.now()
    breakers = {r["scope"]: r["state"] for r in conn.execute("SELECT scope, state FROM breakers")}
    out = []
    for spec in SPECS:
        st = _state(conn, spec.id, "")
        rows = conn.execute("SELECT next_fetch_at, last_status FROM sources_state WHERE source = ?",
                            (spec.id,)).fetchall()
        tenants = len(_tenants(conn, spec.id)) if spec.kind == "ats" else None
        if spec.kind == "ats":
            due = any(_is_due(conn, spec.id, t, now) for t in _tenants(conn, spec.id))
        else:
            due = _is_due(conn, spec.id, "", now)
        last = max((r["last_status"] for r in rows if r["last_status"] is not None), default=None) if rows else None
        out.append({"id": spec.id, "lane": "api", "enabled": enabled(spec, cfg), "due": bool(due),
                    "last_status": (st["last_status"] if st is not None and st["last_status"] is not None else last),
                    "breaker": breakers.get("api:" + spec.id, "closed"), "tenants": tenants})
    from .. import searches
    prof = profile if profile is not None else jobs.load_profile()
    on = set(searches.enabled_sites(prof, cfg))
    for site in searches.BROWSER_SITES:
        scope = searches.site_scope(site)[0]
        out.append({"id": site, "lane": "browser", "enabled": site in on, "due": None, "last_status": None,
                    "breaker": breakers.get(scope, "closed"), "tenants": None})
    return out


def _is_due(conn, source: str, tenant: str, now: str) -> bool:
    st = _state(conn, source, tenant)
    return st is None or not st["next_fetch_at"] or st["next_fetch_at"] <= now


def add_tenant(conn, ats: str, tenant: str, company: str | None = None, *, client=None,
               config: dict | None = None) -> dict:
    """Validate an ATS tenant with one list request (404 or no jobs means a wrong token, E_VALIDATION) and add it
    to the polled tenants. Runs its own transaction after the request."""
    if ats not in ATS_IDS:
        raise Denied("E_USAGE", "ats must be one of %s" % ", ".join(ATS_IDS))
    mod = module(BY_ID[ats])
    tenant = (tenant or "").strip()
    if not mod.TENANT_RE.match(tenant):
        raise Denied("E_VALIDATION", "%r is not a valid %s tenant" % (tenant, ats))
    cfg = config if config is not None else jobs.load_config()
    client = client or make_client(cfg)
    try:
        items = mod.fetch_list(client, tenant, {"queries": build_ctx(jobs.load_profile(), cfg)["queries"]})
    except NotFound:
        raise Denied("E_VALIDATION", "%s has no job board named %r" % (ats, tenant))
    except RateLimited:
        raise Denied("E_NETWORK", "%s rate-limited the check; try again later" % ats)
    except (HttpError, ValueError) as exc:
        raise Denied("E_NETWORK", "could not check the tenant: %s" % str(exc)[:200])
    if not items:
        raise Denied("E_VALIDATION", "%s board %r lists no jobs; check the token" % (ats, tenant),
                     data={"validated": False, "jobs_seen": 0})
    name = company or (items[0].get("company") if items else None)
    with db.tx(conn):
        conn.execute("INSERT INTO ats_tenants (ats, tenant, company_id, active, validated_at, source) VALUES "
                     "(?, ?, NULL, 1, ?, 'human') ON CONFLICT (ats, tenant) DO UPDATE SET active = 1, "
                     "validated_at = excluded.validated_at", (ats, tenant, canon.now()))
        cid = _link_company(conn, ats, tenant, name)
        _save_state(conn, ats, tenant, next_fetch_at=None)
        log_event(conn, "ats_tenant_added", ats=ats, tenant=tenant, jobs_seen=len(items))
    return {"ats": ats, "tenant": tenant, "validated": True, "jobs_seen": len(items), "company_id": cid}
