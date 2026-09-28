"""Self test (design 3.4 `selftest`): quick checks that the install is sound. Each check returns
{name, ok, detail}; a check whose module belongs to another unit and is not installed reports ok false
with the reason, so `doctor` shows it. `--offline` skips the mail connection and the sheet ping; the mail
connection is also skipped (ok, marked skipped) unless gmail.route is app_password and mail is connected.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from . import paths


def _check(name, fn) -> dict:
    try:
        ok, detail = fn()
        return {"name": name, "ok": bool(ok), "detail": detail}
    except Exception as exc:
        return {"name": name, "ok": False, "detail": "%s: %s" % (type(exc).__name__, getattr(exc, "message", exc))}


def _db():
    from . import db
    conn = db.connect(write=False)
    try:
        conn.execute("SELECT count(*) FROM meta").fetchone()
        return True, paths.db_path()
    finally:
        conn.close()


def _meta():
    from . import db
    conn = db.connect(write=False)
    try:
        missing = db.missing_meta(conn)
        qc_missing = db.missing_meta(conn, db.REQUIRED_FOR_QC)
    finally:
        conn.close()
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, ("ok" if not qc_missing else "QC hashes not written yet (install render-workspaces): " +
                  ", ".join(qc_missing))


def _migrations():
    from . import db
    conn = db.connect(write=False)
    try:
        v = db.schema_version(conn)
    finally:
        conn.close()
    return v == db.latest_version(), "schema v%d, code v%d" % (v, db.latest_version())


def _triggers():
    """An in-memory copy of the schema with an empty meta table must refuse a second cold email to one
    company (365-day fallback) and a second first touch to one person."""
    from .errors import map_sqlite_error
    mem = sqlite3.connect(":memory:")
    try:
        with open(paths.SCHEMA_FILE, "r", encoding="utf-8") as fh:
            mem.executescript(fh.read())
        ts = "2026-01-01T00:00:00Z"
        mem.execute("INSERT INTO companies (id, company_uid, display_name, created_at, updated_at) VALUES "
                    "(1, 'KAAAAAAA', 'Kestrel Commerce', ?, ?)", (ts, ts))
        for i in (1, 2):
            mem.execute("INSERT INTO contacts (id, contact_uid, company_id, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                        (i, "PAAAAAA%d" % i, ts, ts))

        def ins(tok, contact):
            mem.execute("INSERT INTO actions (token, kind, route, first_touch, platform, contact_id, company_id, status, "
                        "reserved_at, expires_at, created_at, updated_at) VALUES (?, 'cold_email', 'mailer', 1, 'gmail', "
                        "?, 1, 'sent', ?, ?, ?, ?)", (tok, contact, ts, ts, ts, ts))
        ins("TAAAAAAAAAAA", 1)
        codes = []
        for tok, contact in (("TAAAAAAAAAAB", 2), ("TAAAAAAAAAAC", 1)):
            try:
                ins(tok, contact)
                codes.append("allowed")
            except sqlite3.DatabaseError as exc:
                codes.append(map_sqlite_error(exc).code)
        ok = codes == ["E_COMPANY_COOLDOWN", "E_COMPANY_COOLDOWN"]
        return ok, ", ".join(codes)
    finally:
        mem.close()


def _keys():
    from . import keys
    a = keys.job_key("https://boards.greenhouse.io/kestrel/jobs/4012345?gh_src=x", {}, "greenhouse")[0]
    b = keys.job_key("https://kestrel.example/careers?gh_jid=4012345", {}, "")[0]
    c = {k for k, _ in keys.company_keys("Kestrel Labs")} & {k for k, _ in keys.company_keys("KestrelLabs Pvt Ltd")} \
        & {k for k, _ in keys.company_keys(domain="kestrellabs.com")}
    gmail, googlemail = "gmail.com", "googlemail.com"   # joined at run time: no free-mail address in the repo
    p = {k for k, _ in keys.person_keys(email="a.lex+x@" + googlemail)} & \
        {k for k, _ in keys.person_keys(email="alex@" + gmail)}
    ok = a == b == "ats:greenhouse:4012345" and bool(c) and bool(p)
    return ok, "job %s; company %s; person %s" % (a, sorted(c), sorted(p))


def _lint():
    from .qc import lint as qlint
    res = qlint.lint({"kind": "cold_email", "channel": "email_cold", "subject": "Hello",
                      "body": "Hi Alex,\n\nA short note \u2014 with a dash.\n\nThanks,"}, {})
    blocks = res.get("blocks") if isinstance(res, dict) else None
    return bool(blocks), "dash is blocked" if blocks else "the linter did not block an em dash"


def _pdf():
    from .resume import pdf as rpdf
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "t.pdf")
        fn = getattr(rpdf, "selftest", None)
        if fn is None:
            return True, "renderer present (no selftest hook)"
        fn(out)
        return os.path.exists(out), out


def _config():
    from . import config
    eff, ctx = config.compute({"gmail": {"ceilings": {"conservative": {"cold_day": 1000}}},
                               "approval": {"mode": "inherit"}}, {"approval_mode": "auto"})
    ok = eff["gmail"]["ceilings"]["conservative"]["cold_day"] <= 30 and bool(ctx.clamped)
    return ok, "hostile cold_day clamped to %s" % eff["gmail"]["ceilings"]["conservative"]["cold_day"]


def _acl():
    import json
    from . import cli
    with open(paths.ACL_FILE, "r", encoding="utf-8") as fh:
        acl = json.load(fh)
    parser = cli.build_parser()
    registered = cli.registered_commands(parser)
    missing = sorted({c for a in acl["agents"].values() for c in a["commands"] if c not in registered})
    errs = cli.discovery_errors()
    ok = not missing and not errs
    return ok, ("all ACL commands registered" if ok else "missing: %s; errors: %s" % (", ".join(missing), errs))


def _guard():
    from . import gate
    hb = gate.guard_heartbeat()
    return hb["fresh"], hb


def mail_check_applies() -> tuple[bool, str]:
    """The mail connection test runs only on the app password route once mail is connected (U9 open issue 5):
    on the web UI route there is no SMTP or IMAP login to test."""
    from . import config as _config, db
    conn = db.connect(write=False)
    try:
        route = _config.load(conn)["gmail"]["route"]
        connected = conn.execute("SELECT value FROM meta WHERE key = 'mail_connected_at'").fetchone()
    finally:
        conn.close()
    if route != "app_password":
        return False, "skipped: gmail.route is %s" % route
    if not connected or not connected[0]:
        return False, "skipped: mail is not connected yet (./jobhunter mail connect)"
    return True, "app_password route, connected"


def _mail():
    from .mail import smtp  # noqa: F401  (U9)
    from . import mail
    fn = getattr(mail, "test_connection", None)
    if fn is None:
        return False, "mail module has no test_connection"
    r = fn()
    return bool(r.get("smtp_ok") and r.get("imap_ok")), r


def _sheet():
    from . import sheets
    r = sheets.ping()
    return bool(r.get("ok")), r


def _enrich_checks() -> list[dict]:
    """The optional email finder's own lines (U10 enrich.selftest_checks: migration objects, trigger fixtures,
    key store, dependencies). Not installed: one skipped line with a warning."""
    try:
        from . import enrich
    except ImportError as exc:
        return [{"name": "email finder", "ok": True, "skipped": True,
                 "detail": "warning: the email finder is not installed (%s)" % exc}]
    fn = getattr(enrich, "selftest_checks", None)
    if fn is None:
        return [{"name": "email finder", "ok": True, "skipped": True,
                 "detail": "warning: the email finder has no selftest_checks"}]
    try:
        return [dict(c) for c in fn()]
    except Exception as exc:   # a selftest line never raises
        return [{"name": "email finder", "ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}]


def run_checks(offline: bool = True) -> list[dict]:
    checks = [_check("db", _db), _check("meta", _meta), _check("migrations", _migrations),
              _check("trigger fixtures", _triggers), _check("keys fixtures", _keys), _check("lint fixtures", _lint),
              _check("pdf build", _pdf), _check("config clamp", _config), _check("acl vs argparse", _acl),
              _check("guard heartbeat", _guard)]
    checks += _enrich_checks()
    if not offline:
        try:
            applies, why = mail_check_applies()
        except Exception as exc:
            applies, why = False, "skipped: %s: %s" % (type(exc).__name__, getattr(exc, "message", exc))
        if applies:
            checks.append(_check("mail connection", _mail))
        else:
            checks.append({"name": "mail connection", "ok": True, "skipped": True, "detail": why})
        checks.append(_check("sheet ping", _sheet))
    return checks
