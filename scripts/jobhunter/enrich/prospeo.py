"""Prospeo Enrich Person adapter (finder). POST https://api.prospeo.io/enrich-person, header X-KEY.

Always sends only_verified_email=true and enrich_mobile=false. A non-empty `mobile` in the answer although
enrich_mobile was false is `unexpected_phone` (the value is never read further). Charge: 1 credit per
verified email; a returned mobile costs 10 and is recorded honestly.
"""
from __future__ import annotations

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, clean_status,
                        common_status, get_path, linkedin_profile_url, load_json, normalize_status)

ERRORS = {
    "NO_MATCH": "miss",
    "INSUFFICIENT_CREDITS": "quota_exhausted",
    "INVALID_API_KEY": "auth_failed",
    "INVALID_DATAPOINTS": "bad_response",
    "INVALID_REQUEST": "bad_response",
    "INTERNAL_ERROR": "server_error",
    "RATE_LIMITED": "rate_limited",
}


class Prospeo(BaseAdapter):
    name = "prospeo"
    HOSTS = frozenset({"api.prospeo.io"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"find_name_domain", "find_linkedin"})
    budget_period = "rolling_31d"
    max_charge = {"find_name_domain": 1.0, "find_linkedin": 1.0}
    PHONE_FLAG_PATHS = ("mobile.mobile", "mobile.number", "mobile.phone", "person.mobile", "person.phone")

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        q: PersonQuery = arg
        if op == "find_name_domain":
            if q.first_name and q.last_name:
                data = {"first_name": q.first_name, "last_name": q.last_name, "company_website": q.domain}
            else:
                data = {"full_name": q.full_name, "company_website": q.domain}
        elif op == "find_linkedin":
            if not q.li_handle:
                raise ValueError("no LinkedIn handle")
            data = {"linkedin_url": linkedin_profile_url(q.li_handle)}
        else:
            raise NotImplementedError(op)
        return HttpRequest(method="POST", host="api.prospeo.io", path="/enrich-person",
                           headers=self.ua() + (("Content-Type", "application/json"),),
                           secret_headers=(("X-KEY", "api_key", ""),),
                           json_body={"only_verified_email": True, "enrich_mobile": False, "data": data})

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        data = load_json(body)
        code = None
        if isinstance(data, dict):
            code = clean_status(data.get("error_code"))
        if code and code.upper() in ERRORS:
            outcome = ERRORS[code.upper()]
            r = self.result(outcome=outcome, http_status=status, raw_status=code)
            if outcome == "rate_limited":
                from .providers import retry_after
                r = r.with_(retry_after_s=retry_after(headers))
            return self.finish(op, r)
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common.with_(raw_status=code))
        if not isinstance(data, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="not_json"))
        if data.get("error") is True:
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status=code))
        phone = self.phone_present(data) or _mobile_value(data)
        raw = clean_status(get_path(data, "email.status"))
        email = clean_email(get_path(data, "email.email"))
        verification = normalize_status(self.name, raw) if email else "none"
        if phone:
            charge = 10.0 + (1.0 if email and verification == "valid" else 0.0)
            return self.result(outcome="unexpected_phone", http_status=status, raw_status=raw,
                               credits_charged=charge)
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status, raw_status=raw))
        return self.finish(op, self.result(outcome="hit", email=email, verification=verification,
                                           http_status=status, raw_status=raw))

    def charge_for(self, op: str, r: EnrichResult) -> float:
        if r.outcome == "hit":
            return 1.0 if r.verification == "valid" else 0.0
        return super().charge_for(op, r)


def _mobile_value(data: dict) -> bool:
    """`mobile` may be an object or a plain string; either non-empty value counts."""
    m = data.get("mobile")
    if isinstance(m, str):
        return bool(m.strip())
    if isinstance(m, dict):
        from .providers import non_empty
        return any(non_empty(m.get(k)) for k in ("mobile", "number", "phone", "international", "national"))
    return False


ADAPTER = Prospeo()
