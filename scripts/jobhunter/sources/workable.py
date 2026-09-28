"""Workable widget API (research job-sources 1A). With details=true the list carries the description.

List: GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
Job URL: https://apply.workable.com/{account}/j/{shortcode}/ (key ats:workable:{account}:{shortcode}).
"""
from __future__ import annotations

import re
import urllib.parse

from .http import base_item, clean, html_to_text, to_date

ATS = "workable"
NEEDS_DETAIL = False
USES_VALIDATORS = True
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def list_url(account: str) -> str:
    return "https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % urllib.parse.quote(account, safe="")


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    data = client.get_json(list_url(tenant), etag=ctx.get("etag"), last_modified=ctx.get("last_modified"))
    return parse_list(data, tenant)


def _etype(v) -> str | None:
    s = str(v or "").lower()
    for key, t in (("intern", "internship"), ("part", "part_time"), ("contract", "contract"),
                   ("temp", "temporary"), ("full", "full_time")):
        if key in s:
            return t
    return None


def parse_list(data, account: str) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("workable: no jobs list")
    company = clean(data.get("name")) or account
    out = []
    for j in jobs:
        if not isinstance(j, dict) or not j.get("shortcode"):
            continue
        code = str(j["shortcode"])
        locs = j.get("locations") if isinstance(j.get("locations"), list) else []
        places = [", ".join(x for x in (clean(l.get("city")), clean(l.get("region")), clean(l.get("country")))
                            if x) for l in locs if isinstance(l, dict)]
        if not places:
            places = [", ".join(x for x in (clean(j.get("city")), clean(j.get("state")), clean(j.get("country")))
                                if x)]
        out.append(base_item(
            source_url="https://apply.workable.com/%s/j/%s/" % (account, code),
            company=company,
            title=clean(j.get("title")),
            location=" / ".join(p for p in places if p) or None,
            work_mode="remote" if j.get("telecommuting") is True else None,
            employment_type=_etype(j.get("employment_type")),
            posted_at=to_date(j.get("published_on") or j.get("created_at")),
            jd_text=html_to_text(j.get("description")) or None,
            tenant={"ats": ATS, "tenant": account},
        ))
    return out
