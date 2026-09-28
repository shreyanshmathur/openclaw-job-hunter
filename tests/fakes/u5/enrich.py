"""Fake `jobhunter.enrich.budget.summary(conn)` results (U10 shape, ENRICH-SPEC 12) for the U5 status, digest
and Sheet tests. No key material: `key` is only true or false, as in the real summary."""
from __future__ import annotations


def _p(enabled=True, key=True, spent_31d=0.0, budget_31d=0.0, spent_24h=0.0, day_credits=0.0, requests_24h=0,
       day_requests=0, spent_lifetime=None, budget_lifetime=None, exhausted_until=None, breaker=None) -> dict:
    return {"enabled": enabled, "key": key, "backend": "test", "spent_31d": spent_31d, "budget_31d": budget_31d,
            "spent_24h": spent_24h, "day_credits": day_credits, "requests_24h": requests_24h,
            "day_requests": day_requests, "spent_lifetime": spent_31d if spent_lifetime is None else spent_lifetime,
            "budget_lifetime": budget_lifetime, "exhausted_until": exhausted_until, "breaker": breaker,
            "next_call_at": None}


def sample_summary(exhausted_until: str = "2026-10-20T00:00:00Z") -> dict:
    """Hunter in use, Tomba stopped (key refused), GetProspect out of credits, ZeroBounce unused, Prospeo on but
    without a key, Anymail Finder off and never used (not shown)."""
    return {
        "enabled": True, "paused": False, "breaker": None,
        "providers": {
            "prospeo": _p(key=False, budget_31d=90.0, day_credits=4.0, day_requests=8),
            "hunter": _p(spent_31d=12.5, budget_31d=45.0, spent_24h=1.0, day_credits=3.0, requests_24h=2,
                         day_requests=10),
            "tomba": _p(spent_31d=2.0, budget_31d=22.0, day_credits=1.0, day_requests=2, breaker="auth_failed"),
            "getprospect": _p(spent_31d=45.0, budget_31d=45.0, day_credits=2.0, day_requests=6,
                              exhausted_until=exhausted_until),
            "anymailfinder": _p(enabled=False, key=False, budget_lifetime=0.0, day_credits=1.0, day_requests=2),
            "zerobounce": _p(budget_31d=90.0, day_credits=4.0, day_requests=6),
        },
        "totals": {"spent_31d": 59.5, "spent_24h": 1.0},
        "lookups_24h": 3,
        "unsent_found": 2,
    }


def off_summary() -> dict:
    return {"enabled": False, "paused": False, "breaker": None, "providers": {
        p: _p(enabled=False, key=False) for p in ("prospeo", "hunter", "tomba", "getprospect", "zerobounce")},
        "totals": {"spent_31d": 0.0, "spent_24h": 0.0}, "lookups_24h": 0, "unsent_found": 0}
