"""Self test (design 3.4 `selftest`): quick checks that the install is sound. Each check returns
{name, ok, detail}; a check whose module belongs to another unit and is not installed reports ok false
with the reason, so `doctor` shows it. `--offline` skips the mail connection, the sheet ping and every check
that calls openclaw; the mail connection is also skipped (ok, marked skipped) unless gmail.route is
app_password and mail is connected.

Claude subscription route checks (CLI route 8):
- agent identity: the guard heartbeat announces proof version 2 and the carriers jh.py requires; with
  probe_since (epoch seconds) every tool agent left a probe file (`whoami`) at or after it with those
  carriers and WS_ROOT/<role>/work/probe/ok.txt holds OK, and no `native_tool` guard log line is newer.
- exec policy: every jobhunter agent's effective exec policy (allowlist or qc deny, ask off, elevated off,
  host approvals ask off and askFallback deny). Red on failure.
- identity boundary: one red line per other agent with an unconfined shell (4.4); never fails the run.
- claude settings: warnings from the key names (never values) of the owner's Claude Code user settings.
- other plugins: enabled OpenClaw plugins besides jobhunter-guard (they can ask for approvals).
- cli mode: the reduced-protection native tool mode (mode N) is red.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from . import paths

TOOL_AGENTS = ("jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach")
JOBHUNTER_AGENTS = TOOL_AGENTS + ("jobhunter-qc",)
OPENCLAW_CHECKS = ("exec policy", "identity boundary", "other plugins")


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


# ---------------------------------------------------------------- claude subscription route (CLI route 8)
def _epoch_ts(epoch) -> str:
    import datetime as _dt
    from .canon import fmt_ts
    return fmt_ts(_dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc))


def _native_tool_lines(since_ts: str) -> int:
    """Guard log lines (logs/guard-YYYY-MM.jsonl) of kind native_tool at or after since_ts."""
    n = 0
    try:
        names = sorted(f for f in os.listdir(paths.logs_dir()) if f.startswith("guard-") and f.endswith(".jsonl"))
    except OSError:
        return 0
    for name in names:
        if name[6:13] < since_ts[:7]:
            continue
        try:
            with open(os.path.join(paths.logs_dir(), name), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("kind") == "native_tool" and \
                            str(rec.get("ts") or "") >= since_ts:
                        n += 1
        except OSError:
            continue
    return n


def _probe_problems(agent: str, carriers: list, since_ts: str) -> list[str]:
    from .commands.core import probe_dir
    out = []
    try:
        with open(os.path.join(probe_dir(), agent + ".json"), "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return ["no probe file (the agent never proved its identity to jh.py)"]
    if not isinstance(rec, dict) or str(rec.get("at") or "") < since_ts:
        out.append("probe file is older than this test")
    elif sorted(rec.get("carriers") or []) != carriers:
        out.append("identity carriers %s, expected %s" % ("+".join(sorted(rec.get("carriers") or [])) or "none",
                                                           "+".join(carriers)))
    ok_file = os.path.join(paths.ws_dir(agent), "work", "probe", "ok.txt")
    try:
        with open(ok_file, "r", encoding="utf-8") as fh:
            if fh.read().strip() != "OK":
                out.append("work/probe/ok.txt does not hold OK")
    except (OSError, UnicodeDecodeError):
        out.append("work/probe/ok.txt was not written")
    return out


def check_agent_identity(probe_since=None) -> dict:
    from . import auth, gate
    try:
        carriers = auth.required_carriers()
    except Exception as exc:
        return {"name": "agent identity", "ok": False, "detail": getattr(exc, "message", str(exc))}
    hb = gate.guard_heartbeat()
    problems = []
    if not hb["fresh"]:
        problems.append("the guard heartbeat is missing or stale")
    elif hb.get("proof_version") != 2:
        problems.append("the guard does not mint identity proof version 2 (old guard; run ./install.sh)")
    elif hb.get("carriers") is not None and hb["carriers"] != carriers:
        problems.append("the guard sends %s proofs, jh.py requires %s" % ("+".join(hb["carriers"]) or "no",
                                                                          "+".join(carriers)))
    agents = {}
    if probe_since is not None:
        since_ts = _epoch_ts(probe_since)
        for agent in TOOL_AGENTS:
            p = _probe_problems(agent, carriers, since_ts)
            agents[agent] = "ok" if not p else "; ".join(p)
            problems += ["%s: %s" % (agent, x) for x in p]
        native = _native_tool_lines(since_ts)
        if native:
            problems.append("%d Claude Code native tool call(s) reached the guard: a run was not restricted" % native)
    detail = {"carriers": carriers, "proof_version": hb.get("proof_version"), "problems": problems}
    if agents:
        detail["agents"] = agents
    return {"name": "agent identity", "ok": not problems, "detail": detail}


def check_exec_policy() -> dict:
    from . import ocrun
    policy = ocrun.exec_policy_doc()
    if policy is None:
        return {"name": "exec policy", "ok": False, "red": True,
                "detail": "openclaw exec-policy show --json could not be read"}
    bad, view = [], {}
    for agent in JOBHUNTER_AGENTS:
        eff = ocrun.effective_exec(agent, policy_doc=policy)
        problems = ocrun.exec_policy_problems(agent, eff)
        view[agent] = {k: eff.get(k) for k in ("security", "ask", "mode", "elevated", "approvals_ask",
                                               "approvals_fallback")}
        if problems:
            bad.append("%s: %s" % (agent, ", ".join(problems)))
    return {"name": "exec policy", "ok": not bad, "red": bool(bad), "detail": {"agents": view, "problems": bad}}


def check_identity_boundary() -> dict:
    """Never fails the run (the owner's choice); one red line per unconfined non-jobhunter agent."""
    from . import ocrun
    policy = ocrun.exec_policy_doc()
    if policy is None:
        return {"name": "identity boundary", "ok": True, "skipped": True,
                "detail": "warning: openclaw exec-policy show --json could not be read"}
    lines = ["agent %s has an unconfined shell and can impersonate jobhunter agents; set its exec policy to "
             "allowlist with ask off" % a["agent"] for a in ocrun.unconfined_agents(policy, ocrun.agents_list())]
    return {"name": "identity boundary", "ok": True, "red_lines": lines,
            "detail": "; ".join(lines) if lines else "no other agent has an unconfined shell"}


def claude_settings_path() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "settings.json")


def check_claude_settings(path: str | None = None) -> dict:
    """Key names only, never values (6.5). Restricted runs ignore user settings; these matter for stray runs."""
    path = path or claude_settings_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {"name": "claude settings", "ok": True, "detail": "no Claude Code user settings"}
    except (OSError, ValueError):
        return {"name": "claude settings", "ok": True, "detail": "warning: the Claude Code user settings are unreadable"}
    warn = []
    if isinstance(doc, dict):
        if "disableAllHooks" in doc:
            warn.append("disableAllHooks")
        perms = doc.get("permissions") if isinstance(doc.get("permissions"), dict) else {}
        if perms.get("allow"):
            warn.append("permissions.allow")
        if "defaultMode" in perms:
            warn.append("permissions.defaultMode")
        for k in ("hooks", "mcpServers", "enabledMcpjsonServers"):
            if doc.get(k):
                warn.append(k)
    return {"name": "claude settings", "ok": True, "warnings": warn,
            "detail": ("warning: the Claude Code user settings set %s; they apply to any unrestricted run" %
                       ", ".join(warn)) if warn else "nothing that affects agent runs"}


def check_other_plugins() -> dict:
    from . import ocrun
    names = ocrun.plugins_enabled()
    if names is None:
        return {"name": "other plugins", "ok": True, "skipped": True,
                "detail": "warning: openclaw plugins list --json could not be read"}
    others = [n for n in names if n != "jobhunter-guard"]
    return {"name": "other plugins", "ok": True, "warnings": others,
            "detail": ("warning: other enabled plugins can return requireApproval: %s" % ", ".join(others))
            if others else "only jobhunter-guard"}


def check_cli_mode() -> dict:
    try:
        route = paths.home().get("cli_route")
    except Exception:
        route = None
    mode = (route or {}).get("cli_tools") if isinstance(route, dict) else None
    if mode == "native":
        return {"name": "cli mode", "ok": False, "red": True,
                "detail": "mode N (Claude Code native tools, reduced protection): the exec allowlist and "
                          "workspaceOnly do not apply to native tools and AskUserQuestion can wait"}
    return {"name": "cli mode", "ok": True, "detail": "restricted runs"}


def route_checks(offline: bool = True, probe_since=None) -> list[dict]:
    checks = [_safe("agent identity", lambda: check_agent_identity(probe_since)),
              _safe("claude settings", check_claude_settings), _safe("cli mode", check_cli_mode)]
    for name, fn in (("exec policy", check_exec_policy), ("identity boundary", check_identity_boundary),
                     ("other plugins", check_other_plugins)):
        if offline:
            checks.append({"name": name, "ok": True, "skipped": True, "detail": "skipped: --offline"})
        else:
            checks.append(_safe(name, fn))
    return checks


def _safe(name, fn) -> dict:
    try:
        return fn()
    except Exception as exc:   # a selftest line never raises
        return {"name": name, "ok": False, "detail": "%s: %s" % (type(exc).__name__, getattr(exc, "message", exc))}


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


CHECK_NAMES = ("db", "meta", "migrations", "trigger fixtures", "keys fixtures", "lint fixtures", "pdf build",
               "config clamp", "acl vs argparse", "guard heartbeat", "email finder", "agent identity", "claude settings",
               "cli mode", "exec policy", "identity boundary", "other plugins", "mail connection", "sheet ping")


def run_checks(offline: bool = True, probe_since=None, only: str | None = None) -> list[dict]:
    """Every check (or only the named one). probe_since: epoch seconds for the agent identity probe check."""
    if only is not None:
        from .errors import Denied
        if only not in CHECK_NAMES:
            raise Denied("E_USAGE", "unknown check %r (%s)" % (only, ", ".join(CHECK_NAMES)))
        if only in ("agent identity", "claude settings", "cli mode") + OPENCLAW_CHECKS:
            return [c for c in route_checks(offline, probe_since) if c["name"] == only]
    checks = [_check("db", _db), _check("meta", _meta), _check("migrations", _migrations),
              _check("trigger fixtures", _triggers), _check("keys fixtures", _keys), _check("lint fixtures", _lint),
              _check("pdf build", _pdf), _check("config clamp", _config), _check("acl vs argparse", _acl),
              _check("guard heartbeat", _guard)]
    checks += _enrich_checks()
    checks += route_checks(offline, probe_since)
    if only is not None:
        return [c for c in checks if c["name"] == only or (only == "email finder" and c["name"].startswith("email"))]
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
