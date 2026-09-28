"""Fixed paths of one install (design section 2, intro, and 1.1.1).

`REPO` is derived from this file's own location (scripts/jobhunter/paths.py -> repo root). There is no
`--home` option and no JOBHUNTER_HOME variable: a set variable is ignored (see `env_warnings`).

Two roots:
- code files (schema, migrations, acl.json, detect/*.json, data/*) always come from `PKG_DIR` in the
  real repo;
- install data (`private/`, `state/`, `logs/`, `exports/`) lives under `root()`, which is `REPO` except
  after `use_test_home(tmpdir)`, a Python-only API for tests that the CLI cannot reach.
"""
from __future__ import annotations

import json
import os
import re

from .errors import Denied

PKG_DIR: str = os.path.dirname(os.path.abspath(__file__))
REPO: str = os.path.dirname(os.path.dirname(PKG_DIR))
SCRIPTS_DIR: str = os.path.join(REPO, "scripts")
MIGRATIONS_DIR: str = os.path.join(PKG_DIR, "migrations")
SCHEMA_FILE: str = os.path.join(PKG_DIR, "schema.sql")
ACL_FILE: str = os.path.join(PKG_DIR, "acl.json")
DB_NAME = "jobhunter.sqlite3"

# jobhunter agent id -> workspace role
AGENT_ROLES = {
    "jobhunter-scout": "scout",
    "jobhunter-evaluator": "evaluator",
    "jobhunter-applier": "applier",
    "jobhunter-outreach": "outreach",
    "jobhunter-qc": "qc",
}
ROLES = tuple(AGENT_ROLES.values())

_CYCLE_RE = re.compile(r"^C[0-9]{8}T[0-9]{6}Z[A-Z2-7]{4}$")
_test_root: str | None = None


def root() -> str:
    """Install data root: REPO, or the temp dir installed by use_test_home()."""
    return _test_root if _test_root is not None else REPO


def is_test_home() -> bool:
    return _test_root is not None


def private_dir() -> str:
    return os.path.join(root(), "private")


def state_dir() -> str:
    return os.path.join(root(), "state")


def logs_dir() -> str:
    return os.path.join(root(), "logs")


def exports_dir() -> str:
    return os.path.join(root(), "exports")


def home_file() -> str:
    return os.path.join(private_dir(), "home.json")


def consent_file() -> str:
    """private/consent.json: the owner's per-site consent to the agent using a site's login (identity.py).
    Written only by the human-only `browser consent grant|revoke` commands (PIN), mode 0600."""
    return os.path.join(private_dir(), "consent.json")


def db_path() -> str:
    """Fixed database path: <root>/state/jobhunter.sqlite3."""
    return os.path.join(state_dir(), DB_NAME)


def lock_file() -> str:
    """flock target for migrations and file staging."""
    return os.path.join(state_dir(), ".lock")


def paused_file() -> str:
    return os.path.join(state_dir(), "PAUSED")


def guard_dir() -> str:
    return os.path.join(state_dir(), "guard")


def home() -> dict:
    """Read private/home.json. Missing or unreadable -> Denied(E_CONFIG_INVALID)."""
    path = home_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise Denied("E_CONFIG_INVALID", "private/home.json is missing; run ./jobhunter init",
                     data={"path": path})
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "private/home.json is unreadable: %s" % exc, data={"path": path})
    if not isinstance(data, dict):
        raise Denied("E_CONFIG_INVALID", "private/home.json is not a JSON object", data={"path": path})
    return data


def check_home_binding(h: dict | None = None) -> dict:
    """home.json must describe this clone: repo equals the derived root, db_path (when present) equals
    the fixed database path. Otherwise Denied(E_HOME_MISMATCH)."""
    h = home() if h is None else h
    want_repo = os.path.realpath(root())
    got_repo = h.get("repo")
    if not got_repo or os.path.realpath(str(got_repo)) != want_repo:
        raise Denied("E_HOME_MISMATCH", "private/home.json belongs to another clone",
                     data={"home_repo": got_repo, "derived_repo": want_repo})
    got_db = h.get("db_path")
    if got_db and os.path.realpath(str(got_db)) != os.path.realpath(db_path()):
        raise Denied("E_HOME_MISMATCH", "private/home.json names another database",
                     data={"home_db_path": got_db, "db_path": db_path()})
    if not h.get("install_id"):
        raise Denied("E_HOME_MISMATCH", "private/home.json has no install_id")
    return h


def ws_root() -> str:
    """WS_ROOT from home.json (agent workspaces live outside the repo)."""
    h = home()
    ws = h.get("ws_root")
    if not ws:
        raise Denied("E_CONFIG_INVALID", "private/home.json has no ws_root; run ./jobhunter init")
    return str(ws)


def role_for_agent(agent_id: str) -> str:
    """'jobhunter-applier' -> 'applier'; the bare role name is accepted too."""
    if agent_id in AGENT_ROLES:
        return AGENT_ROLES[agent_id]
    if agent_id in ROLES:
        return agent_id
    raise Denied("E_CALLER_NOT_ALLOWED", "unknown jobhunter agent %r" % agent_id)


def ws_dir(role: str) -> str:
    """WS_ROOT/<role>."""
    if role not in ROLES:
        role = role_for_agent(role)
    return os.path.join(ws_root(), role)


def work_dir(role: str, cycle_id: str) -> str:
    """WS_ROOT/<role>/work/<cycle_id> (cycle id format checked, so no path tricks)."""
    if not _CYCLE_RE.match(cycle_id or ""):
        raise Denied("E_USAGE", "bad cycle id %r" % cycle_id)
    return os.path.join(ws_dir(role), "work", cycle_id)


def inbox_dir(role: str) -> str:
    return os.path.join(ws_dir(role), "inbox")


def _under(path: str, base: str) -> bool:
    base = os.path.realpath(base)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def ensure_agent_path(path: str, agent_id: str) -> str:
    """Return the realpath of an agent-supplied file path when it lies under that agent's work/ or
    inbox/ folder (symlinks resolved first); otherwise Denied(E_PATH_NOT_ALLOWED)."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise Denied("E_PATH_NOT_ALLOWED", "empty or invalid path")
    if not os.path.isabs(path):
        raise Denied("E_PATH_NOT_ALLOWED", "path must be absolute", data={"path": path})
    role = role_for_agent(agent_id)
    real = os.path.realpath(path)
    base = ws_dir(role)
    for sub in ("work", "inbox"):
        allowed = os.path.join(base, sub)
        if _under(real, allowed) and real != os.path.realpath(allowed):
            return real
    raise Denied("E_PATH_NOT_ALLOWED", "file must be under the agent's own work/ or inbox/ folder",
                 data={"path": path, "role": role})


def env_warnings(env: dict | None = None) -> list[str]:
    """Environment variables that look like a home override; they are ignored, `status` shows them."""
    env = os.environ if env is None else env
    out = []
    for name in ("JOBHUNTER_HOME", "JH_HOME", "JOBHUNTER_DB"):
        if env.get(name):
            out.append("%s is set and ignored; the database path is fixed" % name)
    return out


def use_test_home(tmpdir: str) -> None:
    """Tests only: point every install-data path at tmpdir and write a matching private/home.json
    (unless one exists). Code files still come from the real repo."""
    global _test_root
    real = os.path.realpath(tmpdir)
    _test_root = real
    for sub in ("private", "state", "logs", "exports", os.path.join("state", "guard")):
        os.makedirs(os.path.join(real, sub), exist_ok=True)
    hf = home_file()
    if not os.path.exists(hf):
        from .canon import new_uid, now
        data = {
            "install_id": new_uid("I", 8),
            "repo": real,
            "db_path": db_path(),
            "ws_root": os.path.join(real, "ws"),
            "oc_bin": "/nonexistent/openclaw",
            "oc_profile": "",
            "python": "/usr/bin/python3",
            "created_at": now(),
        }
        with open(hf, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
    ws = os.path.join(real, "ws")
    for role in ROLES:
        for sub in ("work", "inbox"):
            os.makedirs(os.path.join(ws, role, sub), exist_ok=True)


def clear_test_home() -> None:
    """Tests only: undo use_test_home()."""
    global _test_root
    _test_root = None
