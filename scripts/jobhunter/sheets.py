"""Google Sheet sync through the person's bound Apps Script web app (design 7.1, 7.4).

The database is the source of truth; the Sheet is a mirror. `sync()` pushes changed rows per tab in chunks
of at most `sheets.max_ops_per_post` operations, sends queued row deletes, re-renders the Dashboard, Start
here and Limits and settings tabs, pulls the person's edits (Your decision, Your call, Outcome, Your notes),
applies each one once through the owning unit's function and acknowledges it in the next request.

Transport: urllib POST with a JSON body (the 256-bit secret travels in the body, never in the URL). Apps
Script answers with a 302 to a one-time googleusercontent URL, which urllib follows with a GET. An HTML
answer means the deployment is not "Anyone" (E_SHEET_ACCESS). Failures are stored in sheet_state.last_error
and retried by the next run; a failing sheet never blocks or fails a send.

Secrets live in private/secrets.json (mode 600) under `sheet_webapp_url` and `sheet_secret`; they are never
printed, logged, passed as arguments or returned in any envelope.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import os
import re
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import canon, db, paths
from . import sheets_labels as L
from . import sheets_rows as R
from . import status as S
from .errors import Denied

TIMEOUT_S = 60
MAX_OPS_HARD = 500
SECRETS_FILE = "secrets.json"
_SECRET_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")
_EDIT_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

FIX_ACCESS = ("The sheet answered with a Google sign-in page instead of data. In Apps Script open Deploy > "
              "Manage deployments, edit the web app and set Who has access to Anyone, then deploy again.")
FIX_SECRET = ("The sheet refused the connection secret. In the sheet open Job Hunter > Show connection secret and "
              "run ./jobhunter sheet connect again.")
FIX_SCHEMA = ("The script in your sheet is older than this version. Paste the new sheets/Code.gs into Apps Script, "
              "then Deploy > Manage deployments > edit > Version: New version > Deploy.")


# ---------------------------------------------------------------- secrets
def _secrets_path() -> str:
    return os.path.join(paths.private_dir(), SECRETS_FILE)


def load_secrets() -> dict:
    try:
        with open(_secrets_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_secrets(data: dict) -> None:
    """Atomic write with mode 600; other keys (the mail app password) are kept as they are."""
    d = paths.private_dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, _secrets_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.chmod(_secrets_path(), 0o600)


def save_sheet_secrets(url: str, secret: str) -> None:
    with db.file_lock():
        data = load_secrets()
        data["sheet_webapp_url"] = url
        data["sheet_secret"] = secret
        _write_secrets(data)


def is_connected() -> bool:
    s = load_secrets()
    return bool(s.get("sheet_webapp_url") and s.get("sheet_secret"))


def validate_url(url: str) -> str:
    """https Apps Script URL ending in /exec; plain http only for a loopback test server."""
    url = (url or "").strip()
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        raise Denied("E_VALIDATION", "that is not a web address")
    host = (p.hostname or "").lower()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if p.scheme == "https" and (host == "script.google.com" or host.endswith(".google.com")
                                or host.endswith(".googleusercontent.com")):
        if not p.path.rstrip("/").endswith("/exec"):
            raise Denied("E_VALIDATION", "the web app address must end in /exec (copy it from Deploy > "
                                         "Manage deployments)")
        return url
    if loopback and p.scheme in ("http", "https"):
        return url
    raise Denied("E_VALIDATION", "the web app address must start with https://script.google.com/ and end in /exec")


def validate_secret(secret: str) -> str:
    s = (secret or "").strip()
    if not _SECRET_RE.match(s):
        raise Denied("E_VALIDATION", "the connection secret is 64 letters and digits; copy it again from "
                                     "Job Hunter > Show connection secret")
    return s.lower()


# ---------------------------------------------------------------- transport
class Transport:
    """POST one JSON request to the web app and return the decoded JSON answer."""

    def __init__(self, url: str, timeout: float = TIMEOUT_S):
        self.url = url
        self.timeout = timeout

    def post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": L.CLIENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read(20 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise Denied("E_SHEET_ACCESS", FIX_ACCESS, data={"http_status": exc.code})
            raise Denied("E_SHEET_ERROR", "the sheet web app answered HTTP %d" % exc.code,
                         data={"http_status": exc.code})
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Denied("E_NETWORK", "could not reach the sheet web app: %s" % reason)
        text = raw.decode("utf-8", "replace").strip()
        if "html" in ctype.lower() or text[:1] == "<":
            raise Denied("E_SHEET_ACCESS", FIX_ACCESS, data={"content_type": ctype})
        try:
            data = json.loads(text)
        except ValueError:
            raise Denied("E_SHEET_ERROR", "the sheet web app did not answer with JSON")
        if not isinstance(data, dict):
            raise Denied("E_SHEET_ERROR", "the sheet web app answered with an unexpected shape")
        return data


def _check(resp: dict) -> dict:
    if resp.get("ok") is True:
        return resp
    err = str(resp.get("error") or "unknown")
    if err == "bad_secret":
        raise Denied("E_SHEET_ACCESS", FIX_SECRET, data={"error": err})
    if err == "schema_mismatch":
        raise Denied("E_SHEET_ERROR", FIX_SCHEMA, data={"error": err, "expected": resp.get("expected")})
    raise Denied("E_SHEET_ERROR", "the sheet web app reported %s" % err,
                 data={"error": err, "detail": L.clip(str(resp.get("detail") or ""), 300)})


def _transport(transport=None) -> Transport:
    if transport is not None:
        return transport
    s = load_secrets()
    if not (s.get("sheet_webapp_url") and s.get("sheet_secret")):
        raise Denied("E_PRECONDITION", "the Google Sheet is not connected; run ./jobhunter sheet connect")
    return Transport(s["sheet_webapp_url"])


def _secret() -> str:
    s = load_secrets().get("sheet_secret")
    if not s:
        raise Denied("E_PRECONDITION", "the Google Sheet is not connected; run ./jobhunter sheet connect")
    return s


def _request(action: str, secret: str, **extra) -> dict:
    req = {"secret": secret, "action": action, "schema_version": L.SCHEMA_VERSION, "client": L.CLIENT}
    req.update(extra)
    return req


def call(action: str, transport=None, secret: str | None = None, **extra) -> dict:
    t = _transport(transport)
    return _check(t.post(_request(action, secret or _secret(), **extra)))


def ping(transport=None, secret: str | None = None) -> dict:
    r = call("ping", transport, secret)
    return {"ok": True, "app": r.get("app"), "schema_version": r.get("schema_version"), "tz": r.get("tz"),
            "tabs": r.get("tabs")}


def configure(timezone: str, transport=None, secret: str | None = None) -> dict:
    if not _TZ_RE.match(timezone or ""):
        raise Denied("E_VALIDATION", "bad time zone name %r" % timezone)
    r = call("configure", transport, secret, timezone=timezone)
    return {"tz": r.get("tz")}


def format_sheet(transport=None) -> dict:
    call("format", transport)
    return {"formatted": True}


def connect(conn, url: str, secret: str, transport=None, config: dict | None = None) -> dict:
    """`sheet connect`: validate, ping with the given secret, store both, set the sheet's time zone to the
    person's, then run the first full sync."""
    url = validate_url(url)
    secret = validate_secret(secret)
    t = transport or Transport(url)
    info = ping(t, secret)
    if info.get("app") != "openclaw-job-hunter":
        raise Denied("E_SHEET_ERROR", "that web app is not the Job Hunter script; check the URL")
    save_sheet_secrets(url, secret)
    config = S.load_config() if config is None else config
    tz = S.tz_name(config)
    tzres = configure(tz, t, secret)
    result = sync(conn, full=True, transport=t)
    return {"ok": True, "app": info.get("app"), "schema_version": info.get("schema_version"),
            "tz": tzres.get("tz"), "sync": result}


# ---------------------------------------------------------------- sheet_state
def _state_get(conn, tab: str):
    return conn.execute("SELECT * FROM sheet_state WHERE tab = ?", (tab,)).fetchone()


def _state_set(conn, tab: str, **cols) -> None:
    conn.execute("INSERT INTO sheet_state (tab) VALUES (?) ON CONFLICT (tab) DO NOTHING", (tab,))
    if cols:
        sets = ", ".join("%s = ?" % k for k in cols)
        conn.execute("UPDATE sheet_state SET %s WHERE tab = ?" % sets, list(cols.values()) + [tab])


def _record_error(conn, tabs, message: str) -> None:
    msg = L.clip(message, 500)
    with db.tx(conn):
        for tab in list(tabs) + ["_all"]:
            _state_set(conn, tab, last_error=msg)


@contextlib.contextmanager
def _sync_lock():
    """One sync at a time (cron and a manual run); a second one returns NOTHING_TO_DO."""
    os.makedirs(paths.state_dir(), exist_ok=True)
    fd = os.open(os.path.join(paths.state_dir(), "sheet-sync.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- edits
EDIT_TABS = {"approvals": ("decision",), "jobs": ("your_call",), "skipped": ("your_call",),
             "applications": ("outcome", "notes"), "outreach": ("notes",), "followups": ("outcome",)}
_DEFER = object()


class Handlers:
    """The owning units' functions an edit calls (injectable for tests). Each runs inside our db.tx."""

    def _fn(self, module: str, name: str):
        try:
            mod = importlib.import_module(module)
        except ImportError:
            return None
        return getattr(mod, name, None)

    def approve(self, conn, draft_uid: str, by: str):
        fn = self._fn("jobhunter.approvals", "approve")
        return _DEFER if fn is None else fn(conn, draft_uid, by=by)

    def skip(self, conn, draft_uid: str, reason: str, by: str):
        fn = self._fn("jobhunter.approvals", "skip")
        return _DEFER if fn is None else fn(conn, draft_uid, reason=reason, by=by)

    def set_human_call(self, conn, job_uid: str, call_: str, by: str):
        fn = self._fn("jobhunter.jobs", "set_human_call")
        return _DEFER if fn is None else fn(conn, job_uid, call_, by=by)

    def set_outcome(self, conn, *, thread_key=None, job_uid=None, outcome: str, by: str):
        fn = self._fn("jobhunter.threads", "set_outcome")
        return _DEFER if fn is None else fn(conn, thread_key=thread_key, job_uid=job_uid, outcome=outcome, by=by)

    def set_application_notes(self, conn, job_uid: str, notes: str, by: str):
        fn = self._fn("jobhunter.threads", "set_notes")
        if fn is not None:
            return fn(conn, job_uid=job_uid, notes=notes, by=by)
        row = conn.execute("SELECT ap.id FROM applications ap JOIN jobs j ON j.id = ap.job_id WHERE j.job_uid = ? "
                           "ORDER BY ap.id DESC LIMIT 1", (job_uid,)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no application for job %s" % job_uid)
        conn.execute("UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
                     (notes[:4000], canon.now(), row[0]))
        return None


BY = "human:sheet"


def _dispatch_edit(conn, e: dict, h: Handlers):
    """Run one edit; returns a short result string, or _DEFER when the owning module is missing."""
    tab, col, value, rid = e["tab"], e["col"], e["value"].strip(), e["row_id"]
    if tab == "approvals" and col == "decision":
        if value == "Approve":
            r = h.approve(conn, rid, BY)
        elif value == "Skip":
            r = h.skip(conn, rid, "Skipped in the Google Sheet", BY)
        elif value == "":
            return "ignored_empty"
        else:
            return "ignored_value"
        return _DEFER if r is _DEFER else ("approved" if value == "Approve" else "skipped")
    if col == "your_call":
        code = L.HUMAN_CALL_CODE.get(value)
        if value == "":
            return "ignored_empty"
        if code is None or (tab == "skipped" and code != "apply_anyway"):
            return "ignored_value"
        r = h.set_human_call(conn, rid, code, BY)
        return _DEFER if r is _DEFER else "call_" + code
    if col == "outcome":
        code = L.OUTCOME_CODE.get(value)
        if value == "":
            return "ignored_empty"
        # each tab has its own list: a stale sheet may still offer "Withdrawn" on Follow-ups, which no
        # thread can store, so it is ignored here instead of ending as refused:E_VALIDATION
        if code is None or value not in (L.editable_columns(tab).get("outcome") or ()):
            return "ignored_value"
        if tab == "applications":
            r = h.set_outcome(conn, job_uid=rid, outcome=code, by=BY)
        else:
            r = h.set_outcome(conn, thread_key=rid, outcome=code, by=BY)
        return _DEFER if r is _DEFER else "outcome_" + code
    if col == "notes":
        if tab == "applications":
            h.set_application_notes(conn, rid, e["value"], BY)
            return "notes_saved"
        return "note_kept"   # outreach notes: the text is kept in sheet_edits_applied.value
    return "ignored_column"


def _valid_edit(e) -> dict | None:
    if not isinstance(e, dict):
        return None
    eid, tab, col, rid = e.get("edit_id"), e.get("tab"), e.get("col"), e.get("row_id")
    if not (isinstance(eid, str) and _EDIT_ID_RE.match(eid)):
        return None
    if tab not in EDIT_TABS or col not in EDIT_TABS[tab]:
        return None
    if not (isinstance(rid, str) and 0 < len(rid) <= 64):
        return None
    value = e.get("value")
    value = "" if value is None else str(value)
    return {"edit_id": eid, "tab": tab, "col": col, "row_id": rid, "value": value[:4000], "at": e.get("at")}


def apply_edits(conn, edits: list, handlers: Handlers | None = None) -> tuple[list[str], dict]:
    """Apply each pending human edit once (sheet_edits_applied is the idempotency record).
    Returns (edit ids to acknowledge, counts)."""
    h = handlers or Handlers()
    ack: list[str] = []
    counts = {"applied": 0, "already": 0, "refused": 0, "ignored": 0, "deferred": 0, "invalid": 0}
    for raw in edits or []:
        e = _valid_edit(raw)
        if e is None:
            counts["invalid"] += 1
            eid = raw.get("edit_id") if isinstance(raw, dict) else None
            if isinstance(eid, str) and _EDIT_ID_RE.match(eid):
                ack.append(eid)   # junk rows would otherwise come back forever
            continue
        if conn.execute("SELECT 1 FROM sheet_edits_applied WHERE edit_id = ?", (e["edit_id"],)).fetchone():
            counts["already"] += 1
            ack.append(e["edit_id"])
            continue
        result = None
        try:
            with db.tx(conn):
                result = _dispatch_edit(conn, e, h)
                if result is _DEFER:
                    raise _Deferred()
                _record_edit(conn, e, result)
        except _Deferred:
            counts["deferred"] += 1
            continue
        except Denied as d:
            if d.code in ("E_LOCKED", "E_INTERNAL"):
                counts["deferred"] += 1
                continue
            result = "refused:" + d.code
            with db.tx(conn):
                _record_edit(conn, e, result)
                db.enqueue_notification(
                    conn, "sheet_edit:" + e["edit_id"], "normal", "info",
                    "Your change in the Google Sheet (%s, %s = %s) was not applied: %s"
                    % (L.TAB_TITLES.get(e["tab"], e["tab"]), e["row_id"], L.clip(e["value"], 40), d.message))
        except Exception as exc:  # a bug or stub in the owning module: keep the edit for the next run
            counts["deferred"] += 1
            db.log_event(conn, "sheet_edit_deferred", edit_id=e["edit_id"], tab=e["tab"],
                         error="%s: %s" % (type(exc).__name__, L.clip(str(exc), 200)))
            continue
        ack.append(e["edit_id"])
        if result.startswith("refused:"):
            counts["refused"] += 1
        elif result.startswith("ignored"):
            counts["ignored"] += 1
        else:
            counts["applied"] += 1
    return ack, counts


class _Deferred(Exception):
    pass


def _record_edit(conn, e: dict, result: str) -> None:
    conn.execute("INSERT INTO sheet_edits_applied (edit_id, tab, row_id, col_key, value, result, applied_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (e["edit_id"], e["tab"], e["row_id"], e["col"], e["value"], result, canon.now()))
    db.log_event(conn, "sheet_edit_applied", edit_id=e["edit_id"], tab=e["tab"], row_id=e["row_id"], col=e["col"],
                 result=result)


# ---------------------------------------------------------------- deletes
def queue_delete(conn, tab: str, row_id: str) -> None:
    """Queue a Sheet row delete (used by `forget` and the Skipped tab window). Runs inside the caller's tx."""
    if tab not in L.TABLE_TABS:
        raise Denied("E_VALIDATION", "unknown sheet tab %r" % tab)
    conn.execute("INSERT INTO sheet_deletes (tab, row_id, created_at) VALUES (?, ?, ?) "
                 "ON CONFLICT (tab, row_id) DO UPDATE SET done_at = NULL", (tab, row_id, canon.now()))


def _queue_skipped_deletes(conn, ctx: R.RowCtx, full: bool) -> None:
    uids = R.skipped_expired(conn, ctx, None if full else 14)
    if not uids:
        return
    with db.tx(conn):
        for uid in uids:
            conn.execute("INSERT INTO sheet_deletes (tab, row_id, created_at) VALUES ('skipped', ?, ?) "
                         "ON CONFLICT (tab, row_id) DO NOTHING", (uid, canon.now()))


# ---------------------------------------------------------------- sync
def _max_ops(config: dict) -> int:
    try:
        n = int(S.cfg(config, "sheets.max_ops_per_post", 400))
    except (TypeError, ValueError):
        n = 400
    return max(1, min(n, MAX_OPS_HARD))


def _overlap_since(watermark: str | None, minutes: int) -> str | None:
    if not watermark:
        return None
    try:
        return canon.ts_add(watermark, minutes=-minutes)
    except ValueError:
        return None


def _batch_id() -> str:
    return "B" + canon.utcnow().strftime("%Y%m%dT%H%M%SZ") + canon.new_uid("X", 4)[1:]


def render_payload(conn, config: dict) -> dict:
    limits = S.limits_today(conn)
    return {"dashboard": S.dashboard(conn, config, limits), "settings": S.settings_rows(conn, config, limits),
            "start": {"agent_state": S.agent_state(conn), "last_sync": canon.now()}}


def sync(conn, full: bool = False, dry_run: bool = False, tab: str | None = None, *, transport=None,
         handlers: Handlers | None = None, config: dict | None = None) -> dict:
    """Push changed rows, deletes and renders; pull and apply edits. Manages its own transactions."""
    config = S.load_config() if config is None else config
    if not S.cfg(config, "sheets.enabled", True):
        return {"skipped": "sheets.enabled is false", "pushed": {}}
    if tab is not None and tab not in L.TABLE_TABS:
        raise Denied("E_USAGE", "unknown tab %r; tabs: %s" % (tab, ", ".join(L.TABLE_ORDER)))
    if not dry_run and transport is None and not is_connected():
        return {"skipped": "not_connected", "pushed": {}}
    with _sync_lock() as got:
        if not got:
            return {"skipped": "another sync is running", "pushed": {}}
        return _sync(conn, full, dry_run, tab, transport, handlers, config)


def _sync(conn, full, dry_run, only_tab, transport, handlers, config) -> dict:
    started = canon.now()
    ctx = R.RowCtx.load(config)
    try:
        overlap = int(S.cfg(config, "sheets.resync_overlap_minutes", 10))
    except (TypeError, ValueError):
        overlap = 10
    tabs = [only_tab] if only_tab else list(L.TABLE_ORDER)
    if not dry_run and (only_tab in (None, "skipped", "jobs")):
        _queue_skipped_deletes(conn, ctx, full)

    ops: list[dict] = []
    stamps: dict[str, str] = {}
    counts: dict[str, int] = {}
    for t in tabs:
        st = _state_get(conn, t)
        since = None if full else _overlap_since(st["watermark"] if st else None, overlap)
        rows = R.build_rows_stamped(conn, t, since, ctx)
        counts[t] = len(rows)
        for rid, row, stamp in rows:
            ops.append({"tab": t, "id": rid, "row": row})
            if stamp and stamp > stamps.get(t, ""):
                stamps[t] = stamp
    dels = [{"tab": r["tab"], "id": r["row_id"], "_rowid": r["id"]}
            for r in conn.execute("SELECT id, tab, row_id FROM sheet_deletes WHERE done_at IS NULL ORDER BY id")
            if not only_tab or r["tab"] == only_tab]
    render = render_payload(conn, config)

    if dry_run:
        return {"dry_run": True, "pushed": counts, "deletes": len(dels), "ops": len(ops),
                "sample": ops[:3], "render": sorted(render)}

    t_obj = _transport(transport)
    secret = _secret()
    max_ops = _max_ops(config)
    chunks: list[tuple[list, list]] = []
    items: list[tuple[str, dict]] = [("del", d) for d in dels] + [("op", o) for o in ops]
    for i in range(0, len(items), max_ops):
        part = items[i:i + max_ops]
        chunks.append(([x for k, x in part if k == "op"], [x for k, x in part if k == "del"]))
    if not chunks:
        chunks.append(([], []))

    pending_ack: list[str] = []
    pushed_ok: dict[str, int] = {}
    failed_tabs: set[str] = set()
    totals = {"created": 0, "updated": 0, "deleted": 0, "row_errors": 0, "edits_applied": 0, "requests": 0}
    edit_counts: dict[str, int] = {}
    error: Denied | None = None
    batch = _batch_id()
    for idx, (chunk_ops, chunk_dels) in enumerate(chunks):
        last = idx == len(chunks) - 1
        req = _request("sync", secret, batch_id="%s-%d" % (batch, idx + 1), ack_edits=pending_ack,
                       deletes=[{"tab": d["tab"], "id": d["id"]} for d in chunk_dels], ops=chunk_ops)
        if last:
            req["render"] = render
        try:
            resp = _send_chunk(t_obj, req)
        except Denied as d:
            error = d
            for later_ops, _later_dels in chunks[idx:]:
                failed_tabs.update(o["tab"] for o in later_ops)
            break
        totals["requests"] += 1
        pending_ack = []
        for k in ("created", "updated", "deleted"):
            totals[k] += int(resp.get(k) or 0)
        totals["row_errors"] += len(resp.get("errors") or [])
        for o in chunk_ops:
            pushed_ok[o["tab"]] = pushed_ok.get(o["tab"], 0) + 1
        if chunk_dels:
            with db.tx(conn):
                for d in chunk_dels:
                    conn.execute("UPDATE sheet_deletes SET done_at = ? WHERE id = ?", (canon.now(), d["_rowid"]))
        ack, ec = apply_edits(conn, resp.get("edits") or [], handlers)
        for k, v in ec.items():
            edit_counts[k] = edit_counts.get(k, 0) + v
        pending_ack = ack
    if error is None and pending_ack:
        try:
            _send_chunk(t_obj, _request("sync", secret, batch_id="%s-ack" % batch, ack_edits=pending_ack,
                                        deletes=[], ops=[]))
            totals["requests"] += 1
            pending_ack = []
        except Denied as d:
            error = d   # the edits are recorded; the next sync acknowledges them
    totals["edits_applied"] = edit_counts.get("applied", 0)

    now = canon.now()
    # Nothing committed before (start minus overlap) can still be invisible to this run, so the watermark may
    # move up to there even when a tab had no changes; the next run re-reads from (watermark minus overlap).
    floor = canon.ts_add(started, minutes=-overlap)
    with db.tx(conn):
        for t in tabs:
            if t in failed_tabs:
                continue
            cols = {"last_push_at": now}
            old_wm = (_state_get(conn, t) or {"watermark": ""})["watermark"] or ""
            cols["watermark"] = max(stamps.get(t, ""), floor, old_wm)
            if full:
                cols["last_full_at"] = now
            cols["last_error"] = None if error is None else L.clip(error.message, 500)
            _state_set(conn, t, **cols)
            if pushed_ok.get(t):
                conn.execute("UPDATE sheet_state SET rows_pushed_total = rows_pushed_total + ? WHERE tab = ?",
                             (pushed_ok[t], t))
        if error is None:
            _state_set(conn, "_all", last_push_at=now, last_error=None, **({"last_full_at": now} if full else {}))
    if error is not None:
        _record_error(conn, failed_tabs, "%s: %s" % (error.code, error.message))
        raise Denied(error.code, error.message,
                     data=dict(error.data or {}, pushed=pushed_ok, failed_tabs=sorted(failed_tabs)))
    return {"pushed": pushed_ok, "built": counts, "deleted": totals["deleted"], "created": totals["created"],
            "updated": totals["updated"], "row_errors": totals["row_errors"], "edits_applied": totals["edits_applied"],
            "edits": edit_counts, "requests": totals["requests"], "render": sorted(render), "full": bool(full)}


def _send_chunk(t_obj, req: dict, depth: int = 0) -> dict:
    """POST one chunk; `too_large` splits the operations in half (at most 8 levels)."""
    resp = t_obj.post(req)
    if resp.get("ok") is not True and resp.get("error") == "too_large" and depth < 8 and len(req["ops"]) > 1:
        half = len(req["ops"]) // 2
        first = dict(req, ops=req["ops"][:half])
        first.pop("render", None)
        second = dict(req, ops=req["ops"][half:], ack_edits=[], deletes=[], batch_id=req["batch_id"] + "b")
        r1 = _send_chunk(t_obj, first, depth + 1)
        r2 = _send_chunk(t_obj, second, depth + 1)
        return {"ok": True, "created": int(r1.get("created") or 0) + int(r2.get("created") or 0),
                "updated": int(r1.get("updated") or 0) + int(r2.get("updated") or 0),
                "deleted": int(r1.get("deleted") or 0), "errors": (r1.get("errors") or []) + (r2.get("errors") or []),
                "edits": r2.get("edits") or r1.get("edits") or []}
    return _check(resp)
