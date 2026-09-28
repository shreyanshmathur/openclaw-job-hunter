// jobhunter-guard: OpenClaw plugin entry (design 1.4). Registrations only; the rules live in
// src/policy.ts (pure) and the state in src/runtime.ts.
//
// The entry is a plain object in the shape definePluginEntry() returns, so the plugin has no runtime
// dependency on the openclaw package (the tests import it directly with node --test).

import { GuardRuntime, parseConfig, type Logger } from "./src/runtime.ts";

export const PLUGIN_ID = "jobhunter-guard";
const HEARTBEAT_MS = 5 * 60 * 1000;

type Api = {
  id?: string;
  registrationMode?: string;
  pluginConfig?: Record<string, unknown>;
  logger?: Logger;
  registerTrustedToolPolicy?: (policy: {
    id: string;
    description: string;
    evaluate: (event: any, ctx: any) => unknown;
  }) => void;
  on: (hook: string, handler: (event: any, ctx: any) => unknown, opts?: Record<string, unknown>) => void;
  registerCommand: (command: Record<string, unknown>) => void;
  registerService?: (service: { id: string; start: (ctx: any) => void | Promise<void>; stop?: (ctx: any) => void | Promise<void> }) => void;
};

const jsonSchema = {
  type: "object",
  additionalProperties: false,
  required: ["repo", "python", "homeFile", "publicReadonlyAgents"],
  properties: {
    repo: { type: "string" },
    python: { type: "string" },
    homeFile: { type: "string" },
    publicReadonlyAgents: { type: "array", items: { type: "string" } },
    ownerFallback: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["channel", "senderId"],
        properties: { channel: { type: "string" }, senderId: { type: "string" } },
      },
    },
  },
};

export function register(api: Api): GuardRuntime | null {
  const mode = api.registrationMode || "full";
  if (mode === "cli-metadata" || mode === "setup-only") return null;
  const logger: Logger = api.logger || { info: () => {}, warn: () => {}, error: () => {} };
  let runtime: GuardRuntime | null = null;
  let configError = "";
  try {
    runtime = new GuardRuntime(parseConfig(api.pluginConfig), { logger });
  } catch (e) {
    configError = (e as Error).message || String(e);
    logger.error("jobhunter-guard: invalid plugin config: " + configError);
  }

  // Without a valid config every jobhunter-* tool call is blocked (fail closed).
  const evaluate = (event: any, ctx: any) => {
    if (runtime) return runtime.evaluate(event, ctx || {});
    const agent = String((ctx && ctx.agentId) || "");
    const key = String((ctx && ctx.sessionKey) || "");
    if (agent.startsWith("jobhunter-") || key.startsWith("agent:jobhunter-")) {
      return { block: true, blockReason: "G_GUARD_UNHEALTHY: the jobhunter-guard plugin config is invalid (" + configError + ")" };
    }
    return undefined;
  };

  // Trusted tier first. The host only reports (does not throw) a refused trusted registration, for
  // example when the plugin was not explicitly enabled, so the same policy is also registered as an
  // ordinary before_tool_call hook with the highest priority. The hook skips every call the trusted tier
  // already decided (runtime.markTrusted / consumeTrusted), so a call is never decided twice.
  if (typeof api.registerTrustedToolPolicy === "function") {
    try {
      api.registerTrustedToolPolicy({
        id: PLUGIN_ID,
        description: "Fences the jobhunter-* agents: exec ACL, file confinement, browser token gate, host classes.",
        evaluate: (event: any, ctx: any) => {
          if (runtime) runtime.markTrusted(event, ctx || {});
          return evaluate(event, ctx);
        },
      });
    } catch (e) {
      logger.warn("jobhunter-guard: trusted tool policy rejected (" + ((e as Error).message || e) + "); the before_tool_call hook decides alone");
    }
  }
  api.on(
    "before_tool_call",
    (event: any, ctx: any) => {
      if (runtime && runtime.consumeTrusted(event, ctx || {})) return undefined;
      return evaluate(event, ctx);
    },
    { priority: 100000 },
  );

  api.on("after_tool_call", (event: any, ctx: any) => {
    if (!runtime) return;
    try {
      runtime.observe(event, ctx || {});
    } catch (e) {
      logger.error("jobhunter-guard: observe failed: " + ((e as Error).message || e));
    }
  });

  api.on("resolve_exec_env", (_event: any, ctx: any) => (runtime ? runtime.execEnv(ctx || {}) : undefined));

  // Owner only. With no ownerFallback the host must report the sender as the owner (requiredScopes on
  // a chat surface is satisfied only by an owner, and it makes the host expose senderIsOwner). With an
  // ownerFallback list the handler checks the list itself.
  const hasFallback = !!(runtime && runtime.config.ownerFallback && runtime.config.ownerFallback.length);
  api.registerCommand({
    name: "jh",
    description: "Job hunter: approve, skip, edit, answer, pause, status, inbox, lower",
    acceptsArgs: true,
    requireAuth: true,
    ...(hasFallback ? {} : { requiredScopes: ["operator.admin"] }),
    handler: async (ctx: any) => {
      if (!runtime) return { text: "G_GUARD_UNHEALTHY: the jobhunter-guard plugin config is invalid." };
      try {
        return await runtime.command(ctx || {});
      } catch (e) {
        logger.error("jobhunter-guard: /jh failed: " + ((e as Error).message || e));
        return { text: "The command failed inside the guard; see the gateway log." };
      }
    },
  });

  if (mode === "full" && typeof api.registerService === "function" && runtime) {
    let timer: ReturnType<typeof setInterval> | null = null;
    const rt = runtime;
    api.registerService({
      id: "jobhunter-guard-heartbeat",
      start: () => {
        rt.heartbeat();
        timer = setInterval(() => {
          try {
            rt.heartbeat();
          } catch (e) {
            logger.error("jobhunter-guard: heartbeat failed: " + ((e as Error).message || e));
          }
        }, HEARTBEAT_MS);
        if (timer && typeof timer.unref === "function") timer.unref();
      },
      stop: () => {
        if (timer) clearInterval(timer);
        timer = null;
        rt.close();
      },
    });
  }
  return runtime;
}

const entry = {
  id: PLUGIN_ID,
  name: "Job hunter guard",
  description: "Trusted tool policy, result observer, exec environment, heartbeat and /jh command for openclaw-job-hunter.",
  configSchema: {
    jsonSchema,
    safeParse(value: unknown) {
      try {
        parseConfig(value);
        return { success: true, data: value };
      } catch (e) {
        return { success: false, error: { issues: [{ path: [], message: (e as Error).message }] } };
      }
    },
  },
  register(api: Api): void {
    register(api);
  },
};

export default entry;
