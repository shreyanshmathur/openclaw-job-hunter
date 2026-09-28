"""Himalayas remote jobs API (research job-sources 1F). Credit and link back to Himalayas.

Search: GET https://himalayas.app/jobs/api/search?country={Country}&sort=recent
Browse: GET https://himalayas.app/jobs/api?limit=20
Keys: board:himalayas:{job slug}; the application link (often an ATS) is kept as an alias.
"""
from __future__ import annotations

import urllib.parse

from .http import base_item, clean, html_to_text, last_segment, salary, to_date

SOURCE = "himalayas"
NEEDS_DETAIL = False
USES_VALIDATORS = False
SEARCH = "https://himalayas.app/jobs/api/search?country=%s&sort=recent"
BROWSE = "https://himalayas.app/jobs/api?limit=20"


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    names = [n for n in (ctx.get("country_names") or []) if n][:2]
    urls = [SEARCH % urllib.parse.quote(n) for n in names] or [BROWSE]
    out, seen = [], set()
    for url in urls:
        for it in parse_list(client.get_json(url)):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    return out


def _etype(v) -> str | None:
    s = str(v or "").lower()
    for key, t in (("intern", "internship"), ("part", "part_time"), ("contract", "contract"),
                   ("full", "full_time")):
        if key in s:
            return t
    return None


def parse_list(data) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("himalayas: no jobs list")
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        guid = j.get("guid") if isinstance(j.get("guid"), str) else None
        if not guid or not guid.startswith("https://"):
            continue
        slug = last_segment(guid)
        restr = [clean(x) for x in (j.get("locationRestrictions") or []) if clean(x)]
        app = j.get("applicationLink")
        out.append(base_item(
            source_url=guid,
            apply_url=app if isinstance(app, str) and app.startswith("https://") else None,
            company=clean(j.get("companyName")),
            title=clean(j.get("title")),
            location="Remote" + (" (%s)" % ", ".join(restr) if restr else ""),
            work_mode="remote",
            remote_scope=", ".join(restr) if restr else "Worldwide",
            employment_type=_etype(j.get("employmentType")),
            posted_at=to_date(j.get("pubDate")),
            salary=salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), "year"),
            jd_text=html_to_text(j.get("description")) or clean(j.get("excerpt")),
            native_ids={"board_job_id": slug} if slug else {},
        ))
    return out
