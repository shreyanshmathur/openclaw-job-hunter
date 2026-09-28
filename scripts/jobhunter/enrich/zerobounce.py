"""ZeroBounce validate adapter (verifier).

POST https://api.zerobounce.net/v2/validate with a form body api_key + email: the key travels in the
body and never in a URL (GET is not used). status: valid -> valid; invalid, spamtrap, abuse and
do_not_mail -> invalid; catch-all -> accept_all; unknown -> unknown and not charged. An answer with an
`error` field is a key or credit problem. Whether the POST form body is accepted is [verify] (section 20);
if it is not, ZeroBounce stays disabled and the Hunter verifier remains.
"""
from __future__ import annotations

import re

from .providers import (BaseAdapter, EnrichResult, HttpRequest, clean_email, clean_status, common_status,
                        load_json, normalize_status)

CREDIT_RE = re.compile(r"(credit|quota|limit)", re.I)
KEY_RE = re.compile(r"(api key|api_key|apikey|key)", re.I)


class ZeroBounce(BaseAdapter):
    name = "zerobounce"
    HOSTS = frozenset({"api.zerobounce.net", "api-us.zerobounce.net", "api-eu.zerobounce.net"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"verify"})
    budget_period = "rolling_31d"
    max_charge = {"verify": 1.0}

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        if op != "verify":
            raise NotImplementedError(op)
        return HttpRequest(method="POST", host="api.zerobounce.net", path="/v2/validate",
                           headers=self.ua() + (("Content-Type", "application/x-www-form-urlencoded"),),
                           form_body=(("email", str(arg)),), secret_body_fields=(("api_key", "api_key"),))

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common)
        doc = load_json(body)
        if not isinstance(doc, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="not_json"))
        err = doc.get("error")
        if isinstance(err, str) and err.strip():
            if CREDIT_RE.search(err):
                return self.finish(op, self.result(outcome="quota_exhausted", http_status=status))
            if KEY_RE.search(err):
                return self.finish(op, self.result(outcome="auth_failed", http_status=status))
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="error"))
        raw = clean_status(doc.get("status"))
        if raw is None:
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="no_status"))
        verification = normalize_status(self.name, raw)
        outcome = "invalid" if verification == "invalid" else "hit"
        return self.finish(op, self.result(outcome=outcome, email=clean_email(doc.get("address")),
                                           verification=verification, http_status=status, raw_status=raw))

    def charge_for(self, op: str, r: EnrichResult) -> float:
        if r.outcome in ("hit", "invalid"):
            return 0.0 if r.verification == "unknown" else 1.0
        return super().charge_for(op, r)


ADAPTER = ZeroBounce()
