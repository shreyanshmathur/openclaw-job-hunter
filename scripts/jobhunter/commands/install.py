"""`jh.py install ...` commands (design 3.4 Install; owned by U7).

Spec commands: check-path, render-crons, render-workspaces, render-agents-patch, render-guard-config, manifest.
Helper commands used by install.sh, uninstall.sh and ./jobhunter (bash 3.2 cannot parse JSON, so these print
plain lines with --human): shell-env, job-ids, cron-summary, agents, render-approvals, render-extradirs,
render-uninstall-patch, render-headless-patch, render-stayawake, board-domains, wait-guard, version-check,
check-sqlite, render-shared-skills, upload-root, mail-state, forget-mail-secret, owner (the wizard's owner
details step), route (how the wrapper runs any other jh.py command: with the PIN or without), and the browser
consent helpers of `./jobhunter browser consent|forget|import|login`: consent-sites, chrome-profiles,
consent-record (human only: the owner PIN), consent-revoke, consent-imports, login-probe and login-check.
All are system or human callers; no agent may run them.
"""
from __future__ import annotations

import argparse
import json
import os

from .. import install as ins
from .. import paths
from ..commands import Result, add_command
from ..errors import Denied


def register(subparsers):
    add_command(subparsers, "install check-path", cmd_check_path, callers="SH",
                help="refuse repo paths with spaces or inside privacy-protected folders")

    p = add_command(subparsers, "install render-crons", cmd_render_crons, callers="SH",
                    help="the openclaw cron add (or --alerts: cron edit) argument lists")
    p.add_argument("--alerts", action="store_true", help="render the failure-alert edits for known job ids")

    add_command(subparsers, "install render-workspaces", cmd_render_workspaces, callers="SH",
                help="render agent-templates into WS_ROOT and record the reviewer hashes")

    p = add_command(subparsers, "install render-agents-patch", cmd_render_agents_patch, callers="SH",
                    help="write the agents.entries[jobhunter-*] config patch")
    p.add_argument("--route", choices=["cli", "api_key"], default="cli")

    add_command(subparsers, "install render-guard-config", cmd_render_guard_config, callers="SH",
                help="write the jobhunter-guard plugin config patch")

    p = add_command(subparsers, "install manifest", cmd_manifest, callers="SH",
                    help="show or update state/install-manifest.json")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--set", dest="set_file", metavar="<json-file>",
                   help="merge a JSON object, or store job ids from `openclaw cron list --all --json` output")
    g.add_argument("--show", action="store_true")

    add_command(subparsers, "install shell-env", cmd_shell_env, callers="SH",
                help="home.json values as shell assignments (use with --human)")

    p = add_command(subparsers, "install job-ids", cmd_job_ids, callers="SH",
                    help="cron job ids for a wrapper action (use with --human)")
    p.add_argument("--which", choices=["all", "resume", "pause", "agents", "lane"], required=True)
    p.add_argument("--lane")
    p.add_argument("--from-list", metavar="<json-file>", help="also read ids from a fresh cron list")

    p = add_command(subparsers, "install cron-summary", cmd_cron_summary, callers="SH",
                    help="table of the jobhunter automations from a cron list file")
    p.add_argument("--file", required=True)

    p = add_command(subparsers, "install agents", cmd_agents, callers="SH",
                    help="declared agents; with --missing-from, only those absent from `agents list --json`")
    p.add_argument("--missing-from", metavar="<json-file>")

    p = add_command(subparsers, "install render-approvals", cmd_render_approvals, callers="SH",
                    help="merge the jobhunter exec approvals into `approvals get --json` output")
    p.add_argument("--current", required=True, metavar="<json-file>")
    p.add_argument("--remove", action="store_true")

    p = add_command(subparsers, "install render-extradirs", cmd_render_extradirs, callers="SH",
                    help="skills.load.extraDirs patch adding (or removing) shared-skills")
    p.add_argument("--current", required=True, metavar="<json-file>")
    p.add_argument("--remove", action="store_true")

    add_command(subparsers, "install render-uninstall-patch", cmd_render_uninstall_patch, callers="SH",
                help="config patch that removes the jobhunter agents and plugin entries")
    add_command(subparsers, "install render-headless-patch", cmd_render_headless_patch, callers="SH",
                help="Linux and WSL: keep the jobhunter browser profile headed")
    add_command(subparsers, "install render-stayawake", cmd_render_stayawake, callers="SH",
                help="render the macOS stay-awake LaunchAgent plist")
    add_command(subparsers, "install board-domains", cmd_board_domains, callers="SH",
                help="cookie-import domains of the enabled job boards (use with --human)")

    p = add_command(subparsers, "install wait-guard", cmd_wait_guard, callers="SH",
                    help="wait for a fresh jobhunter-guard heartbeat with this install id")
    p.add_argument("--timeout", type=int, default=60)

    p = add_command(subparsers, "install version-check", cmd_version_check, callers="SH",
                    help="is an `openclaw --version` text new enough")
    p.add_argument("--text", required=True)

    add_command(subparsers, "install check-sqlite", cmd_check_sqlite, callers="SH",
                help="refuse a Python whose SQLite is older than 3.24 (upsert)")

    p = add_command(subparsers, "install render-shared-skills", cmd_render_shared_skills, callers="SH",
                    help="render shared-skills/*/SKILL.template.md to SKILL.md for --chat-control (or --remove)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--if-enabled", action="store_true",
                   help="only when an earlier --chat-control install recorded shared-skills in the manifest")
    g.add_argument("--remove", action="store_true", help="delete the rendered SKILL.md files")

    p = add_command(subparsers, "install upload-root", cmd_upload_root, callers="SH",
                    help="record OpenClaw's resolved browser upload directory in private/home.json")
    p.add_argument("--dir", help="the upload directory to use (overrides the recorded one)")
    p.add_argument("--tmpdir", action="append", default=[], help="a Gateway TMPDIR to look under (repeatable)")

    add_command(subparsers, "install mail-state", cmd_mail_state, callers="SH",
                help="email route and whether it is connected (use with --human: route=<r> connected=<0|1>)")
    add_command(subparsers, "install forget-mail-secret", cmd_forget_mail_secret, callers="SH",
                help="uninstall --purge: delete the app password item from the macOS Keychain")

    p = add_command(subparsers, "install owner", cmd_owner, callers="SH",
                    help="owner name, Gmail address, signature and chat number in private/config.json")
    p.add_argument("--missing", action="store_true",
                   help="list the owner fields that still hold placeholders (use with --human)")
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--gmail")
    p.add_argument("--phone", help="signature phone (empty to clear)")
    p.add_argument("--notify-channel", help="whatsapp, telegram, another OpenClaw channel id, or none")
    p.add_argument("--notify-to", help="your number (international form) or address on that channel")
    p.add_argument("--link", action="append", help="a signature link (repeatable)")
    p.add_argument("--no-links", action="store_true", help="remove every signature link")

    p = add_command(subparsers, "install consent-sites", cmd_consent_sites, callers="SH",
                    help="the sites the browser consent step asks about, with their current consent (use with --human)")
    p.add_argument("--site", action="append", help="only this site (repeatable)")

    p = add_command(subparsers, "install chrome-profiles", cmd_chrome_profiles, callers="SH",
                    help="Chrome profiles by display name from Chrome's Local State (use with --human)")
    p.add_argument("--pick", type=int, help="print folder, work flag and name of choice <n> (tab separated)")
    p.add_argument("--local-state", help="path of Chrome's Local State file (default: this computer's Chrome)")

    p = add_command(subparsers, "install consent-record", cmd_consent_record, callers="H",
                    help="record the owner's per-site consent in private/consent.json (owner PIN)")
    # not --grant: that is the CLI's global chat-grant option
    p.add_argument("--allow", action="append", dest="grant",
                   help="sites the owner allowed (comma separated, repeatable)")
    p.add_argument("--decline", action="append", help="sites the owner declined (comma separated, repeatable)")
    p.add_argument("--method", choices=list(ins.CONSENT_METHODS), required=True)
    p.add_argument("--chrome-profile", help="Chrome profile folder the cookies are copied from (chrome_import)")
    p.add_argument("--chrome-profile-name", help="that profile's display name")

    p = add_command(subparsers, "install consent-revoke", cmd_consent_revoke, callers="SH",
                    help="mark consent revoked for sites (or --all); prints what is left to re-import")
    p.add_argument("--site", action="append", help="site to revoke (repeatable)")
    p.add_argument("--all", action="store_true", dest="all_sites")

    add_command(subparsers, "install consent-imports", cmd_consent_imports, callers="SH",
                help="active chrome_import consents grouped by Chrome profile: <domains> TAB <folder> lines")

    p = add_command(subparsers, "install login-probe", cmd_login_probe, callers="SH",
                    help="the read-only page function of the login check, or --url the page to open for a site")
    p.add_argument("--site")
    p.add_argument("--url", action="store_true")

    p = add_command(subparsers, "install login-check", cmd_login_check, callers="SH",
                    help="verdict of a login check from `openclaw browser --json evaluate` output (verdict TAB text)")
    p.add_argument("--site", required=True)
    p.add_argument("--file", required=True)

    p = add_command(subparsers, "install route", cmd_route, callers="SH",
                    help="how ./jobhunter runs a jh.py command: pin, open, or refused (use with --human)")
    p.add_argument("words", nargs=argparse.REMAINDER, help="the command words and arguments, after --")


# ---------------------------------------------------------------- helpers
def _home() -> dict:
    h = paths.check_home_binding()
    for key in ("ws_root", "python"):
        if not h.get(key):
            raise Denied("E_CONFIG_INVALID", "private/home.json has no %s; run ./jobhunter init" % key)
    return h


def _repo(h: dict | None = None) -> str:
    return str((h or {}).get("repo") or paths.root())


def _load_file_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise Denied("E_NOT_FOUND", "file not found", data={"path": path})
    except OSError as exc:
        raise Denied("E_VALIDATION", "file is unreadable: %s" % exc, data={"path": path})
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except ValueError:
        # openclaw sometimes prints a banner line before the JSON; take the first JSON value in the text
        for i, ch in enumerate(text):
            if ch in "[{":
                try:
                    return json.JSONDecoder().raw_decode(text[i:])[0]
                except ValueError:
                    continue
        raise Denied("E_VALIDATION", "file is not JSON", data={"path": path})


def _lines(items) -> str:
    """--human text for a list. The CLI prints the envelope instead of an empty human text, so an empty list
    becomes a single newline (read by the shell scripts as no lines)."""
    text = "\n".join(items) if not isinstance(items, str) else items
    return text if text else "\n"


def _dispatch_mode(cfg: dict) -> str:
    return str(((cfg.get("dispatch") or {}).get("mode")) or "dispatcher")


# ---------------------------------------------------------------- spec commands
def cmd_check_path(args, ctx):
    data = ins.check_path(paths.REPO)
    return Result(data=data, message="the repo path is fine", human="OK %s" % data["repo"])


def cmd_render_crons(args, ctx):
    h = _home()
    repo = _repo(h)
    cfg = ins.load_config()
    crons = ins.load_crons()
    if args.alerts:
        cmds = ins.render_failure_alerts(crons, ins.known_job_ids(), cfg)
        msg = "%d failure-alert edits" % len(cmds) if cmds else "no failure alerts (owner notifications are off)"
    else:
        cmds = ins.render_cron_commands(crons, py=h["python"], repo=repo, tz=ins.detect_timezone(cfg),
                                        dispatch_mode=_dispatch_mode(cfg),
                                        lanes=(cfg.get("dispatch") or {}).get("lanes"))
        msg = "%d cron add commands" % len(cmds)
    return Result(data={"commands": cmds}, message=msg, human=_lines(ins.shell_lines(cmds)))


def cmd_render_workspaces(args, ctx):
    h = _home()
    res = ins.render_workspaces(ws_root=h["ws_root"], repo=_repo(h), py=h["python"], agents=ins.load_agents())
    conn = ctx.connect()
    from .. import db
    with db.tx(conn):
        for key, value in sorted(res["hashes"].items()):
            db.meta_set(conn, key, value, "install")
        db.log_event(conn, "install_render_workspaces", agents=[r["agent"] for r in res["rendered"]])
    ins.merge_manifest({"ws_root": h["ws_root"]})
    lines = ["%s -> %s" % (r["agent"], r["workspace"]) for r in res["rendered"]]
    return Result(data=res, message="rendered %d workspaces" % len(res["rendered"]), human="\n".join(lines))


def cmd_render_agents_patch(args, ctx):
    h = _home()
    agents = ins.load_agents()
    patch = ins.render_agents_patch(agents, ws_root=h["ws_root"], repo=_repo(h), py=h["python"], route=args.route)
    path = ins.write_json(os.path.join(ins.install_dir(), "agents.patch.json"), patch)
    ins.merge_manifest({"agents": [a["id"] for a in agents],
                        "config_paths": ['agents.entries["%s"]' % a["id"] for a in agents]})
    return Result(data={"path": path, "agents": sorted(patch["agents"]["entries"])},
                  message="agents config patch written", human=path)


def cmd_render_guard_config(args, ctx):
    h = _home()
    patch = ins.render_guard_config(repo=_repo(h), py=h["python"], cfg=ins.load_config())
    path = ins.write_json(os.path.join(ins.install_dir(), "guard.patch.json"), patch)
    ins.merge_manifest({"plugin": ins.GUARD_PLUGIN_ID,
                        "config_paths": ['plugins.entries["%s"]' % ins.GUARD_PLUGIN_ID]})
    return Result(data={"path": path}, message="guard config patch written", human=path)


def cmd_manifest(args, ctx):
    if args.show:
        m = ins.read_manifest()
        return Result(data=m, human=json.dumps(m, indent=2, sort_keys=True))
    doc = _load_file_json(ctx.input_path(args.set_file), default={})
    rows = doc.get("jobs") if isinstance(doc, dict) else doc
    if isinstance(rows, list):
        cfg = ins.load_config()
        res = ins.store_cron_ids(doc, ins.load_crons(), _dispatch_mode(cfg))
        if res["missing"]:
            raise Denied("E_PRECONDITION", "declared automations are missing from openclaw", data=res)
        msg = "stored %d automation ids" % len(res["cron_jobs"])
        if res["enabled_agent_jobs"]:
            msg += "; warning: agent jobs are enabled (the dispatcher starts them): %s" % ", ".join(
                res["enabled_agent_jobs"])
        return Result(data=res, message=msg, human=msg)
    if not isinstance(doc, dict):
        raise Denied("E_VALIDATION", "manifest --set needs a JSON object or a cron list")
    m = ins.merge_manifest(doc)
    return Result(data=m, message="manifest updated", human="manifest updated")


# ---------------------------------------------------------------- helper commands
def cmd_shell_env(args, ctx):
    h = ins.home_or_empty()
    return Result(data={"home_exists": bool(h)}, human=ins.shell_env(h))


def cmd_job_ids(args, ctx):
    cfg = ins.load_config()
    crons = ins.load_crons()
    keys = ins.select_jobs(crons, args.which, args.lane, _dispatch_mode(cfg))
    extra = _load_file_json(ctx.input_path(args.from_list), default={}) if args.from_list else None
    known = ins.known_job_ids(extra)
    ids = [known[k] for k in keys if k in known]
    missing = [k for k in keys if k not in known]
    if args.which == "all" and extra is not None:
        # uninstall: every jobhunter:* job openclaw knows, declared or not
        for k, v in ins.cron_jobs_from_list(extra).items():
            if v["id"] not in ids:
                ids.append(v["id"])
    if missing and args.which != "all":
        raise Denied("E_PRECONDITION", "automation ids are unknown; run ./install.sh", data={"missing": missing})
    return Result(data={"ids": ids, "keys": keys, "missing": missing}, human=_lines(ids))


def cmd_cron_summary(args, ctx):
    doc = _load_file_json(ctx.input_path(args.file), default={})
    text = ins.cron_summary(ins.load_crons(), doc)
    return Result(data={"jobs": ins.cron_jobs_from_list(doc)}, human=text)


def cmd_agents(args, ctx):
    agents = ins.load_agents()
    h = ins.home_or_empty()
    ws_root = h.get("ws_root", "")
    if args.missing_from:
        doc = _load_file_json(ctx.input_path(args.missing_from), default=[])
        rows = ins.missing_agents(agents, doc, ws_root)
    else:
        rows = [{"id": a["id"], "workspace": os.path.join(ws_root, a["role"]), "model": a["model"]} for a in agents]
    human = _lines(["%s\t%s\t%s" % (r["id"], r["workspace"], r["model"]) for r in rows])
    return Result(data={"agents": rows}, human=human)


def cmd_render_approvals(args, ctx):
    h = _home()
    current = _load_file_json(ctx.input_path(args.current), default={})
    doc = ins.merge_approvals(current, ins.load_agents(), repo=_repo(h), py=h["python"], remove=args.remove)
    path = ins.write_json(os.path.join(ins.install_dir(), "approvals.json"), doc)
    if not args.remove:
        ins.merge_manifest({"approvals_agents": sorted(a["id"] for a in ins.load_agents())})
    return Result(data={"path": path}, message="approvals document written", human=path)


def cmd_render_extradirs(args, ctx):
    h = _home()
    current = _load_file_json(ctx.input_path(args.current), default=[])
    patch, new = ins.extra_dirs_patch(current, _repo(h), remove=args.remove)
    if patch is None:
        return Result(data={"path": None, "extraDirs": new}, code="NOTHING_TO_DO",
                      message="skills.load.extraDirs already as wanted", human=_lines([]))
    path = ins.write_json(os.path.join(ins.install_dir(), "extradirs.patch.json"), patch)
    if not args.remove:
        ins.merge_manifest({"shared_skills_dir": os.path.join(_repo(h), "shared-skills")})
    return Result(data={"path": path, "extraDirs": new}, human=path)


def cmd_render_uninstall_patch(args, ctx):
    patch = ins.uninstall_patch(ins.load_agents())
    path = ins.write_json(os.path.join(ins.install_dir(), "uninstall.patch.json"), patch)
    return Result(data={"path": path}, human=path)


def cmd_render_headless_patch(args, ctx):
    path = ins.write_json(os.path.join(ins.install_dir(), "headless.patch.json"), ins.headless_patch())
    return Result(data={"path": path}, human=path)


def cmd_render_stayawake(args, ctx):
    h = _home()
    text = ins.render_stayawake_plist(_repo(h))
    path = os.path.join(ins.install_dir(), ins.STAYAWAKE_LABEL + ".plist")
    ins._write_private(path, text)
    return Result(data={"path": path, "label": ins.STAYAWAKE_LABEL}, human=path)


def _browsed_sites() -> list:
    """Browser sites the scout uses (U2 searches.enabled_sites: boards.sites plus the job sites picked in the
    interview). [] before the profile is confirmed or when that module is unavailable."""
    try:
        from .. import searches
        return list(searches.enabled_sites(searches.load_profile(), searches.load_config()))
    except Exception:   # the cookie import is optional; never fail it on another unit's error
        return []


def cmd_board_domains(args, ctx):
    doms = ins.enabled_board_domains(ins.load_config(), _browsed_sites())
    return Result(data={"domains": doms}, human=_lines(",".join(doms)))


def cmd_wait_guard(args, ctx):
    st = ins.wait_guard(max(0, min(int(args.timeout), 600)))
    if not st["fresh"]:
        raise Denied("E_GUARD_MISSING", "no fresh jobhunter-guard heartbeat for this install (%s)" % st["reason"],
                     data=st)
    return Result(data=st, message="guard heartbeat is fresh", human="guard heartbeat fresh (%ss)" % st["age_s"])


def cmd_version_check(args, ctx):
    v = ins.parse_version(args.text)
    ok = ins.version_ok(args.text)
    want = ".".join(str(x) for x in ins.MIN_OPENCLAW)
    if not ok:
        raise Denied("E_PRECONDITION", "OpenClaw %s or newer is needed; run: openclaw update" % want,
                     data={"found": ".".join(str(x) for x in v) if v else None, "minimum": want})
    return Result(data={"version": ".".join(str(x) for x in v), "minimum": want}, human="ok")


def cmd_check_sqlite(args, ctx):
    import sqlite3
    have = sqlite3.sqlite_version
    want = ".".join(str(x) for x in ins.MIN_SQLITE)
    if not ins.sqlite_ok():
        raise Denied("E_PRECONDITION", "SQLite %s or newer is needed; this python3 has %s. Use a newer python3 "
                     "(macOS: xcode-select --install; Linux: your distribution's python3)" % (want, have),
                     data={"found": have, "minimum": want})
    return Result(data={"sqlite": have, "minimum": want}, human="ok sqlite %s" % have)


def cmd_render_shared_skills(args, ctx):
    h = _home()
    repo = _repo(h)
    if args.remove:
        removed = ins.remove_shared_skills(repo=repo)
        if ins.read_manifest().get("shared_skills_dir"):
            ins.merge_manifest({"shared_skills_dir": None})
        return Result(data={"removed": removed}, message="removed %d rendered shared skills" % len(removed),
                      human=_lines(removed))
    if args.if_enabled and not ins.read_manifest().get("shared_skills_dir"):
        return Result(data={"rendered": []}, code="NOTHING_TO_DO", message="chat control is not installed",
                      human=_lines([]))
    res = ins.render_shared_skills(repo=repo, py=h["python"])
    return Result(data=res, message="rendered %d shared skills" % len(res["rendered"]),
                  human=_lines(res["rendered"]))


def cmd_upload_root(args, ctx):
    h = _home()
    path = ins.resolve_upload_root(explicit=args.dir, current=h.get("upload_root"), tmpdirs=args.tmpdir,
                                   repo=_repo(h))
    if h.get("upload_root") != path:
        ins.store_upload_root(path)
    return Result(data={"upload_root": path}, message="browser upload root %s" % path, human=path)


def _mail_state() -> dict:
    from .. import mail
    route = mail.route(ins.load_config())
    account = mail.stored_account()
    connected = False
    if account:
        conn = None
        try:
            from .. import db
            conn = db.connect(write=False)
            connected = bool(mail.is_connected(conn))
        except Exception:  # noqa: BLE001 (no database yet: fall back to the recorded account)
            connected = True
        finally:
            if conn is not None:
                conn.close()
    return {"route": route, "connected": connected, "account_recorded": bool(account)}


def cmd_mail_state(args, ctx):
    st = _mail_state()
    return Result(data=st, human="route=%s connected=%d" % (st["route"], 1 if st["connected"] else 0))


def cmd_forget_mail_secret(args, ctx):
    """Delete the Keychain item `openclaw-job-hunter.<install_id>` of the recorded Gmail account. The file store
    needs nothing here: it lives in private/secrets.json, which --purge deletes."""
    from .. import mail
    secrets = mail.load_secrets()
    account = mail.stored_account()
    store = secrets.get("mail_store") or ("file" if secrets.get("mail_app_password") else "keychain")
    if not account or store != "keychain" or not mail.keychain_available():
        return Result(data={"deleted": False, "store": store if account else None}, code="NOTHING_TO_DO",
                      message="no Keychain item to delete", human="no Keychain item to delete")
    deleted = mail.keychain_delete(account)
    service = mail.keychain_service()
    if not deleted:
        return Result(data={"deleted": False, "service": service}, code="NOTHING_TO_DO",
                      message="the Keychain item was not found", human="the Keychain item %s was not found" % service)
    return Result(data={"deleted": True, "service": service}, message="Keychain item deleted",
                  human="deleted the Keychain item %s" % service)


def cmd_owner(args, ctx):
    if args.missing:
        miss = ins.owner_missing(ins.load_config())
        return Result(data={"missing": miss}, message="%d owner fields to fill" % len(miss), human=_lines(miss))
    links = [] if args.no_links else args.link
    res = ins.set_owner(first_name=args.first_name, last_name=args.last_name, gmail_address=args.gmail,
                        phone=args.phone, notify_channel=args.notify_channel, notify_to=args.notify_to, links=links)
    lines = ["saved: %s" % ", ".join(res["changed"])] if res["changed"] else ["nothing changed"]
    if res["notify_changed"]:
        lines.append("chat number changed")
    if res["missing"]:
        lines.append("still to fill in private/config.json: %s" % ", ".join(res["missing"]))
    return Result(data=res, message=lines[0], human="\n".join(lines))


def cmd_route(args, ctx):
    from .. import auth, cli
    words = list(args.words or [])
    if words[:1] == ["--"]:
        words = words[1:]
    commands = {path: leaf.get_default("_jh_callers") or ""
                for path, leaf in cli.registered_commands(cli.build_parser()).items()}
    how, what = ins.wrapper_route(words, commands, list(auth.load_acl().get("human_only") or []))
    if how == "none":
        raise Denied("E_USAGE", what)
    return Result(data={"route": how, "command": what}, human="%s %s" % (how, what))


# ---------------------------------------------------------------- browser consent helpers
def cmd_consent_sites(args, ctx):
    rows = ins.consent_sites(ins.load_config(), _browsed_sites(), only=args.site)
    lines = ["%s\t%s\t%s\t%s" % (r["site"], r["status"], ",".join(r["domains"]), r["label"]) for r in rows]
    return Result(data={"sites": rows}, human=_lines(lines))


def cmd_chrome_profiles(args, ctx):
    path = args.local_state or ins.chrome_local_state_path()
    profs = ins.chrome_profiles(path)
    if not profs:
        raise Denied("E_NOT_FOUND", "no Chrome profile found in Chrome's Local State", data={"path": path})
    if args.pick is not None:
        if not 1 <= args.pick <= len(profs):
            raise Denied("E_VALIDATION", "choose a number from 1 to %d" % len(profs))
        p = profs[args.pick - 1]
        return Result(data=p, human="%s\t%d\t%s" % (p["dir"], 1 if p["work"] else 0, p["name"]))
    lines = []
    for i, p in enumerate(profs, 1):
        who = " <%s>" % p["email"] if p["email"] else ""
        line = "  %d. %s%s (folder %s)" % (i, p["name"], who, p["dir"])
        if p["work"]:
            line += "  WORK OR SCHOOL PROFILE: %s" % p["work_reason"]
        lines.append(line)
    return Result(data={"profiles": profs}, human="\n".join(lines))


def cmd_consent_record(args, ctx):
    if ctx.caller.cls != "human":
        raise Denied("E_HUMAN_ONLY", "consent needs the owner PIN (./jobhunter browser consent)")
    res = ins.record_consent(args.grant, args.decline, method=args.method, chrome_profile=args.chrome_profile,
                             chrome_profile_name=args.chrome_profile_name)
    conn = None
    try:
        from .. import db
        conn = db.connect()
        with db.tx(conn):
            db.log_event(conn, "browser_consent", granted=res["granted"], declined=res["declined"],
                         method=args.method)
    except Exception:   # noqa: BLE001 (the consent file is the record; the event log is a convenience)
        pass
    finally:
        if conn is not None:
            conn.close()
    lines = []
    if res["granted"]:
        lines.append("allowed: %s" % ", ".join(res["granted"]))
    if res["declined"]:
        lines.append("not allowed: %s" % ", ".join(res["declined"]))
    return Result(data=res, message=lines[0], human="\n".join(lines))


def _remaining_lines(res: dict) -> list:
    out = ["import\t%s\t%s" % (",".join(g["domains"]), g["chrome_profile"]) for g in res["remaining_imports"]]
    out += ["manual\t%s" % s for s in res["remaining_manual"]]
    return out


def cmd_consent_revoke(args, ctx):
    if not args.all_sites and not args.site:
        raise Denied("E_USAGE", "name a site or use --all")
    res = ins.revoke_consent(args.site, everything=args.all_sites)
    lines = ["revoked\t%s" % s for s in res["revoked"]] + _remaining_lines(res)
    code = "OK" if res["revoked"] else "NOTHING_TO_DO"
    msg = "revoked %s" % ", ".join(res["revoked"]) if res["revoked"] else "no active consent to revoke"
    return Result(data=res, code=code, message=msg, human=_lines(lines))


def cmd_consent_imports(args, ctx):
    res = ins.remaining_imports()
    return Result(data=res, human=_lines(_remaining_lines(res)))


def cmd_login_probe(args, ctx):
    if args.url:
        if args.site not in ins.CONSENT_SITES:
            raise Denied("E_VALIDATION", "unknown site %s" % args.site)
        url = ins.CONSENT_SITES[args.site][2]
        return Result(data={"url": url}, human=url)
    return Result(data={"fn": ins.LOGIN_PROBE_JS}, human=ins.LOGIN_PROBE_JS)


def cmd_login_check(args, ctx):
    if args.site not in ins.CONSENT_SITES:
        raise Denied("E_VALIDATION", "unknown site %s" % args.site)
    try:
        doc = _load_file_json(ctx.input_path(args.file), default=None)
    except Denied:
        doc = None
    probe = ins.find_probe(doc)
    expect = ((ins.load_config().get("owner") or {}).get("gmail_address")) if args.site == "gmail" else None
    verdict, text = ins.login_verdict(args.site, probe, expect)
    return Result(data={"site": args.site, "verdict": verdict, "message": text}, message=text,
                  human="%s\t%s" % (verdict, text))
