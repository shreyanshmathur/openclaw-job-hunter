// execFile wrapper for jh.py (design 1.4): no shell, fixed interpreter and script, bounded time and
// output. The child never inherits an agent proof or a harness marker (the scrub list of the CLI route
// design 5.5, the same as jobhunter.auth.scrub_agent_env), so jh.py classifies the call as the `system`
// caller (or `chat` when a --grant is passed).

import { execFile } from "node:child_process";

export type JhResult = {
  exitCode: number; // -1 when the process could not run or timed out
  stdout: string;
  stderr: string;
  envelope: Record<string, unknown> | null; // the last JSON object printed on stdout, if any
  error: string | null;
};

export type JhRunner = (argv: string[], opts?: { timeoutMs?: number }) => Promise<JhResult>;

// Names dropped from the child env: exact names, then prefixes.
export const SCRUB_NAMES = ["OPENCLAW_SHELL", "OPENCLAW_CHANNEL_CONTEXT", "CLAUDECODE", "JOBHUNTER_HOME", "JOBHUNTER_DB"];
export const SCRUB_PREFIXES = ["OPENCLAW_MCP_", "CLAUDE_CODE_", "JH_"];

export function scrubbed(name: string): boolean {
  return SCRUB_NAMES.includes(name) || SCRUB_PREFIXES.some((p) => name.startsWith(p));
}

export function childEnv(base: NodeJS.ProcessEnv): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(base)) {
    if (v === undefined || scrubbed(k)) continue;
    env[k] = v;
  }
  env.PYTHONIOENCODING = "utf-8";
  env.PYTHONDONTWRITEBYTECODE = "1";
  return env;
}

// Last line of stdout that parses as a JSON object.
export function lastJsonObject(stdout: string): Record<string, unknown> | null {
  const lines = stdout.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.startsWith("{") && l.endsWith("}"));
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const v = JSON.parse(lines[i]);
      if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
    } catch {
      // keep looking
    }
  }
  const t = stdout.trim();
  if (t.startsWith("{")) {
    try {
      const v = JSON.parse(t);
      if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
    } catch {
      return null;
    }
  }
  return null;
}

export function makeJhRunner(python: string, jhScript: string, cwd: string): JhRunner {
  return (argv: string[], opts?: { timeoutMs?: number }) =>
    new Promise<JhResult>((resolve) => {
      execFile(
        python,
        [jhScript, ...argv],
        { cwd, env: childEnv(process.env), timeout: opts?.timeoutMs ?? 55000, maxBuffer: 2 * 1024 * 1024, windowsHide: true, shell: false },
        (err, stdout, stderr) => {
          const out = String(stdout ?? "");
          const errText = String(stderr ?? "");
          let exitCode = 0;
          let error: string | null = null;
          if (err) {
            const e = err as NodeJS.ErrnoException & { code?: number | string; killed?: boolean };
            if (typeof e.code === "number") exitCode = e.code;
            else {
              exitCode = -1;
              error = e.killed ? "timeout" : String(e.code ?? e.message);
            }
          }
          resolve({ exitCode, stdout: out, stderr: errText, envelope: lastJsonObject(out), error });
        },
      );
    });
}
