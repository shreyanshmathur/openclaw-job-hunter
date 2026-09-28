"""HTTPS transport for provider calls (U10, ENRICH-SPEC 3.4). Python stdlib only (urllib.request).

- https only, and the host must be in ALLOWED_HOSTS (the union of the adapters' HOSTS); anything else
  raises before a socket opens.
- ssl.create_default_context(); redirects are refused, so a key is never replayed to another host (a 3xx
  comes back as its status and the adapter treats it as bad_response).
- The response body is read up to MAX_BODY bytes; a larger body is TransportError('oversize').
- Key text only goes into the headers named by HttpRequest.secret_headers or the form fields named by
  secret_body_fields. A query parameter that looks like a key name is refused.
- Every exception becomes TransportError(kind) with kind in connect (nothing was sent), tls,
  timeout_after_send, reset_after_send, oversize. The message never holds the URL, a header or a body.
- `opener` is injectable (tests pass a fake; the test suite also blocks sockets).
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

MAX_BODY = 256 * 1024
ALLOWED_HOSTS = frozenset({
    "api.prospeo.io", "api.hunter.io", "api.tomba.io", "api.getprospect.com", "api.anymailfinder.com",
    "app.findymail.com", "api.apollo.io", "api.zerobounce.net", "api-us.zerobounce.net", "api-eu.zerobounce.net",
})
KEYLIKE_QUERY_NAMES = frozenset({"api_key", "apikey", "key", "secret", "token", "access_token"})
KINDS = ("connect", "tls", "timeout_after_send", "reset_after_send", "oversize")


class TransportError(Exception):
    """Transport failure without any request or response data in it."""

    def __init__(self, kind: str):
        if kind not in KINDS:
            kind = "reset_after_send"
        super().__init__("provider transport error: %s" % kind)
        self.kind = kind

    def __reduce__(self):
        return (TransportError, (self.kind,))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None   # urllib then raises HTTPError with the 3xx status


def default_opener():
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())


def _secret_value(key, field: str) -> str:
    """Revealed key text for one field. `key` is a Secret or {field: Secret}."""
    if isinstance(key, dict):
        s = key.get(field)
    else:
        s = key
    if s is None or not hasattr(s, "reveal"):
        raise TransportError("connect")
    return s.reveal()


def build(req, key):
    """(url, data, headers) for a request; pure apart from revealing the key into headers or body."""
    if req.host not in ALLOWED_HOSTS:
        raise ValueError("host is not on the provider allowlist")
    if req.method not in ("GET", "POST"):
        raise ValueError("method must be GET or POST")
    if not req.path.startswith("/") or "?" in req.path or "#" in req.path:
        raise ValueError("bad request path")
    for name, _value in req.query:
        if name.lower() in KEYLIKE_QUERY_NAMES:
            raise ValueError("key-like query parameters are refused; keys go in headers")
    url = "https://%s%s" % (req.host, req.path)
    if req.query:
        url += "?" + urllib.parse.urlencode(list(req.query))
    headers = {}
    for name, value in req.headers:
        headers[name] = value
    for hname, field, prefix in req.secret_headers:
        headers[hname] = prefix + _secret_value(key, field)
    data = None
    if req.json_body is not None:
        data = json.dumps(req.json_body, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif req.form_body is not None or req.secret_body_fields:
        pairs = list(req.form_body or ())
        for bfield, field in req.secret_body_fields:
            pairs.append((bfield, _secret_value(key, field)))
        data = urllib.parse.urlencode(pairs).encode("ascii")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if req.method == "POST" and data is None:
        data = b""
    return url, data, headers


def _read(resp) -> bytes:
    try:
        body = resp.read(MAX_BODY + 1)
    except (socket.timeout, TimeoutError):
        raise TransportError("timeout_after_send") from None
    except ssl.SSLError:
        raise TransportError("tls") from None
    except (OSError, http.client.HTTPException):
        raise TransportError("reset_after_send") from None
    if body is None:
        body = b""
    if len(body) > MAX_BODY:
        raise TransportError("oversize")
    return body


def _headers(resp) -> dict:
    h = getattr(resp, "headers", None)
    if h is None and hasattr(resp, "info"):
        h = resp.info()
    out = {}
    try:
        for k, v in (h.items() if h is not None else []):
            out[str(k).lower()] = str(v)
    except Exception:
        pass
    return out


def _status(resp) -> int:
    code = getattr(resp, "status", None)
    if code is None:
        code = getattr(resp, "code", None)
    if code is None and hasattr(resp, "getcode"):
        code = resp.getcode()
    return int(code)


def request(req, key, timeout_s: float, opener=None) -> tuple:
    """(status, headers with lower-case names, body bytes). Non-2xx answers are returned, not raised."""
    if req.host not in ALLOWED_HOSTS:
        raise ValueError("host is not on the provider allowlist")
    url, data, headers = build(req, key)
    r = urllib.request.Request(url, data=data, headers=headers, method=req.method)
    op = opener if opener is not None else default_opener()
    try:
        try:
            resp = op.open(r, timeout=float(timeout_s))
        except urllib.error.HTTPError as err:   # 3xx (refused redirect), 4xx, 5xx: a real answer
            try:
                status = int(err.code)
                hdrs = _headers(err)
                body = _read(err) if err.fp is not None else b""
            finally:
                try:
                    err.close()
                except Exception:
                    pass
            return status, hdrs, body
        except urllib.error.URLError as err:    # raised while connecting or sending: nothing answered
            reason = getattr(err, "reason", None)
            if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
                raise TransportError("tls") from None
            raise TransportError("connect") from None
        except ssl.SSLError:
            raise TransportError("tls") from None
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout_after_send") from None
        except (OSError, http.client.HTTPException):
            raise TransportError("reset_after_send") from None
        try:
            status = _status(resp)
            hdrs = _headers(resp)
            body = _read(resp)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return status, hdrs, body
    except TransportError:
        raise
    except ValueError:
        raise
    except Exception:   # anything else: fail closed as "may have been sent", without details
        raise TransportError("reset_after_send") from None
