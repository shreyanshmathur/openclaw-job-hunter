"""Caller classes, owner PIN, chat grants and the ACL check (design 3.1, 3.3, 1.4 R6/R7, 12.19; CLI route 5).

Classes, decided before any command runs:
- agent (proven): the jobhunter-guard minted the call's identity proofs (CLI route design 5):
  * argv proof, the first two tokens of the jh.py arguments: `--agent-proof <T>`,
    T = "jhp2." + agent + "." + ts + "." + nonce + "." + sk + "." + hex HMAC-SHA256(key, "jh-agent-proof\\n2\\n" +
    agent + "\\n" + ts + "\\n" + nonce + "\\n" + sk + "\\n" + hex sha256("\\n".join(rest))), rest = the arguments
    after the proof pair;
  * env proof JH_AGENT_PROOF = "jhe2." + agent + "." + ts + "." + nonce + "." + sk + "." + hex HMAC-SHA256(key,
    "jh-agent-env\\n2\\n" + agent + "\\n" + ts + "\\n" + nonce + "\\n" + sk), set by the guard's resolve_exec_env;
  sk = first 16 hex of sha256(session key), nonce = 16 hex, ts = unix seconds, valid from 10 s in the future to
  120 s old. Every carrier listed in private/home.json cli_route.carriers (default argv and env) is required,
  all carriers must name the same agent and session, the interpreter must run with python -I, and every nonce
  is single use (grants_used, one BEGIN IMMEDIATE transaction). Agents can never present a PIN or a grant.
- agent (unproven): no proof, but a harness marker (OPENCLAW_SHELL, OPENCLAW_MCP_TOKEN, or Claude Code's
  CLAUDECODE or CLAUDE_CODE_ENTRYPOINT, wherever the working directory is): public read-only commands only,
  anything else is E_GUARD_MISSING. A JH_AGENT_ID naming a jobhunter agent without a proof is E_AUTH_FAILED.
- chat: --grant <ts>.<nonce>.<hex hmac> verifies (HMAC-SHA256 with private/guard.key over
  command + "\\n" + json(args) + "\\n" + ts + "\\n" + nonce, at most 120 s old, nonce single use).
- human: --pin-stdin reads a PIN that verifies (scrypt n=2^15 r=8 p=1 when hashlib.scrypt exists, else
  PBKDF2-HMAC-SHA256 600,000 rounds). 5 failures in an hour lock human commands for an hour.
- system: no proof, no marker, no grant, no PIN (command cron jobs, the wrapper's plain calls, install.sh, the
  guard's execFile). Each of these entry points drops the markers itself: scrub_agent_env() for jh.py's own
  children (ocrun.run, the QC worker) and the guard's childEnv; ./jobhunter and install.sh unset CLAUDECODE and
  CLAUDE_CODE_ENTRYPOINT at their start, so a person can run them from a Claude Code terminal; the command cron
  jobs start jh.py through `/usr/bin/env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT` (openclaw/crons.json), in case
  the Gateway itself was started from Claude Code. The markers are a negative signal only: dropping one never
  proves an identity, it only avoids treating a person's own call as an unproven agent.

Grant arguments: `command` is the command words ("approve", "profile answer"); `args` is the list of argv
tokens after the command words with --grant <g>, --quiet and --human removed. json(args) may be Python's
json.dumps default or the compact form (separators "," and ":", as JavaScript JSON.stringify writes it);
the key is the 32 bytes that guard.key holds as 64 hex characters (the hex text itself is accepted too).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from . import paths
from .canon import now, ts_add
from .errors import Denied

GRANT_TTL_S = 120
PROOF_TTL_S = 120                     # an agent proof is minted right before the exec and used at once
PROOF_FUTURE_S = 10
PROOF_FLAG = "--agent-proof"
CARRIERS = ("argv", "env")
DEFAULT_CARRIERS = ["argv", "env"]
AGENT_ID_RE = r"jobhunter-[a-z]{2,20}"
ARGV_PROOF_RE = re.compile(r"^jhp2\.(%s)\.([0-9]{10})\.([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{64})$" % AGENT_ID_RE)
ENV_PROOF_RE = re.compile(r"^jhe2\.(%s)\.([0-9]{10})\.([0-9a-f]{16})\.([0-9a-f]{16})\.([0-9a-f]{64})$" % AGENT_ID_RE)
V1_PROOF_RE = re.compile(r"^[0-9]{9,11}\.[0-9a-f]{64}$")
# Environment names that make a child process look like an agent call (CLI route 5.5): scrub_agent_env drops
# them for children that must stay `system` (ocrun.run, the QC worker); the guard's childEnv uses the same list.
SCRUB_EXACT = frozenset(("OPENCLAW_SHELL", "OPENCLAW_CHANNEL_CONTEXT", "CLAUDECODE", "JOBHUNTER_HOME", "JOBHUNTER_DB"))
SCRUB_PREFIXES = ("OPENCLAW_MCP_", "CLAUDE_CODE_", "JH_")
LOCK_FAILURES = 5
PIN_RE = re.compile(r"^[0-9]{6,12}$")
PBKDF2_ROUNDS = 600000
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


@dataclass
class Caller:
    cls: str                                  # agent | chat | human | system
    agent_id: str | None = None
    detail: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------- acl
def load_acl() -> dict:
    try:
        with open(paths.ACL_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise Denied("E_INTERNAL", "acl.json is unreadable: %s" % exc)


def _human_only_hit(acl: dict, command: str, args: dict) -> bool:
    for entry in acl.get("human_only") or []:
        words = entry.split()
        flags = [w for w in words if w.startswith("--")]
        cmd = " ".join(w for w in words if not w.startswith("--"))
        if cmd != command:
            continue
        if not flags or any(args.get(f) not in (None, False) for f in flags):
            return True
    return False


def _check_value(acl: dict, agent_id: str, flag: str, spec: str, value) -> None:
    optional = spec.endswith("?")
    base = spec[:-1] if optional else spec
    classes = acl.get("value_classes") or {}
    if base.startswith("enum:"):
        allowed = base[5:].split("|")
        if str(value) not in allowed:
            raise Denied("E_CALLER_NOT_ALLOWED", "%s must be one of %s" % (flag, ", ".join(allowed)))
        return
    rx = classes.get(base)
    if rx is None:
        raise Denied("E_INTERNAL", "acl.json names an unknown value class %r" % base)
    if rx == "FLAG":
        if value is not True:
            raise Denied("E_CALLER_NOT_ALLOWED", "%s is a switch" % flag)
        return
    if rx == "WORKDIR":
        paths.ensure_agent_path(str(value), agent_id)
        return
    if isinstance(value, (list, tuple)) or not re.match(rx, str(value)):
        raise Denied("E_CALLER_NOT_ALLOWED", "%s value does not match class %s" % (flag, base), data={"flag": flag})


def require(caller: Caller, command: str, args: dict) -> None:
    """acl.json check (the caller letters were checked by the CLI). Denied(E_CALLER_NOT_ALLOWED |
    E_HUMAN_ONLY | E_GUARD_MISSING | E_PATH_NOT_ALLOWED)."""
    acl = load_acl()
    args = args or {}
    public = set(acl.get("public_readonly") or [])
    if caller.cls == "agent":
        agent = caller.agent_id or ""
        if not agent:
            if command in public:
                return
            raise Denied("E_GUARD_MISSING", "no verified agent identity: this agent call carries no valid "
                         "jobhunter-guard proof (public read-only commands only)")
        if not agent.startswith("jobhunter-"):
            if command in public:
                return
            raise Denied("E_CALLER_NOT_ALLOWED", "only read-only commands for %s; use /jh in your chat" % agent)
        spec = (acl.get("agents") or {}).get(agent)
        cmds = spec.get("commands") if isinstance(spec, dict) else None
        if not isinstance(cmds, dict) or command not in cmds:
            raise Denied("E_CALLER_NOT_ALLOWED", "%s may not run %r" % (agent, command))
        if _human_only_hit(acl, command, args):
            raise Denied("E_HUMAN_ONLY", "%r needs the owner" % command)
        schema = cmds[command]
        for flag, value in args.items():
            if flag not in schema:
                raise Denied("E_CALLER_NOT_ALLOWED", "%s may not pass %s to %r" % (agent, flag, command))
            _check_value(acl, agent, flag, schema[flag], value)
        for flag, cls in schema.items():
            if not cls.endswith("?") and flag not in args:
                raise Denied("E_CALLER_NOT_ALLOWED", "%r needs %s" % (command, flag))
        return
    if caller.cls == "chat":
        if command not in (acl.get("chat") or []) or _human_only_hit(acl, command, args):
            raise Denied("E_CALLER_NOT_ALLOWED", "%r is not a chat command" % command)
        return
    if caller.cls == "human":
        return
    if caller.cls == "system":
        if _human_only_hit(acl, command, args):
            if command == "auth set-pin" and not pin_exists():
                return   # first PIN: the handler checks for a terminal
            raise Denied("E_HUMAN_ONLY", "%r needs the owner PIN (./jobhunter %s)" % (command, command))
        return
    raise Denied("E_CALLER_NOT_ALLOWED", "unknown caller class %r" % caller.cls)


# ---------------------------------------------------------------- classify
def _flags(argv: list[str]) -> tuple[bool, str | None]:
    pin, grant = False, None
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--pin-stdin":
            pin = True
        elif t == "--grant" and i + 1 < len(argv):
            grant = argv[i + 1]
            i += 1
        elif t.startswith("--grant="):
            grant = t.split("=", 1)[1]
        i += 1
    return pin, grant


def grant_args(argv: list[str], command: str) -> list[str]:
    """argv tokens after the command words, without --grant <g>, --quiet and --human."""
    rest = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--grant":
            i += 2
            continue
        if t.startswith("--grant=") or t in ("--quiet", "--human"):
            i += 1
            continue
        rest.append(t)
        i += 1
    words = command.split()
    for j in range(len(rest) - len(words) + 1):
        if rest[j:j + len(words)] == words:
            return rest[j + len(words):]
    return rest


def _chat_command(argv: list[str]) -> str:
    acl = load_acl()
    toks = [t for t in argv if not t.startswith("--")]
    best = ""
    for c in acl.get("chat") or []:
        w = c.split()
        if toks[:len(w)] == w and len(c) > len(best):
            best = c
    return best


def classify(argv: list[str], env: dict, stdin, proof_arg: str | None = None, proof_rest: list | None = None,
             conn=None, command: str | None = None, args: list | None = None) -> Caller:
    """Decide the caller class (see module doc). Verifies agent proofs, a grant or a PIN when one is presented.

    proof_arg is the value of `--agent-proof` (only ever the first jh.py argument, cli._split_globals) and
    proof_rest the arguments after the proof pair. conn is a write connection for the nonce record (one is
    opened when None). The guard key is read once, only when a proof is presented."""
    pin_flag, grant = _flags(list(argv))
    env = env or {}
    ids: dict = {}
    if proof_arg is not None or env.get("JH_AGENT_PROOF"):
        key_hex = read_guard_key()
        if proof_arg is not None:
            ids["argv"] = verify_argv_proof(proof_arg, list(proof_rest or []), key_hex=key_hex)
        if env.get("JH_AGENT_PROOF"):
            # the env carrier alone binds no command and no argv proof names the session: JH_SESSION_KEY must be
            # there and match the proof (with argv+env the sessions of both proofs are compared below)
            ids["env"] = verify_env_proof(env["JH_AGENT_PROOF"], env.get("JH_SESSION_KEY"), key_hex=key_hex,
                                          require_session=proof_arg is None)
    if ids:
        required = required_carriers()
        missing = [c for c in required if c not in ids]
        if missing:
            raise Denied("E_AUTH_FAILED", "missing %s proof" % missing[0])
        if not is_isolated():
            raise Denied("E_AUTH_FAILED", "agent calls must run with python -I")
        agents = {v[0] for v in ids.values()}
        sks = {v[2] for v in ids.values()}
        if len(agents) != 1:
            raise Denied("E_AUTH_FAILED", "proofs name different agents")
        if len(sks) != 1:
            raise Denied("E_AUTH_FAILED", "proofs name different sessions")
        agent = agents.pop()
        if env.get("JH_AGENT_ID") not in (None, "", agent):
            raise Denied("E_AUTH_FAILED", "JH_AGENT_ID does not match the proof")
        if pin_flag or grant is not None:
            raise Denied("E_AUTH_FAILED", "agents cannot present the owner PIN or a chat grant")
        carriers = sorted(ids)
        consume_nonces(conn, [("ap:" if c == "argv" else "ep:") + ids[c][1] for c in carriers], agent)   # last step
        return Caller("agent", agent, {"carriers": carriers, "session": sks.pop(), "markers": harness_markers(env)})
    if (env.get("JH_AGENT_ID") or "").startswith("jobhunter-"):
        raise Denied("E_AUTH_FAILED", "a jobhunter agent id without the guard's proof")
    markers = harness_markers(env)
    if markers:
        if pin_flag or grant is not None:
            raise Denied("E_AUTH_FAILED", "agents cannot present the owner PIN or a chat grant")
        return Caller("agent", None, {"markers": markers})       # public read-only only (cli.check_callers)
    if pin_flag and grant is not None:
        raise Denied("E_AUTH_FAILED", "use either --pin-stdin or --grant")
    if grant is not None:
        cmd = command or _chat_command(argv)
        a = args if args is not None else grant_args(list(argv), cmd)
        nonce = verify_grant(grant, cmd, a)
        return Caller("chat", None, {"nonce": nonce})
    if pin_flag:
        line = stdin.readline() if stdin is not None else ""
        pin = (line or "").rstrip("\r\n")
        check_pin(pin, command or "")
        return Caller("human", None, {"pin_verified": True, "pin": pin})
    return Caller("system", None)


# ---------------------------------------------------------------- agent proofs (CLI route design 5)
def is_isolated() -> bool:
    """True when this interpreter runs with python -I (sys.flags.isolated): no PYTHONPATH, no user site, no
    script directory on sys.path. The guard always starts jh.py that way for agents."""
    import sys
    return bool(getattr(sys.flags, "isolated", 0))


def session_hash(session_key: str | None) -> str:
    """sk: the first 16 hex of sha256(utf8(session key or ""))."""
    return hashlib.sha256((session_key or "").encode("utf-8")).hexdigest()[:16]


def argv_digest(rest: list) -> str:
    """hex sha256 of the jh.py arguments after the proof pair, joined by newlines."""
    return hashlib.sha256("\n".join(str(t) for t in rest).encode("utf-8")).hexdigest()


def _argv_mac(key: bytes, agent_id: str, ts: str, nonce: str, sk: str, digest: str) -> str:
    msg = "jh-agent-proof\n2\n%s\n%s\n%s\n%s\n%s" % (agent_id, ts, nonce, sk, digest)
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _env_mac(key: bytes, agent_id: str, ts: str, nonce: str, sk: str) -> str:
    msg = "jh-agent-env\n2\n%s\n%s\n%s\n%s" % (agent_id, ts, nonce, sk)
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _key_bytes(key_hex: str | None) -> bytes:
    return bytes.fromhex(key_hex or read_guard_key())


def argv_proof(agent_id: str, rest: list, session_key: str | None = None, ts: int | None = None,
               nonce: str | None = None, key_hex: str | None = None) -> str:
    """The `--agent-proof` value the guard mints for `<PY> -I jh.py --agent-proof <T> <rest...>` (tests and the
    shared vectors; the guard's src/grant.ts argvProof computes the same)."""
    ts_s = "%010d" % (_epoch() if ts is None else int(ts))
    nonce = nonce or secrets.token_hex(8)
    sk = session_hash(session_key)
    return "jhp2.%s.%s.%s.%s.%s" % (agent_id, ts_s, nonce, sk,
                                     _argv_mac(_key_bytes(key_hex), agent_id, ts_s, nonce, sk, argv_digest(rest)))


def env_proof(agent_id: str, session_key: str | None = None, ts: int | None = None, nonce: str | None = None,
              key_hex: str | None = None) -> str:
    """The JH_AGENT_PROOF the guard's resolve_exec_env sets (src/grant.ts envProof)."""
    ts_s = "%010d" % (_epoch() if ts is None else int(ts))
    nonce = nonce or secrets.token_hex(8)
    sk = session_hash(session_key)
    return "jhe2.%s.%s.%s.%s.%s" % (agent_id, ts_s, nonce, sk, _env_mac(_key_bytes(key_hex), agent_id, ts_s, nonce, sk))


def _check_age(ts_s: str, what: str) -> None:
    age = _epoch() - int(ts_s)
    if age > PROOF_TTL_S or age < -PROOF_FUTURE_S:
        raise Denied("E_AUTH_FAILED", "%s proof expired" % what)


def verify_argv_proof(token: str, rest: list, key_hex: str | None = None) -> tuple[str, str, str]:
    """Check an `--agent-proof` value against the arguments after it: format, age, HMAC over their digest.
    Returns (agent_id, nonce, sk). Denied(E_AUTH_FAILED) otherwise. The nonce is not consumed here. key_hex is
    the guard key when the caller already read it (classify reads it once for both carriers)."""
    token = token if isinstance(token, str) else ""
    m = ARGV_PROOF_RE.match(token)
    if not m:
        raise Denied("E_AUTH_FAILED", "malformed agent proof")
    agent, ts_s, nonce, sk, mac = m.groups()
    _check_age(ts_s, "agent")
    want = _argv_mac(_key_bytes(key_hex), agent, ts_s, nonce, sk, argv_digest(list(rest)))
    if not hmac.compare_digest(want, mac):
        raise Denied("E_AUTH_FAILED", "agent proof does not match this command")
    return agent, nonce, sk


def verify_env_proof(token: str, session_key: str | None = None, key_hex: str | None = None,
                     require_session: bool = False) -> tuple[str, str, str]:
    """Check JH_AGENT_PROOF: format (a version 1 proof means an old guard), age, HMAC, and the hash of
    JH_SESSION_KEY when that is set. With require_session (the env proof is the only carrier of the call)
    JH_SESSION_KEY must be set as well. Returns (agent_id, nonce, sk). Denied(E_AUTH_FAILED) otherwise."""
    token = token if isinstance(token, str) else ""
    if V1_PROOF_RE.match(token):
        raise Denied("E_AUTH_FAILED", "old guard; run ./install.sh")
    m = ENV_PROOF_RE.match(token)
    if not m:
        raise Denied("E_AUTH_FAILED", "malformed agent environment proof")
    agent, ts_s, nonce, sk, mac = m.groups()
    _check_age(ts_s, "agent environment")
    want = _env_mac(_key_bytes(key_hex), agent, ts_s, nonce, sk)
    if not hmac.compare_digest(want, mac):
        raise Denied("E_AUTH_FAILED", "agent environment proof does not match")
    if require_session and session_key in (None, ""):
        raise Denied("E_AUTH_FAILED", "JH_SESSION_KEY is missing: with the env carrier alone the session must be named")
    if session_key not in (None, "") and session_hash(session_key) != sk:
        raise Denied("E_AUTH_FAILED", "JH_SESSION_KEY does not match the proof")
    return agent, nonce, sk


def required_carriers() -> list[str]:
    """private/home.json cli_route.carriers: the proof carriers jh.py requires (default argv and env).
    An empty value or one outside {argv, env} is E_CONFIG_INVALID."""
    try:
        route = paths.home().get("cli_route")
    except Denied:
        route = None
    if route is None:
        return list(DEFAULT_CARRIERS)
    if not isinstance(route, dict):
        raise Denied("E_CONFIG_INVALID", "private/home.json cli_route must be an object")
    carriers = route.get("carriers", DEFAULT_CARRIERS)
    if isinstance(carriers, str):
        carriers = [c for c in re.split(r"[+,\s]+", carriers) if c]
    if not isinstance(carriers, list) or not carriers or not all(isinstance(c, str) for c in carriers) \
            or not set(carriers) <= set(CARRIERS):
        raise Denied("E_CONFIG_INVALID", "private/home.json cli_route.carriers must name argv, env or both")
    return sorted(set(carriers))


def consume_nonces(conn, nonces: list[str], agent_id: str) -> None:
    """Record every proof nonce in grants_used in one BEGIN IMMEDIATE transaction; a nonce seen before rolls
    all of them back and is E_AUTH_FAILED "proof already used"."""
    import sqlite3
    from . import db
    own = conn is None
    if own:
        try:
            conn = db.connect(write=True)
        except Denied as d:
            raise Denied("E_AUTH_FAILED", "cannot record the agent proof: %s" % d.message)
    try:
        with db.tx(conn):
            for n in nonces:
                try:
                    conn.execute("INSERT INTO grants_used (nonce, command, used_at) VALUES (?, ?, ?)",
                                 (n, "agent-proof " + agent_id, now()))
                except sqlite3.IntegrityError:
                    raise Denied("E_AUTH_FAILED", "proof already used")
    finally:
        if own:
            conn.close()


def harness_markers(env: dict) -> list[str]:
    """Names (never values) of the environment signs that this process was started by an agent harness:
    OPENCLAW_SHELL (OpenClaw's exec tool), OPENCLAW_MCP_TOKEN (a claude child of an OpenClaw CLI run), and
    CLAUDECODE or CLAUDE_CODE_ENTRYPOINT (Claude Code's Bash, from any working directory). A negative signal
    only: it turns `system` into an unproven agent, it never proves an identity. The human and system entry
    points drop them before they start jh.py (see the module doc)."""
    env = env or {}
    out = []
    if env.get("OPENCLAW_SHELL"):
        out.append("OPENCLAW_SHELL")
    if "OPENCLAW_MCP_TOKEN" in env:
        out.append("OPENCLAW_MCP_TOKEN")
    if "CLAUDECODE" in env or "CLAUDE_CODE_ENTRYPOINT" in env:
        out.append("CLAUDECODE")
    return out


def scrub_agent_env(env) -> dict:
    """A copy of env without the agent markers and identity variables (5.5), for children that must be
    classified as `system`."""
    return {k: v for k, v in dict(env or {}).items()
            if k not in SCRUB_EXACT and not any(k.startswith(p) for p in SCRUB_PREFIXES)}


def remove_v1_marker() -> bool:
    """Delete state/agent-proof-seen left by version 1 of the agent proof. True when a file was removed."""
    try:
        os.unlink(os.path.join(paths.state_dir(), "agent-proof-seen"))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- grants
def guard_key_file() -> str:
    return os.path.join(paths.private_dir(), "guard.key")


def read_guard_key() -> str:
    try:
        with open(guard_key_file(), "r", encoding="ascii") as fh:
            text = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        raise Denied("E_AUTH_FAILED", "private/guard.key is missing; run ./jobhunter init")
    if not re.match(r"^[0-9a-f]{64}$", text):
        raise Denied("E_AUTH_FAILED", "private/guard.key is malformed")
    return text


def create_guard_key() -> bool:
    """Write private/guard.key (32 random bytes as hex, mode 600) if absent. Returns True when created."""
    path = guard_key_file()
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(secrets.token_hex(32) + "\n")
    return True


def sign_grant(command: str, args: list, ts: int | None = None, nonce: str | None = None, key_hex: str | None = None) -> str:
    """The grant the guard produces (used by tests and selftest)."""
    key = bytes.fromhex(key_hex or read_guard_key())
    ts = int(time.time()) if ts is None else int(ts)
    nonce = nonce or secrets.token_hex(8)
    msg = "%s\n%s\n%d\n%s" % (command, json.dumps(list(args)), ts, nonce)
    return "%d.%s.%s" % (ts, nonce, hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest())


def _epoch() -> int:
    from .canon import utcnow
    return int(utcnow().timestamp())


def verify_grant(grant: str, command: str, args: list[str]) -> str:
    """Check a chat grant: format, age (<= 120 s), HMAC, single use (recorded in grants_used)."""
    m = re.match(r"^([0-9]{9,11})\.([0-9a-f]{16})\.([0-9a-f]{64})$", grant or "")
    if not m:
        raise Denied("E_AUTH_FAILED", "malformed grant")
    ts, nonce, mac = int(m.group(1)), m.group(2), m.group(3)
    age = _epoch() - ts
    if age > GRANT_TTL_S or age < -10:
        raise Denied("E_AUTH_FAILED", "grant expired")
    key_hex = read_guard_key()
    ok = False
    for key in (bytes.fromhex(key_hex), key_hex.encode("ascii")):
        for body in (json.dumps(list(args)), json.dumps(list(args), separators=(",", ":"), ensure_ascii=False)):
            msg = "%s\n%s\n%d\n%s" % (command, body, ts, nonce)
            want = hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()
            ok = ok or hmac.compare_digest(want, mac)
    if not ok:
        raise Denied("E_AUTH_FAILED", "grant signature does not match")
    from . import db
    try:
        conn = db.connect(write=True)
    except Denied as d:
        raise Denied("E_AUTH_FAILED", "cannot record the grant: %s" % d.message)
    try:
        with db.tx(conn):
            if conn.execute("SELECT 1 FROM grants_used WHERE nonce = ?", (nonce,)).fetchone():
                raise Denied("E_AUTH_FAILED", "grant already used")
            conn.execute("INSERT INTO grants_used (nonce, command, used_at) VALUES (?, ?, ?)", (nonce, command, now()))
    finally:
        conn.close()
    return nonce


# ---------------------------------------------------------------- PIN
def pin_file() -> str:
    return os.path.join(paths.private_dir(), "owner_pin.json")


def pin_exists() -> bool:
    return os.path.exists(pin_file())


def _hash(pin: str, rec: dict) -> str:
    salt = bytes.fromhex(rec["salt"])
    if rec["algo"] == "scrypt":
        if not hasattr(hashlib, "scrypt"):
            raise Denied("E_AUTH_FAILED", "this Python has no scrypt; reset the PIN")
        return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=rec["n"], r=rec["r"], p=rec["p"],
                              maxmem=128 * rec["n"] * rec["r"] * 2 + 1024 * 1024, dklen=32).hex()
    if rec["algo"] == "pbkdf2_sha256":
        return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, int(rec["rounds"]), dklen=32).hex()
    raise Denied("E_AUTH_FAILED", "unknown PIN hash algorithm")


def _new_record(pin: str) -> dict:
    salt = secrets.token_hex(16)
    if hasattr(hashlib, "scrypt"):
        rec = {"algo": "scrypt", "n": SCRYPT["n"], "r": SCRYPT["r"], "p": SCRYPT["p"], "salt": salt}
    else:
        rec = {"algo": "pbkdf2_sha256", "rounds": PBKDF2_ROUNDS, "salt": salt}
    rec["hash"] = _hash(pin, rec)
    rec["set_at"] = now()
    return rec


def _read_record() -> dict | None:
    try:
        with open(pin_file(), "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Denied("E_AUTH_FAILED", "private/owner_pin.json is unreadable")
    if not isinstance(rec, dict) or "hash" not in rec or "salt" not in rec:
        raise Denied("E_AUTH_FAILED", "private/owner_pin.json is malformed")
    return rec


def _attempts_conn():
    from . import db
    try:
        return db.connect(write=True)
    except Denied:
        return None


def check_pin(pin: str, command: str) -> None:
    """Verify the owner PIN with lockout (5 failures within an hour lock human commands for an hour and
    queue a high alert). Denied(E_AUTH_FAILED | E_AUTH_LOCKED). The lockout check and the attempt row are
    one BEGIN IMMEDIATE transaction before the (slow) hash: the attempt counts as a failure until the PIN
    verified, so a parallel burst of guesses is checked against the limit one at a time."""
    from . import db
    from .events import enqueue_notification
    conn = _attempts_conn()
    try:
        attempt_id = None
        if conn is not None:
            with db.tx(conn):
                since = ts_add(now(), hours=-1)
                fails = conn.execute("SELECT count(*) FROM auth_attempts WHERE ok = 0 AND at > ?",
                                     (since,)).fetchone()[0]
                if fails < LOCK_FAILURES:
                    attempt_id = conn.execute("INSERT INTO auth_attempts (at, ok, command) VALUES (?, 0, ?)",
                                              (now(), (command or "-")[:80])).lastrowid
            if attempt_id is None:
                last = conn.execute("SELECT max(at) FROM auth_attempts WHERE ok = 0").fetchone()[0]
                retry = None
                if last:
                    from .canon import seconds_between
                    retry = max(1, 3600 - seconds_between(last, now()))
                raise Denied("E_AUTH_LOCKED", "too many wrong PINs; human commands are locked for an hour",
                             retry_after=retry)
        rec = _read_record()
        if rec is None:
            if conn is not None and attempt_id is not None:
                with db.tx(conn):      # no PIN set is not a guess: the pending attempt does not count
                    conn.execute("DELETE FROM auth_attempts WHERE id = ?", (attempt_id,))
            raise Denied("E_AUTH_FAILED", "no owner PIN is set; run ./jobhunter pin set")
        good = False
        if isinstance(pin, str) and PIN_RE.match(pin or ""):
            good = hmac.compare_digest(_hash(pin, rec), rec["hash"])
        if conn is not None:
            with db.tx(conn):
                if good:
                    conn.execute("UPDATE auth_attempts SET ok = 1 WHERE id = ?", (attempt_id,))
                else:
                    fails = conn.execute("SELECT count(*) FROM auth_attempts WHERE ok = 0 AND at > ?",
                                         (ts_add(now(), hours=-1),)).fetchone()[0]
                    if fails >= LOCK_FAILURES:
                        enqueue_notification(conn, "auth_locked:%s" % now()[:13], "high", "alert",
                                             "Five wrong PINs in an hour: human commands are locked for an hour.")
        if not good:
            raise Denied("E_AUTH_FAILED", "wrong PIN")
    finally:
        if conn is not None:
            conn.close()


def set_pin(old: str | None, new: str, verified: bool = False) -> str:
    """Store a new PIN hash (6 to 12 digits) in private/owner_pin.json (mode 600). When a PIN exists the
    old PIN must verify (or the caller was already verified with --pin-stdin). Returns pin_set_at."""
    if not isinstance(new, str) or not PIN_RE.match(new):
        raise Denied("E_VALIDATION", "the PIN must be 6 to 12 digits")
    rec = _read_record()
    if rec is not None and not verified:
        if old is None or not PIN_RE.match(old) or not hmac.compare_digest(_hash(old, rec), rec["hash"]):
            raise Denied("E_AUTH_FAILED", "the old PIN does not match")
    new_rec = _new_record(new)
    path = pin_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(new_rec, fh, sort_keys=True)
    os.replace(tmp, path)
    return new_rec["set_at"]


def lockout_state(conn) -> dict:
    since = ts_add(now(), hours=-1)
    fails = conn.execute("SELECT count(*) FROM auth_attempts WHERE ok = 0 AND at > ?", (since,)).fetchone()[0]
    return {"failures_last_hour": fails, "locked": fails >= LOCK_FAILURES}

