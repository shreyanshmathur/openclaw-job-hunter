"""Google Jobs through SerpApi (research job-sources 1G; optional, off by default, the person's own key in
private/secrets.json as "serpapi_key"). Used to find postings and ATS tenants, never to apply.

Search: GET https://serpapi.com/search.json?engine=google_jobs&q={query}&location={place}&api_key={key}
The posting's first https apply link (an ATS link when there is one) is the source URL.
"""
from __future__ import annotations

import re
import urllib.parse

from .http import base_item, clean, to_date

SOURCE = "serpapi"
NEEDS_DETAIL = False
USES_VALIDATORS = False
API = "https://serpapi.com/search.json?engine=google_jobs&q=%s&location=%s&api_key=%s"
MAX_QUERIES = 2
_ATS_HOSTS = re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|"
                        r"workable\.com|recruitee\.com|bamboohr\.com)", re.I)


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    key = (ctx.get("secrets") or {}).get("serpapi_key")
    if not key:
        return []
    place = (ctx.get("places") or ctx.get("country_names") or [""])[0] or ""
    out, seen = [], set()
    for q in (ctx.get("queries") or [])[:MAX_QUERIES]:
        url = API % (urllib.parse.quote(q), urllib.parse.quote(place), urllib.parse.quote(key))
        for it in parse_list(client.get_json(url)):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    return out


def parse_list(data) -> list[dict]:
    res = data.get("jobs_results") if isinstance(data, dict) else None
    if res is None:
        return []
    if not isinstance(res, list):
        raise ValueError("serpapi: jobs_results is not a list")
    out = []
    for j in res:
        if not isinstance(j, dict):
            continue
        links = [o.get("link") for o in (j.get("apply_options") or []) if isinstance(o, dict)
                 and isinstance(o.get("link"), str) and o["link"].startswith("https://")]
        if not links:
            continue
        links.sort(key=lambda u: 0 if _ATS_HOSTS.search(u) else 1)
        ext = j.get("detected_extensions") if isinstance(j.get("detected_extensions"), dict) else {}
        sched = str(ext.get("schedule_type") or "").lower()
        etype = ("internship" if "intern" in sched else "part_time" if "part" in sched else
                 "contract" if "contract" in sched else "full_time" if "full" in sched else None)
        out.append(base_item(
            source_url=links[0],
            redirect_urls=links[1:4],
            company=clean(j.get("company_name")),
            title=clean(j.get("title")),
            location=clean(j.get("location")),
            work_mode="remote" if ext.get("work_from_home") is True else None,
            employment_type=etype,
            posted_at=to_date(ext.get("posted_at")),
            jd_text=j.get("description") if isinstance(j.get("description"), str) else None,
        ))
    return out
