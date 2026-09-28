"""Fake U1 core commands for the install tests (owned by U7; design 11 rule 4).

Copied into the temporary repo copy as scripts/jobhunter/commands/zz_u7_fake.py. It registers only the
commands that are still missing when it runs (it sorts last), so once U1's real `init`, `selftest`, `pause`,
`unpause` and `status` land, the install test exercises the real ones.

`init` follows the contract install.sh relies on: it writes private/home.json from the environment variables
JH_OC_BIN, JH_OC_PROFILE and JH_PYTHON (ws_root under ~/.openclaw-job-hunter/<install_id>/workspaces),
creates private/guard.key and initialises the database.
"""
from __future__ import annotations

import json
import os
import secrets

from jobhunter import db, paths
from jobhunter.canon import new_uid, now
from jobhunter.commands import add_command


def _missing(subparsers, name: str) -> bool:
    return name not in subparsers.choices


def register(subparsers):
    if _missing(subparsers, "init"):
        p = add_command(subparsers, "init", cmd_init, callers="SH", help="(fake) create local state")
        p.add_argument("--force-examples", action="store_true")
    if _missing(subparsers, "selftest"):
        p = add_command(subparsers, "selftest", cmd_ok, callers="SH", help="(fake) selftest")
        p.add_argument("--offline", action="store_true")
    if _missing(subparsers, "pause"):
        p = add_command(subparsers, "pause", cmd_pause, callers="SHC", help="(fake) pause")
        p.add_argument("--scope", default="all")
        p.add_argument("--reason")
    if _missing(subparsers, "unpause"):
        p = add_command(subparsers, "unpause", cmd_ok, callers="H", help="(fake) unpause")
        p.add_argument("--scope", default="all")
    if _missing(subparsers, "status"):
        add_command(subparsers, "status", cmd_ok, callers="SHCR", help="(fake) status")


def cmd_ok(args, ctx):
    return {"fake": True}


def cmd_pause(args, ctx):
    os.makedirs(paths.state_dir(), exist_ok=True)
    with open(paths.paused_file(), "w", encoding="utf-8") as fh:
        fh.write(now() + "\n")
    return {"paused": args.scope}


def cmd_init(args, ctx):
    created = []
    for d in (paths.private_dir(), paths.state_dir(), paths.logs_dir(), paths.exports_dir()):
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700)
            created.append(d)
    hf = paths.home_file()
    if not os.path.exists(hf):
        iid = new_uid("I", 8)
        home = os.path.expanduser("~")
        data = {"install_id": iid, "repo": paths.root(), "db_path": paths.db_path(),
                "ws_root": os.path.join(home, ".openclaw-job-hunter", iid, "workspaces"),
                "oc_bin": ctx.env.get("JH_OC_BIN", ""), "oc_profile": ctx.env.get("JH_OC_PROFILE", ""),
                "python": ctx.env.get("JH_PYTHON", ""), "created_at": now()}
        with open(hf, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.chmod(hf, 0o600)
        created.append(hf)
    key = os.path.join(paths.private_dir(), "guard.key")
    if not os.path.exists(key):
        with open(key, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(key, 0o600)
    h = paths.home()
    os.makedirs(h["ws_root"], mode=0o700, exist_ok=True)
    conn = db.init_db()
    conn.close()
    return {"created": created, "install_id": h["install_id"], "ws_root": h["ws_root"]}
