"""We Work Remotely RSS feed (research job-sources 1F). Credit WWR.

Feed: GET https://weworkremotely.com/remote-jobs.rss ; item titles read "Company: Role".
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .http import base_item, clean, html_to_text, to_date

SOURCE = "weworkremotely"
NEEDS_DETAIL = False
USES_VALIDATORS = True
FEED = "https://weworkremotely.com/remote-jobs.rss"


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    return parse_rss(client.get_text(FEED, etag=ctx.get("etag"), last_modified=ctx.get("last_modified")))


def parse_rss(text: str) -> list[dict]:
    if "<!ENTITY" in (text or "") or "<!DOCTYPE" in (text or ""):
        raise ValueError("wwr: feeds with a DOCTYPE are not parsed")
    root = ET.fromstring(text)
    out = []
    for item in root.iter("item"):
        def t(tag):
            el = item.find(tag)
            return el.text if el is not None and el.text else None
        link = (t("link") or t("guid") or "").strip()
        if not link.startswith("https://"):
            continue
        raw = clean(t("title")) or ""
        company, _, role = raw.partition(": ")
        if not role:
            company, role = None, raw
        region = clean(t("region"))
        typ = str(t("type") or "").lower()
        etype = ("contract" if "contract" in typ else "part_time" if "part" in typ else
                 "full_time" if "full" in typ else None)
        out.append(base_item(
            source_url=link,
            company=clean(company) or "Unknown company",
            title=clean(role),
            location="Remote" + (" (%s)" % region if region else ""),
            work_mode="remote",
            remote_scope=region,
            employment_type=etype,
            posted_at=to_date(t("pubDate")),
            jd_text=html_to_text(t("description")) or None,
        ))
    return out
