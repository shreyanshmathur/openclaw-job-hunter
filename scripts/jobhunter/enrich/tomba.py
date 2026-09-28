"""Tomba Email Finder and LinkedIn Finder adapter (finder). Two secret headers: X-Tomba-Key and
X-Tomba-Secret. Never sends enrich_mobile or webhook_url.

Reads email, score, accept_all, verification.status and sources[].uri, either nested under `data` or at
the top level (the docs example is ambiguous; both paths are accepted). phone_number and phone_data are
never read. accept_all=true forces the verification to accept_all. Charge: 1 per email found.
Free tier: 5 requests per day, 2 per minute; 429 carries Retry-After.
"""
from __future__ import annotations

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, clean_score,
                        clean_status, clean_urls, common_status, get_path, linkedin_profile_url, load_json,
                        normalize_status)


class Tomba(BaseAdapter):
    name = "tomba"
    HOSTS = frozenset({"api.tomba.io"})
    KEY_FIELDS = ("key", "secret")
    ops = frozenset({"find_name_domain", "find_linkedin"})
    budget_period = "rolling_31d"
    max_charge = {"find_name_domain": 1.0, "find_linkedin": 1.0}

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        q: PersonQuery = arg
        secret = (("X-Tomba-Key", "key", ""), ("X-Tomba-Secret", "secret", ""))
        if op == "find_name_domain":
            query = (("domain", q.domain), ("first_name", q.first_name or ""), ("last_name", q.last_name or ""))
            return HttpRequest(method="GET", host="api.tomba.io", path="/v1/email-finder", query=query,
                               headers=self.ua(), secret_headers=secret)
        if op == "find_linkedin":
            if not q.li_handle:
                raise ValueError("no LinkedIn handle")
            return HttpRequest(method="GET", host="api.tomba.io", path="/v1/linkedin",
                               query=(("url", linkedin_profile_url(q.li_handle)),), headers=self.ua(),
                               secret_headers=secret)
        raise NotImplementedError(op)

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common)
        doc = load_json(body)
        if not isinstance(doc, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="not_json"))
        data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        email = clean_email(data.get("email"))
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status))
        raw = clean_status(get_path(data, "verification.status"))
        verification = normalize_status(self.name, raw) if raw else "unknown"
        if data.get("accept_all") is True and verification != "invalid":
            verification = "accept_all"
        sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        urls = clean_urls(s.get("uri") for s in sources if isinstance(s, dict))
        return self.finish(op, self.result(outcome="hit", email=email, confidence=clean_score(data.get("score")),
                                           verification=verification, http_status=status, raw_status=raw,
                                           source_urls=urls, source_url=urls[0] if urls else None))


ADAPTER = Tomba()
