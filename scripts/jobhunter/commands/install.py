"""`jh.py install ...` commands (design 3.4 Install; owned by U7).

Spec commands: check-path, render-crons (also --repair), render-workspaces, render-agents-patch,
render-guard-config, manifest.
CLI route commands (CLI-ROUTE design 6, 9): cron-id, run-preflight (heartbeat and drift check before every
`cron run` of the wrapper and install.sh), verify-exec-policy (the effective exec policy and host approvals of
every jobhunter agent; --fix unsets stale keys), identity-boundary (other agents with an unconfined shell),
foreign-jobs (automations outside the manifest that run a jobhunter agent), workshop-mode and
render-workshop-patch (OpenClaw's weekly Skill Workshop reviews), render-confine-patch (the fail-closed config of
an install that stopped before its agents were restricted), probe-stamp, cli-route, claude-check (claude flags,
and the Claude Code version each jobhunter model needs) and oc-doctor (`openclaw doctor --lint --json` judged by
finding severity).
Helper commands used by install.sh, uninstall.sh and ./jobhunter (bash 3.2 cannot parse JSON, so these print
plain lines with --human): shell-env, job-ids, cron-summary, agents, render-approvals, render-extradirs,
render-uninstall-patch, render-headless-patch, render-stayawake, board-domains, wait-guard, pin-hook, version-check,
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
    g = p.add_mutually_exclusive_group()
    g.add_argument("--alerts", action="store_true", help="render the failure-alert edits for known job ids")
    g.add_argument("--repair", metavar="<cron-list.json>",
                   help="full-field cron edit lines for jobs that differ from their declaration")
    p.add_argument("--replace", action="store_true",
                   help="with --repair: cron rm and cron add for jobs that still differ (keeps the enabled state)")
    p.add_argument("--check", action="store_true",
                   help="with --repair: render nothing; fail with E_CRON_DRIFT when any job differs")
    p.add_argument("--report", action="store_true",
                   help="with --repair: render nothing; one line per job that differs (field names only) or is "
                        "missing from a list that holds other jobhunter jobs")

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
    p.add_argument("--proof-version", type=int, help="also require this identity proof version (2)")

    add_command(subparsers, "install pin-hook", cmd_pin_hook, callers="SH",
                help="has the guard's tool pin hook (before_prompt_build) ever run? (doctor, informational)")

    p = add_command(subparsers, "install version-check", cmd_version_check, callers="SH",
                    help="is an `openclaw --version` text new enough")
    p.add_argument("--text", required=True)
    p.add_argument("--minimum", help="compare with this version instead of the supported minimum (exit 11 below it)")

    p = add_command(subparsers, "install cron-id", cmd_cron_id, callers="SH",
                    help="the job id of one declared automation (use with --human)")
    p.add_argument("key", help="declaration key, for example jobhunter:dispatch")
    p.add_argument("--from-list", metavar="<json-file>", help="read the id from a fresh cron list instead")
    p.add_argument("--if-enabled", action="store_true", help="with --from-list: print the id only when enabled")

    p = add_command(subparsers, "install run-preflight", cmd_run_preflight, callers="SH",
                    help="guard heartbeat (proof version 2) and drift check of one job before a cron run; "
                         "prints its id")
    p.add_argument("key", help="declaration key, for example jobhunter:probe-evaluator")
    p.add_argument("--with-wait", action="store_true",
                   help="print the id, a tab and the wait in minutes (job timeout plus 5, for cron run --wait-timeout)")

    p = add_command(subparsers, "install verify-exec-policy", cmd_verify_exec_policy, callers="SH",
                    help="read back the effective exec policy of every jobhunter agent (read only openclaw calls)")
    p.add_argument("--fix", action="store_true",
                   help="unset exec keys the installer does not write, then read back once more")

    add_command(subparsers, "install identity-boundary", cmd_identity_boundary, callers="SH",
                help="other OpenClaw agents with an unconfined shell (use with --human: one line each)")

    p = add_command(subparsers, "install foreign-jobs", cmd_foreign_jobs, callers="SH",
                    help="automations that run a jobhunter agent but are not in the install manifest")
    p.add_argument("--from-list", metavar="<json-file>",
                   help="read `openclaw cron list --all --json` output instead of listing (read only)")
    p.add_argument("--check", action="store_true", help="fail with E_PRECONDITION when such a job is enabled")

    p = add_command(subparsers, "install workshop-mode", cmd_workshop_mode, callers="SH",
                    help="OpenClaw's Skill Workshop mode from `config get %s --json` output (auto when unset)"
                         % ins.WORKSHOP_MODE_PATH)
    p.add_argument("--current", required=True, metavar="<file>")

    add_command(subparsers, "install render-workshop-patch", cmd_render_workshop_patch, callers="SH",
                help="config patch setting %s to %s (the owner agreed in install.sh)"
                     % (ins.WORKSHOP_MODE_PATH, ins.WORKSHOP_SAFE_MODE))

    p = add_command(subparsers, "install render-confine-patch", cmd_render_confine_patch, callers="SH",
                    help="fail-closed config patch (exec deny, every tool denied) for the jobhunter agents present")
    p.add_argument("--present", required=True, metavar="<json-file>", help="`openclaw agents list --json` output")

    p = add_command(subparsers, "install probe-stamp", cmd_probe_stamp, callers="SH",
                    help="versions the identity probes last passed with (read --match or write)")
    p.add_argument("action", choices=["read", "write"])
    p.add_argument("--match", action="store_true", help="with read: fail unless the stamp matches these versions")
    p.add_argument("--openclaw", default="", help="the `openclaw --version` text")
    p.add_argument("--claude", default="", help="the `claude --version` text")
    p.add_argument("--route", choices=["cli", "api_key"], default="cli")

    p = add_command(subparsers, "install cli-route", cmd_cli_route, callers="SH",
                    help="the CLI route switches in private/home.json (identity carriers, QC reply, tool mode)")
    p.add_argument("action", choices=["show", "get", "set"])
    p.add_argument("field", nargs="?", choices=["carriers", "qc_reply", "cli_tools"], help="with get")
    p.add_argument("--carriers", choices=sorted(ins.CARRIER_CHOICES))
    p.add_argument("--qc-reply", choices=list(ins.QC_REPLY_MODES))
    p.add_argument("--cli-tools", choices=list(ins.CLI_TOOL_MODES))
    p.add_argument("--i-accept-reduced-protection", action="store_true", dest="accept_reduced")

    p = add_command(subparsers, "install model-route", cmd_model_route, callers="SH",
                    help="the model route this clone was installed with (cli: your Claude login; api_key)")
    p.add_argument("action", choices=["get", "set"])
    p.add_argument("route", nargs="?", choices=list(ins.MODEL_ROUTES), help="with set")

    p = add_command(subparsers, "install claude-check", cmd_claude_check, callers="SH",
                    help="refuse a claude CLI whose --help lacks the flags restricted runs need, or (with "
                         "--version-text) that is older than a jobhunter model needs")
    p.add_argument("--help-file", required=True, metavar="<file>", help="output of `claude --help`")
    p.add_argument("--version-text", metavar="<text>",
                   help="output of `claude --version`, compared with the Claude Code each jobhunter model needs")

    p = add_command(subparsers, "install oc-doctor", cmd_oc_doctor, callers="SH",
                    help="judge `openclaw doctor --lint --json` output by finding severity (./jobhunter doctor)")
    p.add_argument("--file", required=True, metavar="<file>", help="its standard output")
    p.add_argument("--stderr-file", metavar="<file>", help="its standard error (shown when the output is not JSON)")
    p.add_argument("--rc", type=int, default=0, help="its exit status")

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
    p.add_argument("--method", choices=list(ins.CONSENT_METHODS))
    p.add_argument("--chrome-profile", help="Chrome profile folder the cookies are copied from (chrome_import)")
    p.add_argument("--chrome-profile-name", help="that profile's display name")

    p.add_argument("--capability", action="append", choices=("email_codes", "ats_accounts"),
                   help="with --decline: record a No for email codes or site accounts on these sites")

    p = add_command(subparsers, "install capability-sites", cmd_capability_sites, callers="SH",
                    help="ATS sites and capabilities the consent step asks about: site TAB capability TAB status TAB label")
    p.add_argument("--site", action="append")

    p = add_command(subparsers, "install browser-cdp", cmd_browser_cdp, callers="SH",
                    help="record the jobhunter profile's loopback CDP port from `openclaw config get "
                         "browser.profiles.jobhunter --json` output in a file")
    p.add_argument("--file", required=True)

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
    kw = dict(py=h["python"], repo=repo, ws_root=h["ws_root"], tz=ins.detect_timezone(cfg),
              dispatch_mode=_dispatch_mode(cfg), lanes=(cfg.get("dispatch") or {}).get("lanes"))
    if (args.replace or args.check or args.report) and not args.repair:
        raise Denied("E_USAGE", "--replace, --check and --report go with --repair <cron list file>")
    if args.report and (args.replace or args.check):
        raise Denied("E_USAGE", "--report goes with --repair alone")
    if args.alerts:
        cmds = ins.render_failure_alerts(crons, ins.known_job_ids(), cfg)
        msg = "%d failure-alert edits" % len(cmds) if cmds else "no failure alerts (owner notifications are off)"
    elif args.repair:
        doc = _load_file_json(ctx.input_path(args.repair), default={})
        res = ins.repair_commands(crons, doc, replace=args.replace, **kw)
        if args.report:
            listed = ins.listed_jobs(doc)
            # a first install lists no jobhunter job: nothing is missing then, every job is new
            missing = sorted(j["key"] for j in ins.declared_jobs(crons) if j["key"] not in listed) if listed else []
            lines = ["%s (%s differed from its declaration)" % (k, ", ".join(v))
                     for k, v in sorted(res["drift"].items())]
            lines += ["%s (it was missing)" % k for k in missing]
            return Result(data={"drift": res["drift"], "missing": missing},
                          message="%d automations differ from their declaration, %d are missing" % (
                              len(res["drift"]), len(missing)), human=_lines(lines))
        if args.check:
            if res["drift"]:
                raise Denied("E_CRON_DRIFT", "automations differ from their declaration after the repair: %s"
                             % "; ".join("%s (%s)" % (k, ", ".join(v)) for k, v in sorted(res["drift"].items())),
                             data={"drift": res["drift"]})
            return Result(data={"drift": {}}, message="every automation matches its declaration",
                          human=_lines([]))
        cmds = res["commands"]
        msg = ("%d automations differ from their declaration: %s" % (len(res["drift"]), ", ".join(sorted(res["drift"])))
               if res["drift"] else "every automation matches its declaration")
        return Result(data={"commands": cmds, "drift": res["drift"]}, message=msg, human=_lines(ins.shell_lines(cmds)))
    else:
        cmds = ins.render_cron_commands(crons, **kw)
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
    cr = ins.read_cli_route(h)
    patch = ins.render_agents_patch(agents, ws_root=h["ws_root"], repo=_repo(h), py=h["python"], route=args.route,
                                    cli_tools=cr["cli_tools"], accept_reduced=cr["accepted_reduced_protection"],
                                    qc_reply=cr["qc_reply"])
    path = ins.write_json(os.path.join(ins.install_dir(), "agents.patch.json"), patch)
    ins.merge_manifest({"agents": [a["id"] for a in agents],
                        "config_paths": ['agents.entries["%s"]' % a["id"] for a in agents]})
    return Result(data={"path": path, "agents": sorted(patch["agents"]["entries"])},
                  message="agents config patch written", human=path)


def cmd_render_guard_config(args, ctx):
    h = _home()
    cr = ins.read_cli_route(h)
    patch = ins.render_guard_config(repo=_repo(h), py=h["python"], cfg=ins.load_config(), ws_root=h["ws_root"],
                                    state_dir=ins.oc_state_dir(h.get("oc_profile") or None),
                                    carriers=cr["carriers"], cli_tools=cr["cli_tools"],
                                    qc_verdict_file=cr["qc_reply"] == "file")
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
        crons = ins.load_crons()
        h = _home()
        specs = ins.cron_specs(crons, py=h["python"], repo=_repo(h), ws_root=h["ws_root"],
                               qc_reply=ins.read_cli_route(h)["qc_reply"])
        res = ins.store_cron_ids(doc, crons, _dispatch_mode(cfg), specs=specs)
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
    doc = ins.merge_approvals(current, ins.load_agents(), repo=_repo(h), py=h["python"], remove=args.remove,
                              carrier=ins.read_cli_route(h)["carriers"])
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
    st = ins.wait_guard(max(0, min(int(args.timeout), 600)), proof_version=args.proof_version)
    if not st["fresh"]:
        why = st["reason"]
        if why == "old_guard":
            why = "the guard reports identity proof version %s, not %s; restart the Gateway" % (
                st.get("proof_version"), args.proof_version)
        raise Denied("E_GUARD_MISSING", "no fresh jobhunter-guard heartbeat for this install (%s)" % why, data=st)
    return Result(data=st, message="guard heartbeat is fresh", human="guard heartbeat fresh (%ss)" % st["age_s"])


def cmd_pin_hook(args, ctx):
    st = ins.guard_status()
    line = ins.pin_hook_line(st)
    data = {k: st.get(k) for k in ("fresh", "reason", "pin_tool_surface", "pin_hook_reported", "pin_hook_seen_at")}
    data["never_seen"] = line.startswith("WARN")
    return Result(data=data, message=line.split("\n", 1)[0][6:], human=line)


def cmd_version_check(args, ctx):
    v = ins.parse_version(args.text)
    minimum = ins.MIN_OPENCLAW
    if args.minimum:
        minimum = ins.parse_version(args.minimum)
        if minimum is None:
            raise Denied("E_USAGE", "--minimum must look like 2026.9.7")
    ok = ins.version_ok(args.text, minimum)
    want = ".".join(str(x) for x in minimum)
    if not ok:
        msg = ("OpenClaw %s or newer is needed (this one is %s). Update it with: openclaw update, then run "
               "./install.sh again" % (want, ".".join(str(x) for x in v) if v else "unknown")
               if not args.minimum else "OpenClaw is older than %s" % want)
        raise Denied("E_PRECONDITION", msg, data={"found": ".".join(str(x) for x in v) if v else None,
                                                  "minimum": want})
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


# ---------------------------------------------------------------- CLI route commands (CLI-ROUTE design 6, 9)
def _ocrun(name: str):
    """A jobhunter.ocrun function of the core (U1). Missing means an incomplete checkout: refuse, never skip."""
    from .. import ocrun
    fn = getattr(ocrun, name, None)
    if fn is None:
        raise Denied("E_PRECONDITION", "this jh.py has no ocrun.%s; update the repo (git pull) and run ./install.sh"
                     % name)
    return fn


def _declared(key: str) -> dict:
    job = ins._job(ins.load_crons(), key)
    if not job:
        raise Denied("E_NOT_FOUND", "%s is not a declared automation" % key)
    return job


def cmd_cron_id(args, ctx):
    _declared(args.key)
    if args.if_enabled and not args.from_list:
        raise Denied("E_USAGE", "--if-enabled goes with --from-list")
    if args.from_list:
        found = ins.cron_jobs_from_list(_load_file_json(ctx.input_path(args.from_list), default={})).get(args.key)
        if found is None or (args.if_enabled and not found["enabled"]):
            return Result(data={"id": None, "enabled": bool(found and found["enabled"])}, code="NOTHING_TO_DO",
                          message="%s is %s" % (args.key, "missing" if found is None else "disabled"),
                          human=_lines([]))
        return Result(data={"id": found["id"], "enabled": found["enabled"]}, human=found["id"])
    jid = ins.known_job_ids().get(args.key)
    if not jid:
        raise Denied("E_NOT_FOUND", "the automation %s is not installed; run ./install.sh again" % args.key)
    return Result(data={"id": jid}, human=jid)


def cmd_run_preflight(args, ctx):
    """Heartbeat with identity proof version 2, then the drift check of the job against the manifest (U1
    ocrun.preflight). Every `cron run` of ./jobhunter and install.sh goes through here."""
    job = _declared(args.key)
    if job["kind"] == "agent-oneshot":
        raise Denied("E_USAGE", "the QC one-shot job is run by jh.py qc, not by hand")
    jid = _ocrun("preflight")(args.key)
    if not isinstance(jid, str) or not jid:
        raise Denied("E_NOT_FOUND", "the automation %s is not installed; run ./install.sh again" % args.key)
    wait_m = (int(job["timeout_s"]) + 300 + 59) // 60          # the job timeout plus 5 minutes (m7)
    data = {"id": jid, "key": args.key, "wait": "%dm" % wait_m}
    return Result(data=data, message="%s may run" % args.key,
                  human="%s\t%dm" % (jid, wait_m) if args.with_wait else jid)


def _exec_config(agent_id: str):
    """agents.entries.<id>.tools.exec from `openclaw config get --json` (read only); {} when unset."""
    from .. import ocrun
    r = ocrun.run(ocrun.oc_argv("config", "get", "agents.entries.%s.tools.exec" % agent_id, "--json"), 60)
    doc = ocrun.last_json(r["stdout"]) if r["ok"] else None
    return doc if isinstance(doc, dict) else {}


def _approvals_doc():
    """`openclaw approvals get --json` (read only), parsed; None when it cannot be read."""
    from .. import ocrun
    r = ocrun.run(ocrun.oc_argv("approvals", "get", "--json"), 60)
    doc = ocrun.last_json(r["stdout"]) if r["ok"] else None
    return doc if isinstance(doc, dict) else None


def _exec_policy_report(agents: list, cr: dict, h: dict) -> dict:
    eff_fn = _ocrun("effective_exec")
    appr = _approvals_doc()
    if appr is None:
        appr_problems = {a["id"]: ["openclaw approvals get --json could not be read"] for a in agents}
    else:
        appr_problems = ins.approvals_problems(appr, agents, repo=_repo(h), py=h["python"], carrier=cr["carriers"])
    out = {}
    for a in agents:
        try:
            eff = eff_fn(a["id"])
        except Denied as d:
            eff = {"error": d.message}
        problems = ins.exec_policy_problems(a, eff, cr["cli_tools"])
        stale = ins.stale_exec_keys(_exec_config(a["id"]), a, cr["cli_tools"], cr["accepted_reduced_protection"])
        if stale:
            problems.append("exec keys the installer does not write: %s" % ", ".join(stale))
        problems += ["approvals: %s" % p for p in appr_problems.get(a["id"], [])]
        out[a["id"]] = {"effective": eff, "problems": problems, "stale_keys": stale}
    return out


def cmd_verify_exec_policy(args, ctx):
    """6.1 read-back: for every jobhunter agent the effective exec security is allowlist (deny for jobhunter-qc;
    full only in mode N), ask off, elevated off, and its host approvals entry has ask off and askFallback deny.
    The host approvals document must add nothing for a jobhunter agent: no agents["*"] allowlist entries, no
    entries of its own beyond the rendered one, autoAllowSkills false (install.approvals_problems).
    --fix removes exec keys the installer does not write (`config unset`) and reads back once more."""
    h = _home()
    cr = ins.read_cli_route(h)
    agents = ins.load_agents()
    report = _exec_policy_report(agents, cr, h)
    unset = []
    if args.fix and any(r["stale_keys"] for r in report.values()):
        from .. import ocrun
        for aid, r in sorted(report.items()):
            for key in r["stale_keys"]:
                path = "agents.entries.%s.tools.exec.%s" % (aid, key)
                res = ocrun.run(ocrun.oc_argv("config", "unset", path), 60)
                if not res["ok"]:
                    raise Denied("E_PRECONDITION", "openclaw config unset %s failed: %s" % (path, res["error"]))
                unset.append(path)
        report = _exec_policy_report(agents, cr, h)
    bad = {aid: r["problems"] for aid, r in report.items() if r["problems"]}
    lines = ["unset %s (an exec key the installer does not write)" % path for path in unset]
    for aid in sorted(report):
        eff = report[aid]["effective"]
        state = "FAIL" if report[aid]["problems"] else "ok"
        lines.append("%-5s %s: security %s, ask %s, elevated %s, approvals ask %s, askFallback %s%s" % (
            state, aid, eff.get("security"), eff.get("ask"), ins._elevated_on(eff.get("elevated")),
            eff.get("approvals_ask"), eff.get("approvals_fallback"),
            ("; " + "; ".join(report[aid]["problems"])) if report[aid]["problems"] else ""))
    data = {"agents": report, "unset": unset, "cli_tools": cr["cli_tools"]}
    if bad:
        raise Denied("E_PRECONDITION", "the effective exec policy of %s is not allowlist with ask off and only "
                     "its own jh.py allowlist entry: %s" % (
            ", ".join(sorted(bad)), " | ".join("%s: %s" % (k, "; ".join(v)) for k, v in sorted(bad.items()))),
            data=data)
    return Result(data=data, message="exec policy of every jobhunter agent verified", human="\n".join(lines))


def cmd_foreign_jobs(args, ctx):
    """Automations that run a jobhunter agent but are not in the install manifest (OpenClaw's weekly
    skill-collection-review:<agent> jobs, a leftover QC one-shot, a job added by hand). They bypass the drift check
    and the pause, so an enabled one fails --check; disabled ones are listed only."""
    if args.from_list:
        doc = _load_file_json(ctx.input_path(args.from_list), default={})
    else:
        from .. import ocrun
        res = ocrun.cron_list()
        if not res["ok"]:
            raise Denied("E_PRECONDITION", "openclaw cron list failed: %s" % res["error"])
        doc = res["doc"]
    ids = [a["id"] for a in ins.load_agents()]
    jobs = ins.foreign_agent_jobs(doc, ids, ins.known_job_ids())
    enabled = [j for j in jobs if j["enabled"]]
    disabled = [j for j in jobs if not j["enabled"]]
    why = {"skill_review": "OpenClaw's weekly Skill Workshop review; set %s to %s" % (ins.WORKSHOP_MODE_PATH,
                                                                                     ins.WORKSHOP_SAFE_MODE),
           "qc_oneshot": "a leftover QC one-shot; remove it: openclaw cron rm <id>",
           "other": "not created by this install; disable or remove it: openclaw cron disable <id>"}
    lines = ["enabled automation %s (%s) runs %s outside the install: %s" % (
        j["key"] or j["name"] or "?", j["id"], j["agent"], why[j["kind"]].replace("<id>", j["id"])) for j in enabled]
    data = {"enabled": enabled, "disabled": disabled}
    if args.check and enabled:
        raise Denied("E_PRECONDITION", "; ".join(lines), data=data)
    msg = "%d enabled and %d disabled automations outside the install run a jobhunter agent" % (len(enabled),
                                                                                               len(disabled))
    human = lines + (["%d disabled automations outside the install name a jobhunter agent (%s)" % (
        len(disabled), ", ".join(sorted(set(j["kind"] for j in disabled))))] if disabled else [])
    return Result(data=data, message=msg, human=_lines(human))


def cmd_workshop_mode(args, ctx):
    try:
        with open(ctx.input_path(args.current), "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    mode = ins.workshop_mode(text)
    return Result(data={"mode": mode, "weekly_reviews": mode == "auto"}, human=mode)


def cmd_render_workshop_patch(args, ctx):
    path = ins.write_json(os.path.join(ins.install_dir(), "workshop.patch.json"), ins.workshop_patch())
    return Result(data={"path": path}, human=path)


def cmd_render_confine_patch(args, ctx):
    doc = _load_file_json(ctx.input_path(args.present), default=[])
    patch = ins.confine_patch(ins.load_agents(), ins.agent_ids_from_list(doc))
    if patch is None:
        return Result(data={"path": None, "agents": []}, code="NOTHING_TO_DO",
                      message="no jobhunter agent is present", human=_lines([]))
    path = ins.write_json(os.path.join(ins.install_dir(), "confine.patch.json"), patch)
    return Result(data={"path": path, "agents": sorted(patch["agents"]["entries"])}, human=path)


def cmd_identity_boundary(args, ctx):
    """4.4: every other OpenClaw agent whose effective shell is unconfined. Information for doctor and install
    (printed in red); the exit status is 0 either way."""
    listed = _ocrun("agents_list")()
    if listed is None:
        return Result(data={"unconfined": [], "checked": [], "error": "openclaw config get agents.entries failed"},
                      code="NOTHING_TO_DO", message="the OpenClaw agent list could not be read", human=_lines([]))
    ids = [i for i in listed if isinstance(i, str) and not i.startswith("jobhunter-")]
    eff_fn = _ocrun("effective_exec")
    rows = []
    for aid in sorted(ids):
        try:
            eff = eff_fn(aid)
        except Denied:
            eff = {}
        why = ins.unconfined(eff)
        if why:
            rows.append({"agent": aid, "why": why, "line": ins.boundary_line(aid, why)})
    return Result(data={"unconfined": rows, "checked": sorted(ids)},
                  message="%d agents with an unconfined shell" % len(rows),
                  human=_lines([r["line"] for r in rows]))


def cmd_probe_stamp(args, ctx):
    h = _home()
    if args.action == "write":
        stamp = ins.write_probe_stamp(ins.stamp_versions(openclaw=args.openclaw, claude=args.claude,
                                                         route=args.route, h=h))
        return Result(data=stamp, message="probe stamp written", human="probe stamp %s" % stamp["at"])
    st = ins.probe_stamp()
    if args.match:
        cur = ins.stamp_versions(openclaw=args.openclaw, claude=args.claude, route=args.route, h=h)
        if not ins.probe_stamp_matches(cur):
            raise Denied("E_PRECONDITION", "the identity probes have not passed for these versions",
                         data={"stamp": st, "current": cur})
    return Result(data=st, human="last passed %s" % st["at"] if st.get("at") else "never passed")


def cmd_cli_route(args, ctx):
    h = _home()
    if args.action == "set":
        if not (args.carriers or args.qc_reply or args.cli_tools):
            raise Denied("E_USAGE", "cli-route set needs --carriers, --qc-reply or --cli-tools")
        cr = ins.set_cli_route(carriers=args.carriers, qc_reply=args.qc_reply, cli_tools=args.cli_tools,
                               accept_reduced=args.accept_reduced)
    else:
        if args.accept_reduced or args.carriers or args.qc_reply or args.cli_tools:
            raise Denied("E_USAGE", "options go with cli-route set")
        cr = ins.read_cli_route(h)
    if args.action == "get":
        if not args.field:
            raise Denied("E_USAGE", "cli-route get <carriers|qc_reply|cli_tools>")
        val = cr[args.field]
        return Result(data={args.field: val}, human="+".join(val) if isinstance(val, list) else str(val))
    human = "carriers=%s qc_reply=%s cli_tools=%s" % ("+".join(cr["carriers"]), cr["qc_reply"], cr["cli_tools"])
    return Result(data=cr, message=human, human=human)


def cmd_model_route(args, ctx):
    if args.action == "set":
        if not args.route:
            raise Denied("E_USAGE", "model-route set cli|api_key")
        r = ins.set_model_route(args.route)
    else:
        if args.route:
            raise Denied("E_USAGE", "model-route get takes no route")
        r = ins.recorded_model_route()
    return Result(data={"model_route": r}, human=r)


def cmd_claude_check(args, ctx):
    try:
        with open(ctx.input_path(args.help_file), "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise Denied("E_VALIDATION", "cannot read the claude --help output: %s" % exc)
    missing = ins.claude_flags_missing(text)
    if missing:
        raise Denied("E_PRECONDITION", "this Claude Code is too old for restricted agent runs (its --help has no "
                     "%s). Update it with: claude update, then run ./install.sh again" % ", ".join(missing), data={"missing": missing})
    data = {"missing": []}
    if args.version_text is None:
        return Result(data=data, human="ok")
    res = ins.claude_model_check(args.version_text, ins.load_agents(), ins.load_crons())
    data.update(res)
    if res["too_old"]:
        raise Denied("E_PRECONDITION", "Claude Code %s is too old for %s; update it: claude update" % (
            res["version"], "; ".join("%s of %s (%s or newer is needed)" % (
                r["model"], ", ".join(r["agents"]), r["needs"]) for r in res["too_old"])) + ", then run ./install.sh again", data=data)
    if res["unknown"]:
        return Result(data=data, human="the Claude Code version is unknown; %s" % "; ".join(
            "%s of %s needs %s or newer" % (r["model"], ", ".join(r["agents"]), r["needs"]) for r in res["unknown"]))
    return Result(data=data, human="ok")


def cmd_oc_doctor(args, ctx):
    """./jobhunter doctor: `openclaw doctor --lint --json` judged by finding severity (install.doctor_verdict).
    Fails (E_PRECONDITION, the lines as its message) on an error finding; warnings and the expected findings of
    the jobhunter agents are printed lines."""
    from .. import ocrun

    def read(path):
        if not path:
            return ""
        try:
            with open(ctx.input_path(path), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""
    text = read(args.file)
    doc = ocrun.last_json(text)
    output = (text + "\n" + read(args.stderr_file)).splitlines()
    v = ins.doctor_verdict(doc, args.rc, [a["id"] for a in ins.load_agents()], output)
    if not v["ok"]:
        raise Denied("E_PRECONDITION", "\n".join(v["lines"]))
    return Result(data=v, message="openclaw doctor: %d warnings, no errors" % len(v["warnings"]),
                  human=_lines(v["lines"]))


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
    if args.capability:
        if args.grant or not args.decline:
            raise Denied("E_USAGE", "--capability records a No only (--decline <site>); allow with "
                         "./jobhunter browser consent")
        from .. import db, identity
        conn = ctx.connect()
        with db.tx(conn):
            res = identity.decline_capability(conn, args.capability, args.decline, by="human:cli")
        return Result(data=res, human="not allowed: %s" % ", ".join("%s on %s" % (r["capability"], r["site"])
                                                                   for r in res["declined"]))
    if not args.method:
        raise Denied("E_USAGE", "--method is required")
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


def cmd_capability_sites(args, ctx):
    rows = ins.capability_sites(ins.load_config(), only=args.site)
    return Result(data={"sites": rows}, human=_lines(["%s\t%s\t%s\t%s" % (r["site"], r["capability"], r["status"],
                                                                         r["label"]) for r in rows]))


def cmd_browser_cdp(args, ctx):
    with open(os.path.realpath(args.file), "r", encoding="utf-8") as fh:
        port = ins.cdp_port_from(fh.read())
    if port is None:
        raise Denied("E_NOT_FOUND", "no cdpPort in the jobhunter browser profile; code-owned steps stay off")
    res = ins.record_browser_cdp(port)
    return Result(data=res, human="browser_cdp port %d" % port)
