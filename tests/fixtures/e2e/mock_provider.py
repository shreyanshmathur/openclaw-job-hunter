"""A scripted OpenAI-compatible model provider on 127.0.0.1 (INT, CLI-ROUTE-DESIGN 11.3 and 13 T12). Not a test.

It stands in for the model of the API-key (embedded) route, so the route can be tested with no API key and no
network: every chat-completion request is answered with the next turn of a script (tool calls, then a final
word), and every request is recorded (model, messages, tool names; never the Authorization header).

Unit mode (tests/test_e2e_api_route_mock.py): MockProvider(script, vars_).start() serves on a random port; the
test plays the embedded runtime's loop against it and reads `requests`.

Live mode (T12, on the jhtest profile only, after the operator configured a jhtest-only custom provider
`jhmock` with base URL http://127.0.0.1:<port>/v1 and the dummy key x):

    python3 tests/fixtures/e2e/mock_provider.py --script probe --var PY=<python> --var REPO=<repo> \\
        --var WS_ROOT=<ws root> [--port 0] [--log <requests.jsonl>]

prints `listening 127.0.0.1:<port>` and serves until interrupted. It binds to 127.0.0.1 only.

The turn to answer is the number of assistant messages already in the request, so the server keeps no state
between requests and a retried request gets the same answer. Script placeholders: {PY}, {REPO}, {WS_ROOT} and
{ROLE} (filled from vars). Fictional data only.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "scripted"
FORGED_PROOF = "jhp2.jobhunter-evaluator.1790000000.0123456789abcdef.0123456789abcdef." + "a" * 64

# The probe of CLI-ROUTE-DESIGN 6.3 plus the T3a, T4a and T5b refusals (a command that is not jh.py, shell
# metacharacters, a command outside the ACL, reads outside the workspace), as one scripted evaluator run.
SCRIPTS = {
    "probe": {
        "role": "evaluator",
        "turns": [
            {"tool_calls": [{"name": "exec", "arguments": {"command": "{PY} {REPO}/scripts/jh.py whoami",
                                                           "timeoutSeconds": 90}}]},
            {"tool_calls": [{"name": "read", "arguments": {"path": "{WS_ROOT}/{ROLE}/ref/profile_inference.md"}},
                            {"name": "write", "arguments": {"path": "{WS_ROOT}/{ROLE}/work/probe/ok.txt",
                                                            "content": "OK"}}]},
            {"tool_calls": [{"name": "read", "arguments": {"path": "{WS_ROOT}/{ROLE}/work/probe/ok.txt"}}]},
            {"tool_calls": [{"name": "exec", "arguments": {
                "command": "{PY} -I {REPO}/scripts/jh.py --agent-proof " + FORGED_PROOF + " whoami",
                "timeoutSeconds": 90}}]},
            {"tool_calls": [{"name": "read", "arguments": {"path": "~/.ssh/id_ed25519"}},
                            {"name": "read", "arguments": {"path": "/etc/hosts"}},
                            {"name": "exec", "arguments": {"command": "/bin/ls /", "timeoutSeconds": 90}},
                            {"name": "exec", "arguments": {"command": "{PY} {REPO}/scripts/jh.py whoami && /usr/bin/id",
                                                           "timeoutSeconds": 90}},
                            {"name": "exec", "arguments": {"command": "{PY} {REPO}/scripts/jh.py dispatch tick",
                                                           "timeoutSeconds": 90}}]},
            {"content": "PROBE_DONE"},
        ],
    },
}


def fill(v, vars_: dict):
    if isinstance(v, str):
        for k, val in vars_.items():
            v = v.replace("{%s}" % k, str(val))
        return v
    if isinstance(v, list):
        return [fill(x, vars_) for x in v]
    if isinstance(v, dict):
        return {k: fill(x, vars_) for k, x in v.items()}
    return v


def load_script(script, vars_: dict) -> dict:
    """A script by name (SCRIPTS) or as a dict, with its placeholders filled ({ROLE} from the script)."""
    sc = SCRIPTS[script] if isinstance(script, str) else script
    v = dict(vars_)
    v.setdefault("ROLE", sc.get("role", ""))
    return fill(sc, v)


def answer(script: dict, request: dict) -> dict:
    """The assistant message and finish reason for one request: turn n = assistant messages already sent."""
    msgs = request.get("messages") if isinstance(request.get("messages"), list) else []
    n = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "assistant")
    turns = script["turns"]
    turn = turns[n] if n < len(turns) else {"content": "DONE"}
    if turn.get("tool_calls"):
        calls = [{"id": "call_%d_%d" % (n, i), "type": "function",
                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], sort_keys=True)}}
                 for i, c in enumerate(turn["tool_calls"])]
        return {"message": {"role": "assistant", "content": None, "tool_calls": calls}, "finish": "tool_calls"}
    return {"message": {"role": "assistant", "content": turn.get("content", "")}, "finish": "stop"}


class MockProvider:
    """The HTTP server. requests: one record per chat-completion request, in arrival order."""

    def __init__(self, script="probe", vars_: dict | None = None, port: int = 0, log_path: str | None = None):
        self.script = load_script(script, vars_ or {})
        self.requests: list = []
        self.log_path = log_path
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.httpd.server_address[1]
        self.thread = None

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.port

    def start(self) -> "MockProvider":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def record(self, rec: dict) -> None:
        with self.lock:
            self.requests.append(rec)
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def _handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):        # quiet
                pass

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") in ("/v1/models", "/models"):
                    return self._json(200, {"object": "list", "data": [
                        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "jhmock"}]})
                return self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                    return self._json(404, {"error": {"message": "not found"}})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._json(400, {"error": {"message": "bad JSON"}})
                tools = [((t.get("function") or {}).get("name") or t.get("name")) for t in req.get("tools") or []
                         if isinstance(t, dict)]
                provider.record({"path": self.path, "model": req.get("model"), "stream": bool(req.get("stream")),
                                 "messages": req.get("messages"), "tools": tools,
                                 "auth_header": bool(self.headers.get("Authorization"))})
                out = answer(provider.script, req)
                cid, created = "chatcmpl-mock-%d" % len(provider.requests), int(time.time())
                if req.get("stream"):
                    return self._stream(cid, created, req.get("model") or MODEL_ID, out)
                return self._json(200, {"id": cid, "object": "chat.completion", "created": created,
                                        "model": req.get("model") or MODEL_ID,
                                        "choices": [{"index": 0, "message": out["message"],
                                                     "finish_reason": out["finish"]}],
                                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

            def _stream(self, cid: str, created: int, model: str, out: dict) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                def chunk(delta: dict, finish=None) -> None:
                    obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(("data: %s\n\n" % json.dumps(obj)).encode("utf-8"))

                msg = out["message"]
                chunk({"role": "assistant", "content": ""})
                for i, c in enumerate(msg.get("tool_calls") or []):
                    chunk({"tool_calls": [dict(c, index=i)]})
                if msg.get("content"):
                    chunk({"content": msg["content"]})
                chunk({}, out["finish"])
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="scripted OpenAI-compatible provider on 127.0.0.1 (jhtest only)")
    ap.add_argument("--script", default="probe", choices=sorted(SCRIPTS))
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log", metavar="<requests.jsonl>")
    a = ap.parse_args(argv)
    vars_ = {}
    for item in a.var:
        name, sep, value = item.partition("=")
        if not sep or not name:
            ap.error("--var needs NAME=VALUE")
        vars_[name] = value
    mp = MockProvider(a.script, vars_, a.port, a.log)
    print("listening 127.0.0.1:%d" % mp.port, flush=True)
    try:
        mp.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mp.httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
