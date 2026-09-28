"""A fake of the Job Hunter Apps Script web app (sheets/Code.gs) on http.server, for tests only.

It speaks the same JSON protocol (design 7.4): secret and schema checks, ping, configure, format, setup and
sync with ops, deletes, ack_edits and render. It keeps an in-memory model of the table tabs with the same
upsert rule as Code.gs (editable columns are written only when a row is created) and a list of pending
human edits. Like Apps Script, a POST is answered with a 302 to a one-time URL that must be fetched with GET.

It also checks every row against the column contract of sheets_labels (known keys, value types, choice
values) and records problems in `violations`, so the row builders are tested against what Code.gs expects.

Modes: `html_login` answers with a Google sign-in page; `fail_on` maps a request number to an error
answer; `too_large_over` answers too_large when a request has more ops than that.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tests  # noqa: F401
from jobhunter import sheets_labels as L

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")


class FakeSheet:
    def __init__(self, secret: str):
        self.secret = secret
        self.schema_version = L.SCHEMA_VERSION
        self.tz = "Etc/UTC"
        self.tabs: dict[str, dict[str, dict]] = {k: {} for k in L.TABLE_TABS}
        self.edits: list[dict] = []
        self.render: dict = {}
        self.requests: list[dict] = []
        self.violations: list[str] = []
        self.formatted = 0
        self.html_login = False
        self.fail_on: dict[int, dict] = {}
        self.too_large_over: int | None = None
        self.lock = threading.Lock()

    # helpers for tests
    def human_edit(self, tab: str, row_id: str, col: str, value: str) -> str:
        eid = str(uuid.uuid4())
        with self.lock:
            if row_id in self.tabs[tab]:
                self.tabs[tab][row_id][col] = value
            self.edits.append({"edit_id": eid, "tab": tab, "row_id": row_id, "col": col, "value": value,
                               "at": "2026-09-27T05:18:03.000Z"})
        return eid

    def sync_requests(self) -> list[dict]:
        return [r for r in self.requests if r.get("action") == "sync"]

    # protocol
    def handle(self, req) -> dict:
        with self.lock:
            self.requests.append(req if isinstance(req, dict) else {"_raw": req})
            n = len(self.requests)
            if n in self.fail_on:
                return self.fail_on[n]
            if not isinstance(req, dict):
                return {"ok": False, "error": "bad_json"}
            if req.get("secret") != self.secret:
                return {"ok": False, "error": "bad_secret"}
            if req.get("schema_version") != self.schema_version:
                return {"ok": False, "error": "schema_mismatch", "expected": self.schema_version}
            action = req.get("action")
            if action == "ping":
                return {"ok": True, "app": "openclaw-job-hunter", "schema_version": self.schema_version,
                        "tz": self.tz, "tabs": list(L.TAB_ORDER)}
            if action == "configure":
                if req.get("timezone"):
                    self.tz = req["timezone"]
                return {"ok": True, "tz": self.tz}
            if action in ("format", "setup"):
                self.formatted += 1
                return {"ok": True}
            if action == "sync":
                return self._sync(req)
            return {"ok": False, "error": "unknown_action"}

    def _sync(self, req: dict) -> dict:
        ops = req.get("ops") or []
        if self.too_large_over is not None and len(ops) > self.too_large_over:
            return {"ok": False, "error": "too_large"}
        want = set(req.get("ack_edits") or [])
        self.edits = [e for e in self.edits if e["edit_id"] not in want]
        deleted = 0
        for d in req.get("deletes") or []:
            if d.get("tab") in self.tabs and d.get("id") in self.tabs[d["tab"]]:
                del self.tabs[d["tab"]][d["id"]]
                deleted += 1
        created = updated = 0
        errors = []
        for op in ops:
            tab = op.get("tab")
            if tab not in self.tabs:
                errors.append({"tab": tab, "id": op.get("id"), "error": "unknown_tab"})
                continue
            rid, row = op.get("id"), op.get("row")
            if not isinstance(rid, str) or not rid or not isinstance(row, dict):
                errors.append({"tab": tab, "id": rid, "error": "bad_op"})
                continue
            self._check_row(tab, rid, row)
            cols = {c[0]: c for c in L.columns(tab)}
            if rid in self.tabs[tab]:
                cur = self.tabs[tab][rid]
                for k, v in row.items():
                    if k in cols and not cols[k][3] and k != "id":
                        cur[k] = v
                updated += 1
            else:
                self.tabs[tab][rid] = {k: v for k, v in row.items() if k in cols}
                created += 1
        if req.get("render"):
            self.render = req["render"]
            self._check_render(req["render"])
        return {"ok": True, "batch_id": req.get("batch_id"), "created": created, "updated": updated,
                "deleted": deleted, "errors": errors, "edits": list(self.edits)}

    def _check_row(self, tab: str, rid: str, row: dict) -> None:
        cols = {c[0]: c for c in L.columns(tab)}
        for k, v in row.items():
            where = "%s/%s/%s" % (tab, rid, k)
            if k not in cols or k == "id":
                self.violations.append("%s: unknown column" % where)
                continue
            typ, choices = cols[k][2], cols[k][4]
            if v is None:
                continue
            if typ == "datetime" and not (isinstance(v, str) and _TS_RE.match(v)):
                self.violations.append("%s: datetime %r" % (where, v))
            elif typ == "date" and not (isinstance(v, str) and _DATE_RE.match(v)):
                self.violations.append("%s: date %r" % (where, v))
            elif typ in ("int", "score", "num2") and (isinstance(v, bool) or not isinstance(v, (int, float))):
                self.violations.append("%s: number %r" % (where, v))
            elif typ == "link" and not (isinstance(v, dict) and isinstance(v.get("text"), str)
                                        and str(v.get("url", "")).startswith(("https://", "http://"))):
                self.violations.append("%s: link %r" % (where, v))
            elif typ == "status" and (not isinstance(v, str) or (v and L.status_group(v) is None)):
                self.violations.append("%s: status label %r has no color group" % (where, v))
            elif typ == "choice" and (not isinstance(v, str) or (v and v not in (choices or ()))):
                self.violations.append("%s: choice %r" % (where, v))
            elif typ in ("text", "long") and not isinstance(v, str):
                self.violations.append("%s: text %r" % (where, v))
        missing = [c for c in cols if c not in row and c != "id"]
        if missing:
            self.violations.append("%s/%s: missing columns %s" % (tab, rid, missing))

    def _check_render(self, render: dict) -> None:
        d = render.get("dashboard") or {}
        for key in ("agent_state", "undelivered_high", "headline", "activity", "funnel", "limits", "safety",
                    "attention", "skip_reasons", "trend"):
            if key not in d:
                self.violations.append("render.dashboard missing %s" % key)
        # [Setting, Value, Used today, What it means] plus an optional style (renderSettings_ in Code.gs)
        for row in render.get("settings") or []:
            if not (isinstance(row, list) and len(row) in (4, 5) and all(isinstance(x, str) for x in row)
                    and (len(row) == 4 or row[4] in SETTINGS_STYLES)):
                self.violations.append("render.settings row %r" % (row,))
        if "agent_state" not in (render.get("start") or {}):
            self.violations.append("render.start missing agent_state")


SETTINGS_STYLES = ("section", "good", "wait", "bad", "muted", "info")
LOGIN_PAGE = b"<!DOCTYPE html><html><head><title>Sign in to Google Accounts</title></head><body>Sign in</body></html>"


class FakeWebApp:
    """Start with `with FakeWebApp(secret) as app:`; `app.url` ends in /exec."""

    def __init__(self, secret: str):
        self.sheet = FakeSheet(secret)
        self._results: dict[str, bytes] = {}
        self._server = None
        self._thread = None
        self.url = ""

    def __enter__(self) -> "FakeWebApp":
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if app.sheet.html_login:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(LOGIN_PAGE)))
                    self.end_headers()
                    self.wfile.write(LOGIN_PAGE)
                    return
                try:
                    req = json.loads(raw.decode("utf-8"))
                except ValueError:
                    req = None
                out = app.sheet.handle(req) if req is not None else {"ok": False, "error": "bad_json"}
                token = uuid.uuid4().hex
                app._results[token] = json.dumps(out).encode("utf-8")
                self.send_response(302)
                self.send_header("Location", "%s/echo?t=%s" % (app.base, token))
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                token = self.path.split("t=", 1)[1] if "t=" in self.path else ""
                body = app._results.pop(token, None)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self._server.server_address[1]
        self.url = self.base + "/macros/s/fake/exec"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
