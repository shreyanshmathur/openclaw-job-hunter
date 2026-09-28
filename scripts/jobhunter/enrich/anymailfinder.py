"""Anymail Finder Find Person Email adapter (reserve finder: one-time trial credits, human-run only).

POST https://api.anymailfinder.com/v5.1/find-email/person, JSON {first_name, last_name, domain} (or
full_name), key in `Authorization: <key>` with no Bearer prefix. email_status: valid -> valid (valid_email),
risky -> unknown, blacklisted -> invalid, not_found -> miss. credits_charged in the answer is kept as
provider_reported_charge and is the charge when present. Recommended timeout 180 s; the human caller uses
120 s. Budget period: lifetime.
"""
from __future__ import annotations

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, clean_status,
                        common_status, load_json, normalize_status)


class AnymailFinder(BaseAdapter):
    name = "anymailfinder"
    HOSTS = frozenset({"api.anymailfinder.com"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"find_name_domain"})
    budget_period = "lifetime"
    max_charge = {"find_name_domain": 1.0}

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        if op != "find_name_domain":
            raise NotImplementedError(op)
        q: PersonQuery = arg
        if q.first_name and q.last_name:
            body = {"first_name": q.first_name, "last_name": q.last_name, "domain": q.domain}
        else:
            body = {"full_name": q.full_name, "domain": q.domain}
        return HttpRequest(method="POST", host="api.anymailfinder.com", path="/v5.1/find-email/person",
                           headers=self.ua() + (("Content-Type", "application/json"),),
                           secret_headers=(("Authorization", "api_key", ""),), json_body=body)

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common)
        doc = load_json(body)
        if not isinstance(doc, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="not_json"))
        raw = clean_status(doc.get("email_status"))
        charged = doc.get("credits_charged")
        reported = float(charged) if isinstance(charged, (int, float)) and not isinstance(charged, bool) else None
        if raw is None:
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="no_status",
                                               provider_reported_charge=reported))
        if raw.lower() == "not_found":
            return self.finish(op, self.result(outcome="miss", http_status=status, raw_status=raw,
                                               provider_reported_charge=reported))
        verification = normalize_status(self.name, raw)
        email = clean_email(doc.get("valid_email")) if verification == "valid" else None
        email = email or clean_email(doc.get("email"))
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status, raw_status=raw,
                                               provider_reported_charge=reported))
        outcome = "invalid" if verification == "invalid" else "hit"
        return self.finish(op, self.result(outcome=outcome, email=email, verification=verification,
                                           http_status=status, raw_status=raw, provider_reported_charge=reported))

    def charge_for(self, op: str, r: EnrichResult) -> float:
        if r.provider_reported_charge is not None and r.outcome in ("hit", "miss", "invalid"):
            return max(0.0, r.provider_reported_charge)
        if r.outcome == "hit":
            return 1.0 if r.verification == "valid" else 0.0
        if r.outcome == "invalid":
            return 0.0
        return super().charge_for(op, r)


ADAPTER = AnymailFinder()
