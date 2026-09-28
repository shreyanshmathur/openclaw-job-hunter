"""Lever postings API (research job-sources 1A). The list carries the full description.

List: GET https://api.lever.co/v0/postings/{site}?mode=json (EU: api.eu.lever.co; tenant written "eu:{site}")
Job URL: https://jobs.lever.co/{site}/{id} (key ats:lever:{id}).
"""
from __future__ import annotations

import re
import urllib.parse

from .http import base_item, clean, html_to_text, salary, to_date, work_mode_of

ATS = "lever"
NEEDS_DETAIL = False
USES_VALIDATORS = True
TENANT_RE = re.compile(r"^(eu:)?[A-Za-z0-9_.-]{1,80}$")


def split_tenant(tenant: str) -> tuple[str, bool]:
    return (tenant[3:], True) if tenant.startswith("eu:") else (tenant, False)


def list_url(tenant: str) -> str:
    site, eu = split_tenant(tenant)
    return "https://api.%slever.co/v0/postings/%s?mode=json" % ("eu." if eu else "", urllib.parse.quote(site, safe=""))


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    data = client.get_json(list_url(tenant), etag=ctx.get("etag"), last_modified=ctx.get("last_modified"))
    return parse_list(data, tenant)


def _etype(v) -> str | None:
    s = str(v or "").lower()
    if "intern" in s:
        return "internship"
    if "part" in s:
        return "part_time"
    if "contract" in s or "freelance" in s:
        return "contract"
    if "temp" in s:
        return "temporary"
    if "full" in s or "permanent" in s:
        return "full_time"
    return None


def parse_list(data, tenant: str) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("lever: expected a list of postings")
    site, eu = split_tenant(tenant)
    out = []
    for p in data:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        pid = str(p["id"])
        cats = p.get("categories") if isinstance(p.get("categories"), dict) else {}
        loc = clean(cats.get("location")) or clean(", ".join(x for x in (cats.get("allLocations") or [])
                                                               if isinstance(x, str)))
        hosted = p.get("hostedUrl") if isinstance(p.get("hostedUrl"), str) else None
        if not hosted or not hosted.startswith("https://"):
            hosted = "https://jobs.%slever.co/%s/%s" % ("eu." if eu else "", site, pid)
        parts = [p.get("descriptionPlain") or html_to_text(p.get("description"))]
        for lst in p.get("lists") or []:
            if isinstance(lst, dict):
                parts.append((clean(lst.get("text")) or "") + "\n" + html_to_text(lst.get("content")))
        parts.append(p.get("additionalPlain") or html_to_text(p.get("additional")))
        jd = "\n\n".join(x.strip() for x in parts if isinstance(x, str) and x.strip())
        sr = p.get("salaryRange") if isinstance(p.get("salaryRange"), dict) else {}
        out.append(base_item(
            source_url=hosted,
            apply_url=p.get("applyUrl") if isinstance(p.get("applyUrl"), str) and p["applyUrl"].startswith(
                "https://") else None,
            company=site,
            title=clean(p.get("text")),
            location=loc,
            work_mode=work_mode_of(p.get("workplaceType")),
            employment_type=_etype(cats.get("commitment")),
            posted_at=to_date(p.get("createdAt")),
            salary=salary(sr.get("min"), sr.get("max"), sr.get("currency"), sr.get("interval")) if sr else None,
            jd_text=jd or None,
            tenant={"ats": ATS, "tenant": tenant},
        ))
    return out
