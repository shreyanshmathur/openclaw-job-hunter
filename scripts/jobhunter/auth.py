"""Caller classes, owner PIN, chat grants and the ACL check (design 3.1, 3.3, 1.4 R6/R7, 12.19).

Classes, decided before any command runs:
- agent: OPENCLAW_SHELL is set. JH_AGENT_ID (injected only by the guard) names the agent; without it only
  public read-only commands run, anything else is E_GUARD_MISSING. Agents can never present a PIN or a grant.
  The guard also sets JH_AGENT_PROOF = "<unix ts>.<hex HMAC-SHA256(guard.key, 'agent\n' + id + '\n' + ts)>"
  (13.1 #4). A proof that is present must verify (else E_AUTH_FAILED); once a valid proof has been seen on
  this install (state/agent-proof-seen), a jobhunter-* agent id without one is refused too, so JH_AGENT_ID
  cannot be spoofed by an exec that sets the variable itself.
- chat: --grant <ts>.<nonce>.<hex hmac> verifies (HMAC-SHA256 with private/guard.key over
  command + "\\n" + json(args) + "\\n" + ts + "\\n" + nonce, at most 120 s old, nonce single use).
- human: --pin-stdin reads a PIN that verifies (scrypt n=2^15 r=8 p=1 when hashlib.scrypt exists, else
  PBKDF2-HMAC-SHA256 600,000 rounds). 5 failures in an hour lock human commands for an hour.
- system: everything else (command cron jobs, the wrapper's plain calls, the guard's execFile).

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
AGENT_PROOF_MAX_AGE_S = 6 * 3600      # one agent exec (a whole cycle at most) runs under one proof
AGENT_PROOF_FUTURE_S = 60
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
            raise Denied("E_GUARD_MISSING", "agent call without JH_AGENT_ID; the jobhunter-guard plugin is not active")
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


def classify(argv: list[str], env: dict, stdin, command: str | None = None, args: list | None = None) -> Caller:
    """Decide the caller class (see module doc). Verifies a grant or a PIN when one is presented."""
    pin_flag, grant = _flags(list(argv))
    if env.get("OPENCLAW_SHELL"):
        if pin_flag or grant is not None:
            raise Denied("E_AUTH_FAILED", "agents cannot present the owner PIN or a chat grant")
        agent = env.get("JH_AGENT_ID") or None
        proof = env.get("JH_AGENT_PROOF") or None
        if agent and proof is not None:
            verify_agent_proof(agent, proof)
            _note_proof_seen()
        elif agent and agent.startswith("jobhunter-") and _proof_seen():
            raise Denied("E_AUTH_FAILED", "JH_AGENT_ID without the guard's JH_AGENT_PROOF")
        return Caller("agent", agent)
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


def agent_proof(agent_id: str, ts: int | None = None, key_hex: str | None = None) -> str:
    """The JH_AGENT_PROOF the guard sets for an agent exec (src/grant.ts agentProof; used by tests)."""
    key = bytes.fromhex(key_hex or read_guard_key())
    ts = _epoch() if ts is None else int(ts)
    mac = hmac.new(key, ("agent\n%s\n%d" % (agent_id, ts)).encode("utf-8"), hashlib.sha256).hexdigest()
    return "%d.%s" % (ts, mac)


def verify_agent_proof(agent_id: str, proof: str) -> None:
    """Check JH_AGENT_PROOF for JH_AGENT_ID: format, age and HMAC. Denied(E_AUTH_FAILED) otherwise."""
    m = re.match(r"^([0-9]{9,11})\.([0-9a-f]{64})$", proof or "")
    if not m:
        raise Denied("E_AUTH_FAILED", "malformed agent proof")
    ts = int(m.group(1))
    age = _epoch() - ts
    if age > AGENT_PROOF_MAX_AGE_S or age < -AGENT_PROOF_FUTURE_S:
        raise Denied("E_AUTH_FAILED", "agent proof expired")
    key_hex = read_guard_key()
    msg = ("agent\n%s\n%s" % (agent_id, m.group(1))).encode("utf-8")
    ok = False
    for key in (bytes.fromhex(key_hex), key_hex.encode("ascii")):
        ok = hmac.compare_digest(hmac.new(key, msg, hashlib.sha256).hexdigest(), m.group(2)) or ok
    if not ok:
        raise Denied("E_AUTH_FAILED", "agent proof does not match %s" % agent_id)


def _proof_marker() -> str | None:
    try:
        return os.path.join(paths.state_dir(), "agent-proof-seen")
    except Exception:
        return None


def _proof_seen() -> bool:
    p = _proof_marker()
    return bool(p) and os.path.exists(p)


def _note_proof_seen() -> None:
    """From the first verified proof on, this install's guard is known to send proofs."""
    p = _proof_marker()
    if not p or os.path.exists(p):
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        pass


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

