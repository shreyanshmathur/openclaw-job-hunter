"""HTTP client and parsing helpers for the API discovery lane (design 1.3.1). Stdlib only.

- `HttpClient`: https only, GET and JSON POST (the Workday search is a read-only POST), at most one request per
  second per host (`sources.per_host_min_interval_ms`), ETag and Last-Modified validators, a size cap, and typed
  errors: `NotModified` (304), `NotFound` (404, 410, or a redirect to another host), `RateLimited` (429 or 999)
  and `HttpError` (anything else, status 0 for network errors). The first response's `Date` header is kept for
  the clock check.
- `html_to_text`, `to_date`, `clean` turn API payloads into the plain values of the ingest contract (12.1).

Tests never use HttpClient against the network: they pass a fake client with the same `get_json`, `get_text` and
`post_json` methods (tests/fakes/u2/http.py).
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import gzip
import html
import html.parser
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 12_000_000


class HttpError(Exception):
    def __init__(self, status: int, url: str, message: str = "", retry_after: int | None = None):
        super().__init__("HTTP %s for %s %s" % (status, _short(url), message))
        self.status = status
        self.url = url
        self.retry_after = retry_after


class NotModified(HttpError):
    pass


class NotFound(HttpError):
    pass


class RateLimited(HttpError):
    pass


def _short(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        return "%s%s" % (p.netloc, p.path[:80])
    except ValueError:
        return url[:100]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise HttpError(code, newurl, "redirect to a non-https URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    def __init__(self, user_agent: str, min_interval_ms: int = 1000, timeout: float = 30.0,
                 max_bytes: int = MAX_BYTES, sleep=time.sleep, clock=time.monotonic, opener=None):
        self.user_agent = user_agent
        self.min_interval = max(1.0, (min_interval_ms or 1000) / 1000.0)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.sleep = sleep
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self._last: dict[str, float] = {}
        self.first_date: str | None = None
        self.requests = 0
        self.validators: tuple[str | None, str | None] = (None, None)

    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()

    def request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None,
                etag: str | None = None, last_modified: str | None = None, same_host: bool = True):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise HttpError(0, str(url), "only https URLs are fetched")
        host = urllib.parse.urlsplit(url).netloc.lower()
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
                "Accept-Encoding": "gzip"}
        if etag:
            hdrs["If-None-Match"] = etag
        if last_modified:
            hdrs["If-Modified-Since"] = last_modified
        hdrs.update(headers or {})
        self._throttle(host)
        self.requests += 1
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            self._note_date(exc.headers)
            ra = _retry_after(exc.headers)
            if exc.code == 304:
                raise NotModified(304, url)
            if exc.code in (404, 410):
                raise NotFound(exc.code, url)
            if exc.code in (429, 999):
                raise RateLimited(exc.code, url, retry_after=ra)
            raise HttpError(exc.code, url)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise HttpError(0, url, str(exc)[:200])
        with resp:
            self._note_date(resp.headers)
            final = resp.geturl() or url
            if same_host and urllib.parse.urlsplit(final).netloc.lower() != host:
                raise NotFound(resp.status, url, "redirected to %s" % _short(final))
            raw = resp.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise HttpError(resp.status, url, "response larger than %d bytes" % self.max_bytes)
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            self.validators = (resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
            return resp.status, raw

    def _note_date(self, headers) -> None:
        if self.first_date is None and headers is not None and headers.get("Date"):
            self.first_date = headers.get("Date")

    def get_text(self, url: str, **kw) -> str:
        _, raw = self.request("GET", url, **kw)
        return raw.decode("utf-8", "replace")

    def get_json(self, url: str, **kw):
        _, raw = self.request("GET", url, **kw)
        return _json(raw, url)

    def post_json(self, url: str, obj, **kw):
        headers = dict(kw.pop("headers", None) or {})
        headers["Content-Type"] = "application/json"
        _, raw = self.request("POST", url, data=json.dumps(obj).encode("utf-8"), headers=headers, **kw)
        return _json(raw, url)


def _json(raw: bytes, url: str):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HttpError(200, url, "response is not JSON")


def _retry_after(headers) -> int | None:
    try:
        v = headers.get("Retry-After") if headers is not None else None
        return int(v) if v and str(v).isdigit() else None
    except (TypeError, ValueError):
        return None


def date_skew_seconds(date_header: str | None, now: _dt.datetime) -> int | None:
    """|server Date - now| in seconds, or None when the header is missing or unparsable."""
    if not date_header:
        return None
    try:
        d = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return int(abs((d - now).total_seconds()))


# ---------------------------------------------------------------- text
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
          "header", "footer", "table", "blockquote", "pre", "hr", "dd", "dt"}


class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(s: str | None, unescape_first: bool = False) -> str:
    """Plain text of an HTML fragment: block tags become line breaks, scripts and styles are dropped, entities
    decoded, spaces collapsed. unescape_first handles Greenhouse's entity-escaped `content`."""
    if not s:
        return ""
    if unescape_first:
        s = html.unescape(s)
    p = _Text()
    try:
        p.feed(s)
        p.close()
    except Exception:
        return clean(re.sub(r"<[^>]+>", " ", s))
    text = "".join(p.parts).replace("\r", "\n").replace("\u00a0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def clean(s) -> str | None:
    """One-line text or None."""
    if s is None:
        return None
    s = " ".join(html.unescape(str(s)).split())
    return s or None


_REL_RE = re.compile(r"(?:posted\s+)?(today|yesterday|just now|(\d+)\+?\s+(day|hour|minute)s?\s+ago)", re.I)


def to_date(v, now: _dt.datetime | None = None) -> str | None:
    """YYYY-MM-DD from ISO strings, epoch seconds or milliseconds, RFC 2822 dates, or Workday's
    "Posted 3 Days Ago"; None when unknown."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        ts = float(v) / (1000.0 if v > 1e11 else 1.0)
        try:
            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    if s.isdigit():
        return to_date(int(s), now)
    m = _REL_RE.search(s)
    if m:
        from .. import canon
        base = now or canon.utcnow()
        word = m.group(1).lower()
        if word in ("today", "just now") or (m.group(3) or "").lower() in ("hour", "minute"):
            days = 0
        elif word == "yesterday":
            days = 1
        else:
            days = int(m.group(2))
        return (base - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            return d.strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        pass
    return None


def emails_in(text: str | None) -> list[str]:
    if not text:
        return []
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", text)
    out = []
    for e in found:
        e = e.lower().rstrip(".")
        if e not in out:
            out.append(e)
    return out


def base_item(**kw) -> dict:
    """A 12.1 job item with every optional key present (None when unknown)."""
    item = {"source_url": None, "apply_url": None, "redirect_urls": [], "company": None, "company_domain": None,
            "title": None, "location": None, "work_mode": None, "remote_scope": None, "employment_type": None,
            "posted_at": None, "years": None, "salary": None, "apply_route_hint": None, "apply_email": None,
            "jd_text": None, "hiring_team": [], "native_ids": {}}
    item.update({k: v for k, v in kw.items() if v is not None or k in item})
    return item


def salary(min_v=None, max_v=None, currency=None, period=None) -> dict | None:
    def num(x):
        if x is None or isinstance(x, bool):
            return None
        try:
            f = float(x)
        except (TypeError, ValueError):
            return None
        return f if f >= 0 else None
    lo, hi = num(min_v), num(max_v)
    if lo is None and hi is None:
        return None
    cur = str(currency).upper() if isinstance(currency, str) and re.match(r"^[A-Za-z]{3}$", currency) else None
    per = None
    p = str(period or "").lower()
    for key, name in (("hour", "hour"), ("day", "day"), ("week", "week"), ("month", "month"), ("year", "year"),
                      ("annual", "year"), ("yr", "year")):
        if key in p:
            per = name
            break
    return {"min": lo, "max": hi, "currency": cur, "period": per}


def work_mode_of(v) -> str | None:
    """remote, hybrid or onsite from the many spellings APIs use; None when unknown."""
    s = re.sub(r"[^a-z]", "", str(v or "").lower())
    if not s:
        return None
    if "hybrid" in s:
        return "hybrid"
    if "remote" in s or s in ("wfh", "anywhere"):
        return "remote"
    if s in ("onsite", "office", "inoffice", "inperson") or "onsite" in s:
        return "onsite"
    return None


def last_segment(url: str | None) -> str | None:
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    seg = path.rsplit("/", 1)[-1] if path else ""
    return seg or None
