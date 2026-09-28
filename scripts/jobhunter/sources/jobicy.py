"""Jobicy remote jobs API (research job-sources 1F; off by default).

List: GET https://jobicy.com/api/v2/remote-jobs?count=50[&geo={slug}]
"""
from __future__ import annotations

import re

from .http import base_item, clean, html_to_text, salary, to_date

SOURCE = "jobicy"
NEEDS_DETAIL = False
USES_VALIDATORS = False
API = "https://jobicy.com/api/v2/remote-jobs?count=50"


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    geo = [re.sub(r"[^a-z]+", "-", n.lower()).strip("-") for n in (ctx.get("country_names") or []) if n][:1]
    url = API + ("&geo=%s" % geo[0] if geo else "")
    return parse_list(client.get_json(url))


def parse_list(data) -> list[dict]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("jobicy: no jobs list")
    out = []
    for j in jobs:
        url = j.get("url") if isinstance(j, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        geo = clean(j.get("jobGeo"))
        types = " ".join(str(x) for x in (j.get("jobType") or [])).lower()
        etype = ("internship" if "intern" in types else "part_time" if "part" in types else
                 "contract" if "contract" in types else "full_time" if "full" in types else None)
        out.append(base_item(
            source_url=url,
            company=clean(j.get("companyName")),
            title=clean(j.get("jobTitle")),
            location="Remote" + (" (%s)" % geo if geo else ""),
            work_mode="remote",
            remote_scope=geo,
            employment_type=etype,
            posted_at=to_date(j.get("pubDate")),
            salary=salary(j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency"), "year"),
            jd_text=html_to_text(j.get("jobDescription")) or clean(j.get("jobExcerpt")),
        ))
    return out
