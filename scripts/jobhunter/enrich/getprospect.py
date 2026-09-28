"""GetProspect Email Finder and LinkedIn insights adapter (finder). Header apiKey.

Finder: GET https://api.getprospect.com/v2/email-finder?first_name=&last_name=&domain=.
LinkedIn: GET https://api.getprospect.com/public/v1/insights/contact?linkedinUrl=... (402 no credits,
404 not found). The `status` enum is not documented: only `valid` maps to valid, any other value is
`unknown` with raw_status kept for enum discovery. free_email=true sets free_mail_flag (rejected later).
Charge: 1 per email found; not-found is free (pricing FAQ).
"""
from __future__ import annotations

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, clean_status,
                        common_status, linkedin_profile_url, load_json, normalize_status)


class GetProspect(BaseAdapter):
    name = "getprospect"
    HOSTS = frozenset({"api.getprospect.com"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"find_name_domain", "find_linkedin"})
    budget_period = "rolling_31d"
    max_charge = {"find_name_domain": 1.0, "find_linkedin": 1.0}

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        q: PersonQuery = arg
        secret = (("apiKey", "api_key", ""),)
        if op == "find_name_domain":
            query = (("first_name", q.first_name or ""), ("last_name", q.last_name or ""), ("domain", q.domain))
            return HttpRequest(method="GET", host="api.getprospect.com", path="/v2/email-finder", query=query,
                               headers=self.ua(), secret_headers=secret)
        if op == "find_linkedin":
            if not q.li_handle:
                raise ValueError("no LinkedIn handle")
            return HttpRequest(method="GET", host="api.getprospect.com", path="/public/v1/insights/contact",
                               query=(("linkedinUrl", linkedin_profile_url(q.li_handle)),), headers=self.ua(),
                               secret_headers=secret)
        raise NotImplementedError(op)

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        if status == 404:
            return self.finish(op, self.result(outcome="miss", http_status=status))
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common)
        doc = load_json(body)
        if not isinstance(doc, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="not_json"))
        data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        email = clean_email(data.get("email"))
        raw = clean_status(data.get("status"))
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status, raw_status=raw))
        return self.finish(op, self.result(outcome="hit", email=email, verification=normalize_status(self.name, raw)
                                           if raw else "unknown", http_status=status, raw_status=raw,
                                           free_mail_flag=data.get("free_email") is True))


ADAPTER = GetProspect()
