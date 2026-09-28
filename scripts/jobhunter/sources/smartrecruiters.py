"""SmartRecruiters posting API (research job-sources 1A). List pages, detail for survivors only.

List:   GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings?limit=100&offset={n}
Detail: GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings/{postingId}
Job URL: https://jobs.smartrecruiters.com/{companyId}/{postingId} (key ats:smartrecruiters:{postingId}).
"""
from __future__ import annotations

import re
import urllib.parse

from .http import base_item, clean, html_to_text, to_date

ATS = "smartrecruiters"
NEEDS_DETAIL = True
USES_VALIDATORS = False
TENANT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
API = "https://api.smartrecruiters.com/v1/companies/%s/postings"
PAGE = 100
MAX_PAGES = 2
_URL_RE = re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/([^/?#]+)")


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    out = []
    base = API % urllib.parse.quote(tenant, safe="")
    for page in range(MAX_PAGES):
        data = client.get_json("%s?limit=%d&offset=%d" % (base, PAGE, page * PAGE))
        items = parse_list(data, tenant)
        out.extend(items)
        total = data.get("totalFound") if isinstance(data, dict) else None
        if not isinstance(total, int) or (page + 1) * PAGE >= total or not items:
            break
    return out


def _etype(v) -> str | None:
    s = str(v or "").lower()
    for key, t in (("intern", "internship"), ("part", "part_time"), ("contract", "contract"),
                   ("temp", "temporary"), ("full", "full_time"), ("permanent", "full_time")):
        if key in s:
            return t
    return None


def parse_list(data, company: str) -> list[dict]:
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise ValueError("smartrecruiters: no content list")
    out = []
    for p in content:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        comp = p.get("company") if isinstance(p.get("company"), dict) else {}
        ident = comp.get("identifier") or company
        loc = p.get("location") if isinstance(p.get("location"), dict) else {}
        place = ", ".join(x for x in (clean(loc.get("city")), clean(loc.get("region")), clean(loc.get("country")))
                          if x) or None
        mode = "remote" if loc.get("remote") is True else ("hybrid" if loc.get("hybrid") is True else None)
        te = p.get("typeOfEmployment") if isinstance(p.get("typeOfEmployment"), dict) else {}
        out.append(base_item(
            source_url="https://jobs.smartrecruiters.com/%s/%s" % (ident, p["id"]),
            company=clean(comp.get("name")) or company,
            title=clean(p.get("name")),
            location=place,
            work_mode=mode,
            employment_type=_etype(te.get("label") or te.get("id")),
            posted_at=to_date(p.get("releasedDate")),
            tenant={"ats": ATS, "tenant": company},
        ))
    return out


def detail_url(job: dict) -> str | None:
    m = _URL_RE.search(job.get("source_url") or "")
    return "%s/%s" % (API % m.group(1), m.group(2)) if m else None


def fetch_detail(client, job: dict) -> dict | None:
    url = detail_url(job)
    return parse_detail(client.get_json(url)) if url else None


def parse_detail(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("smartrecruiters: bad detail")
    ad = data.get("jobAd") if isinstance(data.get("jobAd"), dict) else {}
    sections = ad.get("sections") if isinstance(ad.get("sections"), dict) else {}
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key)
        if isinstance(sec, dict):
            title = clean(sec.get("title"))
            body = html_to_text(sec.get("text"))
            if body:
                parts.append(((title + "\n") if title else "") + body)
    return {"jd_text": "\n\n".join(parts), "posted_at": to_date(data.get("releasedDate"))}
