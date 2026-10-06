"""U6 test support: run jh.py commands as a jobhunter agent the way the guard runs them (CLI-ROUTE design 5, 8).

    rc, envelope = agent_cli("jobhunter-outreach", ["reply", "record", "--file", path])
    rc, envelope = agent_cli_isolated("jobhunter-applier", ["apply", "next"])

Identity. Every call carries both carriers of proof version 2 for one session, minted fresh for the call (each
is single use): the env proof (JH_AGENT_PROOF with OPENCLAW_SHELL, JH_AGENT_ID and JH_SESSION_KEY) and the argv
proof put in front of the command (`--agent-proof <T> <argv...>`). They come from `tests.helpers.agent_env` and
`tests.helpers.agent_argv` (U1); while those are missing, from `jobhunter.auth.env_proof`/`argv_proof` with the
test home's guard key; with neither (a core before proof version 2), the legacy agent env is used.

Interpreter. The guard always runs jh.py as `<PY> -I`, and jh.py refuses an agent proof in any other
interpreter. `agent_cli_isolated` runs the call in a real child `python -I` against the test home, clock and the
U6 fakes of the calling test (the fake config is copied over). `agent_cli` runs `cli.main` in this process, so
a test's `mock.patch` and the fake modules' recorded state apply; while the call runs it stands in for `-I` with
the core's test hook `auth.set_test_isolated(True)` (honoured only in a test home), or, on a core without it,
with `sys.flags` reporting the flags `-I` sets. jh.py's check itself is unchanged, and the child path proves it
with a real `python -I`.

Everything is fictional and stays inside the test home; nothing reaches the network."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
SCRIPTS = os.path.join(REPO, "scripts")
JH = os.path.join(SCRIPTS, "jh.py")
CHILD_PATH = "/usr/bin:/bin"

if __name__ != "__main__":
    # Load the CLI (and the modules it imports) once, before any test patches sys.modules: U6TestCase drops the
    # modules first imported inside a test, and a re-imported jobhunter module would not match the CLI's classes.
    import tests  # noqa: F401  (puts scripts/ on sys.path)
    from jobhunter import cli as _cli  # noqa: F401


def v2_helpers():
    """(agent_env, agent_argv) from tests.helpers when U1 has landed them, else None."""
    from tests import helpers
    mk_env = getattr(helpers, "agent_env", None)
    mk_argv = getattr(helpers, "agent_argv", None)
    if callable(mk_env) and callable(mk_argv):
        return mk_env, mk_argv
    return None


def session_for(agent_id: str) -> str:
    return "agent:%s:test" % agent_id


def legacy_env(agent_id: str) -> dict:
    """The agent env before proof version 2 (no proof at all)."""
    return {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": agent_id}


def core_proofs():
    """(env_proof, argv_proof) of jobhunter.auth when the core speaks proof version 2, else None."""
    from jobhunter import auth
    env_p, argv_p = getattr(auth, "env_proof", None), getattr(auth, "argv_proof", None)
    if callable(env_p) and callable(argv_p):
        return env_p, argv_p
    return None


def proofs_available() -> bool:
    return bool(v2_helpers() or core_proofs())


def identity(agent_id: str, argv: list, session: str | None = None) -> tuple[dict, list, str]:
    """(env, argv, mode) for one agent call; mode is "v2" (both carriers, fresh proofs) or "legacy"."""
    from jobhunter import paths
    session = session or session_for(agent_id)
    argv = [str(t) for t in argv]
    helpers = v2_helpers()
    if helpers:
        mk_env, mk_argv = helpers
        root = paths.root()
        env = dict(mk_env(root, agent_id, session=session))
        full = [str(t) for t in mk_argv(root, agent_id, argv, session=session)]
        return env, full, "v2"
    core = core_proofs()
    if core:
        from jobhunter import auth
        env_p, argv_p = core
        auth.create_guard_key()          # the test home's own key (no-op when it exists)
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": agent_id, "JH_SESSION_KEY": session,
               "JH_AGENT_PROOF": env_p(agent_id, session_key=session)}
        return env, ["--agent-proof", argv_p(agent_id, argv, session_key=session)] + argv, "v2"
    return legacy_env(agent_id), argv, "legacy"


class _IsolatedFlags:
    """sys.flags as `python -I` sets them; every other flag is the real one."""
    isolated = 1
    ignore_environment = 1
    no_user_site = 1

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def as_isolated(argv: list | None = None):
    """While active, jh.py treats this process as a `python -I` run and sys.argv is `jh.py <argv>`, as in the
    process the guard starts (in-process agent calls in a test home only)."""
    from jobhunter import auth
    hook = getattr(auth, "set_test_isolated", None)
    real_flags, real_argv = sys.flags, sys.argv
    if callable(hook):
        hook(True)
    else:
        sys.flags = _IsolatedFlags(real_flags)
    if argv is not None:
        sys.argv = [JH] + [str(t) for t in argv]
    try:
        yield
    finally:
        if callable(hook):
            hook(None)
        sys.flags, sys.argv = real_flags, real_argv


def _envelope(rc: int, text: str):
    text = (text or "").strip()
    last = text.splitlines()[-1] if text else ""
    try:
        return rc, json.loads(last)
    except ValueError:
        return rc, text


def run_in_process(argv: list, env: dict, stdin: str = "", modules=None, isolated: bool = True):
    """cli.main(argv, env=env) in this process; with isolated=True under the flags of `python -I`."""
    out = io.StringIO()
    kwargs = {"env": dict(env), "stdin": io.StringIO(stdin), "stdout": out}
    if modules is not None:
        kwargs["modules"] = modules
    ctx = as_isolated(argv) if isolated else contextlib.nullcontext()
    with ctx:
        rc = _cli.main([str(t) for t in argv], **kwargs)
    return _envelope(rc, out.getvalue())


def agent_cli(agent_id: str, argv: list, stdin: str = "", session: str | None = None, modules=None):
    """One jh.py call as `agent_id` in this process, with every identity carrier the core accepts."""
    env, full, _mode = identity(agent_id, argv, session)
    return run_in_process(full, env, stdin=stdin, modules=modules)


# ---------------------------------------------------------------- real `python -I` child
def run_isolated(argv: list, env: dict, stdin: str = "") -> tuple[int, object]:
    """Run cli.main(argv, env=env) in a child `python -I` against the current test home, test clock and the U6
    fakes (with the calling test's fake config). Returns (exit code, the last JSON envelope or the raw text)."""
    from jobhunter import canon, paths
    from tests.fakes.u6 import deps
    spec = {"root": paths.root(), "now": canon.now() if paths.is_test_home() else None,
            "argv": [str(t) for t in argv], "env": {k: str(v) for k, v in env.items()}, "stdin": stdin,
            "config": copy.deepcopy(deps.CONFIG)}
    child_env = dict(spec["env"])
    child_env["PATH"] = CHILD_PATH
    cmd = [sys.executable, "-I"]
    if getattr(sys, "pycache_prefix", None):      # reuse the caller's bytecode cache (speed only)
        cmd += ["-X", "pycache_prefix=%s" % sys.pycache_prefix]
    proc = subprocess.run(cmd + [os.path.abspath(__file__)], input=json.dumps(spec), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=child_env, cwd=paths.root(), universal_newlines=True,
                          timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not lines:
        raise AssertionError("agent child failed (exit %d): %s %s" % (proc.returncode, proc.stdout[-2000:],
                                                                    proc.stderr[-2000:]))
    res = json.loads(lines[-1])
    if res.get("error"):
        raise AssertionError("agent child raised: %s" % res["error"])
    if res.get("isolated") != 1:
        raise AssertionError("agent child did not run under python -I")
    return _envelope(res["rc"], res.get("out") or "")


def agent_cli_isolated(agent_id: str, argv: list, stdin: str = "", session: str | None = None):
    """One jh.py call as `agent_id` in a real child `python -I` (see run_isolated)."""
    env, full, _mode = identity(agent_id, argv, session)
    return run_isolated(full, env, stdin=stdin)


def _child() -> int:
    real_out = sys.stdout
    try:
        spec = json.loads(sys.stdin.read())
        for p in (SCRIPTS, REPO):
            if p not in sys.path:
                sys.path.insert(0, p)
        from jobhunter import canon, paths
        paths.use_test_home(spec["root"])
        if spec.get("now"):
            canon.set_test_clock(spec["now"])
        from tests.fakes.u6 import deps
        deps.reset()
        deps.CONFIG.clear()
        deps.CONFIG.update(spec.get("config") or {})
        sys.modules.update(deps.MODULES)
        from jobhunter import cli
        argv = list(spec["argv"])
        sys.argv = [JH] + argv
        out = io.StringIO()
        rc = cli.main(argv, env=dict(spec["env"]), stdin=io.StringIO(spec.get("stdin") or ""), stdout=out)
        res = {"rc": rc, "out": out.getvalue(), "isolated": sys.flags.isolated}
    except BaseException:
        res = {"error": traceback.format_exc()[-4000:], "isolated": sys.flags.isolated}
    real_out.write("\n" + json.dumps(res) + "\n")
    real_out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_child())
