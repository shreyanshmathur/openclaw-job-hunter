"""YC jobs pages (research job-sources 1E): the listings are JSON inside the page's `data-page` attribute.

Pages: GET https://www.ycombinator.com/jobs/location/{slug} for the person's countries (and remote).
Job URL: https://www.workatastartup.com/jobs/{id} (key board:yc:{id}). Applying is a message to the founder on
Work at a Startup, so the apply route is the board's own flow.
"""
from __future__ import annotations

import html
import json
import re

from .http import base_item, clean, html_to_text

SOURCE = "yc"
NEEDS_DETAIL = False
USES_VALIDATORS = False
PAGE = "https://www.ycombinator.com/jobs/location/%s"
SLUGS = {"IN": "india", "US": "united-states", "GB": "united-kingdom", "CA": "canada", "SG": "singapore",
         "DE": "germany", "AU": "australia"}
_ATTR_RE = re.compile(r'data-page="([^"]*)"')
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs)")


def page_urls(ctx: dict) -> list[str]:
    slugs = [SLUGS[c] for c in (ctx.get("countries") or []) if c in SLUGS][:2]
    if ctx.get("remote"):
        slugs.append("remote")
    return [PAGE % s for s in (slugs or ["remote"])]


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    out, seen = [], set()
    for url in page_urls(ctx):
        for it in parse_page(client.get_text(url)):
            if it["source_url"] not in seen:
                seen.add(it["source_url"])
                out.append(it)
    return out


def _postings(obj):
    """The first list of dicts that look like job postings (defensive: the page layout is not an API)."""
    if isinstance(obj, dict):
        for key in ("jobPostings", "jobs", "postings"):
            v = obj.get(key)
            if isinstance(v, list) and v and all(isinstance(x, dict) and "title" in x for x in v):
                return v
        for v in obj.values():
            found = _postings(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _postings(v)
            if found:
                return found
    return None


def parse_page(text: str) -> list[dict]:
    m = _ATTR_RE.search(text or "")
    if not m:
        raise ValueError("yc: no data-page attribute")
    data = json.loads(html.unescape(m.group(1)))
    posts = _postings(data) or []
    out = []
    for p in posts:
        pid = p.get("id")
        if not str(pid or "").isdigit():
            continue
        loc = clean(p.get("location"))
        years = None
        ym = _YEARS_RE.search(str(p.get("minExperience") or ""))
        if ym:
            years = {"min": int(ym.group(1)), "max": None}
        typ = str(p.get("type") or "").lower()
        etype = ("internship" if "intern" in typ else "contract" if "contract" in typ else
                 "part_time" if "part" in typ else "full_time" if "full" in typ else None)
        desc = p.get("description") or p.get("descriptionHtml")
        out.append(base_item(
            source_url="https://www.workatastartup.com/jobs/%s" % pid,
            company=clean(p.get("companyName")) or clean(p.get("company")),
            title=clean(p.get("title")),
            location=loc,
            work_mode="remote" if loc and "remote" in loc.lower() else None,
            employment_type=etype,
            years=years,
            apply_route_hint="board_inapp",
            jd_text=html_to_text(desc) if isinstance(desc, str) else None,
            native_ids={"board_job_id": str(pid)},
        ))
    return out
