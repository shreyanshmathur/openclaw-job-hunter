"""Ashby posting API (research job-sources 1A). The list carries the description and compensation.

List: GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
Job URL: https://jobs.ashbyhq.com/{org}/{id} (key ats:ashby:{id}).
"""
from __future__ import annotations

import re
import urllib.parse

from .http import base_item, clean, html_to_text, salary, to_date, work_mode_of

ATS = "ashby"
NEEDS_DETAIL = False
USES_VALIDATORS = True
TENANT_RE = re.compile(r"^[A-Za-z0-9_.%-]{1,80}$")
_ETYPE = {"fulltime": "full_time", "parttime": "part_time", "intern": "internship", "contract": "contract",
          "temporary": "temporary"}


def list_url(org: str) -> str:
    return "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % urllib.parse.quote(org,
                                                                                                           safe="")


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    data = client.get_json(list_url(tenant), etag=ctx.get("etag"), last_modified=ctx.get("last_modified"))
    return parse_list(data, tenant)


def _salary(comp) -> dict | None:
    if not isinstance(comp, dict):
        return None
    for c in comp.get("summaryComponents") or []:
        if isinstance(c, dict) and str(c.get("compensationType", "")).lower() == "salary":
            return salary(c.get("minValue"), c.get("maxValue"), c.get("currencyCode"), c.get("interval"))
    return None


def parse_list(data, org: str) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("ashby: no jobs list")
    out = []
    for j in jobs:
        if not isinstance(j, dict) or not j.get("id") or j.get("isListed") is False:
            continue
        jid = str(j["id"])
        url = j.get("jobUrl") if isinstance(j.get("jobUrl"), str) and j["jobUrl"].startswith("https://") else \
            "https://jobs.ashbyhq.com/%s/%s" % (org, jid)
        locs = [clean(j.get("location"))] + [clean(x.get("location")) for x in (j.get("secondaryLocations") or [])
                                             if isinstance(x, dict)]
        mode = work_mode_of(j.get("workplaceType")) or ("remote" if j.get("isRemote") else None)
        out.append(base_item(
            source_url=url,
            apply_url=j.get("applyUrl") if isinstance(j.get("applyUrl"), str) and j["applyUrl"].startswith(
                "https://") else None,
            company=clean(j.get("organizationName")) or org,
            title=clean(j.get("title")),
            location=" / ".join(x for x in locs if x) or None,
            work_mode=mode,
            employment_type=_ETYPE.get(re.sub(r"[^a-z]", "", str(j.get("employmentType") or "").lower())),
            posted_at=to_date(j.get("publishedAt")),
            salary=_salary(j.get("compensation")),
            jd_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")) or None,
            tenant={"ats": ATS, "tenant": org},
        ))
    return out
