"""Credit budgets, reserve and settle, provider state and the enrich breakers (U10, ENRICH-SPEC 8 and 10).

Windows: rolling 31 days (any billing month of up to 31 days lies inside one, so no reset day is needed),
rolling 24 hours (credits and requests, misses included) and lifetime (one-time trial credits). One pool
per provider covers all of its ops.

Reserve, call, settle: `try_reserve` checks the budget in Python inside BEGIN IMMEDIATE and inserts an
`inflight` call at the op's maximum charge (the SQL trigger t_enrich_budget repeats the check as the last
line; a missing meta row counts as budget 0). The HTTP call happens outside any transaction. `settle` then
records the documented charge: 0 for a miss or an error before anything was found, the maximum for any
failure after the request was sent (fail closed).
"""
from __future__ import annotations

import json

from .. import db
from ..canon import now, parse_ts, ts_add, utcnow
from ..errors import Denied
from ..events import enqueue_notification, log_event
from . import providers, settings

FOREVER = "9999-12-31T00:00:00Z"
RATE_BACKOFF_S = (3600, 6 * 3600, 24 * 3600, 24 * 3600)
PROVIDER_ERRORS_TRIP = 3
PROVIDER_ERRORS_COOLDOWN_S = 6 * 3600
BAD_RESPONSES_TRIP = 2
HUMAN_REASONS = ("auth_failed", "tls", "schema_changed", "unexpected_phone", "bounce_strikes")
ERROR_OUTCOMES = ("server_error", "timeout_after_send", "network_before_send")


def in_tx(conn, fn):
    """Run fn(conn) in the caller's transaction, or in a new one when none is open."""
    if conn.in_transaction:
        return fn(conn)
    with db.tx(conn):
        return fn(conn)


# ---------------------------------------------------------------- breakers (enrich scopes)
def breaker_row(conn, scope: str):
    """The open breaker row for scope, or None (an expired auto-close breaker counts as closed)."""
    row = conn.execute("SELECT * FROM breakers WHERE scope = ? AND state = 'open'", (scope,)).fetchone()
    if row is None:
        return None
    if not row["requires_human"] and row["auto_close_at"] and row["auto_close_at"] <= now():
        return None
    return row


def trip(conn, scope: str, reason: str, detail: str, *, requires_human: bool = True,
         auto_close_at: str | None = None, cycle_id: str | None = None) -> dict:
    """Open an `enrich` or `enrich:<provider>` breaker through U1 breakers.trip (same rows, events, evidence
    file and owner alert as every other stop). `requires_human=False` with `auto_close_at` is a stop that ends
    by itself (quota, 429 back-off, error streak); breakers.trip never turns an open stop that waits for the
    owner into one that ends by itself, and an open bounce_strikes stop keeps its reason whatever trips next.
    The finder never aborts the outreach cycle, so `cycle_id` is only kept for the caller's signature. Runs in
    the caller's transaction."""
    from .. import breakers
    if reason != "bounce_strikes":
        row = conn.execute("SELECT reason_code FROM breakers WHERE scope = ? AND state = 'open'", (scope,)).fetchone()
        if row is not None and row["reason_code"] == "bounce_strikes":
            # a bounce stop holds until the owner resets it: a later 429, refused key or error streak on a
            # call that was already in flight never relabels it (gatecheck keys on bounce_strikes)
            log_event(conn, "enrich_trip_kept", scope=scope, reason=reason)
            return {"scope": scope, "reason_code": "bounce_strikes", "kept": True}
    auto = (not requires_human) and bool(auto_close_at)
    return breakers.trip(conn, scope, reason, detail[:500], None, "system:enrich", None,
                         requires_human=False if auto else None, auto_close_at=auto_close_at if auto else None)


def close(conn, scope: str, reason: str | None = None, by: str = "human") -> bool:
    """Close an open enrich breaker (only when its reason matches, if given). Runs in the caller's tx."""
    row = conn.execute("SELECT * FROM breakers WHERE scope = ? AND state = 'open'", (scope,)).fetchone()
    if row is None or (reason and row["reason_code"] != reason):
        return False
    ts = now()
    conn.execute("UPDATE breakers SET state = 'closed', reset_at = ?, reset_by = ?, reset_note = ?, updated_at = ? "
                 "WHERE scope = ?", (ts, by, "closed by enrich", ts, scope))
    conn.execute("INSERT INTO breaker_events (scope, event, reason_code, detail, by, created_at) "
                 "VALUES (?, 'reset', ?, 'closed by enrich', ?, ?)", (scope, row["reason_code"], by, ts))
    log_event(conn, "breaker_reset", scope=scope, by=by)
    return True


# ---------------------------------------------------------------- provider state
def state_row(conn, provider: str):
    return conn.execute("SELECT * FROM enrich_provider_state WHERE provider = ?", (provider,)).fetchone()


def _ensure_state(conn, provider: str) -> None:
    conn.execute("INSERT INTO enrich_provider_state (provider, updated_at) VALUES (?, ?) "
                 "ON CONFLICT (provider) DO NOTHING", (provider, now()))


def _set_state(conn, provider: str, **cols) -> None:
    _ensure_state(conn, provider)
    cols["updated_at"] = now()
    conn.execute("UPDATE enrich_provider_state SET %s WHERE provider = ?" % ", ".join("%s = ?" % k for k in cols),
                 list(cols.values()) + [provider])


# ---------------------------------------------------------------- usage and limits
def usage(conn, provider: str, at: str | None = None) -> dict:
    at = at or now()
    d31 = ts_add(at, days=-31)
    d1 = ts_add(at, days=-1)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN started_at > ? THEN credits_charged END), 0), "
        "COALESCE(SUM(CASE WHEN started_at > ? THEN credits_charged END), 0), "
        "COALESCE(SUM(credits_charged), 0) FROM enrich_calls WHERE provider = ? AND op <> 'account'",
        (d31, d1, provider)).fetchone()
    req = conn.execute("SELECT count(*) FROM enrich_calls WHERE provider = ? AND started_at > ?",
                       (provider, d1)).fetchone()[0]
    return {"spent_31d": float(row[0]), "spent_24h": float(row[1]), "spent_lifetime": float(row[2]),
            "requests_24h": int(req)}


def _meta_num(conn, key: str) -> float:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def limits(conn, provider: str, s: dict) -> dict:
    """Effective limits: the stricter of the settings and the meta rows the trigger reads (fail closed)."""
    pe = s["providers"].get(provider, {})
    on = settings.provider_enabled(s, provider)
    life_setting = pe.get("budget_lifetime", settings.LIFETIME_UNLIMITED) if provider in settings.LIFETIME \
        else settings.LIFETIME_UNLIMITED
    return {
        "budget_31d": min(float(pe.get("budget_31d", 0)) if on else 0.0, _meta_num(conn, "enrich_budget_31d:" + provider)),
        "day_credits": min(float(pe.get("day_credits", 0)) if on else 0.0,
                           _meta_num(conn, "enrich_day_credits:" + provider)),
        "day_requests": int(min(float(pe.get("day_requests", 0)) if on else 0.0,
                                _meta_num(conn, "enrich_day_requests:" + provider))),
        "budget_lifetime": min(float(life_setting) if on else 0.0,
                               _meta_num(conn, "enrich_budget_lifetime:" + provider)),
        "min_interval_s": int(pe.get("min_interval_s", 5)),
    }


def _window_retry(conn, provider: str, days: int, need: float, cap: float, at: str) -> int | None:
    """Seconds until enough old calls leave the rolling window for `need` more credits to fit under cap."""
    rows = conn.execute("SELECT started_at, credits_charged FROM enrich_calls WHERE provider = ? AND op <> 'account' "
                        "AND started_at > ? ORDER BY started_at", (provider, ts_add(at, days=-days))).fetchall()
    total = sum(float(r[1]) for r in rows)
    for r in rows:
        if total + need <= cap:
            break
        total -= float(r[1])
        leave = ts_add(r[0], days=days)
        if total + need <= cap:
            return max(1, int((parse_ts(leave) - parse_ts(at)).total_seconds()) + 1)
    return None


def can_reserve(conn, provider: str, op: str, s: dict | None = None) -> tuple:
    """(ok, reason, retry_after_s). reason in skipped_disabled, skipped_breaker, skipped_budget, skipped_rate."""
    s = s if s is not None else settings.load(conn)
    if not settings.provider_enabled(s, provider):
        return False, "skipped_disabled", None
    adapter = providers.get(provider)
    if op not in adapter.ops:
        return False, "skipped_input", None
    for scope in ("enrich:" + provider, "enrich"):
        b = breaker_row(conn, scope)
        if b is not None:
            retry = None
            if not b["requires_human"] and b["auto_close_at"]:
                retry = max(1, int((parse_ts(b["auto_close_at"]) - utcnow()).total_seconds()))
            return False, "skipped_breaker", retry
    at = now()
    st = state_row(conn, provider)
    if st is not None and st["exhausted_until"] and st["exhausted_until"] > at:
        retry = None if st["exhausted_until"] == FOREVER else \
            max(1, int((parse_ts(st["exhausted_until"]) - utcnow()).total_seconds()))
        return False, "skipped_budget", retry
    if st is not None and st["next_call_at"] and st["next_call_at"] > at:
        return False, "skipped_rate", max(1, int((parse_ts(st["next_call_at"]) - utcnow()).total_seconds()))
    lim = limits(conn, provider, s)
    u = usage(conn, provider, at)
    need = float(adapter.max_charge.get(op, 1.0))
    if st is not None and st["reported_remaining"] is not None and need > float(st["reported_remaining"]):
        return False, "skipped_budget", None
    if u["spent_31d"] + need > lim["budget_31d"]:
        return False, "skipped_budget", _window_retry(conn, provider, 31, need, lim["budget_31d"], at)
    if u["spent_24h"] + need > lim["day_credits"]:
        return False, "skipped_budget", _window_retry(conn, provider, 1, need, lim["day_credits"], at)
    if u["requests_24h"] + 1 > lim["day_requests"]:
        first = conn.execute("SELECT min(started_at) FROM enrich_calls WHERE provider = ? AND started_at > ?",
                             (provider, ts_add(at, days=-1))).fetchone()[0]
        retry = max(1, int((parse_ts(ts_add(first, days=1)) - utcnow()).total_seconds()) + 1) if first else None
        return False, "skipped_budget", retry
    if u["spent_lifetime"] + need > lim["budget_lifetime"]:
        return False, "skipped_budget", None
    return True, None, None


def try_reserve(conn, provider: str, op: str, request_id: int, cycle_id: str | None, s: dict | None = None) -> int:
    """Insert an inflight call at the op's maximum charge; returns its id. Own transaction.
    Denied(E_CEILING, data.reason) when the budget, a breaker or the pacing says no."""
    s = s if s is not None else settings.load(conn)
    adapter = providers.get(provider)
    max_c = float(adapter.max_charge.get(op, 1.0))

    def _do(c):
        ok, reason, retry = can_reserve(c, provider, op, s)
        if not ok:
            raise Denied("E_CEILING", "%s cannot be called now (%s)" % (provider, reason), retry_after=retry,
                         data={"reason": reason, "provider": provider})
        ts = now()
        cur = c.execute("INSERT INTO enrich_calls (request_id, provider, op, outcome, started_at, credits_charged, "
                        "cycle_id, created_at, updated_at) VALUES (?, ?, ?, 'inflight', ?, ?, ?, ?, ?)",
                        (request_id, provider, op, ts, max_c, cycle_id, ts, ts))
        _set_state(c, provider, next_call_at=ts_add(ts, seconds=limits(c, provider, s)["min_interval_s"]))
        return cur.lastrowid

    if conn.in_transaction:
        return _do(conn)
    with db.tx(conn):
        return _do(conn)


# ---------------------------------------------------------------- settle
def charge(provider: str, op: str, result: providers.EnrichResult) -> float:
    max_c = float(providers.get(provider).max_charge.get(op, 1.0))
    if result.outcome in providers.CHARGE_MAX_OUTCOMES:
        c = max(max_c, float(result.credits_charged or 0))
    elif result.outcome in providers.CHARGE_ZERO_OUTCOMES:
        c = 0.0
    else:
        c = float(result.credits_charged or 0)
    if result.provider_reported_charge is not None:
        c = max(c, float(result.provider_reported_charge))
    return max(0.0, c)


def settle(conn, call_id: int, result: providers.EnrichResult, *, grade_hint: str | None = None,
           reject_reason: str | None = None, keep_email: bool = True, cycle_id: str | None = None) -> None:
    """Record the outcome, the documented charge and the whitelisted fields of one call, then update the
    provider state and breakers (section 10). Own transaction unless one is open."""

    def _do(c):
        row = c.execute("SELECT * FROM enrich_calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            raise Denied("E_INTERNAL", "no enrich call %s" % call_id)
        provider, op = row["provider"], row["op"]
        charged = charge(provider, op, result)
        if row["outcome"] == "in_progress":   # a re-poll: never charged twice
            charged = max(float(row["credits_charged"]), 0.0)
        email = result.email if (keep_email and result.email) else None
        domain = email.rsplit("@", 1)[1] if email and "@" in email else None
        urls = list(result.source_urls)[:providers.MAX_SOURCE_URLS]
        c.execute("UPDATE enrich_calls SET outcome = ?, finished_at = ?, http_status = ?, retry_after_s = ?, "
                  "credits_charged = ?, email = ?, email_domain = ?, verification = ?, confidence = ?, "
                  "grade_hint = ?, reject_reason = ?, raw_status = ?, source_url = ?, source_urls_json = ?, "
                  "updated_at = ? WHERE id = ?",
                  (result.outcome, now(), result.http_status, result.retry_after_s, charged, email, domain,
                   result.verification if result.verification in providers.VERIFICATIONS else "none",
                   result.confidence, grade_hint, (reject_reason or None) and reject_reason[:40],
                   (result.raw_status or None) and str(result.raw_status)[:40],
                   result.source_url if (email and result.source_url) else None,
                   json.dumps(urls if email else []), now(), call_id))
        provider_error(c, provider, result, cycle_id=cycle_id)

    in_tx(conn, _do)


def set_grade(conn, call_id: int, grade_hint: str | None, reject_reason: str | None = None) -> None:
    def _do(c):
        c.execute("UPDATE enrich_calls SET grade_hint = ?, reject_reason = COALESCE(?, reject_reason), updated_at = ? "
                  "WHERE id = ?", (grade_hint, reject_reason, now(), call_id))
    in_tx(conn, _do)


def provider_error(conn, provider: str, result: providers.EnrichResult, cycle_id: str | None = None) -> None:
    """Provider state and breakers after one call (section 10). Runs in the caller's transaction."""
    out = result.outcome
    at = now()
    _ensure_state(conn, provider)
    st = state_row(conn, provider)
    scope = "enrich:" + provider
    if result.reported_remaining is not None:
        _set_state(conn, provider, reported_remaining=float(result.reported_remaining), reported_at=at)
    if out in ("hit", "miss", "invalid", "in_progress"):
        _set_state(conn, provider, consecutive_errors=0, backoff_level=0)
        return
    if out == "auth_failed":
        trip(conn, scope, "auth_failed", "HTTP %s. Run ./jobhunter enrich connect %s with a working key; that "
             "also clears this stop" % (result.http_status or "-", provider), cycle_id=cycle_id)
        return
    if out == "quota_exhausted":
        adapter = providers.get(provider)
        until = FOREVER if adapter.budget_period == "lifetime" else ts_add(at, days=31)
        _set_state(conn, provider, exhausted_until=until, exhausted_reason="quota")
        enqueue_notification(conn, "enrich_quota:%s:%s" % (provider, at[:10]), "normal", "info",
                             "Email finder: %s says the free credits are used up; it is skipped until %s."
                             % (provider, "you add credits" if until == FOREVER else until[:10]))
        log_event(conn, "enrich_quota_exhausted", provider=provider, until=until)
        return
    if out == "rate_limited":
        level = int(st["backoff_level"] or 0)
        wait = max(int(result.retry_after_s or 0), RATE_BACKOFF_S[min(level, 3)])
        trip(conn, scope, "rate_limited", "HTTP 429 from %s; waiting %d s" % (provider, wait), requires_human=False,
             auto_close_at=ts_add(at, seconds=wait), cycle_id=cycle_id)
        _set_state(conn, provider, backoff_level=min(3, level + 1))
        return
    if out == "unexpected_phone":
        trip(conn, scope, "unexpected_phone", "a phone field came back although every phone flag was off; the "
                                              "value was discarded", cycle_id=cycle_id)
        return
    if out == "network_before_send" and result.raw_status == "tls":
        trip(conn, scope, "tls", "TLS verification failed for %s (possible interception)" % provider,
             cycle_id=cycle_id)
        return
    if out == "bad_response":
        n = conn.execute("SELECT count(*) FROM enrich_calls WHERE provider = ? AND outcome = 'bad_response' "
                         "AND started_at > ?", (provider, ts_add(at, days=-1))).fetchone()[0]
        if n >= BAD_RESPONSES_TRIP:
            trip(conn, scope, "schema_changed", "%d unreadable answers from %s in 24 hours; the API may have "
                                                "changed" % (n, provider), cycle_id=cycle_id)
        return
    if out in ERROR_OUTCOMES:
        n = int(st["consecutive_errors"] or 0) + 1
        if n >= PROVIDER_ERRORS_TRIP:
            trip(conn, scope, "provider_errors", "%d failed calls in a row to %s" % (n, provider),
                 requires_human=False, auto_close_at=ts_add(at, seconds=PROVIDER_ERRORS_COOLDOWN_S), cycle_id=cycle_id)
            n = 0
        _set_state(conn, provider, consecutive_errors=n)


def mark_key_set(conn, provider: str) -> bool:
    """After `enrich connect`: key_set_at, and an open auth_failed breaker for the provider is closed."""
    _set_state(conn, provider, key_set_at=now())
    return close(conn, "enrich:" + provider, reason="auth_failed", by="human")


# ---------------------------------------------------------------- summary
def summary(conn, s: dict | None = None) -> dict:
    """Per provider budget view for `enrich budget`, U1 `budget` and U5 status. No key text."""
    from . import keystore
    s = s if s is not None else settings.load(conn)
    out = {"enabled": bool(s.get("enabled")), "providers": {}}
    tot = {"spent_31d": 0.0, "spent_24h": 0.0}
    for p in settings.PROVIDERS:
        u = usage(conn, p)
        lim = limits(conn, p, s)
        st = state_row(conn, p)
        ks = keystore.status(p)
        br = breaker_row(conn, "enrich:" + p)
        out["providers"][p] = {
            "enabled": settings.provider_enabled(s, p), "key": ks["key"], "backend": ks["backend"],
            "spent_31d": u["spent_31d"], "budget_31d": lim["budget_31d"], "spent_24h": u["spent_24h"],
            "day_credits": lim["day_credits"], "requests_24h": u["requests_24h"], "day_requests": lim["day_requests"],
            "spent_lifetime": u["spent_lifetime"],
            "budget_lifetime": lim["budget_lifetime"] if p in settings.LIFETIME else None,
            "exhausted_until": st["exhausted_until"] if st is not None else None,
            "breaker": (br["reason_code"] or "open") if br is not None else None,
            "next_call_at": st["next_call_at"] if st is not None else None,
        }
        tot["spent_31d"] += u["spent_31d"]
        tot["spent_24h"] += u["spent_24h"]
    all_br = breaker_row(conn, "enrich")
    pause = breaker_row(conn, "pause:enrich")
    out["breaker"] = (all_br["reason_code"] or "open") if all_br is not None else None
    out["paused"] = pause is not None
    out["totals"] = tot
    out["lookups_24h"] = conn.execute(
        "SELECT count(DISTINCT request_id) FROM enrich_calls WHERE request_id IS NOT NULL AND op <> 'account' "
        "AND started_at > ?", (ts_add(now(), days=-1),)).fetchone()[0]
    from . import eligibility
    out["unsent_found"] = eligibility.unsent_found(conn)
    return out
