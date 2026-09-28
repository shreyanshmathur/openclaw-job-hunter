"""Recruitee offers API (research job-sources 1A). The list carries description, requirements and salary.

List: GET https://{company}.recruitee.com/api/offers/
Job URL: https://{company}.recruitee.com/o/{slug}. The design keys it ats:recruitee:{company}:{offer_id} (2.2,
2.4.1). The offer id is not in the URL, so every item carries native_ids.ats_job_id = the numeric offer id and
keys.job_key accepts it for source 'recruitee' on a <company>.recruitee.com/o/<slug> URL. A careers_url on any
other host or path (a custom careers domain) would not take that key, so the source URL is then the
<company>.recruitee.com/o/<slug> URL built from the slug. Offers without a numeric id or a plain slug are
skipped.
"""
from __future__ import annotations

import re

from .http import base_item, clean, html_to_text, salary, to_date

ATS = "recruitee"
NEEDS_DETAIL = False
USES_VALIDATORS = True
TENANT_RE = re.compile(r"^[a-z0-9-]{1,63}$")
OFFER_URL_RE = re.compile(r"^https://[A-Za-z0-9-]+\.recruitee\.com(/l/[a-z-]{2,10})?/o/[^/?#]+/?$")
OFFER_ID_RE = re.compile(r"^[0-9]{1,15}$")
SLUG_RE = re.compile(r"^[^/?#\s]+$")


def list_url(company: str) -> str:
    return "https://%s.recruitee.com/api/offers/" % company


def fetch_list(client, tenant: str, ctx: dict) -> list[dict]:
    data = client.get_json(list_url(tenant), etag=ctx.get("etag"), last_modified=ctx.get("last_modified"))
    return parse_list(data, tenant)


def _etype(v) -> str | None:
    s = str(v or "").lower()
    for key, t in (("intern", "internship"), ("part", "part_time"), ("contract", "contract"),
                   ("freelance", "contract"), ("temp", "temporary"), ("full", "full_time"),
                   ("permanent", "full_time")):
        if key in s:
            return t
    return None


def parse_list(data, company: str) -> list[dict]:
    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, list):
        raise ValueError("recruitee: no offers list")
    out = []
    for o in offers:
        if not isinstance(o, dict) or not isinstance(o.get("slug"), str) or not SLUG_RE.match(o["slug"]) or \
                isinstance(o.get("id"), bool):
            continue
        offer_id = str(o.get("id") or "").strip()
        if not OFFER_ID_RE.match(offer_id):
            continue
        cu = o.get("careers_url")
        url = cu if isinstance(cu, str) and OFFER_URL_RE.match(cu) else "https://%s.recruitee.com/o/%s" % (
            company, o["slug"])
        mode = "remote" if o.get("remote") is True else ("hybrid" if o.get("hybrid") is True else
                                                         ("onsite" if o.get("on_site") is True else None))
        sal = o.get("salary") if isinstance(o.get("salary"), dict) else {}
        jd = "\n\n".join(x for x in (html_to_text(o.get("description")), html_to_text(o.get("requirements"))) if x)
        out.append(base_item(
            source_url=url,
            apply_url=o.get("careers_apply_url") if isinstance(o.get("careers_apply_url"), str) and
            o["careers_apply_url"].startswith("https://") else None,
            company=clean(o.get("company_name")) or company,
            title=clean(o.get("title")),
            location=clean(o.get("location")) or ", ".join(x for x in (clean(o.get("city")), clean(o.get("country")))
                                                          if x) or None,
            work_mode=mode,
            employment_type=_etype(o.get("employment_type_code")),
            posted_at=to_date(o.get("published_at") or o.get("created_at")),
            salary=salary(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("period")) if sal else None,
            jd_text=jd or None,
            native_ids={"ats_job_id": offer_id},
            tenant={"ats": ATS, "tenant": company},
        ))
    return out
