"""Working Nomads exposed jobs API (research job-sources 1F).

List: GET https://www.workingnomads.com/api/exposed_jobs/
"""
from __future__ import annotations

from .http import base_item, clean, html_to_text, to_date

SOURCE = "workingnomads"
NEEDS_DETAIL = False
USES_VALIDATORS = True
API = "https://www.workingnomads.com/api/exposed_jobs/"


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    return parse_list(client.get_json(API, etag=ctx.get("etag"), last_modified=ctx.get("last_modified")))


def parse_list(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("workingnomads: expected a list")
    out = []
    for j in data:
        url = j.get("url") if isinstance(j, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        loc = clean(j.get("location"))
        out.append(base_item(
            source_url=url,
            company=clean(j.get("company_name")),
            title=clean(j.get("title")),
            location="Remote" + (" (%s)" % loc if loc else ""),
            work_mode="remote",
            remote_scope=loc,
            posted_at=to_date(j.get("pub_date")),
            jd_text=html_to_text(j.get("description")) or None,
        ))
    return out
