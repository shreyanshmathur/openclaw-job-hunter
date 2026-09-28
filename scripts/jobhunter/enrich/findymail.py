"""Findymail name and business-profile adapter (reserve finder: trial credits, human-run only).

POST https://app.findymail.com/api/search/name {name, domain} and
POST https://app.findymail.com/api/search/business-profile {linkedin_url}, `Authorization: Bearer <key>`.
Findymail returns only verified addresses, so an address in the answer is `valid`; anything else is a
miss. The response field names are not confirmed by the research ([verify], section 20): the parser reads
contact.email and falls back to a top-level email. api/search/phone is never called. Budget: lifetime.
"""
from __future__ import annotations

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, common_status,
                        get_path, linkedin_profile_url, load_json)


class Findymail(BaseAdapter):
    name = "findymail"
    HOSTS = frozenset({"app.findymail.com"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"find_name_domain", "find_linkedin"})
    budget_period = "lifetime"
    max_charge = {"find_name_domain": 1.0, "find_linkedin": 1.0}

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        q: PersonQuery = arg
        headers = self.ua() + (("Content-Type", "application/json"),)
        secret = (("Authorization", "api_key", "Bearer "),)
        if op == "find_name_domain":
            name = q.full_name or " ".join(x for x in (q.first_name, q.last_name) if x)
            return HttpRequest(method="POST", host="app.findymail.com", path="/api/search/name", headers=headers,
                               secret_headers=secret, json_body={"name": name, "domain": q.domain})
        if op == "find_linkedin":
            if not q.li_handle:
                raise ValueError("no LinkedIn handle")
            return HttpRequest(method="POST", host="app.findymail.com", path="/api/search/business-profile",
                               headers=headers, secret_headers=secret,
                               json_body={"linkedin_url": linkedin_profile_url(q.li_handle)})
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
        email = clean_email(get_path(doc, "contact.email")) or clean_email(doc.get("email"))
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status))
        return self.finish(op, self.result(outcome="hit", email=email, verification="valid", http_status=status,
                                           raw_status="verified"))


ADAPTER = Findymail()
