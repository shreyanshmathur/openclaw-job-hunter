"""Apollo People Enrichment adapter (optional finder, budget 0 by default).

POST https://api.apollo.io/api/v1/people/match, header x-api-key. Always sends reveal_personal_emails=false,
reveal_phone_number=false and run_waterfall_phone=false, never webhook_url. Whether Apollo reads these as
query or body parameters is not confirmed ([verify], section 20), so they go in both. Reads person.email
and person.email_status (verified -> valid, else unknown). Any non-empty personal phone field is
`unexpected_phone`; personal_emails is never read.
"""
from __future__ import annotations

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, clean_status,
                        common_status, get_path, load_json, normalize_status)

FLAGS = (("reveal_personal_emails", "false"), ("reveal_phone_number", "false"), ("run_waterfall_phone", "false"))


class Apollo(BaseAdapter):
    name = "apollo"
    HOSTS = frozenset({"api.apollo.io"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"find_name_domain"})
    budget_period = "rolling_31d"
    max_charge = {"find_name_domain": 1.0}
    PHONE_FLAG_PATHS = ("person.phone_numbers", "person.mobile_phone", "person.sanitized_phone", "person.phone",
                        "person.contact.phone_numbers", "person.contact.sanitized_phone", "person.direct_dial")

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        if op != "find_name_domain":
            raise NotImplementedError(op)
        q: PersonQuery = arg
        body = {"first_name": q.first_name, "last_name": q.last_name, "domain": q.domain,
                "reveal_personal_emails": False, "reveal_phone_number": False, "run_waterfall_phone": False}
        return HttpRequest(method="POST", host="api.apollo.io", path="/api/v1/people/match", query=FLAGS,
                           headers=self.ua() + (("Content-Type", "application/json"),),
                           secret_headers=(("x-api-key", "api_key", ""),), json_body=body)

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common)
        doc = load_json(body)
        if not isinstance(doc, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="not_json"))
        if self.phone_present(doc):
            return self.result(outcome="unexpected_phone", http_status=status, credits_charged=1.0)
        person = doc.get("person")
        if not isinstance(person, dict):
            return self.finish(op, self.result(outcome="miss", http_status=status))
        email = clean_email(person.get("email"))
        raw = clean_status(get_path(doc, "person.email_status"))
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status, raw_status=raw))
        return self.finish(op, self.result(outcome="hit", email=email, verification=normalize_status(self.name, raw)
                                           if raw else "unknown", http_status=status, raw_status=raw))


ADAPTER = Apollo()
