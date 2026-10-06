"""U2 test double: run one jh.py command as a jobhunter agent the way the guard does it (CLI-ROUTE design 5, 8).

    rc, envelope = agent_call("jobhunter-scout", ["--cycle", CYCLE, "searches", "due"], deps=self.deps)

The command runs in a child `python -I` (so `sys.flags.isolated` is 1, as for every guarded agent exec) against
the test home of the calling test and its frozen clock. The child installs the same fake `jobhunter.profile` and
`jobhunter.config` modules as `tests.fakes.u2.install` when `deps` is given, and gets a minimal environment, so
nothing of the caller's own environment (for example CLAUDECODE) reaches it.

Identity: every call carries both carriers for one session, minted by U1's `tests.helpers.agent_env` (env
proof) and `tests.helpers.agent_argv` (argv proof in front of the command). This differs from
`tests.helpers.run_jh` only in the fake profile and config modules the U2 tests need in the child. Everything is
fictional and stays inside the test home."""
from __future__ import annotations

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


def session_for(agent_id: str) -> str:
    return "agent:%s:test" % agent_id


def proofs(agent_id: str, argv: list, session: str | None = None) -> tuple[dict, list]:
    """(env, argv) for one agent call: a fresh env proof and a fresh argv proof for the same session."""
    from jobhunter import paths
    from tests.helpers import agent_argv, agent_env
    session = session or session_for(agent_id)
    root = paths.root()
    env = dict(agent_env(root, agent_id, session=session))
    full = [str(t) for t in agent_argv(root, agent_id, list(argv), session=session)]
    return env, full


def run_isolated(argv: list, env: dict, deps=None, stdin: str = "") -> tuple[int, object]:
    """Run cli.main(argv, env=env) in a child `python -I` against the current test home and test clock.
    Returns (exit code, the last JSON envelope, or the raw text when the output is not JSON)."""
    from jobhunter import canon, paths
    spec = {"root": paths.root(), "now": canon.now(), "argv": [str(t) for t in argv], "env": dict(env),
            "stdin": stdin, "fake_deps": deps is not None}
    if deps is not None:
        spec["profile"] = deps.profile
        spec["cfg"] = deps.cfg
    child_env = {k: str(v) for k, v in env.items()}
    child_env["PATH"] = CHILD_PATH
    cmd = [sys.executable, "-I"]
    if getattr(sys, "pycache_prefix", None):       # reuse the caller's bytecode cache (speed only)
        cmd += ["-X", "pycache_prefix=%s" % sys.pycache_prefix]
    proc = subprocess.run(cmd + [os.path.abspath(__file__)], input=json.dumps(spec),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env, cwd=paths.root(),
                          universal_newlines=True, timeout=120)
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not lines:
        raise AssertionError("agent child failed (exit %d): %s %s" % (proc.returncode, proc.stdout[-2000:],
                                                                    proc.stderr[-2000:]))
    res = json.loads(lines[-1])
    if res.get("error"):
        raise AssertionError("agent child raised: %s" % res["error"])
    if res.get("isolated") != 1:
        raise AssertionError("agent child did not run under python -I")
    text = (res.get("out") or "").strip()
    last = text.splitlines()[-1] if text else ""
    try:
        return res["rc"], json.loads(last)
    except ValueError:
        return res["rc"], text


def agent_call(agent_id: str, argv: list, deps=None, session: str | None = None, stdin: str = "") -> tuple[int, object]:
    """One jh.py call as `agent_id`, with every identity carrier the current core accepts (see module doc)."""
    env, full = proofs(agent_id, argv, session)
    return run_isolated(full, env, deps=deps, stdin=stdin)


# ---------------------------------------------------------------- child side
def _install_fakes(spec: dict) -> None:
    import copy
    import types
    from jobhunter.errors import Denied
    profile = spec.get("profile")
    cfg = spec.get("cfg")
    prof_mod = types.ModuleType("jobhunter.profile")

    def load_confirmed():
        if profile is False:
            raise Denied("E_PROFILE_UNCONFIRMED", "profile not confirmed (fake)")
        return copy.deepcopy(profile)

    def facts():
        if not profile:
            return {}
        return {k: v["text"] for k, v in (profile.get("facts") or {}).items()}

    prof_mod.load_confirmed = load_confirmed
    prof_mod.facts = facts
    cfg_mod = types.ModuleType("jobhunter.config")
    cfg_mod.load = lambda: copy.deepcopy(cfg)
    sys.modules["jobhunter.profile"] = prof_mod
    sys.modules["jobhunter.config"] = cfg_mod


def _child() -> int:
    real_out = sys.stdout
    try:
        spec = json.loads(sys.stdin.read())
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        from jobhunter import canon, paths
        paths.use_test_home(spec["root"])
        if spec.get("now"):
            canon.set_test_clock(spec["now"])
        if spec.get("fake_deps"):
            _install_fakes(spec)
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
