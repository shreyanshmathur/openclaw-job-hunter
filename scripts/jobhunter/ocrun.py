"""OpenClaw subprocess helper (design 1.2, 1.5, 5.3). Every call runs the binary and profile recorded in
private/home.json, with an argv list (never a shell), stdin closed and a timeout. Output shapes are
[verify] items (13.1 #8, #17): JSON is parsed defensively (the last JSON object in the text), and any
non-zero exit or unparsable output is a failure.
"""
from __future__ import annotations

import json
import os
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


def run(argv: list[str], timeout_s: int) -> dict:
    """{ok, returncode, stdout, stderr, error}; ok means exit status 0."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("JH_")}
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout_s, env=env, cwd=paths.REPO)
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": "openclaw binary not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": "timeout after %d s" % timeout_s}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "", "error": str(exc)}
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": out[-20000:], "stderr": err[-4000:],
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


def agent_turn(agent: str, session_key: str, message_file: str, timeout_s: int) -> dict:
    """One agent turn: openclaw agent --agent <a> --session-key <k> --message-file <f> --json --timeout <s>.
    {ok, text, raw}. A reviewer error is never a verdict (the caller decides)."""
    argv = oc_argv("agent", "--agent", agent, "--session-key", session_key, "--message-file", message_file, "--json",
                   "--timeout", str(int(timeout_s)))
    r = run(argv, int(timeout_s) + 30)
    if not r["ok"]:
        return {"ok": False, "text": None, "raw": (r["stdout"] or r["error"] or "")[-4000:], "error": r["error"]}
    obj = last_json(r["stdout"])
    text = _text_of(obj) if obj is not None else None
    if text is None:
        text = r["stdout"]
    return {"ok": True, "text": text, "raw": r["stdout"][-20000:]}


# The options of `openclaw message send` in the captured help of OpenClaw 2026.9.5 (13.1 #8). It has no
# --message-file (only `openclaw agent` does), so the text is read here and passed as the value of --message.
MESSAGE_SEND_FLAGS = frozenset(("--account", "--channel", "--delivery", "--dry-run", "--force-document",
                                "--gif-playback", "--help", "--json", "--message", "--media", "--pin",
                                "--presentation", "--reply-to", "--silent", "--target", "--thread-id", "--verbose",
                                "-h", "-m", "-t"))
MESSAGE_MAX_CHARS = 60000


def message_send_argv(channel: str, target: str, text: str) -> list[str]:
    """argv of one `openclaw message send`. The body goes in the --message=<text> form, so a body that starts
    with a dash is never read as an option."""
    return oc_argv("message", "send", "--channel", channel, "--target", target, "--message=" + text, "--json")


def message_send(channel: str, target: str, message_file: str) -> dict:
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
    r = run(message_send_argv(channel, target, text), 60)
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
