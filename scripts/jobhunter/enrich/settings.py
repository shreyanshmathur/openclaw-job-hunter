"""Effective `enrich.*` settings (U10, ENRICH-SPEC sections 8.3 and 14).

This module is the one source of the numbers: U1 hardmax imports DEFAULTS, HARD_MAX, PROVIDER_HARD_MAX,
HARD_MIN, PROVIDER_HARD_MIN, BOUNDS and the item sets from here, and U1 config.load() carries the clamped
`enrich` block. load() clamps it again with the same rules (defence in depth, idempotent): limit keys (L) can
only be lowered by the file (raising needs `config raise`, stored as meta `raise:<path>`, and never above
HARD_MAX); floor keys (F) can only be raised by the file (never below HARD_MIN). When config.load() fails,
the raw private/config.json block is clamped instead.

`meta_rows()` gives the meta values the SQL budget triggers read (U1 `config apply` writes them through
config._enrich_meta; missing rows mean budget 0, so the failure mode is "no calls").
"""
from __future__ import annotations

import copy
import json

PROVIDERS = ("prospeo", "hunter", "tomba", "getprospect", "anymailfinder", "findymail", "apollo", "zerobounce")
ROLLING = ("prospeo", "hunter", "tomba", "getprospect", "apollo", "zerobounce")
LIFETIME = ("anymailfinder", "findymail")
CHAIN_ITEMS = ("prospeo", "hunter", "tomba", "getprospect", "apollo")
VERIFIER_ITEMS = ("zerobounce", "hunter")
RESERVE_ITEMS = ("anymailfinder", "findymail")
TARGET_ROLES = ("hiring_manager", "recruiter", "founder")
LIFETIME_UNLIMITED = 1000000

DEFAULTS: dict = {
    "enabled": False,
    "key_store": "auto",
    "chain": ["prospeo", "hunter", "tomba", "getprospect"],
    "verifiers": ["zerobounce", "hunter"],
    "reserve_chain": ["anymailfinder", "findymail"],
    "max_finders_per_person": 2,
    "max_lookups_per_day": 8,
    "max_lookups_per_cycle": 2,
    "max_unsent_found": 10,
    "min_confidence": 90,
    "use_linkedin_identifier": False,
    "timeout_s": 12,
    "retention_days": 90,
    "max_result_age_days": 180,
    "max_share_of_cold_sends": 0.5,
    "bounce_strikes": {"per_provider_30d": 2, "all_providers_30d": 3},
    "skip_locales": [],
    "providers": {
        "prospeo": {"enabled": True, "budget_31d": 90, "day_credits": 4, "day_requests": 8, "min_interval_s": 5},
        "hunter": {"enabled": True, "budget_31d": 45, "day_credits": 3, "day_requests": 10, "min_interval_s": 2},
        "tomba": {"enabled": True, "budget_31d": 22, "day_credits": 1, "day_requests": 2, "min_interval_s": 31},
        "getprospect": {"enabled": True, "budget_31d": 45, "day_credits": 2, "day_requests": 6, "min_interval_s": 3},
        "zerobounce": {"enabled": True, "budget_31d": 90, "day_credits": 4, "day_requests": 6, "min_interval_s": 2},
        "anymailfinder": {"enabled": False, "budget_31d": 0, "budget_lifetime": 0, "day_credits": 1,
                          "day_requests": 2, "min_interval_s": 5},
        "findymail": {"enabled": False, "budget_31d": 0, "budget_lifetime": 0, "day_credits": 1, "day_requests": 2,
                      "min_interval_s": 5},
        "apollo": {"enabled": False, "budget_31d": 0, "day_credits": 2, "day_requests": 4, "min_interval_s": 5},
    },
}

# L keys: higher is looser (file can only lower; `config raise` up to HARD_MAX)
HARD_MAX: dict = {
    "max_finders_per_person": 4, "max_lookups_per_day": 20, "max_lookups_per_cycle": 4, "max_unsent_found": 25,
    "retention_days": 180, "max_result_age_days": 365, "max_share_of_cold_sends": 1.0,
    "bounce_strikes.per_provider_30d": 2, "bounce_strikes.all_providers_30d": 3,
}
PROVIDER_HARD_MAX = {   # free-tier caps (section 8.3): budget_31d, day_credits, day_requests, budget_lifetime
    "prospeo": {"budget_31d": 100, "day_credits": 10, "day_requests": 30},
    "hunter": {"budget_31d": 50, "day_credits": 10, "day_requests": 30},
    "tomba": {"budget_31d": 25, "day_credits": 5, "day_requests": 5},
    "getprospect": {"budget_31d": 50, "day_credits": 10, "day_requests": 30},
    "zerobounce": {"budget_31d": 100, "day_credits": 10, "day_requests": 30},
    "anymailfinder": {"budget_31d": 20, "day_credits": 5, "day_requests": 10, "budget_lifetime": 100},
    "findymail": {"budget_31d": 10, "day_credits": 5, "day_requests": 10, "budget_lifetime": 10},
    "apollo": {"budget_31d": 75, "day_credits": 10, "day_requests": 30},
}
# F keys: lower is looser (file can only raise; never below HARD_MIN)
HARD_MIN: dict = {"min_confidence": 80}
PROVIDER_HARD_MIN = {"prospeo": 2, "hunter": 1, "tomba": 31, "getprospect": 1, "zerobounce": 1, "anymailfinder": 1,
                     "findymail": 1, "apollo": 2}
BOUNDS = {"timeout_s": (5, 20)}

_test_overrides: dict | None = None


def use_test_overrides(block: dict | None) -> None:
    """Tests only: an `enrich` block used instead of private/config.json (None switches it off)."""
    global _test_overrides
    _test_overrides = copy.deepcopy(block) if block is not None else None


# ---------------------------------------------------------------- clamping
def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _raise_value(meta: dict, path: str):
    raw = meta.get("raise:enrich." + path)
    if raw is None:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if _num(v) else None


def _limit(value, default, hard, raised):
    base = raised if raised is not None else default
    v = value if _num(value) else default
    out = min(v, base, hard) if hard is not None else min(v, base)
    return max(0, out)


def _floor(value, default, hard, raised):
    base = raised if raised is not None else default
    v = value if _num(value) else default
    return max(v, base, hard) if hard is not None else max(v, base)


def _items(value, allowed, default) -> list:
    if not isinstance(value, list):
        return list(default)
    out = []
    for x in value:
        if isinstance(x, str) and x in allowed and x not in out:
            out.append(x)
    return out


def clamp(block: dict | None, meta: dict | None = None) -> dict:
    """Effective enrich settings from a raw or pre-clamped block."""
    b = block if isinstance(block, dict) else {}
    meta = meta or {}
    d = DEFAULTS
    out: dict = {}
    out["enabled"] = b.get("enabled") is True
    ks = b.get("key_store")
    out["key_store"] = ks if ks in ("auto", "keychain", "file") else "auto"
    out["chain"] = _items(b.get("chain"), CHAIN_ITEMS, d["chain"])
    out["verifiers"] = _items(b.get("verifiers"), VERIFIER_ITEMS, d["verifiers"])
    out["reserve_chain"] = _items(b.get("reserve_chain"), RESERVE_ITEMS, d["reserve_chain"])
    for k in ("max_finders_per_person", "max_lookups_per_day", "max_lookups_per_cycle", "max_unsent_found",
              "retention_days", "max_result_age_days"):
        out[k] = int(_limit(b.get(k), d[k], HARD_MAX[k], _raise_value(meta, k)))
    out["max_share_of_cold_sends"] = float(_limit(b.get("max_share_of_cold_sends"), d["max_share_of_cold_sends"],
                                                  HARD_MAX["max_share_of_cold_sends"],
                                                  _raise_value(meta, "max_share_of_cold_sends")))
    out["min_confidence"] = int(min(100, _floor(b.get("min_confidence"), d["min_confidence"],
                                                HARD_MIN["min_confidence"], _raise_value(meta, "min_confidence"))))
    out["use_linkedin_identifier"] = b.get("use_linkedin_identifier") is True
    t = b.get("timeout_s")
    lo, hi = BOUNDS["timeout_s"]
    out["timeout_s"] = int(min(max(t, lo), hi)) if _num(t) else d["timeout_s"]
    bs = b.get("bounce_strikes") if isinstance(b.get("bounce_strikes"), dict) else {}
    out["bounce_strikes"] = {}
    for k in ("per_provider_30d", "all_providers_30d"):
        v = _limit(bs.get(k), d["bounce_strikes"][k], HARD_MAX["bounce_strikes." + k],
                   _raise_value(meta, "bounce_strikes." + k))
        out["bounce_strikes"][k] = max(1, int(v))   # 0 would mean "trip on nothing"; 1 is the strictest
    sl = b.get("skip_locales")
    out["skip_locales"] = sorted({str(x).strip().upper()[:2] for x in sl if isinstance(x, str) and x.strip()}) \
        if isinstance(sl, list) else []
    provs = b.get("providers") if isinstance(b.get("providers"), dict) else {}
    out["providers"] = {}
    for p in PROVIDERS:
        pd = d["providers"][p]
        pf = provs.get(p) if isinstance(provs.get(p), dict) else {}
        eff = {"enabled": (pf.get("enabled") is True) if "enabled" in pf else pd["enabled"]}
        for k in ("budget_31d", "day_credits", "day_requests", "budget_lifetime"):
            if k not in pd and k not in PROVIDER_HARD_MAX[p]:
                continue
            default = pd.get(k, 0)
            hard = PROVIDER_HARD_MAX[p].get(k, default)
            v = _limit(pf.get(k), default, hard, _raise_value(meta, "providers.%s.%s" % (p, k)))
            eff[k] = int(v) if k == "day_requests" else float(v)
        eff["min_interval_s"] = int(_floor(pf.get("min_interval_s"), pd["min_interval_s"], PROVIDER_HARD_MIN[p],
                                           _raise_value(meta, "providers.%s.min_interval_s" % p)))
        out["providers"][p] = eff
    return out


# ---------------------------------------------------------------- load
def _meta(conn) -> dict:
    try:
        if conn is not None:
            return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'raise:%'")}
        from .. import config
        return config._meta(None)
    except Exception:
        return {}


def load(conn=None, cfg: dict | None = None) -> dict:
    """Effective enrich settings plus the few other effective values U10 reads:
    `_grades_allowed` (gmail.address_grades_allowed), `_email_outreach` (channels.email_outreach.enabled),
    `_targets` (outreach.targets limited to hiring_manager, recruiter, founder)."""
    from .. import config
    eff = cfg
    if eff is None:
        try:
            eff = config.load(conn)
        except Exception:
            eff = {}
    if _test_overrides is not None:
        block = _test_overrides
    elif isinstance(eff.get("enrich"), dict):
        block = eff["enrich"]
    else:
        try:
            block = config.read_file().get("enrich")
        except Exception:
            block = None
    out = clamp(block, _meta(conn))
    grades = _get(eff, "gmail.address_grades_allowed", ["A", "B"])
    out["_grades_allowed"] = [g for g in grades if g in ("A", "B", "C")] if isinstance(grades, list) else ["A", "B"]
    out["_email_outreach"] = _get(eff, "channels.email_outreach.enabled", True) is True
    targets = _get(eff, "outreach.targets", list(TARGET_ROLES))
    out["_targets"] = [t for t in targets if t in TARGET_ROLES] if isinstance(targets, list) else list(TARGET_ROLES)
    return out


def _get(d, path: str, default):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def provider_enabled(s: dict, provider: str) -> bool:
    return bool(s.get("enabled")) and bool(s["providers"].get(provider, {}).get("enabled"))


def meta_rows(s: dict) -> dict:
    """Meta rows for the SQL triggers (written by U1 config apply). 0 when enrich or the provider is off."""
    rows = {}
    for p in PROVIDERS:
        pe = s["providers"][p]
        on = provider_enabled(s, p)
        rows["enrich_budget_31d:" + p] = _fmt(pe.get("budget_31d", 0) if on else 0)
        rows["enrich_day_credits:" + p] = _fmt(pe.get("day_credits", 0) if on else 0)
        rows["enrich_day_requests:" + p] = str(int(pe.get("day_requests", 0) if on else 0))
        if p in LIFETIME:
            rows["enrich_budget_lifetime:" + p] = _fmt(pe.get("budget_lifetime", 0) if on else 0)
        else:
            rows["enrich_budget_lifetime:" + p] = str(LIFETIME_UNLIMITED if on else 0)
    rows["enrich_max_finders_per_person"] = str(int(s["max_finders_per_person"]))
    return rows


def _fmt(v) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def write_meta(conn, s: dict | None = None, by: str = "config_apply") -> dict:
    """Write meta_rows() inside the caller's transaction (what U1 config apply does; tests and selftest)."""
    from .. import db
    s = s if s is not None else load(conn)
    rows = meta_rows(s)
    for k, v in rows.items():
        db.meta_set(conn, k, v, by)
    return rows
