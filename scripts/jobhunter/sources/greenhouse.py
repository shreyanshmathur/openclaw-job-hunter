"""Greenhouse job board API (research job-sources 1A). List without content, detail for survivors only.

List:   GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
Detail: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}   (`content` is entity-escaped HTML)
Job URL: https://job-boards.greenhouse.io/{token}/jobs/{id} (key ats:greenhouse:{id}; ids are global).
"""
from __future__ import annotations

import re
import urllib.parse

from .http import base_item, clean, html_to_text, to_date

ATS = "greenhouse"
NEEDS_DETAIL = True
USES_VALIDATORS = True
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
API = "https://boards-api.greenhouse.io/v1/boards/%s/jobs"
_URL_RE = re.compile(r"greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)")


def list_url(token: str) -> str:
    return API % urllib.parse.quote(token, safe="")


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    data = client.get_json(list_url(tenant), etag=ctx.get("etag"), last_modified=ctx.get("last_modified"))
    return parse_list(data, tenant)


def parse_list(data, token: str) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("greenhouse: no jobs list")
    out = []
    for j in jobs:
        if not isinstance(j, dict) or not str(j.get("id", "")).isdigit():
            continue
        jid = str(j["id"])
        url = "https://job-boards.greenhouse.io/%s/jobs/%s" % (token, jid)
        absu = j.get("absolute_url")
        loc = j.get("location") if isinstance(j.get("location"), dict) else {}
        out.append(base_item(
            source_url=url,
            apply_url=absu if isinstance(absu, str) and absu.startswith("https://") and absu != url else None,
            company=clean(j.get("company_name")) or token,
            title=clean(j.get("title")),
            location=clean(loc.get("name")),
            posted_at=to_date(j.get("first_published") or j.get("updated_at")),
            jd_text=html_to_text(j.get("content"), unescape_first=True) or None,
            tenant={"ats": ATS, "tenant": token},
        ))
    return out


def detail_url(job: dict) -> str | None:
    m = _URL_RE.search(job.get("source_url") or "")
    if not m:
        return None
    return "%s/%s" % (list_url(m.group(1)), m.group(2))


def fetch_detail(client, job: dict) -> dict | None:
    url = detail_url(job)
    return parse_detail(client.get_json(url)) if url else None


def parse_detail(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("greenhouse: bad detail")
    return {"jd_text": html_to_text(data.get("content"), unescape_first=True),
            "posted_at": to_date(data.get("first_published") or data.get("updated_at"))}
