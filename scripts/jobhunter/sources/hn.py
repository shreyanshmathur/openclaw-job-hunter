"""Hacker News "Ask HN: Who is hiring?" through the Algolia API (research job-sources 1F).

Story:    GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10
Comments: GET https://hn.algolia.com/api/v1/items/{storyId}
Each top-level comment is one posting: first line "Company | Role | Location | ...". Key board:hn:{comment id};
source URL https://news.ycombinator.com/item?id={comment id}. Many ask for an email, which goes through the
email lane and QC.
"""
from __future__ import annotations

import re

from .http import base_item, clean, emails_in, html_to_text, to_date

SOURCE = "hn"
NEEDS_DETAIL = False
USES_VALIDATORS = False
SEARCH = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
ITEM = "https://hn.algolia.com/api/v1/items/%s"
_TITLE_RE = re.compile(r"^ask hn: who is hiring\?", re.I)
_ROLE_RE = re.compile(r"\b(engineer|developer|analyst|scientist|manager|designer|lead|architect|head|director|"
                      r"researcher|specialist|consultant|sre|devops|intern|marketer|writer|recruiter|product)\b", re.I)
_PLACE_RE = re.compile(r"\b(remote|onsite|on-site|hybrid|[A-Z][a-z]+,? ?[A-Z]{2}\b|london|berlin|new york|"
                       r"san francisco|bengaluru|bangalore|india|europe|us|usa)\b", re.I)


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    sid = parse_story_search(client.get_json(SEARCH))
    if sid is None:
        return []
    return parse_thread(client.get_json(ITEM % sid))


def parse_story_search(data) -> str | None:
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise ValueError("hn: no hits list")
    for h in hits:
        if isinstance(h, dict) and _TITLE_RE.match(str(h.get("title") or "")) and str(h.get("objectID", "")).isdigit():
            return str(h["objectID"])
    return None


def _first_line(text_html: str) -> str:
    first = re.split(r"<p>|\n", text_html or "", maxsplit=1)[0]
    return clean(html_to_text(first)) or ""


def parse_thread(data) -> list[dict]:
    kids = data.get("children") if isinstance(data, dict) else None
    if not isinstance(kids, list):
        raise ValueError("hn: no children list")
    out = []
    for c in kids:
        if not isinstance(c, dict) or not str(c.get("id", "")).isdigit() or not c.get("text"):
            continue
        head = _first_line(c["text"])
        segs = [s.strip() for s in head.split("|") if s.strip()]
        if len(segs) < 2:
            continue
        company = segs[0][:120]
        role = next((s for s in segs[1:] if _ROLE_RE.search(s)), segs[1])[:160]
        places = [s for s in segs[1:] if s != role and _PLACE_RE.search(s)]
        body = html_to_text(c["text"])
        mails = emails_in(body)
        low = head.lower()
        mode = "remote" if ("remote" in low and "no remote" not in low and "not remote" not in low) else None
        out.append(base_item(
            source_url="https://news.ycombinator.com/item?id=%s" % c["id"],
            company=company,
            title=role,
            location=", ".join(places[:2]) or None,
            work_mode=mode,
            posted_at=to_date(c.get("created_at") or c.get("created_at_i")),
            apply_email=mails[0] if mails else None,
            apply_route_hint="email" if mails else None,
            jd_text=body,
            native_ids={"board_job_id": str(c["id"])},
        ))
    return out
