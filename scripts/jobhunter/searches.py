"""Browser searches for the scout (design 1.3.2, 3.4 `searches generate` and `searches due`, 12.10).

    generate(profile, config) -> [search]        # pure: URLs are built by code from the confirmed profile
    due(conn) -> [search]                         # read only: what the next scout cycle should open
    due_count(conn) -> int                        # the dispatcher's "scout has work" test
    mark_handed_out(conn, items, cycle_id)        # `searches due` from the scout records the hand-out

Searches live in `private/searches.json` (written by `searches generate`, editable by the person: set
"enabled": false to drop one). Hand-out state lives in `sources_state` rows with source = 'search' and
tenant = search_id: `last_fetch_at` is the hand-out time, `next_fetch_at` when it is due again, `etag` the cycle
that took it (so a repeated `searches due` in the same cycle returns the same list).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse

from . import canon, paths, prefilter
from .errors import Denied
from .jobs import dep, load_config, load_profile

FILE_VERSION = 1
BROWSER_SITES = ("linkedin_jobs", "linkedin_posts", "naukri", "instahyre", "foundit", "cutshort", "hirist",
                 "iimjobs", "wellfound", "indeed", "glassdoor")
DEFAULT_EVERY_HOURS = 24
MAX_PER_SITE = 3
MAX_TOTAL = 12
MAX_QUERIES = 4
MAX_PLACES = 3
MAX_PAGES_PER_SEARCH = 3
DEFAULT_PAGES_DAY = 10
REPEAT_WINDOW_HOURS = 3
STATE_SOURCE = "search"
COUNTRY_NAMES = {"IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
                 "SG": "Singapore", "DE": "Germany", "NL": "Netherlands", "AE": "United Arab Emirates"}
INDEED_HOSTS = {"IN": "in.indeed.com", "GB": "uk.indeed.com", "CA": "ca.indeed.com", "AU": "au.indeed.com",
                "SG": "sg.indeed.com", "DE": "de.indeed.com"}
GLASSDOOR_HOSTS = {"IN": "www.glassdoor.co.in", "GB": "www.glassdoor.co.uk", "CA": "www.glassdoor.ca",
                   "AU": "www.glassdoor.com.au", "SG": "www.glassdoor.sg", "DE": "www.glassdoor.de"}


def searches_file() -> str:
    return os.path.join(paths.private_dir(), "searches.json")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _q(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def site_scope(site: str) -> list[str]:
    """Breaker scopes that stop a site's searches (4.5)."""
    if site == "linkedin_jobs":
        return ["linkedin", "linkedin.search"]
    if site == "linkedin_posts":
        return ["linkedin", "linkedin.search"]
    return ["site:%s" % site]


def build_url(site: str, query: str, place: str | None, remote: bool, country: str | None) -> str | None:
    """Search URL for one site (read-only result pages, newest first where the site supports it)."""
    cname = COUNTRY_NAMES.get(country or "", "")
    if site == "linkedin_jobs":
        loc = place or cname or "Worldwide"
        url = "https://www.linkedin.com/jobs/search/?keywords=%s&location=%s&f_TPR=r86400&sortBy=DD" % (
            _q(query), _q(loc))
        return url + ("&f_WT=2" if remote else "")
    if site == "linkedin_posts":
        words = "hiring %s %s" % (query, "remote" if remote else (place or ""))
        return ("https://www.linkedin.com/search/results/content/?keywords=%s&datePosted=%%22past-week%%22"
                "&sortBy=%%22date_posted%%22" % _q(" ".join(words.split())))
    if site == "naukri":
        if remote:
            return "https://www.naukri.com/%s-jobs?wfhType=2&jobAge=3" % _slug(query)
        return "https://www.naukri.com/%s-jobs-in-%s?jobAge=3" % (_slug(query), _slug(place or cname or "india"))
    if site == "instahyre":
        return "https://www.instahyre.com/%s-jobs-in-%s/" % (_slug(query), _slug("remote" if remote else
                                                                                 (place or "india")))
    if site == "foundit":
        return "https://www.foundit.in/srp/results?query=%s&locations=%s" % (
            _q(query), _q("Remote" if remote else (place or "India")))
    if site == "cutshort":
        return "https://cutshort.io/jobs/%s-jobs-in-%s" % (_slug(query), _slug("remote" if remote else
                                                                            (place or "india")))
    if site == "hirist":
        return "https://www.hirist.tech/search/%s?loc=%s" % (_slug(query), _q("Remote" if remote else (place or "")))
    if site == "iimjobs":
        return "https://www.iimjobs.com/k/%s-jobs-in-%s" % (_slug(query), _slug("remote" if remote else
                                                                             (place or "india")))
    if site == "wellfound":
        if remote:
            return "https://wellfound.com/role/r/%s" % _slug(query)
        return "https://wellfound.com/role/l/%s/%s" % (_slug(query), _slug(place or cname or "india"))
    if site == "indeed":
        host = INDEED_HOSTS.get(country or "", "www.indeed.com")
        return "https://%s/jobs?q=%s&l=%s&fromage=3&sort=date" % (host, _q(query), _q("Remote" if remote else
                                                                                      (place or "")))
    if site == "glassdoor":
        host = GLASSDOOR_HOSTS.get(country or "", "www.glassdoor.com")
        return "https://%s/Job/jobs.htm?sc.keyword=%s&locKeyword=%s&fromAge=3" % (
            host, _q(query), _q("Remote" if remote else (place or "")))
    return None


def enabled_sites(profile: dict | None, config: dict | None) -> list[str]:
    """Browser discovery sites that are switched on: `boards.sites.<site>.discover = "browser"` in the config, or
    the site listed in the confirmed profile's `sites_enabled` (unless the config says `api`). LinkedIn sites also
    need LinkedIn enabled in the profile or the config (the database switch is checked again in `due`)."""
    vals = prefilter.profile_values(profile)
    listed = {str(s) for s in (vals.get("sites_enabled") or []) if isinstance(s, str)}
    li = vals.get("linkedin")
    li_on = bool(li.get("enabled")) if isinstance(li, dict) else bool(li)
    li_on = li_on or bool(prefilter._cfg(config, "channels", "linkedin", "enabled", default=False))
    out = []
    for site in BROWSER_SITES:
        mode = prefilter._cfg(config, "boards", "sites", site, "discover", default="off")
        if mode == "api":
            continue
        if mode != "browser" and site not in listed:
            continue
        if site.startswith("linkedin_") and not li_on:
            continue
        out.append(site)
    return out


def _queries(vals: dict) -> list[str]:
    out = []
    for fam in vals.get("role_families") or []:
        if not isinstance(fam, dict):
            continue
        titles = [t for t in (fam.get("titles") or []) if isinstance(t, str) and t.strip()][:2]
        if not titles and isinstance(fam.get("name"), str):
            titles = [fam["name"]]
        for t in titles:
            t = " ".join(t.split())
            if t.lower() not in [x.lower() for x in out]:
                out.append(t)
    return out[:MAX_QUERIES]


def _places(vals: dict) -> tuple[list[str], bool]:
    loc = vals.get("locations") if isinstance(vals.get("locations"), dict) else {}
    cities = [c for c in (loc.get("cities") or []) if isinstance(c, str) and c.strip()]
    modes = [str(m).lower() for m in (loc.get("work_modes") or [])]
    remote = "remote" in modes or any(c.lower() == "remote" for c in cities)
    places = [c.strip() for c in cities if c.lower() != "remote"]
    return places[:MAX_PLACES], remote


def search_id(site: str, query: str, place: str | None, remote: bool) -> str:
    h = hashlib.sha1(("%s|%s|%s|%d" % (site, query.lower(), (place or "").lower(), remote)).encode("utf-8"))
    return "%s-%s" % (site, h.hexdigest()[:8])


def generate(profile: dict, config: dict) -> list[dict]:
    """Searches for every enabled browser site: each role title of the confirmed role families (at most 4) in each
    confirmed city (at most 3) and, when remote work is wanted, a remote variant."""
    vals = prefilter.profile_values(profile)
    queries = _queries(vals)
    places, remote = _places(vals)
    country = None
    mine = sorted(prefilter.person_countries(vals))
    if mine:
        country = mine[0]
    combos = [(p, False) for p in places] + ([(None, True)] if remote else [])
    if not combos:
        combos = [(None, False)]
    out = []
    for site in enabled_sites(profile, config):
        for query in queries:
            for place, rem in combos:
                url = build_url(site, query, place, rem, country)
                if not url:
                    continue
                out.append({"search_id": search_id(site, query, place, rem), "site": site, "query": query,
                            "location": "remote" if rem else place, "remote": rem, "url": url, "recipe": site,
                            "every_hours": DEFAULT_EVERY_HOURS, "enabled": True})
    return out


def load_file() -> list[dict]:
    path = searches_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "private/searches.json is unreadable: %s" % exc)
    items = data.get("searches") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise Denied("E_CONFIG_INVALID", "private/searches.json has no searches list")
    out = []
    for s in items:
        if (isinstance(s, dict) and isinstance(s.get("search_id"), str) and s.get("site") in BROWSER_SITES
                and isinstance(s.get("url"), str) and s["url"].startswith("https://")):
            out.append(s)
    return out


def write_file(searches: list[dict], profile_version: str = "", overwrite: bool = False) -> dict:
    """Write private/searches.json. Without overwrite the person's existing entries (and their enabled flags) are
    kept and only new searches are added."""
    existing = [] if overwrite else load_file()
    have = {s["search_id"] for s in existing}
    merged = list(existing) + [s for s in searches if s["search_id"] not in have]
    data = {"version": FILE_VERSION, "generated_at": canon.now(), "profile_version": profile_version,
            "searches": merged}
    path = searches_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"path": path, "searches": len(merged), "added": len(merged) - len(existing)}


def _blocked(conn, site: str) -> bool:
    try:
        dep("breakers").check_breakers(conn, site_scope(site))
    except Denied:
        return True
    return False


def due(conn, cycle_id: str | None = None, *, profile: dict | None = None, config: dict | None = None) -> list[dict]:
    """Searches the next scout cycle should open, with a page budget each (read only). Nothing is due while the
    install is paused or the global breaker is open; a site whose breaker is open is skipped. At most 3 searches
    per site and 12 in total; searches handed to `cycle_id` within the last 3 hours are returned again."""
    try:
        dep("breakers").check_breakers(conn, ["global"])
    except Denied:
        return []
    if os.path.exists(paths.paused_file()):
        return []
    items = [s for s in load_file() if s.get("enabled", True) is not False]
    if not items:
        return []
    cfg = config if config is not None else load_config()
    prof = profile if profile is not None else load_profile()
    sites_ok = set(enabled_sites(prof, cfg))
    li_row = conn.execute("SELECT value FROM meta WHERE key = 'channel_linkedin_enabled'").fetchone()
    li_on = bool(li_row and li_row[0] == "1")
    now = canon.now()
    state = {r["tenant"]: r for r in conn.execute(
        "SELECT tenant, last_fetch_at, next_fetch_at, etag FROM sources_state WHERE source = ?", (STATE_SOURCE,))}
    blocked: dict = {}
    per_site: dict = {}
    for s in items:
        site = s["site"]
        if site not in sites_ok or (site.startswith("linkedin_") and not li_on):
            continue
        if site not in blocked:
            blocked[site] = _blocked(conn, site)
        if blocked[site]:
            continue
        st = state.get(s["search_id"])
        repeat = bool(cycle_id and st is not None and st["etag"] == cycle_id and st["last_fetch_at"]
                      and st["last_fetch_at"] >= canon.ts_add(now, hours=-REPEAT_WINDOW_HOURS))
        if not repeat and st is not None and st["next_fetch_at"] and st["next_fetch_at"] > now:
            continue
        per_site.setdefault(site, []).append((0 if repeat else 1, (st["last_fetch_at"] if st else "") or "", s))
    cycles_max = prefilter._cfg(cfg, "dispatch", "lanes", "scout", "cycles_per_day", default=[2, 3])
    try:
        cycles_max = max(1, int(cycles_max[-1]))
    except (TypeError, ValueError, IndexError):
        cycles_max = 3
    out = []
    for site in BROWSER_SITES:
        cands = sorted(per_site.get(site, []), key=lambda x: (x[0], x[1], x[2]["search_id"]))
        if not cands:
            continue
        if site == "linkedin_posts":
            pages_day = _content_search_day(cfg)
        else:
            pages_day = prefilter._cfg(cfg, "boards", "sites", site, "pages_day", default=DEFAULT_PAGES_DAY)
            pages_day = pages_day if isinstance(pages_day, int) and pages_day > 0 else DEFAULT_PAGES_DAY
        per_cycle = max(1, pages_day // cycles_max)
        take = cands[:min(MAX_PER_SITE, per_cycle)]
        budget = max(1, min(MAX_PAGES_PER_SEARCH, per_cycle // len(take)))
        for _, _, s in take:
            out.append({"search_id": s["search_id"], "site": site, "url": s["url"], "page_budget": budget,
                        "recipe": s.get("recipe") or site, "query": s.get("query"), "location": s.get("location"),
                        "every_hours": s.get("every_hours") or DEFAULT_EVERY_HOURS})
    return out[:MAX_TOTAL]


def _content_search_day(cfg: dict) -> int:
    tier = "conservative"
    v = prefilter._cfg(cfg, "linkedin", "ceilings", tier, "content_search", "day", default=3)
    return v if isinstance(v, int) and v > 0 else 3


def due_count(conn) -> int:
    """The dispatcher's test for scout work. Any refusal (config, profile, breakers) counts as no work."""
    try:
        return len(due(conn))
    except Denied:
        return 0


def mark_handed_out(conn, items: list[dict], cycle_id: str | None) -> None:
    """Record that these searches went to a scout cycle (inside the caller's transaction)."""
    now = canon.now()
    for s in items:
        hours = s.get("every_hours") or DEFAULT_EVERY_HOURS
        try:
            hours = max(6, min(int(hours), 24 * 7))
        except (TypeError, ValueError):
            hours = DEFAULT_EVERY_HOURS
        conn.execute(
            "INSERT INTO sources_state (source, tenant, last_fetch_at, next_fetch_at, etag, consecutive_errors) "
            "VALUES (?, ?, ?, ?, ?, 0) ON CONFLICT (source, tenant) DO UPDATE SET "
            "last_fetch_at = CASE WHEN sources_state.etag IS ? AND ? IS NOT NULL THEN sources_state.last_fetch_at "
            "ELSE excluded.last_fetch_at END, next_fetch_at = excluded.next_fetch_at, etag = excluded.etag",
            (STATE_SOURCE, s["search_id"], now, canon.ts_add(now, hours=hours), cycle_id, cycle_id, cycle_id))
