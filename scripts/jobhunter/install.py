"""Install renderers and checks (design 1.2, 10.3, 10.4, 10.8; owned by U7).

Pure functions that turn the committed declarations (openclaw/agents.json, openclaw/crons.json,
openclaw/agents.patch.json5.tmpl, openclaw/exec-approvals.json5.tmpl, agent-templates/, skills-src/)
plus private/home.json and the config into:

- the exact `openclaw cron add` and `cron edit` argument lists (render_cron_commands, render_failure_alerts),
- the per-agent workspaces under WS_ROOT (render_workspaces),
- the config patches (render_agents_patch, render_guard_config, uninstall_patch, extra_dirs_patch),
- the merged exec approvals document (merge_approvals),
- the install manifest (state/install-manifest.json),
- the per-site browser consent in private/consent.json (the Chrome login import of ./jobhunter browser consent)
  and the read-only login check verdicts after an import.

Nothing here calls openclaw. install.sh, uninstall.sh and ./jobhunter run the rendered commands through the
install's own binary and profile. The CLI surface is jobhunter.commands.install.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import shlex
import shutil
import time

from . import paths
from .canon import now, sha256_file
from .errors import Denied

MIN_OPENCLAW = (2026, 9, 5)
MIN_SQLITE = (3, 24, 0)            # INSERT ... ON CONFLICT DO UPDATE (upsert), used by the core ledger
DEFAULT_UPLOAD_ROOT = "/tmp/openclaw/uploads"
SHARED_SKILLS_DIR = "shared-skills"
GUARD_PLUGIN_ID = "jobhunter-guard"
BROWSER_PROFILE = "jobhunter"
STAYAWAKE_LABEL = "ai.openclaw-job-hunter.stayawake"
CRON_PREFIX = "jobhunter:"
FAILURE_ALERT_ARGS = ("--failure-alert-after", "2", "--failure-alert-cooldown", "1h", "--failure-alert-mode",
                      "announce")
OWNER_PLACEHOLDERS = frozenset({"", "+10000000000"})
LANE_JOBS = {"scout": "jobhunter:scout", "evaluator": "jobhunter:evaluate", "applier": "jobhunter:apply",
             "outreach": "jobhunter:outreach", "replies": "jobhunter:replies"}
PATH_OK_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
PROTECTED_HOME_DIRS = ("Documents", "Desktop", "Downloads", os.path.join("Library", "Mobile Documents"))
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")
TEMPLATE_TEXT_EXT = (".md", ".json", ".js", ".txt", ".yaml", ".yml", ".csv")
BOARD_DOMAINS = {
    "naukri": "naukri.com", "instahyre": "instahyre.com", "foundit": "foundit.in", "wellfound": "wellfound.com",
    "cutshort": "cutshort.io", "hirist": "hirist.tech", "iimjobs": "iimjobs.com", "yc": "workatastartup.com",
}

_source_root_override: str | None = None


# ---------------------------------------------------------------- roots
def source_root() -> str:
    """Where committed code and templates are read from: the repo, or a fixture tree in tests."""
    return _source_root_override if _source_root_override is not None else paths.REPO


def use_test_source(root: str | None) -> None:
    """Tests only: read declarations and templates from `root` (None restores the repo)."""
    global _source_root_override
    _source_root_override = root


def install_dir() -> str:
    """state/install: rendered patches and documents (mode 700, files 600)."""
    return os.path.join(paths.state_dir(), "install")


def manifest_file() -> str:
    return os.path.join(paths.state_dir(), "install-manifest.json")


# ---------------------------------------------------------------- json5 subset
def strip_json5(text: str) -> str:
    """Remove // and /* */ comments outside strings and trailing commas before } or ]. Enough JSON5 for the
    committed templates; keys and strings must still be double quoted."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def loads_json5(text: str):
    try:
        return json.loads(strip_json5(text))
    except ValueError as exc:
        raise Denied("E_VALIDATION", "not valid JSON (JSON5 subset): %s" % exc)


def _read_json(path: str, what: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return loads_json5(fh.read())
    except FileNotFoundError:
        raise Denied("E_VALIDATION", "%s is missing" % what, data={"path": path})
    except OSError as exc:
        raise Denied("E_VALIDATION", "%s is unreadable: %s" % (what, exc), data={"path": path})


def _write_private(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)        # a leftover .tmp of an older run keeps its mode otherwise
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


def write_json(path: str, obj) -> str:
    return _write_private(path, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


# ---------------------------------------------------------------- declarations
def load_agents(root: str | None = None) -> list[dict]:
    root = root or source_root()
    doc = _read_json(os.path.join(root, "openclaw", "agents.json"), "openclaw/agents.json")
    agents = doc.get("agents") if isinstance(doc, dict) else None
    if not isinstance(agents, list) or not agents:
        raise Denied("E_VALIDATION", "openclaw/agents.json has no agents list")
    seen = set()
    for a in agents:
        for key in ("id", "role", "template_dir", "model", "tools_allow", "tools_deny", "skills", "ref"):
            if key not in a:
                raise Denied("E_VALIDATION", "agents.json entry lacks %r" % key, data={"agent": a.get("id")})
        if not str(a["id"]).startswith("jobhunter-") or a["id"] in seen:
            raise Denied("E_VALIDATION", "agent ids must be unique and start with jobhunter-", data={"id": a["id"]})
        if paths.AGENT_ROLES.get(a["id"]) != a["role"]:
            raise Denied("E_VALIDATION", "agent %s must have role %s" % (a["id"], paths.AGENT_ROLES.get(a["id"])))
        seen.add(a["id"])
    return agents


def load_crons(root: str | None = None) -> dict:
    root = root or source_root()
    doc = _read_json(os.path.join(root, "openclaw", "crons.json"), "openclaw/crons.json")
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, list) or not jobs:
        raise Denied("E_VALIDATION", "openclaw/crons.json has no jobs list")
    keys = set()
    for j in jobs:
        if not str(j.get("key", "")).startswith(CRON_PREFIX) or j["key"] in keys:
            raise Denied("E_VALIDATION", "cron keys must be unique and start with jobhunter:", data={"key": j.get("key")})
        if j.get("kind") not in ("command", "agent"):
            raise Denied("E_VALIDATION", "cron kind must be command or agent", data={"key": j["key"]})
        keys.add(j["key"])
    return doc


def load_config(root: str | None = None) -> dict:
    """Raw config: private/config.json deep-merged over config.example.json. Only free keys are read from it
    here (timezone, owner.notify, dispatch, boards); limits always go through jobhunter.config."""
    base: dict = {}
    example = os.path.join(root or source_root(), "config.example.json")
    if os.path.exists(example):
        try:
            with open(example, "r", encoding="utf-8") as fh:
                base = json.load(fh)
        except (OSError, ValueError):
            base = {}
    private = os.path.join(paths.private_dir(), "config.json")
    if os.path.exists(private):
        try:
            with open(private, "r", encoding="utf-8") as fh:
                _deep_merge(base, json.load(fh))
        except (OSError, ValueError) as exc:
            raise Denied("E_CONFIG_INVALID", "private/config.json is not valid JSON: %s" % exc)
    return base


def _deep_merge(dst: dict, src) -> dict:
    if not isinstance(src, dict):
        return dst
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _get(cfg: dict, dotted: str, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def home_or_empty() -> dict:
    try:
        return paths.home()
    except Denied:
        return {}


# ---------------------------------------------------------------- path and version checks
def check_path(repo: str, home_dir: str | None = None) -> dict:
    """Refuse a repo path with spaces or unusual characters, or under a macOS privacy-protected folder
    (install check-path). Returns {repo} or raises Denied(E_PRECONDITION)."""
    home_dir = home_dir if home_dir is not None else os.path.expanduser("~")
    real = os.path.realpath(repo)
    suggestion = 'mv "%s" ~/openclaw-job-hunter' % repo
    if not PATH_OK_RE.match(repo) or not PATH_OK_RE.match(real):
        raise Denied("E_PRECONDITION", "the repo path may only contain letters, digits, dot, underscore, "
                     "slash and hyphen (no spaces); move it: %s" % suggestion,
                     data={"repo": repo, "suggestion": suggestion})
    homes = {os.path.realpath(home_dir), os.path.abspath(home_dir)}
    for h in homes:
        for sub in PROTECTED_HOME_DIRS:
            base = os.path.join(h, sub)
            for p in (repo, real):
                if p == base or p.startswith(base.rstrip(os.sep) + os.sep):
                    raise Denied("E_PRECONDITION", "the repo is inside ~/%s, which the Gateway service may not be "
                                 "allowed to read; move it: %s" % (sub, suggestion),
                                 data={"repo": repo, "suggestion": suggestion})
    return {"repo": repo, "realpath": real}


def sqlite_ok(version_info: tuple | None = None) -> bool:
    """The SQLite library linked into this Python is new enough for the upserts (3.24 or newer)."""
    if version_info is None:
        import sqlite3
        version_info = sqlite3.sqlite_version_info
    return tuple(version_info)[:3] >= MIN_SQLITE


def parse_version(text: str) -> tuple | None:
    m = re.search(r"(\d{4})\.(\d{1,3})\.(\d{1,4})", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def version_ok(text: str, minimum: tuple = MIN_OPENCLAW) -> bool:
    v = parse_version(text)
    return v is not None and v >= minimum


# ---------------------------------------------------------------- time zone
def detect_timezone(cfg: dict | None = None, env: dict | None = None) -> str:
    """IANA zone: config `timezone` unless "auto", else TZ, /etc/localtime, /etc/timezone, else UTC."""
    env = os.environ if env is None else env
    tz = str(_get(cfg or {}, "timezone", "auto") or "auto")
    if tz != "auto":
        return tz
    cand = env.get("TZ", "").lstrip(":")
    if re.match(r"^[A-Za-z]+(/[A-Za-z0-9_+-]+)+$", cand) or cand == "UTC":
        return cand
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as fh:
            val = fh.read().strip()
            if val:
                return val
    except OSError:
        pass
    return "UTC"


# ---------------------------------------------------------------- crons
def _subst(s: str, mapping: dict) -> str:
    for k, v in mapping.items():
        s = s.replace("{%s}" % k, v)
    return s


def _cron_days(days) -> str:
    """Config days use 1 = Monday ... 7 = Sunday; cron uses 0 = Sunday."""
    out = sorted({(int(d) % 7) for d in (days or [1, 2, 3, 4, 5])})
    return ",".join(str(d) for d in out) if out else "*"


def fallback_schedule(lane_cfg: dict | None) -> dict:
    """dispatch.mode = cron_schedule: a fixed schedule inside the lane window (1.2 fallback), used with a
    stagger so the minutes differ from run to run."""
    lane_cfg = lane_cfg or {}
    window = lane_cfg.get("window") or ["10:00", "18:00"]
    try:
        start_h, start_m = [int(x) for x in str(window[0]).split(":")]
        end_h = int(str(window[1]).split(":")[0])
    except (ValueError, IndexError):
        start_h, start_m, end_h = 10, 0, 18
    cycles = lane_cfg.get("cycles_per_day") or [2, 3]
    hi = max(1, int(cycles[-1]))
    span = max(1, end_h - start_h)
    step = max(1, span // hi)
    hours = list(range(start_h, end_h + 1, step))[:hi]
    expr = "%d %s * * %s" % (start_m, ",".join(str(h) for h in hours), _cron_days(lane_cfg.get("days")))
    return {"cron": expr, "stagger": "30m"}


def _lane_for_key(key: str) -> str | None:
    for lane, k in LANE_JOBS.items():
        if k == key:
            return lane
    return None


def render_cron_commands(crons: dict, *, py: str, repo: str, tz: str, dispatch_mode: str = "dispatcher",
                         lanes: dict | None = None) -> list[list[str]]:
    """The `openclaw cron add` argument lists (without the binary and profile) for every declared job.
    Every job is created --disabled with --no-deliver and its --declaration-key (idempotent)."""
    mapping = {"PY": py, "REPO": repo}
    out: list[list[str]] = []
    for job in crons["jobs"]:
        args = ["cron", "add", "--name", job["name"], "--display-name", job["display"],
                "--declaration-key", job["key"]]
        if job["kind"] == "agent":
            args += ["--agent", job["agent"], "--session", "isolated"]
        schedule = dict(job.get("schedule") or {})
        if job["kind"] == "agent" and dispatch_mode == "cron_schedule":
            schedule = fallback_schedule((lanes or {}).get(_lane_for_key(job["key"]) or ""))
        if "every" in schedule:
            args += ["--every", str(schedule["every"])]
        elif "cron" in schedule:
            args += ["--cron", str(schedule["cron"]), "--tz", tz]
            if schedule.get("stagger"):
                args += ["--stagger", str(schedule["stagger"])]
        else:
            raise Denied("E_VALIDATION", "cron job has no schedule", data={"key": job["key"]})
        if job["kind"] == "command":
            argv = [_subst(str(a), mapping) for a in job["argv"]]
            args += ["--command-argv", json.dumps(argv, separators=(",", ":")), "--command-cwd", repo,
                     "--timeout-seconds", str(int(job["timeout_s"])),
                     "--no-output-timeout-seconds", str(int(job["no_output_timeout_s"])),
                     "--output-max-bytes", "4000"]
        else:
            args += ["--tools", job["tools"], "--message", _subst(job["message"], mapping),
                     "--model", job["model"], "--fallbacks", job.get("fallbacks", ""),
                     "--thinking", job["thinking"], "--timeout-seconds", str(int(job["timeout_s"]))]
        args += ["--no-deliver", "--disabled"]
        for a in args:
            if "\n" in a or "\r" in a or "\x00" in a:
                raise Denied("E_VALIDATION", "cron argument contains a line break", data={"key": job["key"]})
        out.append(args)
    return out


def notify_target(cfg: dict) -> tuple[str, str] | None:
    """(channel, to) of the owner, or None when notifications are off or the number is a placeholder."""
    channel = str(_get(cfg, "owner.notify.channel", "none") or "none")
    to = str(_get(cfg, "owner.notify.to", "") or "")
    if channel == "none" or to in OWNER_PLACEHOLDERS:
        return None
    return channel, to


def render_failure_alerts(crons: dict, job_ids: dict, cfg: dict) -> list[list[str]]:
    """`cron edit <id> --failure-alert ...` for jobs with failure_alert: true (skipped when the owner has no
    notification channel). Jobs without a known id are skipped."""
    target = notify_target(cfg)
    if target is None:
        return []
    channel, to = target
    out = []
    for job in crons["jobs"]:
        if not job.get("failure_alert"):
            continue
        jid = job_ids.get(job["key"])
        if not jid:
            continue
        out.append(["cron", "edit", str(jid), "--failure-alert", "--failure-alert-channel", channel,
                    "--failure-alert-to", to] + list(FAILURE_ALERT_ARGS))
    return out


def shell_lines(commands: list[list[str]]) -> str:
    """One line per command, every token shell-quoted (install.sh runs `eval "set -- $line"`)."""
    return "\n".join(" ".join(shlex.quote(a) for a in cmd) for cmd in commands)


# ---------------------------------------------------------------- cron list and manifest
def cron_jobs_from_list(doc) -> dict:
    """{declarationKey: {id, enabled, name}} for jobhunter jobs in `openclaw cron list --all --json` output
    (parsed defensively: a bare list or an object with `jobs`)."""
    rows = doc.get("jobs") if isinstance(doc, dict) else doc
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        key = r.get("declarationKey") or r.get("declaration_key") or ""
        if isinstance(key, str) and key.startswith(CRON_PREFIX) and r.get("id"):
            out[key] = {"id": str(r["id"]), "enabled": bool(r.get("enabled")), "name": r.get("name")}
    return out


def read_manifest() -> dict:
    try:
        with open(manifest_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_manifest(update: dict) -> dict:
    """Merge into state/install-manifest.json: dicts merge, lists are unioned (order kept), scalars replace."""
    cur = read_manifest()
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k] = dict(cur[k], **v)
        elif isinstance(v, list) and isinstance(cur.get(k), list):
            cur[k] = cur[k] + [x for x in v if x not in cur[k]]
        else:
            cur[k] = v
    cur["version"] = 1
    cur["updated_at"] = now()
    write_json(manifest_file(), cur)
    return cur


def store_cron_ids(cron_list_doc, crons: dict, dispatch_mode: str = "dispatcher") -> dict:
    """Record the job ids from a cron list in the manifest and in private/home.json (`cron_jobs`). Returns
    {cron_jobs, missing, enabled_agent_jobs}."""
    found = cron_jobs_from_list(cron_list_doc)
    ids = {k: v["id"] for k, v in found.items()}
    declared = [j["key"] for j in crons["jobs"]]
    missing = [k for k in declared if k not in found]
    agent_keys = [j["key"] for j in crons["jobs"] if j["kind"] == "agent"]
    enabled_agent = [k for k in agent_keys if found.get(k, {}).get("enabled")] if dispatch_mode == "dispatcher" else []
    merge_manifest({"cron_jobs": ids})
    hf = paths.home_file()
    if os.path.exists(hf):
        h = paths.home()
        h["cron_jobs"] = dict(h.get("cron_jobs") or {}, **ids)
        write_json(hf, h)
    return {"cron_jobs": ids, "missing": missing, "enabled_agent_jobs": enabled_agent}


def known_job_ids(extra_list_doc=None) -> dict:
    """{key: id} from the manifest, home.json and (optionally) a fresh cron list."""
    ids = dict((home_or_empty().get("cron_jobs") or {}))
    ids.update(read_manifest().get("cron_jobs") or {})
    if extra_list_doc is not None:
        ids.update({k: v["id"] for k, v in cron_jobs_from_list(extra_list_doc).items()})
    return ids


def select_jobs(crons: dict, which: str, lane: str | None = None, dispatch_mode: str = "dispatcher") -> list[str]:
    """Declaration keys for a wrapper action:
    all: every job; resume: command jobs with enable_on_resume (plus agent jobs in cron_schedule mode);
    pause: jobs that `pause all` disables (everything except keep_running_when_paused);
    agents: agent jobs; lane: the agent job of `lane`."""
    jobs = crons["jobs"]
    if which == "all":
        return [j["key"] for j in jobs]
    if which == "resume":
        return [j["key"] for j in jobs if (j["kind"] == "command" and j.get("enable_on_resume"))
                or (j["kind"] == "agent" and dispatch_mode == "cron_schedule")]
    if which == "pause":
        return [j["key"] for j in jobs if not j.get("keep_running_when_paused")]
    if which == "agents":
        return [j["key"] for j in jobs if j["kind"] == "agent"]
    if which == "lane":
        if lane not in LANE_JOBS:
            raise Denied("E_USAGE", "lane must be one of %s" % ", ".join(sorted(LANE_JOBS)))
        return [LANE_JOBS[lane]]
    raise Denied("E_USAGE", "unknown job set %r" % which)


# ---------------------------------------------------------------- workspaces
def _skill_source(root: str, role: str, template_dir: str, name: str) -> str | None:
    """agent-templates/<role>/skills/<name>/ first, then skills-src/<name>/."""
    for cand in (os.path.join(root, template_dir, "skills", name), os.path.join(root, "skills-src", name)):
        if os.path.isfile(os.path.join(cand, "SKILL.template.md")):
            return cand
    return None


def placeholders(*, repo: str, py: str, ws_root: str, role: str, agent_id: str) -> dict:
    """Template placeholders. __WS__ is the agent's own workspace (WS_ROOT/<role>)."""
    return {"__REPO__": repo, "__PY__": py, "__WS_ROOT__": ws_root, "__WS__": os.path.join(ws_root, role),
            "__ROLE__": role, "__AGENT_ID__": agent_id}


def substitute(text: str, mapping: dict) -> str:
    for k in sorted(mapping, key=len, reverse=True):
        text = text.replace(k, mapping[k])
    return text


def _render_file(src: str, dst: str, mapping: dict, leftovers: list) -> None:
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    if src.endswith(TEMPLATE_TEXT_EXT):
        with open(src, "r", encoding="utf-8") as fh:
            text = substitute(fh.read(), mapping)
        left = sorted(set(PLACEHOLDER_RE.findall(text)))
        if left:
            leftovers.append({"file": dst, "placeholders": left})
        _write_private(dst, text)
    else:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)


def _replace_dir(tmp: str, final: str) -> None:
    if os.path.isdir(final):
        old = final + ".old"
        shutil.rmtree(old, ignore_errors=True)
        os.rename(final, old)
        os.rename(tmp, final)
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.rename(tmp, final)


def render_workspaces(*, ws_root: str, repo: str, py: str, agents: list[dict], root: str | None = None,
                      strict: bool = True) -> dict:
    """Render agent-templates/<role>/ and the listed skills and ref files into WS_ROOT/<role>/.

    Managed paths only: AGENTS.md, SOUL.md, IDENTITY.md, skills/jobhunter-*/ and ref/; work/ and inbox/ are
    created; anything OpenClaw writes (memory/, .git, ...) is left alone. With strict, a missing template,
    skill or ref file raises Denied(E_VALIDATION) before anything is written."""
    root = root or source_root()
    plan = []
    missing = []
    for a in agents:
        role, tdir = a["role"], a["template_dir"]
        files = []
        for name, out in (("AGENTS.template.md", "AGENTS.md"), ("SOUL.md", "SOUL.md"), ("IDENTITY.md", "IDENTITY.md")):
            src = os.path.join(root, tdir, name)
            if os.path.isfile(src):
                files.append((src, out))
            else:
                missing.append(os.path.relpath(src, root))
        skills = []
        for sk in a["skills"]:
            src = _skill_source(root, role, tdir, sk)
            if src is None:
                missing.append("skill %s (%s/skills/%s or skills-src/%s)" % (sk, tdir, sk, sk))
            else:
                skills.append((sk, src))
        refs = []
        for r in a["ref"]:
            matches = sorted(glob.glob(os.path.join(root, r["from"])))
            if not matches:
                missing.append(r["from"])
            for m in matches:
                dest = r["to"]
                if dest.endswith("/"):
                    dest = dest + os.path.basename(m)
                refs.append((m, dest))
        plan.append((a, files, skills, refs))
    if missing and strict:
        raise Denied("E_VALIDATION", "templates are missing; the repo is incomplete", data={"missing": missing})

    os.makedirs(ws_root, mode=0o700, exist_ok=True)
    os.chmod(ws_root, 0o700)
    rendered, leftovers = [], []
    for a, files, skills, refs in plan:
        role = a["role"]
        mapping = placeholders(repo=repo, py=py, ws_root=ws_root, role=role, agent_id=a["id"])
        ws = os.path.join(ws_root, role)
        for sub in ("", "work", "inbox", "skills"):
            os.makedirs(os.path.join(ws, sub), mode=0o700, exist_ok=True)
        for src, out in files:
            _render_file(src, os.path.join(ws, out), mapping, leftovers)
        wanted = {sk for sk, _ in skills}
        for entry in os.listdir(os.path.join(ws, "skills")):
            if entry.startswith("jobhunter-") and entry not in wanted:
                shutil.rmtree(os.path.join(ws, "skills", entry), ignore_errors=True)
        for sk, src in skills:
            tmp = os.path.join(ws, "skills", "." + sk + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            for dirpath, _dirs, fnames in os.walk(src):
                for fn in sorted(fnames):
                    if fn.startswith(".") or fn.endswith(".pyc"):
                        continue
                    s = os.path.join(dirpath, fn)
                    rel = os.path.relpath(s, src)
                    if rel == "SKILL.template.md":
                        rel = "SKILL.md"
                    _render_file(s, os.path.join(tmp, rel), mapping, leftovers)
            _replace_dir(tmp, os.path.join(ws, "skills", sk))
        tmp = os.path.join(ws, ".ref.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, mode=0o700)
        for src, dest in refs:
            _render_file(src, os.path.join(tmp, dest), mapping, leftovers)
        _replace_dir(tmp, os.path.join(ws, "ref"))
        rendered.append({"agent": a["id"], "workspace": ws, "skills": sorted(wanted), "ref": [d for _, d in refs]})

    hashes = {}
    reviewer = os.path.join(root, "prompts", "reviewer.md")
    qc_agents = os.path.join(ws_root, "qc", "AGENTS.md")
    if os.path.isfile(reviewer) and os.path.isfile(qc_agents):
        hashes = {"reviewer_prompt_sha256": sha256_file(reviewer), "qc_agents_md_sha256": sha256_file(qc_agents)}
    elif strict:
        raise Denied("E_VALIDATION", "prompts/reviewer.md or the qc AGENTS.md is missing")
    return {"rendered": rendered, "missing": missing, "leftover_placeholders": leftovers, "hashes": hashes}


# ---------------------------------------------------------------- shared skills (--chat-control)
def shared_skill_names(root: str | None = None) -> list[str]:
    """Folders under shared-skills/ that hold a SKILL.template.md."""
    base = os.path.join(root or source_root(), SHARED_SKILLS_DIR)
    if not os.path.isdir(base):
        return []
    return sorted(n for n in os.listdir(base)
                  if not n.startswith(".") and os.path.isfile(os.path.join(base, n, "SKILL.template.md")))


def render_shared_skills(*, repo: str, py: str, root: str | None = None) -> dict:
    """Render shared-skills/<name>/SKILL.template.md to <repo>/shared-skills/<name>/SKILL.md (gitignored), the
    folder that --chat-control adds to skills.load.extraDirs. OpenClaw loads only SKILL.md, so without this the
    folder holds no skill. Placeholders: __REPO__ and __PY__. Any other placeholder refuses before anything is
    written (Denied E_VALIDATION)."""
    root = root or source_root()
    names = shared_skill_names(root)
    if not names:
        raise Denied("E_VALIDATION", "shared-skills/ holds no SKILL.template.md; the repo is incomplete")
    mapping = {"__REPO__": repo, "__PY__": py}
    texts = []
    for n in names:
        with open(os.path.join(root, SHARED_SKILLS_DIR, n, "SKILL.template.md"), "r", encoding="utf-8") as fh:
            text = substitute(fh.read(), mapping)
        left = sorted(set(PLACEHOLDER_RE.findall(text)))
        if left:
            raise Denied("E_VALIDATION", "shared skill %s has placeholders the installer does not know" % n,
                         data={"skill": n, "placeholders": left})
        texts.append((n, text))
    rendered = []
    for n, text in texts:
        rendered.append(_write_private(os.path.join(repo, SHARED_SKILLS_DIR, n, "SKILL.md"), text))
    return {"dir": os.path.join(repo, SHARED_SKILLS_DIR), "rendered": rendered}


def remove_shared_skills(*, repo: str, root: str | None = None) -> list[str]:
    """Delete the rendered shared-skills/<name>/SKILL.md files, only for the names that have a
    SKILL.template.md (so nothing else is ever removed). Returns the removed paths."""
    removed = []
    for n in sorted(set(shared_skill_names(root or source_root())) | set(shared_skill_names(repo))):
        dst = os.path.join(repo, SHARED_SKILLS_DIR, n, "SKILL.md")
        if os.path.isfile(dst):
            os.remove(dst)
            removed.append(dst)
    return removed


# ---------------------------------------------------------------- browser upload root
def resolve_upload_root(*, explicit: str | None = None, current: str | None = None, tmpdirs=(),
                        repo: str | None = None) -> str:
    """OpenClaw's browser upload directory, resolved (design 6.4, 13.1 item 11): an explicit --upload-root
    wins, then the value already in home.json, then the first existing `<TMPDIR>/openclaw/uploads` of the
    given temp folders (the Gateway's TMPDIR), then /tmp/openclaw/uploads. Symlinks are resolved (on macOS
    /tmp is /private/tmp). A path inside the repo is refused."""
    if explicit:
        cand = explicit
    elif current:
        cand = current
    else:
        cand = DEFAULT_UPLOAD_ROOT
        for t in tmpdirs:
            if t and os.path.isdir(os.path.join(t, "openclaw", "uploads")):
                cand = os.path.join(t, "openclaw", "uploads")
                break
    cand = os.path.expanduser(str(cand))
    if not os.path.isabs(cand):
        raise Denied("E_VALIDATION", "the upload root must be an absolute path", data={"upload_root": cand})
    real = os.path.realpath(cand)
    if real == os.path.sep:
        raise Denied("E_VALIDATION", "the upload root cannot be /")
    repo_real = os.path.realpath(repo or paths.root())
    if real == repo_real or real.startswith(repo_real + os.sep):
        raise Denied("E_VALIDATION", "the upload root must be outside the repo", data={"upload_root": real})
    return real


def store_upload_root(path: str) -> dict:
    """Record `upload_root` in private/home.json (read by resume stage) and in the manifest."""
    hf = paths.home_file()
    h = paths.home()
    h["upload_root"] = path
    write_json(hf, h)
    merge_manifest({"upload_root": path})
    return h


# ---------------------------------------------------------------- config patches
def regex_escape_path(p: str) -> str:
    """Escape for an ECMAScript regex (the repo path is limited to [A-Za-z0-9._/-] by check_path)."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", p)


HUMAN_ONLY_WORDS = ("approve", "skip", "edit", "forget", "unpause", "auth", "mail connect", "mail import-history",
                    "sheet connect", "qc golden", "config raise", "approval set", "tier set", "linkedin enable",
                    "breaker reset", "reconcile confirm-not-sent", "companies", "contacts split",
                    "exclusions (remove|import --deactivate)", "enrich (connect|disconnect|retry|test)",
                    "enrich find .*--i[a-z-]*", "answers add", "install")
# `enrich find .*--i[a-z-]*` also catches an abbreviated --include-reserve (argparse accepts unique prefixes);
# `install` covers every install helper, among them consent-record and consent-revoke: agents run none of them.


def arg_pattern(repo: str) -> str:
    """argPattern of the exec allowlist entry (10.3 step 9): jh.py only, optional --cycle and --quiet, and never
    a human-only command. Matched against argv[1:] joined by spaces."""
    return ("^%s/scripts/jh\\.py( --cycle C[0-9A-Z]+)?( --quiet)? (?!(%s)( |$))[a-z]"
            % (regex_escape_path(repo), "|".join(HUMAN_ONLY_WORDS)))


def _walk_subst(obj, typed: dict, text: dict):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nv = _walk_subst(v, typed, text)
            if nv is _OMIT:
                continue
            out[k] = nv
        return out
    if isinstance(obj, list):
        return [x for x in (_walk_subst(v, typed, text) for v in obj) if x is not _OMIT]
    if isinstance(obj, str):
        if obj in typed:
            return typed[obj]
        return substitute(obj, text)
    return obj


_OMIT = object()


def _template(name: str, root: str | None = None):
    return _read_json(os.path.join(root or source_root(), "openclaw", name), "openclaw/" + name)


def render_agents_patch(agents: list[dict], *, ws_root: str, repo: str, py: str, route: str = "cli",
                        root: str | None = None) -> dict:
    """{"agents": {"entries": {"jobhunter-*": {...}}}} from agents.patch.json5.tmpl. Nothing outside
    agents.entries["jobhunter-*"] is ever produced."""
    if route not in ("cli", "api_key"):
        raise Denied("E_USAGE", "route must be cli or api_key")
    tmpl = _template("agents.patch.json5.tmpl", root)
    if not isinstance(tmpl, dict):
        raise Denied("E_VALIDATION", "agents.patch.json5.tmpl must be one object")
    entries = {}
    for a in agents:
        models = [a["model"]] + list(a.get("fallbacks") or [])
        runtime = {m: {"agentRuntime": {"id": "claude-cli"}} for m in models} if route == "cli" else _OMIT
        typed = {"__MODEL__": a["model"], "__SKILLS__": list(a["skills"]), "__TOOLS_ALLOW__": list(a["tools_allow"]),
                 "__TOOLS_DENY__": list(a["tools_deny"]), "__MODELS_RUNTIME__": runtime}
        text = placeholders(repo=repo, py=py, ws_root=ws_root, role=a["role"], agent_id=a["id"])
        entries[a["id"]] = _walk_subst(tmpl, typed, text)
    patch = {"agents": {"entries": entries}}
    assert_patch_scope(patch, [a["id"] for a in agents])
    return patch


def assert_patch_scope(patch: dict, agent_ids: list[str]) -> None:
    """A patch may only touch agents.entries of our agents and plugins.entries.jobhunter-guard, and
    skills.load.extraDirs; never bindings, channels, agents.defaults or browser.defaultProfile."""
    allowed_top = {"agents", "plugins", "skills", "browser"}
    bad = [k for k in patch if k not in allowed_top]
    if "agents" in patch:
        a = patch["agents"]
        if set(a) != {"entries"} or any(k not in agent_ids for k in a["entries"]):
            bad.append("agents")
    if "plugins" in patch:
        p = patch["plugins"]
        if set(p) != {"entries"} or set(p["entries"]) - {GUARD_PLUGIN_ID}:
            bad.append("plugins")
    if "skills" in patch and set(patch["skills"]) != {"load"}:
        bad.append("skills")
    if "browser" in patch:
        b = patch["browser"]
        if set(b) != {"profiles"} or set(b["profiles"]) != {BROWSER_PROFILE}:
            bad.append("browser")
    if bad:
        raise Denied("E_VALIDATION", "config patch leaves its allowed scope", data={"keys": bad})


def render_guard_config(*, repo: str, py: str, cfg: dict) -> dict:
    """plugins.entries.jobhunter-guard.config (1.4)."""
    conf = {"repo": repo, "python": py, "homeFile": os.path.join(repo, "private", "home.json"),
            "publicReadonlyAgents": ["main"]}
    target = notify_target(cfg)
    if target is not None:
        conf["ownerFallback"] = [{"channel": target[0], "senderId": target[1]}]
    patch = {"plugins": {"entries": {GUARD_PLUGIN_ID: {"config": conf}}}}
    assert_patch_scope(patch, [])
    return patch


def headless_patch() -> dict:
    """Linux and WSL: the jobhunter browser profile must be headed [verify key name]."""
    return {"browser": {"profiles": {BROWSER_PROFILE: {"headless": False}}}}


def uninstall_patch(agents: list[dict]) -> dict:
    patch = {"agents": {"entries": {a["id"]: None for a in agents}},
             "plugins": {"entries": {GUARD_PLUGIN_ID: None}}}
    assert_patch_scope(patch, [a["id"] for a in agents])
    return patch


def extra_dirs_patch(current, repo: str, remove: bool = False) -> tuple[dict | None, list]:
    """skills.load.extraDirs with $REPO/shared-skills appended (or removed). The full merged array is patched
    because config patch replaces arrays. Returns (patch or None when nothing changes, new list)."""
    cur = current if isinstance(current, list) else []
    cur = [str(x) for x in cur]
    target = os.path.join(repo, "shared-skills")
    if remove:
        new = [x for x in cur if x != target]
    else:
        new = cur + ([target] if target not in cur else [])
    if new == cur:
        return None, new
    return {"skills": {"load": {"extraDirs": new}}}, new


def _approvals_document(current) -> tuple[dict, list[str]]:
    """Find the host approvals document in `approvals get --json` output ([verify] shape). Returns the document
    and the key path it was found under."""
    if isinstance(current, dict):
        if "agents" in current or "defaults" in current:
            return current, []
        for key in ("file", "document", "approvals", "snapshot", "host"):
            inner = current.get(key)
            if isinstance(inner, dict) and ("agents" in inner or "defaults" in inner):
                return inner, [key]
        if not current:
            return {}, []
    if current is None:
        return {}, []
    raise Denied("E_VALIDATION", "unrecognised `openclaw approvals get --json` output; merge the jobhunter "
                 "entries by hand (docs/TROUBLESHOOTING.md)")


def merge_approvals(current, agents: list[dict], *, repo: str, py: str, root: str | None = None,
                    remove: bool = False) -> dict:
    """The approvals document with one entry per jobhunter agent under `agents` (exec agents: python and the
    jh.py argPattern; agents without exec: deny). Other agents and `defaults` are kept as they are."""
    doc, _ = _approvals_document(current)
    doc = json.loads(json.dumps(doc))
    doc.setdefault("version", 1)
    agents_map = doc.get("agents") if isinstance(doc.get("agents"), dict) else {}
    ids = [a["id"] for a in agents]
    if remove:
        for i in ids:
            agents_map.pop(i, None)
    else:
        tmpl = _template("exec-approvals.json5.tmpl", root)
        for a in agents:
            if "exec" in a["tools_allow"]:
                entry = _walk_subst(tmpl["exec_agent"], {}, {"__PY__": py, "__ARG_PATTERN__": arg_pattern(repo)})
            else:
                entry = json.loads(json.dumps(tmpl["no_exec_agent"]))
            agents_map[a["id"]] = entry
    doc["agents"] = agents_map
    return doc


# ---------------------------------------------------------------- guard heartbeat
def guard_status(max_age_s: int = 600) -> dict:
    """{fresh, age_s, install_id, reason} from state/guard/heartbeat.json and private/home.json (12.18)."""
    h = home_or_empty()
    want = h.get("install_id")
    hb_path = os.path.join(paths.guard_dir(), "heartbeat.json")
    try:
        with open(hb_path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except (OSError, ValueError):
        return {"fresh": False, "age_s": None, "install_id": want, "reason": "no_heartbeat"}
    if not isinstance(hb, dict) or hb.get("install_id") != want or not want:
        return {"fresh": False, "age_s": None, "install_id": want, "reason": "install_id_mismatch"}
    try:
        beat = _dt.datetime.strptime(str(hb.get("beat_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return {"fresh": False, "age_s": None, "install_id": want, "reason": "bad_beat_at"}
    age = int((_dt.datetime.now(_dt.timezone.utc) - beat).total_seconds())
    fresh = -120 <= age <= max_age_s
    return {"fresh": fresh, "age_s": age, "install_id": want, "reason": "" if fresh else "stale"}


def wait_guard(timeout_s: int, poll_s: float = 2.0) -> dict:
    deadline = time.monotonic() + max(0, timeout_s)
    while True:
        st = guard_status()
        if st["fresh"] or time.monotonic() >= deadline:
            return st
        time.sleep(poll_s)


# ---------------------------------------------------------------- misc renderers
def enabled_board_domains(cfg: dict, browsed_sites=None) -> list[str]:
    """Cookie-import domains: only boards the person enabled for the browser: discover or apply = browser in
    boards.sites, or a site the scout browses because the confirmed profile lists it (`browsed_sites`, from
    searches.enabled_sites, which is what the interview's job site question turns on)."""
    sites = _get(cfg, "boards.sites", {}) or {}
    browsed = {str(x) for x in (browsed_sites or [])}
    out = []
    for name, dom in BOARD_DOMAINS.items():
        s = sites.get(name) or {}
        if s.get("discover") == "browser" or s.get("apply") == "browser" or name in browsed:
            out.append(dom)
    return out


# ---------------------------------------------------------------- owner details (./jobhunter init)
GMAIL_PLACEHOLDERS = frozenset({"", "you@example.com"})
ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
PHONE_CHARS_RE = re.compile(r"^\+?[0-9 ().-]{7,24}$")
NUMBER_CHANNELS = ("whatsapp", "signal", "sms", "imessage")


def placeholder_link(url: str) -> bool:
    low = str(url).lower()
    return "your-handle" in low or "example" in low or not low.strip()


def owner_missing(cfg: dict) -> list[str]:
    """Owner fields of private/config.json that still hold the shipped placeholders: first_name, last_name,
    gmail_address, notify (a channel without a real number) and signature_links (a placeholder link)."""
    o = cfg.get("owner") if isinstance(cfg.get("owner"), dict) else {}
    out = []
    for k in ("first_name", "last_name"):
        if not str(o.get(k) or "").strip():
            out.append(k)
    addr = str(o.get("gmail_address") or "").strip().lower()
    if addr in GMAIL_PLACEHOLDERS or not ADDRESS_RE.match(addr):
        out.append("gmail_address")
    n = o.get("notify") if isinstance(o.get("notify"), dict) else {}
    if str(n.get("channel") or "none") != "none" and str(n.get("to") or "") in OWNER_PLACEHOLDERS:
        out.append("notify")
    sig = o.get("signature") if isinstance(o.get("signature"), dict) else {}
    if any(placeholder_link(x) for x in sig.get("links") or []):
        out.append("signature_links")
    return out


def _clean_name(v: str, what: str) -> str:
    v = " ".join(str(v).split())
    if not v or len(v) > 60 or any(ord(c) < 32 for c in v) or "@" in v:
        raise Denied("E_VALIDATION", "%s must be 1 to 60 characters" % what)
    return v


def set_owner(first_name=None, last_name=None, gmail_address=None, phone=None, notify_channel=None,
              notify_to=None, links=None) -> dict:
    """Write the given owner fields into private/config.json (only those; everything else in the file is kept).
    Placeholder signature links are always dropped. Returns {changed: [...], notify_changed: bool}."""
    path = os.path.join(paths.private_dir(), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise Denied("E_PRECONDITION", "private/config.json does not exist yet; run ./jobhunter init")
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "private/config.json is not valid JSON: %s" % exc)
    if not isinstance(raw, dict):
        raise Denied("E_CONFIG_INVALID", "private/config.json must hold a JSON object")
    owner = raw.setdefault("owner", {})
    if not isinstance(owner, dict):
        raise Denied("E_CONFIG_INVALID", "owner in private/config.json must be an object")
    sig = owner.setdefault("signature", {})
    notify = owner.setdefault("notify", {})
    if not isinstance(sig, dict) or not isinstance(notify, dict):
        raise Denied("E_CONFIG_INVALID", "owner.signature and owner.notify must be objects")
    changed = []
    old_full = " ".join(x for x in (str(owner.get("first_name") or "").strip(),
                                    str(owner.get("last_name") or "").strip()) if x)
    if first_name is not None:
        owner["first_name"] = _clean_name(first_name, "the first name")
        changed.append("first_name")
    if last_name is not None:
        owner["last_name"] = _clean_name(last_name, "the last name")
        changed.append("last_name")
    if first_name is not None or last_name is not None:
        new_full = " ".join(x for x in (str(owner.get("first_name") or "").strip(),
                                        str(owner.get("last_name") or "").strip()) if x)
        if not str(sig.get("full_name") or "").strip() or str(sig.get("full_name")).strip() == old_full:
            sig["full_name"] = new_full
            changed.append("signature.full_name")
    if gmail_address is not None:
        addr = str(gmail_address).strip().lower()
        if addr in GMAIL_PLACEHOLDERS or not ADDRESS_RE.match(addr) or len(addr) > 254:
            raise Denied("E_VALIDATION", "that is not an email address you can send from")
        owner["gmail_address"] = addr
        changed.append("gmail_address")
    if phone is not None:
        ph = " ".join(str(phone).split())
        digits = re.sub(r"\D", "", ph)
        if ph and (not PHONE_CHARS_RE.match(ph) or not 7 <= len(digits) <= 15):
            raise Denied("E_VALIDATION", "the signature phone number should look like +<country code> <number>")
        sig["phone"] = ph
        changed.append("signature.phone")
    old_links = [str(x) for x in sig.get("links") or []]
    new_links = [x for x in old_links if not placeholder_link(x)]
    if links is not None:
        new_links = []
        for u in links:
            u = str(u).strip()
            if not u:
                continue
            if not re.match(r"^https?://[^\s]+$", u) or placeholder_link(u):
                raise Denied("E_VALIDATION", "signature links must be full https:// addresses of your own pages")
            new_links.append(u)
    if new_links != old_links:
        sig["links"] = new_links
        changed.append("signature.links")
    old_target = (str(notify.get("channel") or "none"), str(notify.get("to") or ""))
    if notify_channel is not None:
        ch = str(notify_channel).strip().lower() or "none"
        if ch != "none" and not CHANNEL_RE.match(ch):
            raise Denied("E_VALIDATION", "the chat channel must be an OpenClaw channel id such as whatsapp, or none")
        notify["channel"] = ch
        changed.append("notify.channel")
    if notify_to is not None:
        to = str(notify_to).strip()
        ch = str(notify.get("channel") or "none")
        if ch in NUMBER_CHANNELS and not E164_RE.match(to.replace(" ", "")):
            raise Denied("E_VALIDATION", "the %s number must be in international form: + country code and number, "
                                         "digits only" % ch)
        if ch in NUMBER_CHANNELS:
            to = to.replace(" ", "")
        if ch != "none" and (not to or len(to) > 100 or any(c.isspace() for c in to)):
            raise Denied("E_VALIDATION", "give the address or number of your chat account")
        notify["to"] = to
        changed.append("notify.to")
    new_target = (str(notify.get("channel") or "none"), str(notify.get("to") or ""))
    if changed:
        _write_private(path, json.dumps(raw, indent=2, ensure_ascii=True) + "\n")
    return {"changed": changed, "notify_changed": new_target != old_target,
            "missing": owner_missing(raw)}


# ---------------------------------------------------------------- ./jobhunter pass-through
def wrapper_route(words: list[str], commands: dict, human_only: list[str]) -> tuple[str, str]:
    """How ./jobhunter runs `jh.py <words>`: ('pin', command) when the command needs the owner (acl.json
    human_only, or only the H caller letter), ('open', command) for a system call, ('none', reason) when the
    words name no command a person runs (agent and automation commands). `commands`: {path: caller letters}."""
    lead = []
    for w in words:
        if w.startswith("-"):
            break
        lead.append(w)
    best = ""
    for i in range(len(lead), 0, -1):
        cand = " ".join(lead[:i])
        if cand in commands:
            best = cand
            break
    if not best:
        group = lead[0] if lead else ""
        subs = sorted(c.split(" ", 1)[1].split(" ")[0] for c, who in commands.items()
                      if group and c.startswith(group + " ") and "H" in str(who))
        if subs:
            return "none", "usage: ./jobhunter %s %s" % (group, "|".join(sorted(set(subs))))
        return "none", "unknown command: %s (./jobhunter help lists the commands)" % " ".join(lead[:2] or words[:1])
    callers = str(commands[best])
    if "H" not in callers:
        return "none", "%s is run by the agents or the automations, not by hand" % best
    flags = {w.split("=", 1)[0] for w in words if w.startswith("--")}
    for entry in human_only:
        ws = str(entry).split()
        need = [w for w in ws if w.startswith("--")]
        if " ".join(w for w in ws if not w.startswith("--")) == best and (not need or any(f in flags for f in need)):
            return "pin", best
    return ("open" if "S" in callers else "pin"), best


def missing_agents(agents: list[dict], agents_list_doc, ws_root: str) -> list[dict]:
    """Agents absent from `openclaw agents list --json` (a list of {id, ...}, or an object with `agents`)."""
    rows = agents_list_doc.get("agents") if isinstance(agents_list_doc, dict) else agents_list_doc
    have = {str(r.get("id")) for r in rows if isinstance(r, dict)} if isinstance(rows, list) else set()
    return [{"id": a["id"], "workspace": os.path.join(ws_root, a["role"]), "model": a["model"]}
            for a in agents if a["id"] not in have]


def render_stayawake_plist(repo: str, root: str | None = None) -> str:
    with open(os.path.join(root or source_root(), "macos", STAYAWAKE_LABEL + ".plist.tmpl"), "r",
              encoding="utf-8") as fh:
        text = fh.read()
    return substitute(text, {"__REPO__": repo, "__LABEL__": STAYAWAKE_LABEL,
                             "__LOG__": os.path.join(repo, "logs", "stay-awake.log")})


def shell_env(h: dict) -> str:
    """KEY=value lines (shell-quoted) for the wrapper and install.sh."""
    pairs = [("JH_HOME_EXISTS", "1" if h else "0"), ("JH_OC_BIN", h.get("oc_bin", "")),
             ("JH_OC_PROFILE", h.get("oc_profile", "")), ("JH_PY", h.get("python", "")),
             ("JH_WS_ROOT", h.get("ws_root", "")), ("JH_INSTALL_ID", h.get("install_id", ""))]
    return "\n".join("%s=%s" % (k, shlex.quote(str(v or ""))) for k, v in pairs)


def cron_summary(crons: dict, cron_list_doc) -> str:
    found = cron_jobs_from_list(cron_list_doc)
    lines = []
    for j in crons["jobs"]:
        f = found.get(j["key"])
        state = "missing" if f is None else ("enabled" if f["enabled"] else "disabled")
        lines.append("%-26s %-9s %s" % (j["key"], state, j["display"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- browser consent (./jobhunter browser consent)
# The agents browse in their own OpenClaw browser profile `jobhunter`. A site's login gets there only with the
# owner's consent for that site: either its cookies are copied from a Chrome profile the owner picks
# (`openclaw browser import-profile --domains <that site's domains>`, macOS) or the owner logs in by hand inside
# the jobhunter profile. private/consent.json records one row per site; only a row with status "granted" is
# active. Writing a grant is a human-only command (PIN); revoking needs no PIN (it only takes access away).
CONSENT_FILE = "consent.json"
CONSENT_VERSION = 1
CONSENT_METHODS = ("chrome_import", "manual_login")
CONSENT_STATUSES = ("granted", "declined", "revoked")
# site: (label, cookie domains for the import filter, login check page)
CONSENT_SITES = {
    "gmail": ("Gmail", ("google.com", "mail.google.com", "accounts.google.com"),
              "https://mail.google.com/mail/u/0/#inbox"),
    "linkedin": ("LinkedIn", ("linkedin.com", "www.linkedin.com"), "https://www.linkedin.com/feed/"),
    "naukri": ("Naukri", ("naukri.com",), "https://www.naukri.com/mnjuser/homepage"),
    "indeed": ("Indeed", ("indeed.com",), "https://myjobs.indeed.com/"),
    "glassdoor": ("Glassdoor", ("glassdoor.com", "glassdoor.co.in"), "https://www.glassdoor.com/member/profile"),
    "foundit": ("Foundit", ("foundit.in",), "https://www.foundit.in/seeker/dashboard"),
    "instahyre": ("Instahyre", ("instahyre.com",), "https://www.instahyre.com/candidate/opportunities/"),
    "wellfound": ("Wellfound", ("wellfound.com",), "https://wellfound.com/jobs"),
    "cutshort": ("Cutshort", ("cutshort.io",), "https://cutshort.io/jobs"),
    "hirist": ("Hirist", ("hirist.tech",), "https://www.hirist.tech/"),
    "iimjobs": ("iimjobs", ("iimjobs.com",), "https://www.iimjobs.com/"),
    "yc": ("Work at a Startup (YC)", ("workatastartup.com", "ycombinator.com"),
           "https://www.workatastartup.com/companies"),
}
CONSENT_ALWAYS_ASK = ("gmail", "linkedin", "naukri", "indeed", "glassdoor", "foundit", "instahyre", "wellfound")
CHROME_DIR_RE = re.compile(r"^(Default|Profile [0-9]{1,4})$")
CHROME_SKIP_DIRS = ("System Profile", "Guest Profile")
WORK_NAME_RE = re.compile(r"\b(work|office|corp|corporate|company|school|university|enterprise)\b", re.I)


def consent_path() -> str:
    return os.path.join(paths.private_dir(), CONSENT_FILE)


def _empty_consent() -> dict:
    return {"version": CONSENT_VERSION, "sites": {}}


def load_consent(strict: bool = True) -> dict:
    """private/consent.json. Missing file: no consent. With `strict` (every reader that decides whether a site
    may be used) a symlink, a file of another user or one that group or others can write, or a file that is not
    valid counts as no consent at all (fail closed). Never raises."""
    path = consent_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return _empty_consent()
    try:
        st = os.fstat(fd)
        if strict and (st.st_uid != os.getuid() or st.st_mode & 0o022):
            return _empty_consent()
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            doc = json.load(fh)
    except (OSError, ValueError):
        return _empty_consent()
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(doc, dict) or not isinstance(doc.get("sites"), dict):
        return _empty_consent()
    sites = {}
    for site, row in doc["sites"].items():
        if site in CONSENT_SITES and isinstance(row, dict) and row.get("status") in CONSENT_STATUSES:
            sites[site] = row
    return {"version": CONSENT_VERSION, "sites": sites, "updated_at": doc.get("updated_at")}


def _row_active(site: str, row) -> bool:
    """The rule every reader shares (identity.consent_active in the core, consent.ts in the guard): status
    granted under the site's own name, a granted_at, and no revoked_at."""
    return (isinstance(row, dict) and row.get("site", site) == site and row.get("status") == "granted"
            and isinstance(row.get("granted_at"), str) and bool(row["granted_at"].strip())
            and row.get("revoked_at") in (None, "", False))


def consent_active(site: str, doc: dict | None = None) -> bool:
    """True when the owner's consent for `site` (gmail, linkedin, or a board name of boards.sites) is active.
    The wrapper's view; preflight and gate reserve enforce the same rule through identity.require_consent."""
    site = str(site)
    return _row_active(site, (doc if doc is not None else load_consent()).get("sites", {}).get(site))


def active_consents(doc: dict | None = None) -> dict:
    doc = doc if doc is not None else load_consent()
    return {s: r for s, r in doc.get("sites", {}).items() if _row_active(s, r)}


def consent_sites(cfg: dict, browsed_sites=None, only=None, doc: dict | None = None) -> list[dict]:
    """The sites to ask about, in order: the fixed list (Gmail, LinkedIn and the main boards), then every other
    board the person enabled for the browser, then any site that already has a row. `only` limits the list to
    named sites (unknown names are refused)."""
    doc = doc if doc is not None else load_consent()
    enabled = set()
    browsed = {str(x) for x in (browsed_sites or [])}
    board_cfg = _get(cfg, "boards.sites", {}) or {}
    for name in CONSENT_SITES:
        s = board_cfg.get(name) if isinstance(board_cfg.get(name), dict) else {}
        if s.get("discover") == "browser" or s.get("apply") == "browser" or name in browsed:
            enabled.add(name)
    names = list(CONSENT_ALWAYS_ASK) + [n for n in CONSENT_SITES if n in enabled and n not in CONSENT_ALWAYS_ASK]
    names += [n for n in CONSENT_SITES if n in doc.get("sites", {}) and n not in names]
    if only:
        wanted = [str(x).strip().lower() for x in only if str(x).strip()]
        bad = [w for w in wanted if w not in CONSENT_SITES]
        if bad:
            raise Denied("E_VALIDATION", "unknown site %s; known: %s" % (", ".join(bad), ", ".join(CONSENT_SITES)))
        names = [n for n in CONSENT_SITES if n in wanted]
    out = []
    for n in names:
        label, domains, url = CONSENT_SITES[n]
        row = doc.get("sites", {}).get(n) or {}
        status = row.get("status") or "none"
        if status == "granted" and not _row_active(n, row):
            status = "revoked" if row.get("revoked_at") else "none"
        out.append({"site": n, "label": label, "domains": list(domains), "check_url": url,
                    "status": status, "method": row.get("method"),
                    "chrome_profile": row.get("chrome_profile")})
    return out


def _site_list(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in items:
        for w in str(item).split(","):
            w = w.strip().lower()
            if not w:
                continue
            if w not in CONSENT_SITES:
                raise Denied("E_VALIDATION", "unknown site %s; known: %s" % (w, ", ".join(CONSENT_SITES)))
            if w not in out:
                out.append(w)
    return out


def _save_consent(doc: dict) -> str:
    doc = {"version": CONSENT_VERSION, "updated_at": now(), "sites": doc.get("sites", {})}
    return _write_private(consent_path(), json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


def record_consent(grant=None, decline=None, *, method: str, chrome_profile: str | None = None,
                   chrome_profile_name: str | None = None) -> dict:
    """Record the owner's answers (the caller checked the PIN). A grant stores the site, its domains, the
    method, the Chrome profile folder and display name (chrome_import only) and granted_at; a decline stores
    declined_at and makes an earlier grant inactive. Returns {granted, declined, path}."""
    grant, decline = _site_list(grant), _site_list(decline)
    if set(grant) & set(decline):
        raise Denied("E_VALIDATION", "a site cannot be granted and declined at once")
    if not grant and not decline:
        raise Denied("E_USAGE", "name at least one site with --grant or --decline")
    if method not in CONSENT_METHODS:
        raise Denied("E_VALIDATION", "method must be one of %s" % ", ".join(CONSENT_METHODS))
    prof, prof_name = None, None
    if method == "chrome_import" and grant:
        prof = str(chrome_profile or "")
        if not CHROME_DIR_RE.match(prof):
            raise Denied("E_VALIDATION", "choose the Chrome profile folder (Default or Profile <n>)")
        prof_name = " ".join(str(chrome_profile_name or prof).split())[:80] or prof
        if any(ord(c) < 32 for c in prof_name):
            raise Denied("E_VALIDATION", "the Chrome profile name has control characters")
    doc = load_consent(strict=False)
    ts = now()
    for site in grant:
        doc["sites"][site] = {"site": site, "status": "granted", "method": method, "domains":
                              list(CONSENT_SITES[site][1]), "chrome_profile": prof,
                              "chrome_profile_name": prof_name, "granted_at": ts, "revoked_at": None,
                              "declined_at": None, "by": "owner"}
    for site in decline:
        old = doc["sites"].get(site) or {}
        doc["sites"][site] = {"site": site, "status": "declined", "method": old.get("method"),
                              "domains": list(CONSENT_SITES[site][1]), "chrome_profile": old.get("chrome_profile"),
                              "chrome_profile_name": old.get("chrome_profile_name"),
                              "granted_at": old.get("granted_at"), "revoked_at": old.get("revoked_at"),
                              "declined_at": ts, "by": "owner"}
    path = _save_consent(doc)
    return {"granted": grant, "declined": decline, "path": path}


def revoke_consent(sites=None, everything: bool = False) -> dict:
    """Mark consent revoked (./jobhunter browser forget). Returns {revoked, remaining_imports: [{chrome_profile,
    domains}], remaining_manual: [site]}: what the wrapper re-imports after it cleared the agent profile's
    cookies, and the manual-login sites that need a new login by hand."""
    doc = load_consent(strict=False)
    if everything:
        targets = [s for s, r in doc["sites"].items() if r.get("status") == "granted"]
    else:
        targets = _site_list(sites)
        if not targets:
            raise Denied("E_USAGE", "name a site or use --all")
    ts = now()
    revoked = []
    for site in targets:
        row = doc["sites"].get(site)
        if row and row.get("status") == "granted":
            row["status"] = "revoked"
            row["revoked_at"] = ts
            revoked.append(site)
    if revoked:
        _save_consent(doc)
    return dict({"revoked": revoked}, **remaining_imports(doc))


def remaining_imports(doc: dict | None = None) -> dict:
    """Active consents grouped for a re-import: chrome_import rows by Chrome profile folder (domains joined),
    manual_login sites listed apart."""
    groups: dict = {}
    manual = []
    for site, row in sorted(active_consents(doc).items()):
        if row.get("method") == "chrome_import" and CHROME_DIR_RE.match(str(row.get("chrome_profile") or "")):
            doms = groups.setdefault(row["chrome_profile"], [])
            for d in CONSENT_SITES[site][1]:
                if d not in doms:
                    doms.append(d)
        else:
            manual.append(site)
    return {"remaining_imports": [{"chrome_profile": k, "domains": v} for k, v in sorted(groups.items())],
            "remaining_manual": manual}


# ---------------------------------------------------------------- Chrome profiles (Local State)
def chrome_local_state_path(home: str | None = None, platform: str | None = None) -> str:
    import sys
    home = home or os.path.expanduser("~")
    platform = platform or sys.platform
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Local State")
    return os.path.join(home, ".config", "google-chrome", "Local State")


def chrome_profiles(local_state_path: str) -> list[dict]:
    """Chrome profiles by display name from Chrome's `Local State` file (profile.info_cache). Nothing else of
    Chrome is read. `work` is true for a profile that looks like a work or school profile: signed in to a
    managed (hosted) domain, marked managed, or named like one; such a profile is never picked silently."""
    try:
        with open(local_state_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise Denied("E_NOT_FOUND", "Google Chrome's Local State file was not found (is Chrome installed and "
                                    "opened once?)", data={"path": local_state_path})
    except (OSError, ValueError) as exc:
        raise Denied("E_VALIDATION", "Chrome's Local State file is unreadable: %s" % type(exc).__name__)
    prof = doc.get("profile") if isinstance(doc, dict) else None
    cache = prof.get("info_cache") if isinstance(prof, dict) else None
    if not isinstance(cache, dict):
        return []
    order = [d for d in (prof.get("profiles_order") or []) if d in cache] if isinstance(prof, dict) else []
    rest = sorted((d for d in cache if d not in order), key=lambda d: (d != "Default", len(d), d))
    out = []
    for d in order + rest:
        info = cache.get(d)
        if d in CHROME_SKIP_DIRS or not isinstance(info, dict) or not CHROME_DIR_RE.match(d):
            continue
        name = " ".join(str(info.get("name") or info.get("gaia_name") or d).split())[:80] or d
        name = "".join(c for c in name if ord(c) >= 32)
        email = str(info.get("user_name") or "").strip()
        hosted = str(info.get("hosted_domain") or "").strip()
        reasons = []
        if hosted and hosted.upper() != "NO_HOSTED_DOMAIN":
            reasons.append("managed by %s" % hosted)
        if info.get("is_managed") or info.get("enterprise_label") or info.get("is_supervised"):
            reasons.append("managed by an organisation")
        if WORK_NAME_RE.search(name):
            reasons.append("named like a work or school profile")
        out.append({"dir": d, "name": name, "email": email, "work": bool(reasons), "work_reason": "; ".join(reasons)})
    return out


# ---------------------------------------------------------------- login check after an import (read only)
LOGIN_PROBE_JS = """() => {
  /* jobhunter login check (read only): where the page is, its title, the Google account address when the page
     shows one, and whether a password field, a CAPTCHA or a verification text is on screen. Never clicks,
     types, stores or navigates. */
  const text = ((document.body && document.body.innerText) || '').slice(0, 5000);
  const labels = Array.from(document.querySelectorAll('a[aria-label], button[aria-label]'))
    .map((el) => el.getAttribute('aria-label') || '').filter((s) => /google account/i.test(s));
  let email = null;
  for (const s of labels.concat([document.title || ''])) {
    const m = s.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}/);
    if (m) { email = m[0].toLowerCase(); break; }
  }
  return {
    jh_login_probe: 1,
    url: location.href,
    title: (document.title || '').slice(0, 200),
    account_email: email,
    password_field: !!document.querySelector('input[type="password"]'),
    captcha: !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], .g-recaptcha, #captcha'),
    challenge_text: /verify it'?s you|unusual activity|security check|confirm it'?s you|are you a robot|verify you are human|security verification/i.test(text)
  };
}"""
CHECKPOINT_URL_WORDS = ("/checkpoint", "/challenge", "captcha", "/sorry/", "/verify", "/signin/rejected",
                        "/interstitial")
LOGGED_OUT_URL_WORDS = ("/login", "/signin", "/sign-in", "/sign_in", "/authwall", "/uas/login", "/auth/",
                        "/nlogin", "/account/login", "servicelogin")


def find_probe(doc):
    """The probe object inside `openclaw browser --json evaluate` output, wherever the CLI nests it."""
    if isinstance(doc, dict):
        if doc.get("jh_login_probe") == 1:
            return doc
        for v in doc.values():
            hit = find_probe(v)
            if hit is not None:
                return hit
    elif isinstance(doc, list):
        for v in doc:
            hit = find_probe(v)
            if hit is not None:
                return hit
    elif isinstance(doc, str) and "jh_login_probe" in doc:
        try:
            return find_probe(json.loads(doc))
        except ValueError:
            return None
    return None


def login_verdict(site: str, probe, expect_email: str | None = None) -> tuple[str, str]:
    """(verdict, message) for a login check: ok, logged_out, checkpoint (a CAPTCHA or verification prompt:
    stop and ask the person, never solve or bypass it), mismatch (Gmail is logged in to another account than
    the configured sender) or unknown (the person looks at the window and answers)."""
    from urllib.parse import urlsplit
    label = CONSENT_SITES.get(site, (site,))[0]
    if not isinstance(probe, dict):
        return "unknown", "%s: the page could not be read" % label
    url = str(probe.get("url") or "")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower()
    where = "%s%s" % (host, parts.path or "")
    low = (host + path + "?" + (parts.query or "")).lower()
    if probe.get("captcha") or probe.get("challenge_text") or any(w in low for w in CHECKPOINT_URL_WORDS):
        return "checkpoint", ("%s shows a CAPTCHA or a verification prompt (%s). Finish it yourself in the jobhunter "
                              "window; the agent never does" % (label, where))
    if probe.get("password_field") or any(w in low for w in LOGGED_OUT_URL_WORDS) or \
            host in ("accounts.google.com", "secure.indeed.com"):
        return "logged_out", "%s is not logged in in the agent profile (%s)" % (label, where)
    doms = CONSENT_SITES.get(site, ("", ()))[1]
    on_site = any(host == d or host.endswith("." + d) for d in doms)
    if site == "gmail":
        if host != "mail.google.com":
            return "unknown", "Gmail did not open (%s)" % where
        got = str(probe.get("account_email") or "").strip().lower()
        want = str(expect_email or "").strip().lower()
        if not got:
            return "unknown", "Gmail is open, but the account address could not be read"
        if want and want not in GMAIL_PLACEHOLDERS and got != want:
            return "mismatch", ("the Gmail account in the agent profile is %s, but your sender address "
                                "(owner.gmail_address) is %s" % (got, want))
        if not want or want in GMAIL_PLACEHOLDERS:
            return "ok", "Gmail is logged in as %s (set the same address as your sender in ./jobhunter init)" % got
        return "ok", "Gmail is logged in as the sender address %s" % got
    if site == "linkedin":
        if on_site and path.startswith("/feed"):
            return "ok", "LinkedIn feed loads, no checkpoint"
        return "unknown", "LinkedIn did not show the feed (%s)" % where
    if on_site:
        return "ok", "%s looks logged in (%s; best effort)" % (label, where)
    return "unknown", "%s opened another site (%s)" % (label, where)
