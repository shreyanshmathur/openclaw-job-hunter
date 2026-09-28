"""RemoteOK API (research job-sources 1F). Element 0 of the array is the legal notice; credit Remote OK.

List: GET https://remoteok.com/api
Keys: board:remoteok:{id}; the apply link is kept as an alias.
"""
from __future__ import annotations

import re

from .http import base_item, clean, html_to_text, salary, to_date

SOURCE = "remoteok"
NEEDS_DETAIL = False
USES_VALIDATORS = True
API = "https://remoteok.com/api"


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    return parse_list(client.get_json(API, etag=ctx.get("etag"), last_modified=ctx.get("last_modified")))


def parse_list(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("remoteok: expected a list")
    out = []
    for j in data:
        if not isinstance(j, dict) or "legal" in j or not str(j.get("id", "")).isdigit():
            continue
        url = j.get("url") if isinstance(j.get("url"), str) else ""
        if not url.startswith("https://"):
            continue
        url = re.sub(r"^https://(?:www\.)?remoteok\.com", "https://remoteok.com", url, flags=re.I)
        app = j.get("apply_url")
        out.append(base_item(
            source_url=url,
            apply_url=app if isinstance(app, str) and app.startswith("https://") else None,
            company=clean(j.get("company")),
            title=clean(j.get("position")),
            location=clean(j.get("location")) or "Remote",
            work_mode="remote",
            remote_scope=clean(j.get("location")),
            posted_at=to_date(j.get("date") or j.get("epoch")),
            salary=salary(j.get("salary_min") or None, j.get("salary_max") or None, "USD", "year"),
            jd_text=html_to_text(j.get("description")) or None,
            native_ids={"board_job_id": str(j["id"])},
        ))
    return out
