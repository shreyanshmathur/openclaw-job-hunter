"""BambooHR careers JSON (research job-sources 1A). List, then detail for survivors only.

List:   GET https://{sub}.bamboohr.com/careers/list   (an unknown subdomain redirects to bamboohr.com: NotFound)
Detail: GET https://{sub}.bamboohr.com/careers/{id}/detail
Job URL: https://{sub}.bamboohr.com/careers/{id} (key ats:bamboohr:{sub}:{id}).
"""
from __future__ import annotations

import re

from .http import base_item, clean, html_to_text, to_date

ATS = "bamboohr"
NEEDS_DETAIL = True
USES_VALIDATORS = True
TENANT_RE = re.compile(r"^[a-z0-9-]{1,63}$")
_URL_RE = re.compile(r"^https://([a-z0-9-]+)\.bamboohr\.com/careers/(\d+)")


def list_url(sub: str) -> str:
    return "https://%s.bamboohr.com/careers/list" % sub


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    data = client.get_json(list_url(tenant), etag=ctx.get("etag"), last_modified=ctx.get("last_modified"))
    return parse_list(data, tenant)


def parse_list(data, sub: str) -> list[dict]:
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("bamboohr: no result list")
    out = []
    for r in rows:
        if not isinstance(r, dict) or not str(r.get("id", "")).isdigit():
            continue
        loc = r.get("atsLocation") if isinstance(r.get("atsLocation"), dict) else (
            r.get("location") if isinstance(r.get("location"), dict) else {})
        place = ", ".join(x for x in (clean(loc.get("city")), clean(loc.get("state")), clean(loc.get("country")))
                          if x) or None
        lt = str(r.get("locationType") or "")
        mode = "remote" if (r.get("isRemote") is True or lt == "1") else ("hybrid" if lt == "2" else
                                                                         ("onsite" if lt == "0" else None))
        status = str(r.get("employmentStatusLabel") or "").lower()
        etype = ("internship" if "intern" in status else "part_time" if "part" in status else
                 "contract" if "contract" in status else "full_time" if "full" in status else None)
        out.append(base_item(
            source_url="https://%s.bamboohr.com/careers/%s" % (sub, r["id"]),
            company=sub,
            title=clean(r.get("jobOpeningName")),
            location=place,
            work_mode=mode,
            employment_type=etype,
            tenant={"ats": ATS, "tenant": sub},
        ))
    return out


def detail_url(job: dict) -> str | None:
    m = _URL_RE.match(job.get("source_url") or "")
    return "https://%s.bamboohr.com/careers/%s/detail" % (m.group(1), m.group(2)) if m else None


def fetch_detail(client, job: dict) -> dict | None:
    url = detail_url(job)
    return parse_detail(client.get_json(url)) if url else None


def parse_detail(data) -> dict:
    res = data.get("result") if isinstance(data, dict) else None
    op = res.get("jobOpening") if isinstance(res, dict) else None
    if not isinstance(op, dict):
        raise ValueError("bamboohr: bad detail")
    return {"jd_text": html_to_text(op.get("description")), "posted_at": to_date(op.get("datePosted"))}
