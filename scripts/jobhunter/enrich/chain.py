"""`enrich find`: the lookup chain for one selected contact (U10, ENRICH-SPEC section 4).

Order: stops, contact, eligibility, cache, claim, free pre-steps (MX, published pattern), at most
`max_finders_per_person` finders from the person's own free tiers, at most one verifier, finish. The first
grade B ends the chain. Every HTTP call happens outside a database transaction: budget is reserved in one
short transaction before the call and settled in another after it. Nothing here sends email, reserves send
tokens or writes drafts; a found address becomes an ordinary contact address (contacts.set_address) that
must pass every existing check.

Returns {code, data, message, next, retry_after_s}; refusals raise errors.Denied.
"""
from __future__ import annotations

import os
import secrets
import time

from .. import breakers as core_breakers
from .. import db, locks, people
from ..canon import now, parse_ts, utcnow
from ..errors import Denied
from ..events import enqueue_notification, log_event
from . import budget, cache, deps, eligibility, keystore, providers, settings, verify
from .providers import EnrichResult, PersonQuery

DEADLINE_AGENT_S = 45
DEADLINE_HUMAN_S = 300
RESERVE_TIMEOUT_S = 120
REPOLL_WAIT_S = 10
SAFETY_S = 3
STEP_PRE, STEP_FINDERS, STEP_VERIFY, STEP_DONE = 0, 1, 2, 3
NEXT_FOUND = "Write the email draft for this contact."
NEXT_NO_ADDRESS = ("No sendable address. Use LinkedIn if enabled, else run outreach skip <target> "
                   "--reason no_address.")
NEXT_PENDING = "Call enrich find again for this contact."
NEXT_UNAVAILABLE = "Skip the email route for this target now."
DEFINITIVE = ("hit", "miss", "invalid", "unexpected_phone", "in_progress")


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)


def _caller(caller) -> tuple:
    """(class, created_by) from a cli Caller or a plain string."""
    if isinstance(caller, str):
        if caller in ("human", "system"):
            return caller, caller
        return "agent", caller
    cls = getattr(caller, "cls", "system")
    if cls == "agent":
        return "agent", getattr(caller, "agent_id", None) or "agent"
    if cls == "human":
        return "human", "human"
    return "system", "system"


def _out(code: str, data: dict, message: str, next_: str, retry: int | None = None) -> dict:
    return {"code": code, "data": data, "message": message, "next": next_, "retry_after_s": retry}


# ---------------------------------------------------------------- stops
def check_stops(conn, s: dict) -> None:
    core_breakers.check_breakers(conn, ["global"])        # E_PAUSED (state/PAUSED) or E_BREAKER_OPEN (global)
    if not s.get("enabled"):
        raise Denied("E_CHANNEL_DISABLED", "the email finder is off (enrich.enabled is false)")
    if not s.get("_email_outreach"):
        raise Denied("E_CHANNEL_DISABLED", "email outreach is off (channels.email_outreach.enabled)")
    for scope in ("pause:enrich", "enrich", "pause:gmail", "gmail", "gmail.cold"):
        row = budget.breaker_row(conn, scope)
        if row is not None:
            retry = None
            if not row["requires_human"] and row["auto_close_at"]:
                retry = max(1, int((parse_ts(row["auto_close_at"]) - utcnow()).total_seconds()))
            raise Denied("E_ENRICH_UNAVAILABLE", "%s is stopped (%s)" % (scope, row["reason_code"] or "open"),
                         retry_after=retry, data={"reason": "breaker", "scope": scope})


# ---------------------------------------------------------------- helpers
def _calls(conn, request_id: int) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM enrich_calls WHERE request_id = ? ORDER BY id",
                                          (request_id,))]


def _tried(calls: list) -> list:
    out = []
    for r in calls:
        item = {"provider": r["provider"], "outcome": "rejected" if r["grade_hint"] == "rejected" else r["outcome"]}
        if r["op"] == "verify":
            item["op"] = "verify"
        out.append(item)
    return out


def _credits(calls: list) -> float:
    return round(sum(float(r["credits_charged"] or 0) for r in calls), 2)


def _finders_seen(calls: list) -> int:
    return sum(1 for r in calls if r["op"] in providers.FINDER_OPS and r["outcome"] != "network_before_send")


def _verifier_used(calls: list) -> bool:
    return any(r["op"] == "verify" and r["outcome"] != "network_before_send" for r in calls)


def _evidence(urls) -> list:
    out = []
    provider_hosts = set()
    for a in providers.REGISTRY.values():
        provider_hosts |= set(a.HOSTS)
    from urllib.parse import urlsplit
    for u in urls or []:
        host = (urlsplit(u).hostname or "").lower()
        if not u.startswith("https://") or providers.is_linkedin_url(u):
            continue
        if host in provider_hosts or any(host.endswith(d) for d in ("hunter.io", "tomba.io", "prospeo.io")):
            continue
        if u not in out:
            out.append(u)
        if len(out) >= 3:
            break
    return out


def _load_urls(row) -> list:
    import json
    try:
        v = json.loads(row["source_urls_json"] or "[]")
        return [u for u in v if isinstance(u, str)]
    except (TypeError, ValueError):
        return []


def _renew(conn, req_uid: str, holder: str) -> None:
    with db.tx(conn):
        if not locks.renew(conn, cache.lock_name(req_uid), holder, cache.LOCK_TTL_S):
            if not locks.acquire(conn, cache.lock_name(req_uid), holder, cache.LOCK_TTL_S):
                raise Denied("E_LOCKED", "another process took over this lookup")


def _release(conn, req_uid: str, holder: str) -> None:
    try:
        if conn.in_transaction:
            return
        with db.tx(conn):
            locks.release(conn, cache.lock_name(req_uid), holder)
    except Exception:
        pass


def _update_request(conn, request_id: int, **cols) -> None:
    cols["updated_at"] = now()
    conn.execute("UPDATE enrich_requests SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in cols),
                 list(cols.values()) + [request_id])


def _get_key(conn, provider: str, tried: list):
    try:
        return keystore.get(provider)
    except keystore.KeystoreError as exc:
        def _note(c):
            # a refused key file (mode, owner, symlink) needs the owner: high; a locked keychain: normal
            enqueue_notification(c, "enrich_keystore:%s:%s" % (exc.kind, now()[:10]),
                                 "high" if exc.kind == "refused" else "normal", "alert",
                                 "Email finder: the key store could not be read (%s); providers are skipped until it "
                                 "is unlocked or fixed." % exc.kind)
        budget.in_tx(conn, _note)
        return None


# ---------------------------------------------------------------- data for the envelope
def request_data(conn, req: dict, *, cached: bool, extra_tried: list | None = None) -> dict:
    calls = _calls(conn, req["id"])
    res = None
    if req.get("result_call_id"):
        res = next((r for r in calls if r["id"] == req["result_call_id"]), None)
    sendable = bool(req.get("sendable"))
    verification = res["verification"] if res else None
    if res is not None and req.get("grade") == "B" and req.get("reason") == "verified_by_verifier":
        verification = "valid"
    return {
        "request_uid": req["request_uid"],
        "status": req["status"] if req["status"] in ("found", "not_found", "running") else "not_found",
        "cached": cached,
        "email": (res["email"] if res else req.get("_email")) if sendable else None,
        "grade": req.get("grade"),
        "sendable": sendable,
        "verification": verification,
        "confidence": res["confidence"] if res else None,
        "provider": res["provider"] if res else ("pattern" if req.get("result_source") == "pattern" else None),
        "source_url": res["source_url"] if res else req.get("_source_url"),
        "evidence_candidates": _evidence(_load_urls(res)) if res else [],
        "providers_tried": _tried(calls) + list(extra_tried or []),
        "credits_spent": _credits(calls),
        "verify_pending": bool(req.get("verify_pending")),
    }


def _finish_envelope(conn, req: dict, *, cached: bool, extra_tried: list | None = None) -> dict:
    data = request_data(conn, req, cached=cached, extra_tried=extra_tried)
    if data["status"] == "found" and data["sendable"]:
        msg = "Found a %s work address (grade %s)." % ("verified" if data["grade"] == "B" else "usable", data["grade"])
        return _out("OK", data, msg, NEXT_FOUND)
    if data["status"] == "found":
        return _out("OK", data, "Found an address that is not sendable (grade %s)." % (data["grade"] or "-"),
                    NEXT_NO_ADDRESS)
    return _out("OK", data, "No work address was found.", NEXT_NO_ADDRESS)


def _req(conn, request_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM enrich_requests WHERE id = ?", (request_id,)).fetchone())


# ---------------------------------------------------------------- the chain
class _Run:
    def __init__(self, conn, s, info, contact_row, req, *, cls, by, cycle_id, include_reserve, transport, clock,
                 deadline, holder):
        self.conn, self.s, self.info, self.c, self.req = conn, s, info, contact_row, req
        self.cls, self.by, self.cycle_id = cls, by, cycle_id
        self.include_reserve, self.transport, self.clock = include_reserve, transport, clock
        self.deadline, self.holder = deadline, holder
        self.skips: list = []
        self.retry_hints: list = []
        self.q = PersonQuery(first_name=info["first"], last_name=info["last"], full_name=info["full"],
                             domain=info["domain"], company_name=info.get("company_name"), li_handle=info["li_slug"])
        self.cdoms = verify.company_domains(conn, info["company_id"]) | {info["domain"]}
        from .. import keys as keys_mod
        self.freemail = set(keys_mod.freemail_domains())
        try:
            from .. import emailcheck
            self.freemail |= set(emailcheck.freemail_domains())
        except Exception:
            pass
        self.hosting = tuple(keys_mod.hosting_domains())

    # -- time
    def time_left(self) -> float:
        return self.deadline - self.clock.monotonic()

    def timeout_for(self, provider: str) -> float:
        if provider in settings.RESERVE_ITEMS and self.cls == "human":
            return float(RESERVE_TIMEOUT_S)
        return float(self.s["timeout_s"])

    def pending(self, step: int) -> dict:
        with db.tx(self.conn):
            _update_request(self.conn, self.req["id"], next_step=step,
                            finders_called=_finders_seen(_calls(self.conn, self.req["id"])))
        waits = [h for h in self.retry_hints if h is not None]
        retry = min(waits) if waits else 0
        req = _req(self.conn, self.req["id"])
        data = request_data(self.conn, req, cached=False, extra_tried=self.skips)
        data["status"] = "running"
        return _out("PENDING", data, "The lookup needs another call to finish.", NEXT_PENDING, retry)

    # -- skip rules shared by finders and verifiers
    def usable(self, provider: str, op: str):
        """(key, None) when the provider can be called for op now, else (None, skip_reason)."""
        if not settings.provider_enabled(self.s, provider):
            return None, "skipped_disabled"
        adapter = providers.get(provider)
        if op not in adapter.ops:
            return None, "skipped_input"
        key = _get_key(self.conn, provider, self.skips)
        if key is None:
            return None, "skipped_no_key"
        ok, reason, retry = budget.can_reserve(self.conn, provider, op, self.s)
        if not ok:
            self.retry_hints.append(retry)
            return None, reason
        return key, None

    def finder_op(self, provider: str) -> str | None:
        adapter = providers.get(provider)
        if self.q.first_name and self.q.last_name and "find_name_domain" in adapter.ops:
            return "find_name_domain"
        if self.q.li_handle and self.s.get("use_linkedin_identifier") and "find_linkedin" in adapter.ops:
            return "find_linkedin"
        return None

    def call(self, provider: str, op: str, arg, key) -> tuple:
        """(call_id, result) or (None, skip_reason)."""
        try:
            call_id = budget.try_reserve(self.conn, provider, op, self.req["id"], self.cycle_id, self.s)
        except Denied as d:
            if d.code == "E_CEILING":
                return None, (d.data or {}).get("reason") or "skipped_budget"
            raise
        adapter = providers.get(provider)
        timeout = self.timeout_for(provider)
        try:
            if op == "find_name_domain":
                res = adapter.find_by_name_domain(arg, key, timeout, self.transport)
            elif op == "find_linkedin":
                res = adapter.find_by_linkedin(arg, key, timeout, self.transport)
            else:
                res = adapter.verify(arg, key, timeout, self.transport)
        except (NotImplementedError, ValueError):
            res = EnrichResult(provider=provider, outcome="network_before_send", raw_status="build_error")
        return call_id, res

    # -- classification of one finder hit
    def classify(self, res: EnrichResult) -> tuple:
        """(grade or 'rejected', reason, name_ok)."""
        reject = verify.precheck_address(res.email, company_domains=self.cdoms, freemail=self.freemail,
                                         hosting=self.hosting, free_mail_flag=res.free_mail_flag,
                                         raw_status=res.raw_status)
        if reject is None:
            reject = verify.suppression(self.conn, res.email, self.info["company_id"])
        if reject:
            return "rejected", reject, False
        local = res.email.rsplit("@", 1)[0]
        name_ok = verify.name_plausible(local, self.q.first_name, self.q.last_name)
        return None, None, name_ok

    def agreement(self, res: EnrichResult) -> bool:
        for r in _calls(self.conn, self.req["id"]):
            if r["op"] in providers.FINDER_OPS and r["email"] == res.email and r["provider"] != res.provider \
                    and r["grade_hint"] in ("B", "C") and ("valid" in (r["verification"], res.verification)):
                return True
        return False

    # -- verifier step
    def run_verifier(self, email: str) -> tuple:
        """('done', result, call_id) | ('none', None, None) | ('used', None, None) | ('pending', None, None)."""
        calls = _calls(self.conn, self.req["id"])
        if _verifier_used(calls):
            return "used", None, None
        called = {r["provider"] for r in calls if r["op"] == "verify"}
        for v in self.s["verifiers"]:
            if v in called:
                continue
            key, why = self.usable(v, "verify")
            if key is None:
                self.skips.append({"provider": v, "outcome": why, "op": "verify"})
                continue
            if self.time_left() < self.timeout_for(v) + SAFETY_S:
                return "pending", None, None
            _renew(self.conn, self.req["request_uid"], self.holder)
            call_id, res = self.call(v, "verify", email, key)
            if call_id is None:
                self.skips.append({"provider": v, "outcome": res, "op": "verify"})
                continue
            if res.outcome == "in_progress":
                budget.settle(self.conn, call_id, res, cycle_id=self.cycle_id)
                if self.time_left() >= REPOLL_WAIT_S + self.timeout_for(v) + SAFETY_S:
                    self.clock.sleep(REPOLL_WAIT_S)
                    try:
                        res = providers.get(v).verify(email, key, self.timeout_for(v), self.transport)
                    except (NotImplementedError, ValueError):
                        res = res.with_(outcome="unknown")
                if res.outcome not in ("hit", "invalid"):
                    res = res.with_(outcome="unknown", verification="unknown")
            budget.settle(self.conn, call_id, res, keep_email=False, cycle_id=self.cycle_id)
            if res.outcome == "network_before_send":
                continue
            if res.outcome in ("hit", "invalid", "unknown"):
                return "done", res, call_id
            return "done", res.with_(verification="unknown"), call_id
        return "none", None, None

    # -- main loop
    def finders(self) -> dict:
        s = self.s
        finder_list = list(s["chain"])
        if self.include_reserve and self.cls == "human":
            finder_list += [p for p in s["reserve_chain"] if p not in finder_list]
        final = None   # (grade, reason, call_id, verify_pending)
        candidates: list = []   # (call row dict) C results kept for agreement and fallback
        for r in _calls(self.conn, self.req["id"]):
            if r["op"] in providers.FINDER_OPS and r["grade_hint"] == "C" and r["email"]:
                candidates.append(r)
        for p in finder_list:
            calls = _calls(self.conn, self.req["id"])
            if any(r["provider"] == p and r["op"] in providers.FINDER_OPS for r in calls):
                continue
            if _finders_seen(calls) >= int(s["max_finders_per_person"]):
                break
            op = self.finder_op(p)
            if op is None:
                self.skips.append({"provider": p, "outcome": "skipped_input"})
                continue
            key, why = self.usable(p, op)
            if key is None:
                self.skips.append({"provider": p, "outcome": why})
                continue
            if self.time_left() < self.timeout_for(p) + SAFETY_S:
                return {"pending": STEP_FINDERS}
            _renew(self.conn, self.req["request_uid"], self.holder)
            call_id, res = self.call(p, op, self.q, key)
            if call_id is None:
                self.skips.append({"provider": p, "outcome": res})
                continue
            if res.outcome != "hit" or not res.email:
                hint = "X" if res.outcome == "invalid" else None
                budget.settle(self.conn, call_id, res, grade_hint=hint, keep_email=res.outcome == "invalid",
                              cycle_id=self.cycle_id)
                continue
            g, reason, name_ok = self.classify(res)
            if g == "rejected":
                budget.settle(self.conn, call_id, res, grade_hint="rejected", reject_reason=reason, keep_email=False,
                              cycle_id=self.cycle_id)
                continue
            if verify.known_invalid(self.conn, res.email):
                # an earlier call judged this address invalid (finder status, or a verifier's invalid or
                # spamtrap verdict): X for good; step 8 goes on to find a different address (5.2)
                budget.settle(self.conn, call_id, res, grade_hint="X", reject_reason="known_invalid",
                              cycle_id=self.cycle_id)
                continue
            agree = self.agreement(res)
            g, reason = verify.grade(res, verifier=None, agreement=agree, pattern_match=False, name_ok=name_ok,
                                     min_confidence=int(s["min_confidence"]))
            budget.settle(self.conn, call_id, res, grade_hint=g, cycle_id=self.cycle_id)
            if g == "B":
                final = ("B", reason, call_id, 0)
                break
            if g == "X":
                continue
            if verify.needs_verifier(res, g, name_ok):
                status, v, _vid = self.run_verifier(res.email)
                if status == "pending":
                    with db.tx(self.conn):
                        _update_request(self.conn, self.req["id"], result_call_id=call_id)
                    return {"pending": STEP_VERIFY}
                if status == "none":
                    final = ("C", reason, call_id, 1)
                    break
                if status == "used":
                    final = ("C", reason, call_id, 0)
                    break
                g2, reason2 = verify.grade(res, verifier=v, agreement=agree, pattern_match=False, name_ok=name_ok,
                                           min_confidence=int(s["min_confidence"]))
                budget.set_grade(self.conn, call_id, g2)
                if g2 == "X":
                    continue
                final = (g2, reason2, call_id, 0)
                break
            row = next(r for r in _calls(self.conn, self.req["id"]) if r["id"] == call_id)
            row["_reason"] = reason
            candidates.append(row)
        candidates = [r for r in candidates if not verify.known_invalid(self.conn, r["email"])]
        if final is None and candidates:
            best = sorted(candidates, key=lambda r: (0 if r["verification"] == "valid" else 1,
                                                     -(r["confidence"] or 0), r["id"]))[0]
            final = ("C", best.get("_reason") or "low_confidence", best["id"], 0)
        return {"final": final}

    def verify_step(self) -> dict:
        """Resume at the verifier for the stored candidate (after PENDING or for verify_pending)."""
        req = _req(self.conn, self.req["id"])
        call_id = req["result_call_id"]
        row = self.conn.execute("SELECT * FROM enrich_calls WHERE id = ?", (call_id,)).fetchone() if call_id else None
        if row is None or not row["email"]:
            return {"final": None}
        if verify.known_invalid(self.conn, row["email"]):
            budget.set_grade(self.conn, call_id, "X")
            return {"final": ("X", "invalid", call_id, 0)}
        res = EnrichResult(email=row["email"], confidence=row["confidence"], verification=row["verification"] or "none",
                           provider=row["provider"], outcome="hit")
        local = row["email"].rsplit("@", 1)[0]
        name_ok = verify.name_plausible(local, self.q.first_name, self.q.last_name)
        status, v, _vid = self.run_verifier(row["email"])
        if status == "pending":
            return {"pending": STEP_VERIFY}
        if status in ("none", "used"):
            g, reason = verify.grade(res, verifier=None, agreement=False, pattern_match=False, name_ok=name_ok,
                                     min_confidence=int(self.s["min_confidence"]))
            return {"final": (g, reason, call_id, 1 if status == "none" else 0)}
        g2, reason2 = verify.grade(res, verifier=v, agreement=False, pattern_match=False, name_ok=name_ok,
                                   min_confidence=int(self.s["min_confidence"]))
        budget.set_grade(self.conn, call_id, g2)
        return {"final": (g2, reason2, call_id, 0)}


def _address_mx(run: _Run, email: str) -> bool | None:
    """MX of the found address's own domain. The pre-step checked only the company domain; a result at a
    company alias (another dom: of the group), a subdomain or a LinkedIn-lookup domain gets its own lookup
    (network, outside any transaction). True or False, or None when DNS did not answer (the address is then
    stored without the MX mark, so gate.reserve refuses it with E_NO_MX until `email verify` checks it)."""
    domain = email.rsplit("@", 1)[1].strip().lower()
    if domain == (run.info.get("domain") or "").strip().lower():
        return True
    try:
        return bool(deps.mx_for_domain(domain).get("mx_ok"))
    except Denied:
        return None


def _write_found(conn, s: dict, run: _Run, final: tuple) -> dict:
    g, reason, call_id, verify_pending = final
    req_id = run.req["id"]
    row = conn.execute("SELECT * FROM enrich_calls WHERE id = ?", (call_id,)).fetchone()
    email = row["email"]
    if g in ("B", "C") and verify.known_invalid(conn, email):
        g, reason, verify_pending = "X", "invalid", 0      # nothing outranks an invalid verdict (5.2)
        budget.set_grade(conn, call_id, "X")
    allowed = s["_grades_allowed"]
    sendable = g in ("B", "C") and g in allowed
    note = None
    mx_ok = None
    if sendable:
        mx_ok = _address_mx(run, email)
        if mx_ok is False:
            sendable, note = False, "no_mx"
    if g == "X":
        with db.tx(conn):
            conn.execute("UPDATE contacts SET email = NULL, email_grade = NULL, email_evidence_url = NULL, "
                         "email_mx_ok = NULL, email_source = NULL, email_enrich_call_id = NULL, updated_at = ? "
                         "WHERE email_source = 'provider' AND email_enrich_call_id = ?", (now(), call_id))
            _update_request(conn, req_id, status="not_found", grade="X", reason="invalid", sendable=0,
                            verify_pending=0, result_call_id=call_id, result_source="provider", next_step=STEP_DONE,
                            finished_at=now())
        return _req(conn, req_id)
    if sendable:
        try:
            with db.tx(conn):
                cid = people.survivor(conn, run.info["contact_id"])
                out = deps.set_address(conn, cid, email=email, grade=g, source="provider",
                                       evidence_url=row["source_url"], enrich_call_id=call_id, mx_ok=mx_ok)
                if out is None:
                    raise Denied("E_PRECONDITION", "contacts.set_address is not installed")
                cid = people.survivor(conn, cid)
                dnc = conn.execute("SELECT do_not_contact FROM contacts WHERE id = ?", (cid,)).fetchone()
                if dnc is not None and dnc[0]:
                    note = "excluded"
        except Denied as d:
            note = "excluded" if d.code in ("E_EXCLUDED", "E_CONTACT_DNC") else \
                ("set_address_missing" if d.code == "E_PRECONDITION" else d.code.lower()[:40])
        if note:
            sendable = False
    with db.tx(conn):
        cache.add_email_key(conn, req_id, email)
        _update_request(conn, req_id, status="found", grade=g, reason=(note or reason)[:40], sendable=1 if sendable else 0,
                        verify_pending=int(verify_pending), result_call_id=call_id, result_source="provider",
                        next_step=STEP_VERIFY if verify_pending else STEP_DONE, finished_at=now(),
                        finders_called=_finders_seen(_calls(conn, req_id)))
    return _req(conn, req_id)


def _pre_steps(conn, s: dict, run: _Run) -> dict | None:
    """MX and the published pattern (free, no provider). Returns an envelope when the request ends here."""
    info = run.info
    mx = deps.mx_for_domain(info["domain"])
    req_id = run.req["id"]
    if not mx.get("mx_ok"):
        with db.tx(conn):
            _update_request(conn, req_id, status="not_found", reason="no_mx", next_step=STEP_DONE, finished_at=now())
        raise Denied("E_NO_MX", "the company domain has no mail server", data={"request_uid": run.req["request_uid"]})
    pat = deps.pattern_evidence(conn, info["domain"])
    if pat and pat.get("pattern") and info["first"] and info["last"]:
        addr = deps.render_pattern(pat["pattern"], info["first"], info["last"])
        if addr:
            addr = addr.strip().lower()
            reject = verify.precheck_address(addr, company_domains=run.cdoms, freemail=run.freemail,
                                             hosting=run.hosting) or verify.suppression(conn, addr, info["company_id"])
            if not reject and verify.known_invalid(conn, addr):
                reject = "known_invalid"
            if not reject:
                urls = [u for u in (pat.get("evidence_urls") or []) if isinstance(u, str) and u.startswith("https://")]
                sendable = "B" in s["_grades_allowed"]
                note = None
                mx_ok = _address_mx(run, addr) if sendable else None
                if mx_ok is False:
                    sendable, note = False, "no_mx"
                if sendable:
                    try:
                        with db.tx(conn):
                            out = deps.set_address(conn, info["contact_id"], email=addr, grade="B", source="pattern",
                                                   evidence_url=urls[0] if urls else None, enrich_call_id=None,
                                                   mx_ok=mx_ok)
                            if out is None:
                                raise Denied("E_PRECONDITION", "contacts.set_address is not installed")
                    except Denied as d:
                        note = "set_address_missing" if d.code == "E_PRECONDITION" else d.code.lower()[:40]
                        sendable = False
                with db.tx(conn):
                    cache.add_email_key(conn, req_id, addr)
                    _update_request(conn, req_id, status="found", grade="B", reason=note or "pattern_evidence",
                                    result_source="pattern", sendable=1 if sendable else 0, next_step=STEP_DONE,
                                    finished_at=now())
                req = _req(conn, req_id)
                req["_email"] = addr
                req["_source_url"] = urls[0] if urls else None
                return _finish_envelope(conn, req, cached=False)
    with db.tx(conn):
        _update_request(conn, req_id, next_step=STEP_FINDERS)
    return None


def run_find(conn, contact_id: int, *, caller, cycle_id: str | None = None, include_reserve: bool = False,
             transport=None, clock=None, target: str | None = None) -> dict:
    """The whole lookup for one contact; manages its own transactions (see module doc)."""
    if conn.in_transaction:
        raise Denied("E_INTERNAL", "run_find manages its own transactions")
    clock = clock or RealClock()
    cls, by = _caller(caller)
    if include_reserve and cls != "human":
        raise Denied("E_HUMAN_ONLY", "--include-reserve spends one-time trial credits and needs the owner PIN")
    deadline = clock.monotonic() + (DEADLINE_HUMAN_S if cls == "human" else DEADLINE_AGENT_S)
    s = settings.load(conn)
    # 1 stops
    check_stops(conn, s)
    # 2 contact
    cid = people.survivor(conn, contact_id)
    c = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    if c["email"] and c["email_grade"] in s["_grades_allowed"] and not c["email_invalid"]:
        return _out("OK", {"status": "already_has_address", "cached": True, "grade": c["email_grade"],
                           "sendable": True, "email": c["email"], "providers_tried": [], "credits_spent": 0.0,
                           "verify_pending": False},
                    "The contact already has a sendable address.", NEXT_FOUND)
    # 3 eligibility (volume caps come later: a cached answer costs nothing)
    info = eligibility.check(conn, cid, target=target, cycle_id=cycle_id, s=s)
    q = PersonQuery(first_name=info["first"], last_name=info["last"], full_name=info["full"], domain=info["domain"],
                    company_name=info.get("company_name"), li_handle=info["li_slug"])
    hashes = cache.keys_for(conn, c, q)
    holder = "enrich:%d:%s" % (os.getpid(), secrets.token_hex(4))
    # 4 cache
    existing = cache.find(conn, [h for h, _ in hashes])
    if existing is not None:
        st = existing["status"]
        if st in ("not_found", "forgotten", "unavailable") or (st == "found" and not existing["verify_pending"]):
            env = _finish_envelope(conn, existing, cached=True)
            log_event(conn, "enrich_request", request_uid=existing["request_uid"], status=existing["status"],
                      cached=True)
            return env
        with db.tx(conn):
            name = cache.lock_name(existing["request_uid"])
            if not locks.acquire(conn, name, holder, cache.LOCK_TTL_S):
                raise Denied("E_LOCKED", "this lookup is running in another process")
        req = existing
        if st == "running" and not _calls(conn, req["id"]):
            try:
                eligibility.check_volume(conn, s, cycle_id)
            except Denied:
                _release(conn, req["request_uid"], holder)
                raise
    else:
        # 5 claim
        eligibility.check_volume(conn, s, cycle_id)
        with db.tx(conn):
            claimed = cache.claim(conn, cid, hashes, by, cycle_id, holder, company_id=info["company_id"])
        req = _req(conn, claimed["id"])
    run = _Run(conn, s, info, c, req, cls=cls, by=by, cycle_id=cycle_id, include_reserve=include_reserve,
               transport=transport, clock=clock, deadline=deadline, holder=holder)
    try:
        return _drive(conn, s, run)
    finally:
        _release(conn, req["request_uid"], holder)


def _drive(conn, s: dict, run: _Run) -> dict:
    req = _req(conn, run.req["id"])
    run.req = req
    if req["status"] == "found" and req["verify_pending"]:
        out = run.verify_step()
        if "pending" in out or out["final"] is None or out["final"][3]:
            return _finish_envelope(conn, _req(conn, req["id"]), cached=True, extra_tried=run.skips)
        done = _write_found(conn, s, run, out["final"])
        _log(conn, done)
        return _finish_envelope(conn, done, cached=False, extra_tried=run.skips)
    if req["next_step"] == STEP_PRE:
        try:
            env = _pre_steps(conn, s, run)
        except Denied as d:
            if d.code == "E_ENRICH_UNAVAILABLE" and not _calls(conn, req["id"]):
                _unavailable(conn, req["id"])
            raise
        if env is not None:
            _log(conn, _req(conn, req["id"]))
            return env
    if _req(conn, req["id"])["next_step"] == STEP_VERIFY:
        out = run.verify_step()
        if "pending" in out:
            return run.pending(STEP_VERIFY)
        if out["final"] is not None and out["final"][0] != "X":
            done = _write_found(conn, s, run, out["final"])
            _log(conn, done)
            return _finish_envelope(conn, done, cached=False, extra_tried=run.skips)
        with db.tx(conn):
            _update_request(conn, req["id"], next_step=STEP_FINDERS)
    out = run.finders()
    if "pending" in out:
        return run.pending(out["pending"])
    final = out["final"]
    calls = _calls(conn, req["id"])
    if final is not None:
        done = _write_found(conn, s, run, final)
        _log(conn, done)
        return _finish_envelope(conn, done, cached=False, extra_tried=run.skips)
    seen = [r for r in calls if r["outcome"] != "network_before_send"]
    if seen:
        with db.tx(conn):
            reason = "no_hit" if any(r["outcome"] in DEFINITIVE for r in seen) else "provider_errors"
            _update_request(conn, req["id"], status="not_found", reason=reason, next_step=STEP_DONE,
                            finished_at=now(), finders_called=_finders_seen(calls))
        done = _req(conn, req["id"])
        _log(conn, done)
        return _finish_envelope(conn, done, cached=False, extra_tried=run.skips)
    _unavailable(conn, req["id"])
    waits = [h for h in run.retry_hints if h is not None]
    tried = _tried(calls) + run.skips
    _log(conn, _req(conn, req["id"]))
    raise Denied("E_ENRICH_UNAVAILABLE", "no email finder can be called now", retry_after=min(waits) if waits else None,
                 data={"reason": "no_provider", "providers_tried": tried})


def _unavailable(conn, request_id: int) -> None:
    """Nothing reached a provider: the request is not a lookup; its key hashes are released."""
    with db.tx(conn):
        cache.release_keys(conn, request_id)
        _update_request(conn, request_id, status="unavailable", next_step=STEP_DONE, finished_at=now())


def _log(conn, req: dict) -> None:
    calls = _calls(conn, req["id"])
    log_event(conn, "enrich_request", request_uid=req["request_uid"], status=req["status"], grade=req.get("grade"),
              providers=sorted({r["provider"] for r in calls}), credits=_credits(calls))
