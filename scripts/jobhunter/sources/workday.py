"""Workday CXS job search (research job-sources 1A). Read only; applying needs an account, so every Workday job
is a human-apply job. Tenants are written "{tenant}.wd{N}/{site}".

List:   POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        body {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "<role title>"}
Detail: GET  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}
Job URL: https://{tenant}.wd{N}.myworkdayjobs.com/{site}{externalPath} (key ats:workday:{tenant}:{REQID}).
"""
from __future__ import annotations

import re

from .http import base_item, clean, html_to_text, to_date, work_mode_of

ATS = "workday"
NEEDS_DETAIL = True
USES_VALIDATORS = False
TENANT_RE = re.compile(r"^([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_-]+)$")
MAX_QUERIES = 4
PAGE = 20
_URL_RE = re.compile(r"^https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)(/job/[^?#]+)")


def split_tenant(tenant: str) -> tuple[str, str, str]:
    m = TENANT_RE.match(tenant or "")
    if not m:
        raise ValueError("workday tenant must look like kestrel.wd5/External")
    return m.group(1), m.group(2), m.group(3)


def list_url(tenant: str) -> str:
    t, wd, site = split_tenant(tenant)
    return "https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s/jobs" % (t, wd, t, site)


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    queries = [q for q in (ctx.get("queries") or []) if q][:MAX_QUERIES] or [""]
    seen, out = set(), []
    for q in queries:
        data = client.post_json(list_url(tenant), {"appliedFacets": {}, "limit": PAGE, "offset": 0, "searchText": q})
        for it in parse_list(data, tenant):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    return out


def parse_list(data, tenant: str) -> list[dict]:
    t, wd, site = split_tenant(tenant)
    posts = data.get("jobPostings") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        raise ValueError("workday: no jobPostings list")
    out = []
    for p in posts:
        path = p.get("externalPath") if isinstance(p, dict) else None
        if not isinstance(path, str) or not path.startswith("/job/"):
            continue
        out.append(base_item(
            source_url="https://%s.%s.myworkdayjobs.com/%s%s" % (t, wd, site, path),
            company=t,
            title=clean(p.get("title")),
            location=clean(p.get("locationsText")),
            posted_at=to_date(p.get("postedOn")),
            apply_route_hint="human",
            tenant={"ats": ATS, "tenant": tenant},
        ))
    return out


def detail_url(job: dict) -> str | None:
    m = _URL_RE.match(job.get("source_url") or "")
    if not m:
        return None
    return "https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s%s" % (m.group(1), m.group(2), m.group(1), m.group(3),
                                                                 m.group(4))


def fetch_detail(client, job: dict) -> dict | None:
    url = detail_url(job)
    return parse_detail(client.get_json(url)) if url else None


def parse_detail(data) -> dict:
    info = data.get("jobPostingInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise ValueError("workday: bad detail")
    can = info.get("canApply")
    return {"jd_text": html_to_text(info.get("jobDescription")),
            "posted_at": to_date(info.get("startDate") or info.get("postedOn")),
            "can_apply": can if isinstance(can, bool) else None,
            "work_mode": work_mode_of(info.get("remoteType"))}
