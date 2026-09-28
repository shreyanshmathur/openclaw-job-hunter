"""Remotive public API (research job-sources 1F). At most sources.remotive_max_calls_day (4) calls a day; the
runner counts them. The public feed is delayed by 24 hours.

List: GET https://remotive.com/api/remote-jobs
"""
from __future__ import annotations

from .http import base_item, clean, html_to_text, to_date

SOURCE = "remotive"
NEEDS_DETAIL = False
USES_VALIDATORS = True
API = "https://remotive.com/api/remote-jobs"


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    return parse_list(client.get_json(API, etag=ctx.get("etag"), last_modified=ctx.get("last_modified")))


def parse_list(data) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("remotive: no jobs list")
    out = []
    for j in jobs:
        url = j.get("url") if isinstance(j, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        scope = clean(j.get("candidate_required_location"))
        jt = str(j.get("job_type") or "").lower()
        etype = ("internship" if "intern" in jt else "part_time" if "part" in jt else
                 "contract" if ("contract" in jt or "freelance" in jt) else "full_time" if "full" in jt else None)
        out.append(base_item(
            source_url=url,
            company=clean(j.get("company_name")),
            title=clean(j.get("title")),
            location="Remote" + (" (%s)" % scope if scope else ""),
            work_mode="remote",
            remote_scope=scope,
            employment_type=etype,
            posted_at=to_date(j.get("publication_date")),
            jd_text=html_to_text(j.get("description")) or None,
        ))
    return out
