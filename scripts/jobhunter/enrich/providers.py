"""Provider interface, normalized result and shared parsing rules (U10, ENRICH-SPEC section 3).

Every adapter builds an `HttpRequest` and parses a response with pure functions (no network, no key), so
all of it is tested offline with recorded fixture responses. The three call methods only glue
`transport.request(build_request(...), key, timeout_s)` to `parse(...)`.

What a response may leave behind is fixed here: `parse` reads named paths only (STORED_FIELDS) and never
copies a provider object. Phone numbers, personal addresses, positions, LinkedIn URLs and every other
profile field are never read, stored or logged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

VERSION = "1.0"
USER_AGENT = "openclaw-job-hunter/%s (personal job search; email finder)" % VERSION

OPS = ("find_name_domain", "find_linkedin", "verify")
FINDER_OPS = ("find_name_domain", "find_linkedin")
VERIFICATIONS = ("valid", "accept_all", "unknown", "invalid", "none")
OUTCOMES = ("hit", "miss", "invalid", "auth_failed", "quota_exhausted", "rate_limited", "server_error",
            "timeout_after_send", "network_before_send", "bad_response", "in_progress", "unexpected_phone")
# outcomes after which the provider may have charged: settled at the reserved maximum (fail closed)
CHARGE_MAX_OUTCOMES = frozenset({"timeout_after_send", "server_error", "bad_response", "unknown"})
# outcomes that never charge (the providers document no charge, and nothing was found)
CHARGE_ZERO_OUTCOMES = frozenset({"miss", "network_before_send", "auth_failed", "quota_exhausted", "rate_limited"})

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,}$")
MAX_SOURCE_URLS = 5
LINKEDIN_HOST_RE = re.compile(r"(^|\.)(linkedin\.com|lnkd\.in)$")

# Local parts that name a function, not a person (5.1). Role inboxes need grade A published evidence.
ROLE_LOCALS = frozenset({
    "info", "contact", "hello", "careers", "jobs", "job", "hr", "recruiting", "recruitment", "talent", "hiring",
    "team", "admin", "support", "sales", "office", "mail", "enquiries", "inquiries", "noreply", "no-reply", "press",
    "media", "billing", "accounts",
})

# The only response paths any adapter reads (documentation and test contract; section 3.2).
STORED_FIELDS = {
    "prospeo": ("error", "error_code", "email.email", "email.status", "mobile (presence only)"),
    "hunter": ("data.email", "data.score", "data.verification.status", "data.sources[].uri",
               "data.sources[].still_on_page", "data.status", "data.webmail", "data.disposable", "errors[].id",
               "errors[].details", "meta.params (never)"),
    "tomba": ("data.email", "data.score", "data.accept_all", "data.verification.status", "data.sources[].uri",
              "errors.message"),
    "getprospect": ("email", "status", "free_email"),
    "anymailfinder": ("email_status", "valid_email", "email", "credits_charged"),
    "findymail": ("contact.email", "email", "verified"),
    "apollo": ("person.email", "person.email_status", "phone fields (presence only)"),
    "zerobounce": ("status", "sub_status", "error"),
}

# Words in an error body that mean "plan or credit limit", not a bad key (403 handling, section 20).
QUOTA_WORDS_RE = re.compile(r"(credit|quota|limit|plan|upgrade|usage|exceeded|exhausted|insufficient)", re.I)


@dataclass(frozen=True)
class PersonQuery:
    first_name: str | None          # NFKC, trimmed; honorifics removed (same rules as keys.person_keys)
    last_name: str | None
    full_name: str | None
    domain: str                     # registrable domain of the contact's company (never free-mail or hosting)
    company_name: str | None = None  # display name, only where a provider needs it alongside the domain
    li_handle: str | None = None    # vanity slug from contacts.li_slug; None unless enrich.use_linkedin_identifier


@dataclass(frozen=True)
class HttpRequest:
    method: str                     # GET | POST
    host: str                       # must be in the adapter's HOSTS and in transport.ALLOWED_HOSTS
    path: str
    query: tuple = ()               # ((name, value), ...); never key text
    headers: tuple = ()             # ((name, value), ...); secret headers are filled by the transport only
    # ((header_name, key_field, prefix), ...): the transport sets header_name = prefix + key[key_field]
    secret_headers: tuple = ()
    json_body: dict | None = None
    form_body: tuple | None = None  # ((name, value), ...)
    # ((body_field, key_field), ...): form body fields that receive key text (ZeroBounce only)
    secret_body_fields: tuple = ()


@dataclass(frozen=True)
class EnrichResult:
    # public part (the normalized result the rest of the system sees)
    email: str | None = None        # lowercased and trimmed; None on a miss
    confidence: int | None = None   # 0..100 when the provider gives a score (Hunter, Tomba), else None
    verification: str = "none"      # 'valid' | 'accept_all' | 'unknown' | 'invalid' | 'none'
    provider: str = ""
    source_url: str | None = None   # first https evidence URL, never a LinkedIn URL
    # bookkeeping part (never shown to the model except outcome)
    outcome: str = "miss"
    source_urls: tuple = ()         # at most 5, https only, LinkedIn hosts dropped
    credits_charged: float = 0.0    # documented charge for this outcome (section 8.2)
    provider_reported_charge: float | None = None
    http_status: int | None = None
    retry_after_s: int | None = None
    raw_status: str | None = None   # provider's own status string, <= 40 chars
    free_mail_flag: bool = False
    reported_remaining: float | None = None

    def with_(self, **kw) -> "EnrichResult":
        return replace(self, **kw)


# ---------------------------------------------------------------- parsing helpers (pure)
def load_json(body: bytes | None) -> Any:
    """Decoded JSON body, or None when it is empty or not JSON."""
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def get_path(obj: Any, path: str) -> Any:
    """obj['a']['b'] for 'a.b'; None when any step is missing or not an object."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def first_path(obj: Any, *paths: str) -> Any:
    for p in paths:
        v = get_path(obj, p)
        if v is not None:
            return v
    return None


def clean_email(value: Any) -> str | None:
    """Lowercased, trimmed address text (<= 254 chars) or None. Syntax is judged later (verify.precheck)."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or len(v) > 254 or any(c.isspace() for c in v):
        return None
    return v


def valid_syntax(email: str | None) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email))


def clean_status(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    s = str(value).strip()
    return s[:40] if s else None


def clean_score(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 100 else None


def is_linkedin_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    return bool(LINKEDIN_HOST_RE.search(host))


def clean_urls(values) -> tuple:
    """https URLs only, LinkedIn hosts dropped, duplicates removed, at most MAX_SOURCE_URLS."""
    out: list[str] = []
    for v in values or ():
        if not isinstance(v, str):
            continue
        u = v.strip()
        if not u.startswith("https://") or len(u) > 500 or any(c.isspace() for c in u):
            continue
        if is_linkedin_url(u) or u in out:
            continue
        out.append(u)
        if len(out) >= MAX_SOURCE_URLS:
            break
    return tuple(out)


def non_empty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, tuple, dict)):
        if isinstance(value, dict):
            return any(non_empty(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(non_empty(v) for v in value)
        return bool(value.strip())
    return True


def retry_after(headers: dict | None) -> int | None:
    """Retry-After in whole seconds (delta form only; an HTTP date is treated as one hour)."""
    if not headers:
        return None
    raw = None
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            raw = v
            break
    if raw is None:
        return None
    s = str(raw).strip()
    if s.isdigit():
        return min(int(s), 7 * 86400)
    return 3600 if s else None


def text_of(body: bytes | None) -> str:
    if not body:
        return ""
    return body[:2000].decode("utf-8", "replace")


def common_status(provider: str, status: int, headers: dict, body: bytes | None) -> EnrichResult | None:
    """Outcome for non-2xx answers every adapter shares; None for 2xx (the adapter decides)."""
    if 200 <= status < 300:
        return None
    base = EnrichResult(provider=provider, http_status=status)
    if 300 <= status < 400:
        return base.with_(outcome="bad_response", raw_status="redirect")
    if status == 401:
        return base.with_(outcome="auth_failed")
    if status == 402:
        return base.with_(outcome="quota_exhausted")
    if status == 403:
        if QUOTA_WORDS_RE.search(text_of(body)):
            return base.with_(outcome="quota_exhausted")
        return base.with_(outcome="auth_failed")   # unknown 403 fails closed (human)
    if status == 429:
        return base.with_(outcome="rate_limited", retry_after_s=retry_after(headers))
    if status >= 500:
        return base.with_(outcome="server_error")
    return base.with_(outcome="bad_response")


# ---------------------------------------------------------------- adapter base
class BaseAdapter:
    """Shared behaviour. Subclasses set the class attributes and implement build_request and parse."""
    name = ""
    HOSTS: frozenset = frozenset()
    KEY_FIELDS: tuple = ("api_key",)
    ops: frozenset = frozenset()
    budget_period = "rolling_31d"
    max_charge: dict = {}
    # response paths whose non-empty value means a phone came back although every phone flag was off
    PHONE_FLAG_PATHS: tuple = ()

    def build_request(self, op: str, arg, timeout_s: float = 12.0) -> HttpRequest:
        raise NotImplementedError(op)

    def parse(self, op: str, status: int, headers: dict, body: bytes | None) -> EnrichResult:
        raise NotImplementedError(op)

    # -- helpers for subclasses
    def ua(self) -> tuple:
        return (("User-Agent", USER_AGENT), ("Accept", "application/json"))

    def result(self, **kw) -> EnrichResult:
        kw.setdefault("provider", self.name)
        return EnrichResult(**kw)

    def phone_present(self, data) -> bool:
        return any(non_empty(get_path(data, p)) for p in self.PHONE_FLAG_PATHS)

    def charge_for(self, op: str, r: EnrichResult) -> float:
        """Documented charge for a parsed outcome (section 8.2); adapters override the special cases."""
        if r.outcome == "hit" and r.email:
            return float(self.max_charge.get(op, 1))
        if r.outcome in CHARGE_MAX_OUTCOMES:
            return float(self.max_charge.get(op, 1))
        return 0.0

    def finish(self, op: str, r: EnrichResult) -> EnrichResult:
        return r.with_(credits_charged=self.charge_for(op, r))

    # -- the three call methods (thin)
    def _call(self, op: str, arg, key, timeout_s: float, transport) -> EnrichResult:
        if op not in self.ops:
            raise NotImplementedError("%s does not support %s" % (self.name, op))
        req = self.build_request(op, arg, timeout_s=timeout_s)
        if req.host not in self.HOSTS:
            raise ValueError("host not allowed for %s" % self.name)
        from . import transport as _transport
        tr = transport if transport is not None else _transport
        try:
            status, headers, body = tr.request(req, key, timeout_s)
        except _transport.TransportError as exc:
            return self.finish(op, transport_result(self.name, exc.kind))
        try:
            return self.parse(op, status, headers or {}, body)
        except Exception:   # a parser crash is a schema change, never a traceback with response data
            return self.finish(op, self.result(outcome="bad_response", http_status=status, raw_status="parse_error"))

    def find_by_name_domain(self, q: PersonQuery, key, timeout_s: float, transport=None) -> EnrichResult:
        return self._call("find_name_domain", q, key, timeout_s, transport)

    def find_by_linkedin(self, q: PersonQuery, key, timeout_s: float, transport=None) -> EnrichResult:
        return self._call("find_linkedin", q, key, timeout_s, transport)

    def verify(self, email: str, key, timeout_s: float, transport=None) -> EnrichResult:
        return self._call("verify", email, key, timeout_s, transport)


def transport_result(provider: str, kind: str) -> EnrichResult:
    """EnrichResult for a transport failure kind (transport.TransportError.kind)."""
    outcome = {
        "connect": "network_before_send",
        "tls": "network_before_send",
        "timeout_after_send": "timeout_after_send",
        "reset_after_send": "timeout_after_send",
        "oversize": "bad_response",
    }.get(kind, "timeout_after_send")   # unknown kinds fail closed (charged at max)
    return EnrichResult(provider=provider, outcome=outcome, raw_status=kind[:40])


LINKEDIN_PROFILE_PREFIX = "https://www.linkedin.com" + "/in/"


def linkedin_profile_url(handle: str) -> str:
    """The public profile URL of a stored vanity handle (sent only under the LinkedIn identifier rule)."""
    return LINKEDIN_PROFILE_PREFIX + handle


# ---------------------------------------------------------------- status normalization maps
STATUS_MAPS = {
    "prospeo": {"verified": "valid"},
    "hunter": {"valid": "valid", "accept_all": "accept_all", "unknown": "unknown", "invalid": "invalid",
               "webmail": "invalid", "disposable": "invalid"},
    "tomba": {"valid": "valid", "invalid": "invalid", "accept_all": "accept_all", "unknown": "unknown"},
    "getprospect": {"valid": "valid"},
    "anymailfinder": {"valid": "valid", "risky": "unknown", "blacklisted": "invalid"},
    "findymail": {"verified": "valid", "valid": "valid"},
    "apollo": {"verified": "valid"},
    "zerobounce": {"valid": "valid", "invalid": "invalid", "spamtrap": "invalid", "abuse": "invalid",
                   "do_not_mail": "invalid", "catch-all": "accept_all", "unknown": "unknown"},
}


def normalize_status(provider: str, raw: str | None) -> str:
    """Provider status string -> one of VERIFICATIONS. None -> 'none'; anything unmapped -> 'unknown'."""
    if raw is None or raw == "":
        return "none"
    return STATUS_MAPS.get(provider, {}).get(str(raw).strip().lower(), "unknown")


# ---------------------------------------------------------------- registry
REGISTRY: dict = {}


def _load_registry() -> None:
    from . import anymailfinder, apollo, findymail, getprospect, hunter, prospeo, tomba, zerobounce
    for mod in (prospeo, hunter, tomba, getprospect, anymailfinder, findymail, apollo, zerobounce):
        REGISTRY[mod.ADAPTER.name] = mod.ADAPTER


FINDER_NAMES = ("prospeo", "hunter", "tomba", "getprospect", "anymailfinder", "findymail", "apollo")
VERIFIER_NAMES = ("zerobounce", "hunter")
PROVIDER_NAMES = ("prospeo", "hunter", "tomba", "getprospect", "anymailfinder", "findymail", "apollo", "zerobounce")

_load_registry()


def get(name: str) -> BaseAdapter:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError("unknown provider %r" % name)


__all__ = ["PersonQuery", "HttpRequest", "EnrichResult", "BaseAdapter", "REGISTRY", "STORED_FIELDS", "ROLE_LOCALS",
           "normalize_status", "get"]
