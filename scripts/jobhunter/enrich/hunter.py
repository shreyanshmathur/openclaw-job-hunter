"""Hunter Email Finder and Email Verifier adapter (finder and verifier). Key in the X-API-KEY header,
never in the api_key query parameter.

Finder: GET https://api.hunter.io/v2/email-finder with domain + first_name + last_name (or domain +
linkedin_handle, the handle and never the URL) and max_duration. Reads data.email, data.score,
data.verification.status and data.sources[].uri (still_on_page first); data.phone_number is never read.
Verifier: GET https://api.hunter.io/v2/email-verifier?email=; HTTP 202 means still in progress.
Charges: 1 credit per email found, 0.5 per completed verification (202 counts, fail closed).
"""
from __future__ import annotations

import re

from .providers import (BaseAdapter, EnrichResult, HttpRequest, PersonQuery, clean_email, clean_score,
                        clean_status, clean_urls, common_status, get_path, load_json, normalize_status,
                        retry_after, text_of)

USAGE_RE = re.compile(r"(usage|credit|quota|plan|monthly|upgrade)", re.I)


class Hunter(BaseAdapter):
    name = "hunter"
    HOSTS = frozenset({"api.hunter.io"})
    KEY_FIELDS = ("api_key",)
    ops = frozenset({"find_name_domain", "find_linkedin", "verify"})
    budget_period = "rolling_31d"
    max_charge = {"find_name_domain": 1.0, "find_linkedin": 1.0, "verify": 0.5}

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        secret = (("X-API-KEY", "api_key", ""),)
        max_duration = str(int(max(3, min(int(timeout_s) - 2, 10))))
        if op == "find_name_domain":
            q: PersonQuery = arg
            query = (("domain", q.domain), ("first_name", q.first_name or ""), ("last_name", q.last_name or ""),
                     ("max_duration", max_duration))
            return HttpRequest(method="GET", host="api.hunter.io", path="/v2/email-finder", query=query,
                               headers=self.ua(), secret_headers=secret)
        if op == "find_linkedin":
            q = arg
            if not q.li_handle:
                raise ValueError("no LinkedIn handle")
            query = (("domain", q.domain), ("linkedin_handle", q.li_handle), ("max_duration", max_duration))
            return HttpRequest(method="GET", host="api.hunter.io", path="/v2/email-finder", query=query,
                               headers=self.ua(), secret_headers=secret)
        if op == "verify":
            return HttpRequest(method="GET", host="api.hunter.io", path="/v2/email-verifier",
                               query=(("email", str(arg)),), headers=self.ua(), secret_headers=secret)
        raise NotImplementedError(op)

    def parse(self, op: str, status: int, headers: dict, body) -> EnrichResult:
        if status == 202 and op == "verify":
            return self.finish(op, self.result(outcome="in_progress", http_status=status))
        if status == 429 and USAGE_RE.search(text_of(body)):
            return self.finish(op, self.result(outcome="quota_exhausted", http_status=status))
        common = common_status(self.name, status, headers, body)
        if common is not None:
            return self.finish(op, common)
        doc = load_json(body)
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="no_data"))
        remaining = _remaining(doc)
        if op == "verify":
            raw = clean_status(data.get("status"))
            if raw is None:
                return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="no_status"))
            verification = normalize_status(self.name, raw)
            outcome = "invalid" if verification == "invalid" else "hit"
            email = clean_email(data.get("email"))
            return self.finish(op, self.result(outcome=outcome, email=email, verification=verification,
                                               http_status=status, raw_status=raw,
                                               free_mail_flag=raw.lower() in ("webmail", "disposable"),
                                               reported_remaining=remaining))
        email = clean_email(data.get("email"))
        if not email:
            return self.finish(op, self.result(outcome="miss", http_status=status, reported_remaining=remaining))
        raw = clean_status(get_path(data, "verification.status"))
        sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        ordered = sorted((s for s in sources if isinstance(s, dict)),
                         key=lambda s: 0 if s.get("still_on_page") is True else 1)
        urls = clean_urls(s.get("uri") for s in ordered)
        return self.finish(op, self.result(outcome="hit", email=email, confidence=clean_score(data.get("score")),
                                           verification=normalize_status(self.name, raw), http_status=status,
                                           raw_status=raw, source_urls=urls, source_url=urls[0] if urls else None,
                                           reported_remaining=remaining))

    def charge_for(self, op: str, r: EnrichResult) -> float:
        if op == "verify":
            if r.outcome in ("hit", "invalid", "in_progress"):
                return 0.5
            return super().charge_for(op, r)
        return super().charge_for(op, r)


def _remaining(doc) -> float | None:
    """Remaining credits when the answer carries meta.credits_remaining (not documented by Hunter; None else)."""
    v = get_path(doc, "meta.credits_remaining") if isinstance(doc, dict) else None
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


ADAPTER = Hunter()
