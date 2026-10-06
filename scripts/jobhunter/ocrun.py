"""OpenClaw subprocess helper (design 1.2, 1.5, 5.3; CLI route 6). Every call runs the binary and profile
recorded in private/home.json, with an argv list (never a shell), stdin closed, a timeout and an environment
without agent markers (auth.scrub_agent_env), so a jh.py started by OpenClaw on our behalf stays `system`.
Output shapes are [verify] items (13.1 #8, #17; CLI route V13 to V15): JSON is parsed defensively (the last JSON
object in the text), and any non-zero exit or unparsable output is a failure.

No jobhunter agent is ever started with `openclaw agent` (that path cannot be restricted). Agent runs are cron
jobs created with an explicit --tools list:
- preflight(key): a fresh guard heartbeat with proof_version 2, then verify_job(key) against the manifest spec
  (state/install-manifest.json cron_specs, written by install); returns the job id for `cron run`.
- verify_job(key): compares every declared field of the listed job with its spec (E_CRON_DRIFT names the
  fields, never values); a toolsAllow that is absent or contains * is always drift.
- qc_turn(): one QC review as a one-shot cron job with --tools "" (F-QC: "write"), run with --wait and removed.
- neutralize_mentions(text), has_file_mention(text): no agent message may hold `@<path>`, which Claude Code turns
  into a file attachment outside every tool (D12); qc_turn neutralizes its packet and a listed job whose message
  holds one drifts (`message_mention`).
- effective_exec(agent), agents_list(): read-only views of OpenClaw's effective exec policy and agents.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess

from . import paths
from .errors import Denied


def oc_argv(*args: str) -> list[str]:
    """[oc_bin, --profile <p> (when set), *args]."""
    h = paths.home()
    oc = h.get("oc_bin") or "openclaw"
    out = [oc]
    if h.get("oc_profile"):
        out += ["--profile", str(h["oc_profile"])]
    return out + [str(a) for a in args]


def last_json(text: str):
    """The last JSON object (or array) in text, or None."""
    if not text:
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    found = None
    i = 0
    while i < len(t):
        j = min([p for p in (t.find("{", i), t.find("[", i)) if p != -1] or [-1])
        if j == -1:
            break
        try:
            obj, end = dec.raw_decode(t, j)
            found = obj
            i = end
        except ValueError:
            i = j + 1
    return found


# stdout is returned whole: JSON reads (a `cron list --all --json` is tens of KB on a real install) must see the
# complete document, never a tail of it. Output above this size is a failure, never a silently cut text.
STDOUT_MAX_BYTES = 64 * 1024 * 1024
STDERR_KEEP_CHARS = 4000


def run(argv: list[str], timeout_s: int) -> dict:
    """{ok, returncode, stdout, stderr, error}; ok means exit status 0. stdout is the child's complete output
    (callers parse it as JSON; whatever they log they cut themselves); stdout larger than STDOUT_MAX_BYTES is a
    failure with empty stdout. stderr keeps its last STDERR_KEEP_CHARS characters. The child gets this process's
    environment without agent markers or identity variables (auth.scrub_agent_env)."""
    from . import auth
    env = auth.scrub_agent_env(os.environ)
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout_s, env=env, cwd=paths.REPO)
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": "openclaw binary not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": "timeout after %d s" % timeout_s}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": str(exc)}
    err = proc.stderr.decode("utf-8", "replace")[-STDERR_KEEP_CHARS:]
    if len(proc.stdout) > STDOUT_MAX_BYTES:
        return {"ok": False, "returncode": proc.returncode, "stdout": "", "stderr": err,
                "error": "output larger than %d bytes (%d)" % (STDOUT_MAX_BYTES, len(proc.stdout))}
    out = proc.stdout.decode("utf-8", "replace")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": out, "stderr": err,
            "error": None if proc.returncode == 0 else (err.strip()[-500:] or "exit %d" % proc.returncode)}


def cron_run(job_id: str) -> dict:
    """Start one cron job (fire and forget, 1.2 [verify #1]). {ok, error, raw}."""
    if not job_id or not isinstance(job_id, str):
        raise Denied("E_VALIDATION", "no cron job id")
    r = run(oc_argv("cron", "run", job_id), 60)
    return {"ok": r["ok"], "error": r["error"], "raw": r["stdout"][-2000:]}


def cron_list() -> dict:
    r = run(oc_argv("cron", "list", "--all", "--json"), 60)
    return {"ok": r["ok"], "doc": last_json(r["stdout"]) if r["ok"] else None, "error": r["error"]}


def _text_of(obj) -> str | None:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("text", "reply", "output", "message", "content", "response", "result", "final"):
            if k in obj:
                t = _text_of(obj[k])
                if t:
                    return t
        for k in ("messages", "payloads"):
            if isinstance(obj.get(k), list):
                for item in reversed(obj[k]):
                    t = _text_of(item)
                    if t:
                        return t
    if isinstance(obj, list):
        parts = [_text_of(x) for x in obj]
        parts = [p for p in parts if p]
        return "\n".join(parts) if parts else None
    return None


# The options of `openclaw message send` in the captured help of OpenClaw 2026.9.5 (13.1 #8). It has no
# --message-file (only `openclaw agent` does), so the text is read here and passed as the value of --message.
MESSAGE_SEND_FLAGS = frozenset(("--account", "--channel", "--delivery", "--dry-run", "--force-document",
                                "--gif-playback", "--help", "--json", "--message", "--media", "--pin",
                                "--presentation", "--reply-to", "--silent", "--target", "--thread-id", "--verbose",
                                "-h", "-m", "-t"))
MESSAGE_MAX_CHARS = 60000


def message_send_argv(channel: str, target: str, text: str, media: str | None = None) -> list[str]:
    """argv of one `openclaw message send`. The body goes in the --message=<text> form, so a body that starts
    with a dash is never read as an option; an image (a CAPTCHA screenshot) goes as --media=<absolute path>."""
    extra = ["--media=" + media] if media else []
    return oc_argv("message", "send", "--channel", channel, "--target", target, "--message=" + text, *extra, "--json")


def message_send(channel: str, target: str, message_file: str, media: str | None = None) -> dict:
    """Read message_file and run openclaw message send --channel <c> --target <t> --message <text> --json.
    Delivered only when the exit status is 0 and the JSON reports success. {ok, error}."""
    try:
        with open(message_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": "cannot read the message file: %s" % exc}
    text = text.replace("\x00", "").rstrip("\n")
    if not text.strip():
        return {"ok": False, "error": "empty message"}
    if len(text) > MESSAGE_MAX_CHARS:
        return {"ok": False, "error": "message longer than %d characters" % MESSAGE_MAX_CHARS}
    if media is not None and not (os.path.isabs(media) and os.path.isfile(media)):
        media = None                     # a missing screenshot: the text alone
    r = run(message_send_argv(channel, target, text, media), 60)
    if not r["ok"]:
        return {"ok": False, "error": r["error"] or "openclaw message send failed"}
    obj = last_json(r["stdout"])
    if not isinstance(obj, dict):
        return {"ok": False, "error": "unparsable output from openclaw message send"}
    if obj.get("ok") is False or obj.get("success") is False or obj.get("error"):
        return {"ok": False, "error": str(obj.get("error") or "reported failure")[:500]}
    good = obj.get("ok") is True or obj.get("success") is True or \
        str(obj.get("status", "")).lower() in ("ok", "sent", "delivered", "queued") or \
        any(k in obj for k in ("messageId", "message_id", "id"))
    return {"ok": bool(good), "error": None if good else "no success flag in the output"}


def cron_job_ids() -> dict:
    """{declaration key: job id} from private/home.json and state/install-manifest.json (written by install)."""
    ids = {}
    try:
        ids.update(paths.home().get("cron_jobs") or {})
    except Denied:
        pass
    try:
        with open(os.path.join(paths.state_dir(), "install-manifest.json"), "r", encoding="utf-8") as fh:
            ids.update((json.load(fh) or {}).get("cron_jobs") or {})
    except (OSError, ValueError, AttributeError):
        pass
    return {k: v for k, v in ids.items() if isinstance(k, str) and isinstance(v, str)}


# ---------------------------------------------------------------- runs that wait (CLI route 6.3)
def cron_run_wait(job_id: str, wait_s: int) -> dict:
    """openclaw cron run <id> --wait --wait-timeout <wait_s>s --json. {ok, error, raw, doc, status, text, cut}; ok
    means OpenClaw reported the run as succeeded (exit status 0). text is the run's final text when the run
    record carries it whole; a text the run record cut at its size cap (summary_cut) is never returned: text is
    None, cut True and error says so. doc and raw come from the complete stdout (raw is the verdict fallback
    text of qc.worker, so it is not cut either)."""
    if not job_id or not isinstance(job_id, str):
        raise Denied("E_VALIDATION", "no cron job id")
    wait_s = max(30, int(wait_s))
    r = run(oc_argv("cron", "run", job_id, "--wait", "--wait-timeout", "%ds" % wait_s, "--json"), wait_s + 60)
    doc = last_json(r["stdout"])
    status = None
    if isinstance(doc, dict):
        status = doc.get("completionStatus") or doc.get("status")
        if isinstance(doc.get("run"), dict):
            status = doc["run"].get("completionStatus") or doc["run"].get("status") or status
    rep = run_reply(doc)
    out = {"ok": r["ok"], "error": r["error"], "raw": r["stdout"], "doc": doc,
           "status": status if isinstance(status, str) else None, "text": rep["text"], "cut": rep["cut"]}
    if rep["cut"]:
        out["error"] = CUT_ERROR
    return out


_RUN_TEXT_KEYS = ("text", "summary", "outputText", "output", "reply", "result", "final", "finalText", "message",
                  "content", "response")

# OpenClaw 2026.9.8 keeps only the first 2000 characters (JavaScript string length) of a cron run's reply in the
# run record (`run.summary`, `task_runs.terminal_summary`) and appends U+2026 (live check D13; V13 fails there).
RUN_TEXT_CAP = 2000
RUN_TEXT_CUT_SLACK = 64        # the cut may also drop trailing whitespace before the ellipsis
CUT_MARK = "\u2026"
CUT_ERROR = ("the run record cut the reply at %d characters (OpenClaw keeps only the start of a long reply there), "
             "so the reply cannot be read from it" % RUN_TEXT_CAP)


def js_len(text: str) -> int:
    """Length of a string as JavaScript counts it (UTF-16 code units), which is how OpenClaw cuts the summary."""
    return len(text.encode("utf-16-le")) // 2


def summary_cut(text) -> bool:
    """True when a run-record text looks cut by the run record's size cap: it ends with U+2026 and is at (or
    within a little of) the cap. A whole reply of ours (a verdict JSON, a final word) never ends like that."""
    if not isinstance(text, str):
        return False
    s = text.rstrip()
    return s.endswith(CUT_MARK) and js_len(s) >= RUN_TEXT_CAP - RUN_TEXT_CUT_SLACK


def run_reply(doc) -> dict:
    """{text, cut} of a cron run from `cron run --wait --json` or `cron runs --json` output (the run record
    first, then the entries of a runs listing). A text cut by the run record (summary_cut) is not a reply:
    text None, cut True."""
    if not isinstance(doc, dict):
        return {"text": None, "cut": False}
    candidates = []
    if isinstance(doc.get("run"), dict):
        candidates.append(doc["run"])
    for k in ("entries", "runs"):
        if isinstance(doc.get(k), list) and doc[k] and isinstance(doc[k][0], dict):
            candidates.append(doc[k][0])
    for c in candidates:
        for k in _RUN_TEXT_KEYS:
            if k in c:
                t = _text_of(c[k])
                if t and t.strip():
                    if summary_cut(t):
                        return {"text": None, "cut": True}
                    return {"text": t, "cut": False}
    return {"text": None, "cut": False}


def run_text(doc) -> str | None:
    """The whole final reply text of a cron run (run_reply), or None: absent, or cut by the run record."""
    return run_reply(doc)["text"]


def cron_runs_reply(job_id: str) -> dict:
    """{text, cut} of the newest run from `cron runs --id <id> --limit 1 --json` (text None when absent)."""
    r = run(oc_argv("cron", "runs", "--id", job_id, "--limit", "1", "--json"), 60)
    if not r["ok"]:
        return {"text": None, "cut": False}
    return run_reply(last_json(r["stdout"]))


def cron_runs_text(job_id: str) -> str | None:
    """The newest run's whole text from `cron runs` (None when absent or cut)."""
    return cron_runs_reply(job_id)["text"]


def cron_rm(job_id: str) -> dict:
    r = run(oc_argv("cron", "rm", job_id, "--json"), 60)
    return {"ok": r["ok"], "error": r["error"]}


# ---------------------------------------------------------------- manifest specs and drift (CLI route 6.3.1)
AGENT_SPEC_FIELDS = ("agent", "session", "tools", "model", "fallbacks", "thinking", "timeout_s", "message_sha256",
                     "delivery")
COMMAND_SPEC_FIELDS = ("argv", "cwd", "env_sha256", "timeout_s")
QC_REVIEW_KEY = "jobhunter:qc-review"
QC_AGENT = "jobhunter-qc"
QC_MESSAGE_MAX_BYTES = 200 * 1024


# Claude Code (2.1.270, claude-cli route) reads `@<path>` and `@"<path>"` in a run's prompt as file mentions and
# attaches the file before the model runs: outside every tool, so neither --tools "", workspaceOnly nor the guard
# sees it (D12). It takes an @ at the start of the text or after whitespace or one of U+3001 U+3002 U+FF01 U+FF1F.
# Here an at sign is live unless it directly follows an ASCII letter, digit or one of ._%+- (an email address
# keeps its form) and it is directly followed by a character other than space, tab or a line break. The
# fullwidth and small at signs count as well, in case the text is NFKC-normalized on the way.
_AT_SIGNS = ("@", "\uff20", "\ufe6b")
_MENTION_SAFE_BEFORE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
_MENTION_SAFE_AFTER = frozenset(" \t\r\n")


def _live_at(text: str, i: int) -> bool:
    return (text[i] in _AT_SIGNS and (i == 0 or text[i - 1] not in _MENTION_SAFE_BEFORE)
            and i + 1 < len(text) and text[i + 1] not in _MENTION_SAFE_AFTER)


def has_file_mention(text) -> bool:
    """True when `text` holds an at sign that Claude Code could read as a file mention (see _live_at)."""
    text = str(text or "")
    return any(_live_at(text, i) for i in range(len(text)))


def neutralize_mentions(text) -> str:
    """`text` with a space after every live at sign, so no prompt token starts with @<something> (D12). The
    result has no live at sign (an at sign followed by a space is never live), and text without one is
    returned unchanged. Every message ocrun gives an agent run goes through this or has_file_mention."""
    text = str(text or "")
    out = []
    for i, ch in enumerate(text):
        out.append(ch)
        if _live_at(text, i):
            out.append(" ")
    return "".join(out)


def message_sha256(text: str) -> str:
    """hex sha256 of a cron message (UTF-8), as stored in the manifest spec."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def env_sha256(env) -> str:
    """hex sha256 of a command job's environment: canonical JSON, keys sorted, no spaces."""
    env = env if isinstance(env, dict) else {}
    return message_sha256(json.dumps({str(k): str(v) for k, v in env.items()}, sort_keys=True, separators=(",", ":")))


def read_manifest() -> dict:
    try:
        with open(os.path.join(paths.state_dir(), "install-manifest.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cron_specs() -> dict:
    """{key: spec} from state/install-manifest.json (written by install, 6.3.1)."""
    specs = read_manifest().get("cron_specs")
    return {k: v for k, v in specs.items() if isinstance(k, str) and isinstance(v, dict)} \
        if isinstance(specs, dict) else {}


def _int_or_none(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _blank(v):
    return None if v in (None, "") else v


def normalize_job(row) -> dict:
    """One row of `openclaw cron list --all --json` in the spec shape (job agentId, sessionTarget, delivery.mode;
    payload kind, toolsAllow, message, model, fallbacks, thinking, timeoutSeconds, argv, cwd, env) [verify V14].
    A missing field stays None, so it never equals a declared value."""
    row = row if isinstance(row, dict) else {}
    p = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    kind = p.get("kind")
    if kind == "command":
        argv = p.get("argv")
        return {"kind": "command", "argv": [str(a) for a in argv] if isinstance(argv, list) else None,
                "cwd": _blank(p.get("cwd")), "env_sha256": env_sha256(p.get("env") or {}),
                "timeout_s": _int_or_none(p.get("timeoutSeconds"))}
    tools = p.get("toolsAllow")
    fallbacks = p.get("fallbacks")
    delivery = row.get("delivery")
    agent = row.get("agentId") if row.get("agentId") is not None else row.get("agent")
    return {"kind": "agent" if kind == "agentTurn" else kind, "agent": _blank(agent),
            "session": _blank(row.get("sessionTarget")),
            "tools": sorted(str(t) for t in tools) if isinstance(tools, list) else None,
            "model": _blank(p.get("model")),
            "fallbacks": [str(f) for f in fallbacks] if isinstance(fallbacks, list) else [],
            "thinking": _blank(p.get("thinking")), "timeout_s": _int_or_none(p.get("timeoutSeconds")),
            "message_sha256": message_sha256(p["message"]) if isinstance(p.get("message"), str) else None,
            "message_mention": has_file_mention(p["message"]) if isinstance(p.get("message"), str) else False,
            "delivery": _blank(delivery.get("mode")) if isinstance(delivery, dict) else None}


def _spec_value(spec: dict, field: str):
    v = spec.get(field)
    if field == "tools":
        return sorted(str(t) for t in v) if isinstance(v, list) else v
    if field == "fallbacks":
        if isinstance(v, str):
            return [f for f in (x.strip() for x in v.split(",")) if f]
        return [str(f) for f in v] if isinstance(v, list) else []
    if field == "timeout_s":
        return _int_or_none(v)
    if field == "argv":
        return [str(a) for a in v] if isinstance(v, list) else v
    return _blank(v)


def job_drift(spec: dict, row) -> list[str]:
    """Names (never values) of the fields where a listed job differs from its spec. A job whose toolsAllow is
    absent or contains * always drifts. `enabled` and the schedule are not compared. A spec without a
    message_sha256 (the QC one-shot spec) skips the message. A job whose message holds a live at sign (a
    Claude Code file mention, D12) always drifts in `message_mention`, whatever its spec says."""
    got = normalize_job(row)
    if spec.get("kind") == "command":
        if got.get("kind") != "command":
            return ["kind"]
        return [f for f in COMMAND_SPEC_FIELDS if _spec_value(spec, f) != got.get(f)]
    if got.get("kind") != "agent":
        return ["kind"]
    fields = [f for f in AGENT_SPEC_FIELDS if f != "message_sha256" or spec.get("message_sha256") is not None]
    out = [f for f in fields if _spec_value(spec, f) != got.get(f)]
    if (got.get("tools") is None or "*" in got["tools"]) and "tools" not in out:
        out.append("tools")
    if got.get("message_mention"):
        out.append("message_mention")
    return out


def _rows(doc) -> list:
    rows = doc.get("jobs") if isinstance(doc, dict) else doc
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def verify_job(key: str, listing=None, job_id: str | None = None, spec: dict | None = None) -> str:
    """Check the cron job of declaration `key` against its manifest spec and return its id. `listing` is a
    parsed `cron list --all --json` (read here when None); `job_id` and `spec` override the recorded ones
    (qc_turn checks the one-shot job it just created). E_CRON_DRIFT names the differing fields, never values;
    E_OPENCLAW_CALL when the list cannot be read."""
    if spec is None:
        spec = cron_specs().get(key)
    if not isinstance(spec, dict):
        raise Denied("E_CRON_DRIFT", "%s has no spec in the install manifest; run ./install.sh again" % key,
                     data={"key": key, "fields": ["spec"]})
    if job_id is None:
        job_id = cron_job_ids().get(key)
    if not job_id:
        raise Denied("E_CRON_DRIFT", "no OpenClaw job id is recorded for %s; run ./install.sh again" % key,
                     data={"key": key, "fields": ["id"]})
    if listing is None:
        res = cron_list()
        if not res["ok"]:
            raise Denied("E_OPENCLAW_CALL", "openclaw cron list failed: %s" % (res["error"] or "no output"))
        listing = res["doc"]
    row = next((r for r in _rows(listing) if str(r.get("id")) == str(job_id)), None)
    if row is None:
        raise Denied("E_CRON_DRIFT", "the OpenClaw job of %s is gone; run ./install.sh again" % key,
                     data={"key": key, "fields": ["missing"]})
    fields = job_drift(spec, row)
    if fields:
        raise Denied("E_CRON_DRIFT", "the OpenClaw job of %s differs from the install manifest in: %s; run "
                     "./install.sh again" % (key, ", ".join(fields)), data={"key": key, "fields": fields})
    return str(job_id)


def require_heartbeat() -> dict:
    """A fresh guard heartbeat that announces proof version 2, else E_GUARD_MISSING."""
    from . import gate
    hb = gate.guard_heartbeat()
    if not hb["fresh"]:
        raise Denied("E_GUARD_MISSING", "the jobhunter-guard heartbeat is missing or stale; no agent run starts",
                     data={"age_s": hb["age_s"], "present": hb["present"]})
    if hb.get("proof_version") != 2:
        raise Denied("E_GUARD_MISSING", "the jobhunter-guard does not mint version 2 identity proofs (old guard); "
                     "run ./install.sh", data={"proof_version": hb.get("proof_version")})
    return hb


def preflight(key: str, listing=None) -> str:
    """Before any `cron run` of an agent job: guard heartbeat with proof_version 2, then verify_job. Returns
    the job id. E_GUARD_MISSING, E_CRON_DRIFT or E_OPENCLAW_CALL otherwise."""
    require_heartbeat()
    return verify_job(key, listing)


# ---------------------------------------------------------------- QC one-shot runs (CLI route 6.3.2)
def cli_route() -> dict:
    """private/home.json cli_route ({} when absent or unreadable)."""
    try:
        route = paths.home().get("cli_route")
    except Denied:
        return {}
    return route if isinstance(route, dict) else {}


def _log(line: str) -> None:
    try:
        if not os.path.isdir(paths.logs_dir()):
            return
        from .canon import now
        with open(os.path.join(paths.logs_dir(), "ocrun.log"), "a", encoding="utf-8") as fh:
            fh.write(("%s %s\n" % (now(), line)).encode("ascii", "replace").decode("ascii"))
    except OSError:
        pass


def _created_id(doc, decl_key: str) -> str | None:
    if isinstance(doc, dict):
        for c in (doc, doc.get("job"), doc.get("result")):
            if isinstance(c, dict) and c.get("id") and (c.get("declarationKey") in (None, decl_key)):
                return str(c["id"])
    res = cron_list()
    for r in _rows(res.get("doc")) if res["ok"] else []:
        if r.get("declarationKey") == decl_key and r.get("id"):
            return str(r["id"])
    return None


def qc_turn(session_key: str, message_file: str, timeout_s: int) -> dict:
    """One QC review turn as a restricted one-shot cron job (6.3.2): create `jobhunter:qc-review-<uid>` for
    jobhunter-qc with --tools "" ("write" when cli_route.qc_reply is "file", F-QC), verify it against the
    jobhunter:qc-review spec with this message's hash, require the guard heartbeat, run it with --wait, read the
    reply from the run record (else from `cron runs`; a reply the record cut is ok False with cut True,
    never a text), and always remove the job. The packet carries text from
    web pages and mail, so every live at sign gets a space after it first (neutralize_mentions, D12): Claude
    Code would otherwise attach the file an `@<path>` names. Returns {ok, text, raw}
    (plus uid, job_id, error), like the old agent_turn; a failed run is never a verdict."""
    uid = hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:12] + secrets.token_hex(2)
    try:
        with open(message_file, "rb") as fh:
            raw_msg = fh.read(QC_MESSAGE_MAX_BYTES + 1)
    except OSError as exc:
        raise Denied("E_VALIDATION", "the QC message file cannot be read: %s" % type(exc).__name__)
    if len(raw_msg) > QC_MESSAGE_MAX_BYTES:
        raise Denied("E_VALIDATION", "the QC message is larger than %d bytes" % QC_MESSAGE_MAX_BYTES)
    try:
        text = raw_msg.decode("utf-8")
    except UnicodeDecodeError:
        raise Denied("E_VALIDATION", "the QC message is not UTF-8 text")
    text = neutralize_mentions(text.replace("\x00", "").strip())
    if not text:
        raise Denied("E_VALIDATION", "the QC message is empty")
    spec = cron_specs().get(QC_REVIEW_KEY)
    if not isinstance(spec, dict) or not spec.get("model"):
        raise Denied("E_CRON_DRIFT", "the %s spec is missing from the install manifest; run ./install.sh again"
                     % QC_REVIEW_KEY, data={"key": QC_REVIEW_KEY, "fields": ["spec"]})
    if spec.get("agent") not in (None, QC_AGENT):
        raise Denied("E_CRON_DRIFT", "the %s spec names another agent" % QC_REVIEW_KEY,
                     data={"key": QC_REVIEW_KEY, "fields": ["agent"]})
    tools = ["write"] if cli_route().get("qc_reply") == "file" else []
    expected = dict(spec, agent=QC_AGENT, session="isolated", kind="agent-oneshot", tools=tools,
                    message_sha256=message_sha256(text), delivery="none")
    job_timeout = _int_or_none(spec.get("timeout_s")) or 600
    fallbacks = _spec_value(spec, "fallbacks")
    require_heartbeat()
    decl = "%s-%s" % (QC_REVIEW_KEY, uid)
    argv = ["cron", "add", "--name", "jobhunter-qc-review-%s" % uid, "--declaration-key", decl,
            "--agent", QC_AGENT, "--session", "isolated", "--cron", "0 0 1 1 *", "--disabled",
            "--tools", ",".join(tools), "--message=" + text, "--model", str(spec["model"])]
    if fallbacks:
        argv += ["--fallbacks", ",".join(fallbacks)]
    if spec.get("thinking"):
        argv += ["--thinking", str(spec["thinking"])]
    argv += ["--timeout-seconds", str(job_timeout), "--no-deliver", "--json"]
    added = run(oc_argv(*argv), 60)
    job_id = _created_id(last_json(added["stdout"]), decl) if added["ok"] else None
    if job_id is None:
        if added["ok"]:
            _log("qc_turn %s: created job not found" % uid)
        return {"ok": False, "text": None, "raw": (added["stdout"] or added["error"] or "")[-4000:],
                "error": added["error"] or "the one-shot QC job was not created", "uid": uid, "job_id": None}
    try:
        verify_job(QC_REVIEW_KEY, job_id=job_id, spec=expected)
        res = cron_run_wait(job_id, max(int(timeout_s), job_timeout) + 120)
        out, cut = res["text"], bool(res.get("cut"))
        if res["ok"] and not out and not cut:
            rep = cron_runs_reply(job_id)
            out, cut = rep["text"], rep["cut"]
        if cut:
            # a cut reply is never a verdict (D13): a clear failure the caller can tell apart (cut True)
            return {"ok": False, "text": None, "raw": res["raw"], "uid": uid, "job_id": job_id, "cut": True,
                    "error": CUT_ERROR + "; replies must come from the verdict file: run ./install.sh --smoke"}
        result = {"ok": bool(res["ok"]), "text": out, "raw": res["raw"], "uid": uid, "job_id": job_id}
        if not res["ok"]:
            result["error"] = res["error"] or "the QC run did not succeed (%s)" % (res["status"] or "unknown")
        return result
    finally:
        rm = cron_rm(job_id)
        if not rm["ok"]:
            _log("qc_turn %s: cron rm %s failed: %s" % (uid, job_id, rm["error"]))


# ---------------------------------------------------------------- effective exec policy (CLI route 6.1, 4.4)
def _exec_scopes(doc) -> list:
    if not isinstance(doc, dict):
        return []
    eff = doc.get("effectivePolicy")
    scopes = eff.get("scopes") if isinstance(eff, dict) else doc.get("scopes")
    return [s for s in scopes if isinstance(s, dict)] if isinstance(scopes, list) else []


def _field(scope: dict, name: str, sub: str):
    v = scope.get(name)
    v = v.get(sub) if isinstance(v, dict) else None
    return v if isinstance(v, str) and v and v != "unknown" else None


def _scope_for(scopes: list, agent: str):
    for s in scopes:
        if s.get("agentId") == agent or s.get("configPath") == "agents.entries.%s.tools.exec" % agent:
            return s
    return None


def _scope_view(scope) -> dict:
    scope = scope if isinstance(scope, dict) else {}
    return {"security": _field(scope, "security", "effective"), "ask": _field(scope, "ask", "effective"),
            "mode": _field(scope, "mode", "effective"), "approvals_ask": _field(scope, "ask", "host"),
            "approvals_fallback": _field(scope, "askFallback", "effective")}


def exec_policy_doc() -> dict | None:
    """Parsed `openclaw exec-policy show --json` (read only), or None."""
    r = run(oc_argv("exec-policy", "show", "--json"), 60)
    doc = last_json(r["stdout"]) if r["ok"] else None
    return doc if isinstance(doc, dict) else None


def sandbox_explain_doc(agent: str) -> dict | None:
    """Parsed `openclaw sandbox explain --agent <id> --json` (read only), or None."""
    r = run(oc_argv("sandbox", "explain", "--agent", agent, "--json"), 60)
    doc = last_json(r["stdout"]) if r["ok"] else None
    return doc if isinstance(doc, dict) else None


def effective_exec(agent: str, explain_doc=None, policy_doc=None) -> dict:
    """{security, ask, mode, elevated, approvals_ask, approvals_fallback, ok} for one agent from `sandbox explain`
    (elevated) and `exec-policy show` (the effective merge of config and host approvals) [verify V15]. A value
    that cannot be read is None and ok is False (fail closed)."""
    if policy_doc is None:
        policy_doc = exec_policy_doc()
    if explain_doc is None:
        explain_doc = sandbox_explain_doc(agent)
    out = _scope_view(_scope_for(_exec_scopes(policy_doc), agent))
    el = explain_doc.get("elevated") if isinstance(explain_doc, dict) else None
    out["elevated"] = el.get("enabled") if isinstance(el, dict) and isinstance(el.get("enabled"), bool) else None
    out["agent"] = agent
    out["ok"] = all(out[k] is not None for k in ("security", "ask", "mode", "elevated", "approvals_ask",
                                                  "approvals_fallback"))
    return out


def exec_policy_problems(agent: str, eff: dict) -> list[str]:
    """Why an agent's effective exec policy is not the one this project needs (6.1): security allowlist (qc
    deny), ask off, elevated off, host approvals ask off and askFallback deny. [] when it passes."""
    want = "deny" if agent == QC_AGENT else "allowlist"
    out = []
    for name, ok in (("security", eff.get("security") == want), ("ask", eff.get("ask") == "off"),
                     ("elevated", eff.get("elevated") is False), ("approvals ask", eff.get("approvals_ask") == "off"),
                     ("approvals askFallback", eff.get("approvals_fallback") == "deny")):
        if not ok:
            out.append(name)
    return out


def unconfined_reason(eff) -> str | None:
    """Why an agent's shell is unconfined (4.4), or None when it is confined. A per-agent `mode` replaces the
    security and ask the agent would otherwise get, so it is judged first: mode deny is confined, mode full, auto
    or ask is unconfined, mode allowlist is confined only with ask off. Without a mode: security deny is
    confined, security full is not, security allowlist needs ask off. Unknown values count as unconfined (fail
    closed)."""
    eff = eff if isinstance(eff, dict) else {}
    sec, mode, ask = eff.get("security"), eff.get("mode"), eff.get("ask")
    if mode is not None:
        if mode == "deny":
            return None
        if mode == "allowlist":
            return None if ask == "off" else "mode allowlist with ask %s" % (ask or "unknown")
        return "mode %s" % mode
    if sec == "deny":
        return None
    if sec == "full":
        return "security full"
    if sec == "allowlist" and ask == "off":
        return None
    if sec is None:
        return "exec policy could not be read"
    if sec != "allowlist":
        return "security %s" % sec
    return "ask %s" % (ask or "unknown")


def unconfined_agents(policy_doc, agent_ids: list | None = None) -> list[dict]:
    """Non-jobhunter agents whose effective shell is unconfined (4.4, unconfined_reason). An agent without its own
    scope gets the global tools.exec scope; a value that cannot be read counts as unconfined.
    [{agent, security, ask, mode, why}]."""
    scopes = _exec_scopes(policy_doc)
    glob = next((s for s in scopes if s.get("configPath") == "tools.exec" or s.get("scopeLabel") == "tools.exec"),
                None)
    ids = set(a for a in (agent_ids or []) if isinstance(a, str))
    ids |= {s["agentId"] for s in scopes if isinstance(s.get("agentId"), str)}
    out = []
    for agent in sorted(ids):
        if agent.startswith("jobhunter-"):
            continue
        v = _scope_view(_scope_for(scopes, agent) or glob)
        why = unconfined_reason(v)
        if why:
            out.append({"agent": agent, "security": v["security"], "ask": v["ask"], "mode": v["mode"], "why": why})
    return out


def agents_list() -> list[str] | None:
    """Agent ids from `openclaw config get agents.entries --json` (read only); None when it cannot be read."""
    r = run(oc_argv("config", "get", "agents.entries", "--json"), 60)
    if not r["ok"]:
        return None
    doc = last_json(r["stdout"])
    if isinstance(doc, dict) and isinstance(doc.get("value"), (dict, list)) and len(doc) <= 3 and "id" not in doc:
        doc = doc["value"]
    if isinstance(doc, dict):
        return sorted(str(k) for k in doc)
    if isinstance(doc, list):
        return sorted(str(e["id"]) for e in doc if isinstance(e, dict) and e.get("id"))
    return None


def plugins_enabled() -> list[str] | None:
    """Names of enabled OpenClaw plugins from `openclaw plugins list --json` (read only); None when unreadable."""
    r = run(oc_argv("plugins", "list", "--json"), 60)
    if not r["ok"]:
        return None
    doc = last_json(r["stdout"])
    rows = doc.get("plugins") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return None
    out = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        name = p.get("id") or p.get("name")
        enabled = p.get("enabled")
        if enabled is None and isinstance(p.get("status"), str):
            enabled = p["status"].lower() in ("enabled", "loaded", "active")
        if isinstance(name, str) and enabled:
            out.append(name)
    return sorted(set(out))
