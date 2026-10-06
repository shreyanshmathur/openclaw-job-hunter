#!/bin/bash
# openclaw-job-hunter installer (design 10.3). Safe to re-run: every step prints DONE or SKIP.
#
# Usage: ./install.sh [options]
#   --api-key                 use an Anthropic API key instead of your Claude subscription (Claude CLI login)
#   --claude-login            use your Claude subscription (the default; switches an API key install back)
#   --profile <name>          use a separate OpenClaw profile (recorded; one clone is one install)
#   --openclaw-bin <path>     use this openclaw executable (absolute path; recorded; one clone is one install)
#   --openclaw-version <v>    OpenClaw version to install when it is missing
#   --chat-control            add shared-skills (the /jh help skill) to your OpenClaw skill folders
#   --upload-root <dir>       OpenClaw's browser upload folder, when it is not /tmp/openclaw/uploads
#   --stay-awake              macOS: keep the Mac awake on AC power (otherwise asked at a terminal)
#   --identity-carrier <c>    how agents prove who they are to jh.py: argv+env (default), argv or env
#   --no-daemon               never install or start a Gateway service; use the Gateway that is running
#   --smoke                   run the agent identity checks even when they passed for these versions
#   --no-smoke                skip the agent identity checks (./jobhunter doctor --probe runs them later)
#   --cli-tools native --i-accept-reduced-protection
#                             reduced protection: agents use Claude Code's own tools (not recommended; argv carrier)
#   --skill-workshop-propose  agree to set skills.workshop.autonomous.mode to propose (asked at a terminal);
#                             OpenClaw's weekly skill reviews must not run the jobhunter agents
#   --yes                     never ask; take the safe default for every optional step
#   --help                    show this help
#
# Nothing here sends mail, applies to jobs or touches LinkedIn. All automations are created disabled;
# ./jobhunter resume turns them on after ./jobhunter init and ./jobhunter doctor. A running dispatcher is
# paused while the installer works and started again only when every step passed.
set -euo pipefail
# A person may run this from a Claude Code terminal. Claude Code's markers would make jh.py treat every call
# as an unproven agent (jobhunter.auth.harness_markers), so this entry point drops them for its children.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT

REPO="$(cd "$(dirname "$0")" && pwd)"
JH="$REPO/scripts/jh.py"
ORIG_PATH="$PATH"   # the person's own PATH, to print openclaw commands they can type as shown
export PATH="$HOME/.openclaw/bin:$HOME/.openclaw/tools/node/bin:$HOME/.local/bin:$PATH"
TAB="$(printf '\t')"

say()   { printf '%s\n' "$*"; }
die()   { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# json_message <jh.py JSON reply>: its one-line message, else the text unchanged
json_message() {
  printf '%s' "$1" | "${PY:-python3}" -c 'import json, sys
t = sys.stdin.read()
try:
    m = json.loads(t).get("message")
except Exception:
    m = None
sys.stdout.write(m if isinstance(m, str) and m else t.strip())' 2>/dev/null || printf '%s' "$1"
}
red()   { if [ -t 1 ]; then printf '\033[31m%s\033[0m\n' "$*"; else printf 'WARNING: %s\n' "$*"; fi; }
STEP_NO=""; STEP_NAME=""
step()  { STEP_NO="$1"; STEP_NAME="$2"; printf '\n[%s] %s\n' "$1" "$2"; }
done_() { printf 'DONE %s\n' "${1:-$STEP_NAME}"; }
skip()  { printf 'SKIP %s\n' "${1:-$STEP_NAME}"; }
is_tty() { [ -t 0 ] && [ -t 1 ]; }
ask() {
  if [ "$ASSUME_YES" = 1 ] || ! is_tty; then return 1; fi
  printf '%s [y/N] ' "$1"
  local ans=""
  IFS= read -r ans || return 1
  case "$ans" in y|Y|yes|YES|Yes) return 0 ;; esac
  return 1
}
usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }

ROUTE="cli"; ROUTE_SET=""; PROFILE=""; PROFILE_SET=0; OC_VERSION=""; CHAT_CONTROL=0; STAY_AWAKE=0; ASSUME_YES=0; SMOKE="auto"
UPLOAD_ROOT=""; CARRIER="argv+env"; CARRIER_SET=0; CLI_TOOLS="restricted"; ACCEPT_REDUCED=0; NO_DAEMON=0; OC_BIN_OPT=""
WORKSHOP_PROPOSE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --api-key) [ "$ROUTE_SET" != "cli" ] || die "choose one: --api-key or --claude-login"; ROUTE="api_key"; ROUTE_SET="api_key" ;;
    --claude-login) [ "$ROUTE_SET" != "api_key" ] || die "choose one: --api-key or --claude-login"; ROUTE="cli"; ROUTE_SET="cli" ;;
    --profile) [ $# -ge 2 ] || die "--profile needs a value"; PROFILE="$2"; PROFILE_SET=1; shift ;;
    --openclaw-version) [ $# -ge 2 ] || die "--openclaw-version needs a value"; OC_VERSION="$2"; shift ;;
    --openclaw-bin) [ $# -ge 2 ] || die "--openclaw-bin needs a value"; OC_BIN_OPT="$2"; shift ;;
    --skill-workshop-propose) WORKSHOP_PROPOSE=1 ;;
    --chat-control) CHAT_CONTROL=1 ;;
    --upload-root) [ $# -ge 2 ] || die "--upload-root needs a value"; UPLOAD_ROOT="$2"; shift ;;
    --stay-awake) STAY_AWAKE=1 ;;
    --identity-carrier) [ $# -ge 2 ] || die "--identity-carrier needs a value"; CARRIER="$2"; CARRIER_SET=1; shift ;;
    --cli-tools) [ $# -ge 2 ] || die "--cli-tools needs a value"; CLI_TOOLS="$2"; shift ;;
    --i-accept-reduced-protection) ACCEPT_REDUCED=1 ;;
    --no-daemon) NO_DAEMON=1 ;;
    --smoke) SMOKE=1 ;;
    --no-smoke) SMOKE=0 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see ./install.sh --help)" ;;
  esac
  shift
done
case "$PROFILE" in
  "") ;;
  *[!a-z0-9-]*|-*) die "--profile must use lowercase letters, digits and hyphens" ;;
esac
case "$CARRIER" in argv+env|argv|env) ;; *) die "--identity-carrier must be argv+env, argv or env" ;; esac
case "$OC_BIN_OPT" in
  "") ;;
  /*) { [ -f "$OC_BIN_OPT" ] && [ -x "$OC_BIN_OPT" ]; } || die "--openclaw-bin is not an executable file: $OC_BIN_OPT" ;;
  *) die "--openclaw-bin needs an absolute path" ;;
esac
case "$CLI_TOOLS" in
  restricted) [ "$ACCEPT_REDUCED" = "0" ] || die "--i-accept-reduced-protection goes with --cli-tools native" ;;
  native)
    [ "$ACCEPT_REDUCED" = "1" ] || die "--cli-tools native lowers the protection (Claude Code's own tools bypass the exec allowlist and workspaceOnly, and AskUserQuestion can wait for a person). Add --i-accept-reduced-protection to confirm"
    [ "$ROUTE_SET" != "api_key" ] || die "--cli-tools native is for the Claude subscription route only"
    # Claude Code's own Bash never gets the env proof (the guard's resolve_exec_env does not run for it), so in
    # this mode agents prove who they are with the argv carrier alone (jobhunter.install.check_cli_tools)
    if [ "$CARRIER_SET" = "0" ]; then
      CARRIER="argv"
      say "NOTE: --cli-tools native uses the argv identity carrier only (--identity-carrier argv)"
    fi
    [ "$CARRIER" = "argv" ] || die "--cli-tools native works only with --identity-carrier argv: Claude Code's own Bash never gets the env proof, so every agent call would be refused" ;;
  *) die "--cli-tools must be restricted or native" ;;
esac
CLI_TOOLS_ARGS=(--cli-tools "$CLI_TOOLS")
if [ "$CLI_TOOLS" = "native" ]; then CLI_TOOLS_ARGS+=(--i-accept-reduced-protection); fi

PY="$(command -v python3 2>/dev/null || true)"
OC="$(command -v openclaw 2>/dev/null || true)"
[ -n "$OC" ] || OC="$HOME/.openclaw/bin/openclaw"
if [ -n "$OC_BIN_OPT" ]; then OC="$OC_BIN_OPT"; fi
OC_ARGS=()
oc()  { "$OC" ${OC_ARGS[@]+"${OC_ARGS[@]}"} "$@"; }
jh()  { "$PY" "$JH" "$@"; }
jhh() { "$PY" "$JH" --human "$@"; }
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/jh-install.XXXXXX")"
DISPATCH_PAUSED=0
# Step 8 adds the agents and restricts them in one step. Until the read-back of their exec policy passed
# (CONFINE_PENDING=1), a stop leaves no jobhunter agent with OpenClaw's fallback policy (security full, no tool
# deny list): the agents this run added are deleted again and every jobhunter agent still present gets exec deny
# and every tool denied, until ./install.sh completes.
ADDED_AGENTS=""
CONFINE_PENDING=0
rollback_agents() {
  local id="" p="" left=""
  for id in $ADDED_AGENTS; do
    if oc agents delete "$id" --force --json >/dev/null 2>&1; then
      printf 'NOTE: removed %s again: the install stopped before it was restricted\n' "$id" >&2
    else
      printf 'ERROR: could not remove %s; remove it by hand: %s agents delete %s\n' "$id" "$(oc_show)" "$id" >&2
    fi
  done
  if oc agents list --json >"$TMPD/agents-left.json" 2>/dev/null \
      && p="$(jhh install render-confine-patch --present "$TMPD/agents-left.json" 2>/dev/null)"; then
    [ -n "$p" ] || return 0
    if oc config patch --file "$p" >/dev/null 2>&1; then
      printf 'NOTE: the jobhunter agents are set to exec deny with every tool denied until ./install.sh completes\n' >&2
      return 0
    fi
  fi
  left="$(jhh install agents 2>/dev/null | cut -f1 | tr '\n' ' ' || true)"
  printf 'ERROR: the jobhunter agents may be unrestricted. Run ./install.sh again, or remove them: %s agents delete <id> (%s)\n' \
    "$(oc_show)" "${left% }" >&2
}
on_exit() {
  local rc=$?
  if [ "$rc" != "0" ] && [ "$CONFINE_PENDING" = "1" ]; then
    CONFINE_PENDING=0
    rollback_agents || true
  fi
  rm -rf "$TMPD"
  if [ "$rc" != "0" ] && [ "$DISPATCH_PAUSED" = "1" ]; then
    printf 'ERROR: dispatch paused; re-run ./install.sh (the dispatcher starts again only after a complete install)\n' >&2
  fi
  if [ "$rc" != "0" ] && [ -n "$STEP_NO" ]; then
    printf '\nThe install stopped at step %s (%s).\n' "$STEP_NO" "$STEP_NAME" >&2
    printf 'What to do: fix what the lines above say, then run ./install.sh again (finished steps print SKIP).\n' >&2
    printf 'Each message and its fix: docs/TROUBLESHOOTING.md\n' >&2
  fi
}
trap on_exit EXIT

# oc_show: the openclaw command as the person types it in their own terminal: `openclaw` when their PATH finds
# this binary, else its full path; plus --profile for a profile install.
oc_bin_show() {
  local b="$OC" found=""
  found="$(PATH="$ORIG_PATH"; hash -r; command -v openclaw 2>/dev/null || true)"
  if [ -n "$found" ] && [ "$found" = "$OC" ]; then b="openclaw"
  else case "$b" in "$HOME"/*) b="~${b#"$HOME"}" ;; esac; fi
  printf '%s' "$b"
}
oc_show() {
  local b=""
  b="$(oc_bin_show)"
  if [ -n "$PROFILE" ]; then b="$b --profile $PROFILE"; fi
  printf '%s' "$b"
}

# Read private/home.json through jh.py (bash cannot parse JSON). Only KEY=value lines are evaluated;
# the values are shell-quoted by jobhunter.install.shell_env.
load_home_env() {
  JH_HOME_EXISTS=0; JH_OC_BIN=""; JH_OC_PROFILE=""; JH_PY=""; JH_WS_ROOT=""; JH_INSTALL_ID=""; JH_MODEL_ROUTE=""
  local txt="" line=""
  txt="$(jhh install shell-env 2>&1)" || die "cannot read private/home.json: $txt"
  while IFS= read -r line; do
    case "$line" in JH_[A-Z_]*=*) eval "$line" ;; esac
  done <<EOF
$txt
EOF
}

# Run each line of rendered openclaw arguments (one command per line, shell-quoted) through oc.
run_rendered() {
  local lines="$1" what="$2" line="" n=0
  while IFS= read -r line <&3; do
    [ -n "$line" ] || continue
    eval "set -- $line"
    [ "${1:-}" = "cron" ] || die "unexpected rendered command: $line"
    oc "$@" >/dev/null || die "openclaw $what failed for: $line"
    n=$((n + 1))
  done 3<<EOF
$lines
EOF
  RENDERED_COUNT=$n
}

manifest_set() {
  printf '%s\n' "$1" >"$TMPD/manifest.json"
  jh install manifest --set "$TMPD/manifest.json" >/dev/null || die "could not update state/install-manifest.json"
}

# store_cron_list: list the automations, record their ids and declared specs in the manifest (and home.json)
store_cron_list() {
  oc cron list --all --json >"$TMPD/cron.json" || die "openclaw cron list failed"
  CRON_STORE_OUT="$(jhh install manifest --set "$TMPD/cron.json" 2>&1)" || die "$CRON_STORE_OUT"
}

# foreign_check: no enabled automation outside the manifest may run a jobhunter agent (OpenClaw's weekly Skill
# Workshop reviews, a job someone added). OpenClaw applies a config change to its own review jobs a moment later,
# so an enabled review job is listed again up to three times before the install stops.
foreign_check() {
  local i=0 out=""
  while :; do
    if out="$(jhh install foreign-jobs --from-list "$TMPD/cron.json" --check 2>&1)"; then
      [ -z "$out" ] || say "$out"
      return 0
    fi
    i=$((i + 1))
    case "$out" in *"Skill Workshop review"*) [ "$i" -le 3 ] || die "$out" ;; *) die "$out" ;; esac
    sleep 5
    oc cron list --all --json >"$TMPD/cron.json" || die "openclaw cron list failed"
  done
}

# apply_agent_config: per-agent config (explicit exec policy), exec approvals (one argPattern per agent), then
# the read-back of the effective exec policy; stale exec keys are removed by verify-exec-policy --fix.
apply_agent_config() {
  local p="" out=""
  p="$(jhh install render-agents-patch --route "$ROUTE" 2>&1)" || die "$p"
  oc config patch --file "$p" --dry-run --json >"$TMPD/dry.json" 2>&1 \
    || { cat "$TMPD/dry.json" >&2; die "the agents config patch did not validate; nothing was changed"; }
  oc config patch --file "$p" >/dev/null || die "openclaw config patch failed"
  oc config validate >/dev/null || die "openclaw config validate failed after the patch; run: $(oc_show) config validate"
  oc approvals get --json >"$TMPD/approvals.json" 2>/dev/null \
    || die "openclaw approvals get failed; exec approvals are required (see docs/TROUBLESHOOTING.md)"
  p="$(jhh install render-approvals --current "$TMPD/approvals.json" 2>&1)" || die "$p"
  oc approvals set --file "$p" >/dev/null || die "openclaw approvals set failed"
  out="$(jhh install verify-exec-policy --fix 2>&1)" \
    || die "the jobhunter agents must run with exec allowlist and ask off, but OpenClaw reports otherwise: $out"
  say "$out"
}

# apply_guard_config: the guard plugin config patch (carriers, protected roots, QC verdict file switch)
apply_guard_config() {
  local p=""
  p="$(jhh install render-guard-config 2>&1)" || die "$p"
  oc config patch --file "$p" >/dev/null || die "writing the guard config failed"
}

# wait_for_guard: a fresh heartbeat with identity proof version 2. OpenClaw before 2026.9.7 loads plugin changes
# only after a Gateway restart; with --no-daemon the person restarts their own Gateway and we wait 5 minutes.
wait_for_guard() {
  local out="" wait=60
  if [ "$OLD_GATEWAY" = "1" ]; then
    if [ "$NO_DAEMON" = "1" ]; then
      say "OpenClaw before 2026.9.7 loads plugin changes only after a Gateway restart. Restart your Gateway now"
      say "(stop the running gateway run and start it again). Waiting up to 5 minutes for the guard."
      wait=300
    else
      oc gateway restart >/dev/null 2>&1 || die "openclaw gateway restart failed; run: $(oc_show) gateway restart"
      wait=120
    fi
  fi
  out="$(jhh install wait-guard --timeout "$wait" --proof-version 2 2>&1)" \
    || { say "$out"; die "the guard did not report in with identity proof version 2. Check: $(oc_show) logs (look for jobhunter-guard), then run ./install.sh again"; }
  say "$out"
}

# ------------------------------------------------------------------ 1
step 1 "Preflight"
[ "$(id -u)" != "0" ] || die "do not run the installer as root"
OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux"; if grep -qi microsoft /proc/version 2>/dev/null; then PLATFORM="wsl"; fi ;;
  *) die "unsupported system $OS (macOS, Linux and Windows WSL2 only)" ;;
esac
[ -n "$PY" ] || die "python3 is missing. macOS: xcode-select --install. Linux: sudo apt-get install -y python3"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is needed ($PY is older). macOS: xcode-select --install, then ./install.sh again"
command -v git >/dev/null 2>&1 || die "git is missing. macOS: xcode-select --install. Linux: sudo apt-get install -y git"
out="$(jhh install check-sqlite 2>&1)" || die "$out"
out="$(jhh install check-path 2>&1)" || die "$out"
load_home_env
if [ "$JH_HOME_EXISTS" = "1" ]; then
  if [ "$PROFILE_SET" = "1" ] && [ "$PROFILE" != "$JH_OC_PROFILE" ]; then
    die "this clone is installed for OpenClaw profile '${JH_OC_PROFILE:-default}'. One clone is one install; clone the repo again for a test install."
  fi
  PROFILE="$JH_OC_PROFILE"
  if [ -n "$OC_BIN_OPT" ] && [ -n "$JH_OC_BIN" ] && [ "$OC_BIN_OPT" != "$JH_OC_BIN" ]; then
    die "this clone is installed for the OpenClaw binary $JH_OC_BIN. One clone is one install; clone the repo again for a test install."
  fi
  # a re-run uses the binary the install recorded, like ./jobhunter and jh.py's own read-backs do
  if [ -z "$OC_BIN_OPT" ] && [ -n "$JH_OC_BIN" ] && [ -x "$JH_OC_BIN" ]; then OC="$JH_OC_BIN"; fi
fi
if [ -n "$PROFILE" ]; then OC_ARGS=(--profile "$PROFILE"); fi
# A re-run keeps the model route of the first install (./jobhunter update passes no options): --api-key or
# --claude-login switch it on purpose.
if [ -z "$ROUTE_SET" ] && [ "${JH_MODEL_ROUTE:-}" = "api_key" ]; then
  ROUTE="api_key"
  say "model route: Anthropic API key, as installed (./install.sh --claude-login switches to your Claude subscription)"
  [ "$CLI_TOOLS" = "restricted" ] || die "--cli-tools native is for the Claude subscription route only"
elif [ "$ROUTE_SET" = "cli" ] && [ "${JH_MODEL_ROUTE:-}" = "api_key" ]; then
  say "model route: switching from the API key to your Claude subscription (Claude CLI login)"
fi
say "system $PLATFORM, python $PY, OpenClaw profile ${PROFILE:-default}, model route $ROUTE"
if [ -n "$OC_BIN_OPT" ]; then say "openclaw binary $OC (--openclaw-bin)"; else say "openclaw binary $OC"; fi
done_

# ------------------------------------------------------------------ 0b
step 0b "Pause the dispatcher"
if [ "$JH_HOME_EXISTS" = "1" ] && "$OC" --version >/dev/null 2>&1 \
    && oc cron list --all --json >"$TMPD/cron-start.json" 2>/dev/null; then
  did="$(jhh install cron-id jobhunter:dispatch --from-list "$TMPD/cron-start.json" --if-enabled 2>/dev/null)" || did=""
  if [ -n "$did" ]; then
    oc cron disable "$did" >/dev/null || die "could not pause the dispatcher (automation $did)"
    DISPATCH_PAUSED=1
    done_ "dispatcher paused until the install is complete"
  else
    skip "the dispatcher is not running"
  fi
else
  skip "nothing to pause"
fi

# ------------------------------------------------------------------ 2
step 2 "OpenClaw"
if "$OC" --version >/dev/null 2>&1; then
  skip "OpenClaw is installed"
elif [ -n "$OC_BIN_OPT" ]; then
  die "$OC --version failed; --openclaw-bin must name a working openclaw (nothing is installed over it)"
else
  command -v curl >/dev/null 2>&1 || die "curl is missing"
  say "Installing OpenClaw with the official installer (no onboarding)."
  if [ -n "$OC_VERSION" ]; then
    curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | bash -s -- --no-onboard --version "$OC_VERSION"
  else
    curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | bash -s -- --no-onboard
  fi
  OC="$HOME/.openclaw/bin/openclaw"
  "$OC" --version >/dev/null 2>&1 || die "OpenClaw did not install; see https://docs.openclaw.ai/install"
  done_ "OpenClaw installed"
fi
VER_TXT="$("$OC" --version 2>/dev/null | head -n 1)"
out="$(jhh install version-check --text "$VER_TXT" 2>&1)" || die "$out"
say "$VER_TXT"
OLD_GATEWAY=0
if ! jhh install version-check --text "$VER_TXT" --minimum 2026.9.7 >/dev/null 2>&1; then OLD_GATEWAY=1; fi

# ------------------------------------------------------------------ 3
step 3 "Model route"
CLAUDE_VER=""
if [ "$ROUTE" = "cli" ]; then
  CLAUDE="$(command -v claude 2>/dev/null || true)"
  if [ -z "$CLAUDE" ]; then
    say "ERROR: Claude Code is not installed. The agents use your Claude subscription through it. Install it,"
    say "sign in, then run ./install.sh again:"
    say "  curl -fsSL https://claude.ai/install.sh | bash"
    say "  exec zsh -l                  (or open a new Terminal window, so the claude command is found)"
    say "  claude auth login            (opens your browser; sign in with your Claude account)"
    say "Or use an Anthropic API key instead: ./install.sh --api-key"
    exit 1
  fi
  if ! "$CLAUDE" auth status --text >/dev/null 2>&1; then
    say "ERROR: Claude Code is not signed in, or its login expired. Sign in again, then run ./install.sh again:"
    say "  claude auth login            (opens your browser; sign in with your Claude account)"
    say "  claude auth status --text    (must say you are logged in)"
    exit 1
  fi
  CLAUDE_VER="$("$CLAUDE" --version 2>/dev/null | head -n 1 || true)"
  "$CLAUDE" --help >"$TMPD/claude-help.txt" 2>&1 || true
  # the flags of restricted runs, and the Claude Code version each jobhunter model needs (claude refuses a model
  # it is too old for only when an agent first runs)
  out="$(jhh install claude-check --help-file "$TMPD/claude-help.txt" --version-text "$CLAUDE_VER" 2>&1)" || die "$out"
  [ "$out" = "ok" ] || say "NOTE: $out"
  say "Claude Code ${CLAUDE_VER:-(version unknown)}"
  done_ "Claude CLI is signed in"
else
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && oc config get gateway.mode >/dev/null 2>&1; then
    # the key reaches OpenClaw only through its first onboarding (step 4), which never runs twice
    say "OpenClaw is already set up, so the key is not asked again. If the key changed, or this install used your"
    say "Claude subscription before, save the key in OpenClaw: $(oc_show) models auth paste-api-key --provider anthropic"
    say "(then ./install.sh --api-key --smoke checks that the agents can use it)."
  elif [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    is_tty || die "the API key is asked at a terminal: run ./install.sh --api-key in the Terminal app (or set ANTHROPIC_API_KEY; it is never written to the repo)"
    printf 'Anthropic API key (input hidden): '
    IFS= read -r -s ANTHROPIC_API_KEY || true
    printf '\n'
    [ -n "$ANTHROPIC_API_KEY" ] || die "no API key given"
  fi
  done_ "Anthropic API key route"
fi

# ------------------------------------------------------------------ 4
step 4 "OpenClaw onboarding"
DAEMON_ARGS=(--install-daemon --daemon-runtime node)
if [ "$NO_DAEMON" = "1" ]; then DAEMON_ARGS=(--no-install-daemon); fi
if oc config get gateway.mode >/dev/null 2>&1; then
  skip "OpenClaw is already set up for this profile (never reset)"
else
  if [ "$ROUTE" = "cli" ]; then
    oc onboard --non-interactive --accept-risk --mode local --auth-choice anthropic-cli \
      --gateway-bind loopback "${DAEMON_ARGS[@]}" \
      --skip-channels --skip-search --skip-skills --json >"$TMPD/onboard.json" 2>&1 \
      || { cat "$TMPD/onboard.json" >&2; die "openclaw onboard failed"; }
  else
    # The key reaches onboarding through the environment of that one command, never on its command line
    # (process arguments are visible to every user of the computer). OpenClaw's non-interactive onboarding
    # reads ANTHROPIC_API_KEY when --anthropic-api-key is absent [verify on new OpenClaw versions].
    if ! ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" oc onboard --non-interactive --accept-risk --mode local \
      --auth-choice apiKey --secret-input-mode plaintext \
      --gateway-bind loopback "${DAEMON_ARGS[@]}" \
      --skip-channels --skip-search --skip-skills --json >"$TMPD/onboard.json" 2>&1; then
      if grep -q -- '--anthropic-api-key' "$TMPD/onboard.json"; then
        # an OpenClaw that only takes the flag: one retry with it, and say so
        say "NOTE: this OpenClaw version needs the key as an argument; it is visible to local processes while onboarding runs."
        ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" oc onboard --non-interactive --accept-risk --mode local \
          --auth-choice apiKey --anthropic-api-key "$ANTHROPIC_API_KEY" --secret-input-mode plaintext \
          --gateway-bind loopback "${DAEMON_ARGS[@]}" \
          --skip-channels --skip-search --skip-skills --json >"$TMPD/onboard.json" 2>&1 \
          || { cat "$TMPD/onboard.json" >&2; die "openclaw onboard failed"; }
      else
        cat "$TMPD/onboard.json" >&2; die "openclaw onboard failed"
      fi
    fi
  fi
  done_ "OpenClaw onboarded (local Gateway, loopback only)"
fi

# ------------------------------------------------------------------ 5
step 5 "Gateway"
if oc gateway status --require-rpc >/dev/null 2>&1; then
  skip "the Gateway is running"
elif [ "$NO_DAEMON" = "1" ]; then
  die "the Gateway is not running. With --no-daemon the installer never installs or starts one: start your Gateway, then run ./install.sh again"
else
  oc gateway start >/dev/null 2>&1 || oc gateway install --runtime node >/dev/null 2>&1 \
    || die "the Gateway did not start; check: $(oc_show) gateway status"
  i=0
  until oc gateway status --require-rpc >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -lt 30 ] || die "the Gateway does not answer; check: $(oc_show) gateway status"
    sleep 1
  done
  done_ "the Gateway is running"
fi

# ------------------------------------------------------------------ 6
step 6 "Local state"
out="$(JH_OC_BIN="$OC" JH_OC_PROFILE="$PROFILE" JH_PYTHON="$PY" "$PY" "$JH" init 2>&1)" || die "jh.py init failed: $out"
for d in private state logs; do
  if [ -d "$REPO/$d" ]; then chmod 700 "$REPO/$d"; fi
done
load_home_env
[ "$JH_HOME_EXISTS" = "1" ] || die "private/home.json was not written"
[ "$JH_OC_PROFILE" = "$PROFILE" ] || die "private/home.json records OpenClaw profile '$JH_OC_PROFILE', expected '$PROFILE'"
# the CLI route switches are set on every run: a run without --cli-tools native returns to restricted runs
ROUTE_TXT="$(jhh install cli-route set --carriers "$CARRIER" "${CLI_TOOLS_ARGS[@]}" 2>&1)" || die "$ROUTE_TXT"
say "agent mode: $ROUTE_TXT"
out="$(jhh install model-route set "$ROUTE" 2>&1)" || die "$out"
if [ -f "$REPO/private/owner_pin.json" ]; then
  skip "owner PIN is set"
elif is_tty && [ "$ASSUME_YES" != "1" ]; then
  say "Your owner PIN stays on this computer; it is not an account password. You type it to approve drafts, allow"
  say "sites and raise limits. The agent never knows it, so it cannot approve its own drafts or raise its own limits."
  "$REPO/jobhunter" pin set || die "setting the owner PIN failed; run ./jobhunter pin set"
else
  say "NOTE: set your owner PIN at a terminal: ./jobhunter pin set"
fi
done_ "local state in private/, state/, logs/ (install id $JH_INSTALL_ID)"

# ------------------------------------------------------------------ 7
step 7 "Agent workspaces"
out="$("$PY" "$REPO/tools/sync_skills.py" --check 2>&1)" || die "skill templates are inconsistent: $out"
out="$(jhh install render-workspaces 2>&1)" || die "rendering the workspaces failed: $out"
say "$out"
done_ "workspaces rendered under $JH_WS_ROOT"

# ------------------------------------------------------------------ 7b
# OpenClaw's Skill Workshop in mode auto (its default) keeps an enabled weekly job for every agent that may rewrite
# the agent's skills. Those system-owned jobs cannot be disabled one by one, so before any jobhunter agent exists the
# owner agrees to the global mode propose (no weekly reviews), or the install stops here.
step 7b "Skill Workshop reviews"
oc config get skills.workshop.autonomous.mode --json >"$TMPD/workshop.json" 2>/dev/null || : >"$TMPD/workshop.json"
wmode="$(jhh install workshop-mode --current "$TMPD/workshop.json" 2>&1)" || die "$wmode"
if [ "$wmode" != "auto" ]; then
  skip "OpenClaw's Skill Workshop runs no weekly agent reviews (skills.workshop.autonomous.mode $wmode)"
else
  say "OpenClaw's Skill Workshop is in mode auto: every week it runs each agent, the jobhunter agents included, with"
  say "file and shell tools to review and rewrite that agent's skills, outside the dispatcher, the pause and the"
  say "checks of this project. The setting is global for this OpenClaw profile: with mode propose your other agents"
  say "get skill proposals instead of these automatic weekly rewrites."
  if [ "$WORKSHOP_PROPOSE" = "1" ] || ask "Set skills.workshop.autonomous.mode to propose for this OpenClaw profile?"; then
    p="$(jhh install render-workshop-patch 2>&1)" || die "$p"
    oc config patch --file "$p" --dry-run --json >"$TMPD/dry-workshop.json" 2>&1 \
      || { cat "$TMPD/dry-workshop.json" >&2; die "the Skill Workshop config patch did not validate; nothing was changed"; }
    oc config patch --file "$p" >/dev/null || die "openclaw config patch failed (skills.workshop.autonomous.mode)"
    manifest_set '{"skill_workshop_mode_before": "auto", "skill_workshop_mode": "propose"}'
    done_ "skills.workshop.autonomous.mode is propose (weekly reviews again: $(oc_show) config set skills.workshop.autonomous.mode auto)"
  else
    die "the jobhunter agents must not get OpenClaw's weekly skill reviews. Run ./install.sh --skill-workshop-propose (sets skills.workshop.autonomous.mode to propose for this OpenClaw profile), or set it yourself: $(oc_show) config set skills.workshop.autonomous.mode propose"
  fi
fi

# ------------------------------------------------------------------ 8
# Adding an agent and restricting it are one step: right after `agents add` the per-agent config (tools, workspace
# only, exec allowlist or deny, elevated off) and the exec approvals are applied and read back. Until that read-back
# passed, a stop removes the agents this run added again and confines the rest (rollback_agents).
step 8 "Agents and their exec policy"
oc agents list --json >"$TMPD/agents.json" 2>/dev/null || die "openclaw agents list failed"
missing="$(jhh install agents --missing-from "$TMPD/agents.json")" || die "$missing"
CONFINE_PENDING=1
if [ -z "$missing" ]; then
  skip "all jobhunter agents exist"
else
  while IFS="$TAB" read -r id ws model <&3; do
    [ -n "$id" ] || continue
    oc agents add "$id" --workspace "$ws" --model "$model" --non-interactive --json >/dev/null \
      || die "openclaw agents add $id failed"
    ADDED_AGENTS="$ADDED_AGENTS $id"
  done 3<<EOF
$missing
EOF
fi
apply_agent_config
CONFINE_PENDING=0
while IFS="$TAB" read -r id ws model <&3; do
  [ -n "$id" ] || continue
  oc agents set-identity --agent "$id" --workspace "$ws" --from-identity >/dev/null 2>&1 \
    || say "NOTE: could not set the identity of $id from IDENTITY.md"
  if [ "$ROUTE" = "cli" ]; then
    if is_tty; then
      oc models auth login --provider anthropic --method cli --agent "$id" >/dev/null \
        || die "openclaw models auth login failed for $id"
    else
      say "NOTE: no terminal, so models auth login is skipped for $id (the Claude CLI route uses your claude login)"
    fi
  fi
  say "added $id"
done 3<<EOF
$missing
EOF
done_ "per-agent config and exec approvals applied (allowlist, never ask)"

# ------------------------------------------------------------------ 9
step 9 "Shared skills"
if [ "$CHAT_CONTROL" = "1" ]; then
  # OpenClaw loads SKILL.md only: render the templates before the folder is added to extraDirs
  out="$(jhh install render-shared-skills 2>&1)" || die "rendering shared-skills failed: $out"
  if oc config get skills.load.extraDirs --json >"$TMPD/extradirs.json" 2>/dev/null; then :; else
    printf '[]\n' >"$TMPD/extradirs.json"
  fi
  p="$(jhh install render-extradirs --current "$TMPD/extradirs.json" 2>&1)" || die "$p"
  if [ -n "$p" ]; then
    oc config patch --file "$p" >/dev/null || die "adding shared-skills to skills.load.extraDirs failed"
    say "shared-skills added to skills.load.extraDirs"
  else
    say "shared-skills is already in skills.load.extraDirs"
  fi
else
  # an earlier --chat-control install: keep the rendered skill in step with the template (./jobhunter update)
  out="$(jhh install render-shared-skills --if-enabled 2>&1)" || die "rendering shared-skills failed: $out"
fi
done_ "shared skills"

# ------------------------------------------------------------------ 10
step 10 "Guard plugin"
PLUGIN_DIR="$REPO/openclaw/plugins/jobhunter-guard"
[ -f "$PLUGIN_DIR/openclaw.plugin.json" ] || die "the guard plugin is missing from this clone ($PLUGIN_DIR)"
ACCEPT=()
if [ "$ASSUME_YES" = "1" ]; then ACCEPT=(--accept-capabilities); fi
# the listing must succeed: the link below is confirmed with --force, which would replace an existing plugin
oc plugins list --json >"$TMPD/plugins.json" 2>"$TMPD/plugins.err" \
  || { cat "$TMPD/plugins.err" >&2; die "openclaw plugins list failed; check: $(oc_show) plugins list"; }
if grep -q '"jobhunter-guard"' "$TMPD/plugins.json"; then
  skip "jobhunter-guard is installed (linked to this clone)"
else
  # OpenClaw asks before it installs a plugin that is not from ClawHub and cancels without a terminal. The guard
  # comes with this clone and is not installed yet (listed above), so --force only confirms that source.
  FORCE=()
  if [ "$ASSUME_YES" = "1" ]; then
    FORCE=(--force)
    say "linking the guard plugin from this clone (--yes confirms OpenClaw's question about a source outside ClawHub)"
  elif is_tty; then
    say "OpenClaw asks before it installs a plugin that is not from ClawHub. The guard plugin comes with this clone:"
    say "  $PLUGIN_DIR"
    if ask "Link the guard plugin from this clone?"; then FORCE=(--force); else
      die "the guard plugin is required (it checks every agent action); nothing was linked. Run ./install.sh again and answer yes, or run it with --yes"
    fi
  fi
  oc plugins install --link "$PLUGIN_DIR" ${FORCE[@]+"${FORCE[@]}"} >"$TMPD/plugin-install.txt" 2>&1 \
    || { cat "$TMPD/plugin-install.txt" >&2
         die "openclaw plugins install --link failed. OpenClaw installs a plugin that is not from ClawHub only after a confirmation: run ./install.sh at a terminal and answer yes, or run it with --yes"; }
  say "jobhunter-guard linked"
fi
# OpenClaw 2026.9.8 validates the plugin config on enable (repo is required): write it first, then enable
apply_guard_config
oc plugins enable jobhunter-guard ${ACCEPT[@]+"${ACCEPT[@]}"} >/dev/null || die "openclaw plugins enable jobhunter-guard failed"
oc plugins reload jobhunter-guard ${ACCEPT[@]+"${ACCEPT[@]}"} >/dev/null 2>&1 \
  || say "NOTE: plugins reload failed; waiting for the Gateway to load the plugin"
wait_for_guard
done_ "guard plugin active"

# ------------------------------------------------------------------ 11
step 11 "Browser profile"
oc browser profiles >"$TMPD/profiles.txt" 2>/dev/null || : >"$TMPD/profiles.txt"
if grep -qw jobhunter "$TMPD/profiles.txt"; then
  skip "browser profile jobhunter exists"
else
  # OpenClaw draws a progress spinner on stderr: keep it out of the install log, show it only on failure
  oc browser create-profile --name jobhunter >/dev/null 2>"$TMPD/create-profile.err" \
    || { cat "$TMPD/create-profile.err" >&2; die "openclaw browser create-profile failed; check: $(oc_show) browser profiles"; }
  manifest_set '{"browser_profile": "jobhunter", "browser_profile_created": true}'
  say "created browser profile jobhunter"
fi
if [ "$PLATFORM" != "macos" ]; then
  p="$(jhh install render-headless-patch 2>&1)" || die "$p"
  oc config patch --file "$p" >/dev/null || say "NOTE: set browser.profiles.jobhunter.headless to false by hand"
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    say "WARNING: no display found. Browser lanes stay off on this host; API discovery, evaluation, email and the Sheet still run."
  fi
fi
# Site consent comes before any cookie copy: ./jobhunter browser consent explains what is copied, asks per site
# (default No), lets you pick the Chrome profile by name, records your answers with your PIN, and only then
# copies the cookies of the allowed sites (openclaw browser import-profile --domains) and checks the logins.
say "The agent browses in its own browser profile (jobhunter). Your normal Chrome is never driven."
if [ "$ASSUME_YES" = "1" ] || ! is_tty; then
  skip "site consent (at a terminal: ./jobhunter init asks per site, or ./jobhunter browser consent)"
elif [ ! -f "$REPO/private/owner_pin.json" ]; then
  skip "site consent (set your owner PIN first: ./jobhunter pin set, then ./jobhunter browser consent)"
elif ask "Choose now which sites the agent may use with your existing Chrome logins (asked per site, default No)?"; then
  "$REPO/jobhunter" browser consent --new || say "NOTE: run ./jobhunter browser consent later"
else
  skip "site consent (later: ./jobhunter init or ./jobhunter browser consent)"
fi
UPLOAD_ARGS=(--tmpdir "${TMPDIR:-/tmp}")
if [ -n "$UPLOAD_ROOT" ]; then UPLOAD_ARGS=(--dir "$UPLOAD_ROOT"); fi
out="$(jhh install upload-root "${UPLOAD_ARGS[@]}" 2>&1)" || die "the browser upload folder is not usable: $out"
say "browser upload folder: $out"
say "A site you allowed can also be logged in by hand inside the jobhunter profile: ./jobhunter browser login <site>"
if oc browser --browser-profile jobhunter doctor >/dev/null 2>&1; then say "browser doctor: ok"; else
  say "NOTE: the browser check reported a problem. Usually the agent browser is just not running yet, which is fine"
  say "      now; ./jobhunter doctor checks it again (details: $(oc_show) browser --browser-profile jobhunter doctor)"
fi
done_ "browser profile ready"

# ------------------------------------------------------------------ 12
step 12 "Chat channel"
oc channels status --probe >"$TMPD/channels.txt" 2>&1 || : >"$TMPD/channels.txt"
linked="$(grep -iE 'linked|connected' "$TMPD/channels.txt" | grep -viE 'not (linked|connected)|disconnected|unlinked' | wc -l || true)"
if [ "${linked// /}" != "0" ] && [ -n "${linked// /}" ]; then
  skip "a chat channel is linked"
else
  say "No chat channel is linked yet. For WhatsApp (a dedicated number is recommended):"
  say "  $(oc_show) plugins install @openclaw/whatsapp"
  say "  $(oc_show) channels login --channel whatsapp     (scan the QR code)"
  say "Approvals then work with /jh approve <code> in that chat. This installer never edits channels."
fi

# ------------------------------------------------------------------ 13
step 13 "Automations"
# what differs before anything is written: `cron add` with a declaration key may itself bring a job back to its
# declaration, and every repair is reported below
oc cron list --all --json >"$TMPD/cron-before.json" 2>/dev/null || printf '{}\n' >"$TMPD/cron-before.json"
drifted="$(jhh install render-crons --repair "$TMPD/cron-before.json" --report 2>&1)" || die "$drifted"
lines="$(jhh install render-crons 2>&1)" || die "$lines"
RENDERED_COUNT=0
run_rendered "$lines" "cron add"
added="$RENDERED_COUNT"
store_cron_list
say "$CRON_STORE_OUT"
# every field of every jobhunter automation must match its declaration: full-field edit, else remove and add
repair="$(jhh install render-crons --repair "$TMPD/cron.json" 2>&1)" || die "$repair"
if [ -n "$repair" ]; then
  run_rendered "$repair" "cron edit"
  say "repaired $RENDERED_COUNT automations that differed from their declaration"
  store_cron_list
  repl="$(jhh install render-crons --repair "$TMPD/cron.json" --replace 2>&1)" || die "$repl"
  if [ -n "$repl" ]; then
    run_rendered "$repl" "cron rm and add"
    say "replaced the automations an edit could not repair"
    store_cron_list
  fi
  out="$(jhh install render-crons --repair "$TMPD/cron.json" --check 2>&1)" || die "$out"
fi
# every job of the list before matches its declaration now (the repair check above passed or found nothing)
nrepaired=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  say "repaired $line"
  nrepaired=$((nrepaired + 1))
done <<DRIFTED
$drifted
DRIFTED
foreign_check
alerts="$(jhh install render-crons --alerts 2>&1)" || die "$alerts"
run_rendered "$alerts" "cron edit"
say "failure alerts: $RENDERED_COUNT"
if [ "$nrepaired" -gt 0 ]; then
  done_ "$added automations declared, $nrepaired repaired, all disabled until ./jobhunter resume"
else
  done_ "$added automations declared, all disabled until ./jobhunter resume"
fi

# ------------------------------------------------------------------ 14
# The identity checks run one restricted cron job per tool agent (jh.py whoami, a write and a read, never a
# question to a person) and one QC turn. They are skipped when they passed before for the same OpenClaw, claude,
# guard and jh.py versions and the same agent mode (--smoke runs them anyway).
QC_FILE_APPLIED=0
qc_smoke() {
  local out="" mode=""
  if out="$(jh qc smoke 2>&1)"; then say "ok    jobhunter-qc answers"; return 0; fi
  mode="$(jhh install cli-route get qc_reply 2>/dev/null || true)"
  if [ "$mode" = "file" ] && [ "$QC_FILE_APPLIED" != "1" ]; then
    QC_FILE_APPLIED=1
    say "this OpenClaw does not return the QC reply from the run record: the reviewer writes a verdict file instead"
    apply_agent_config
    apply_guard_config
    oc plugins reload jobhunter-guard ${ACCEPT[@]+"${ACCEPT[@]}"} >/dev/null 2>&1 || true
    wait_for_guard
    store_cron_list
    if out="$(jh qc smoke 2>&1)"; then say "ok    jobhunter-qc answers (verdict file)"; return 0; fi
  fi
  say "details: $out"
  out="$(json_message "$out")"
  die "the jobhunter-qc test turn failed: ${out#the jobhunter-qc test turn failed: }. The QC reviewer checks every draft, so nothing can be approved or sent until this passes. Check your Claude login (claude auth status --text), then run ./install.sh again. If it fails the same way, see docs/TROUBLESHOOTING.md, Claude subscription route"
}
step 14 "Agent identity checks"
say "Each agent now runs once to prove who it is (about 2 to 4 minutes; it uses your Claude plan or API key)."
STAMP_ARGS=(--openclaw "$VER_TXT" --claude "$CLAUDE_VER" --route "$ROUTE")
if [ "$SMOKE" = "0" ]; then
  skip "agent identity checks (--no-smoke; later: ./jobhunter doctor --probe)"
  out="$(jh selftest --offline 2>&1)" || die "jh.py selftest --offline failed: $out"
elif [ "$SMOKE" = "1" ] || ! jhh install probe-stamp read --match "${STAMP_ARGS[@]}" >/dev/null 2>&1; then
  t0="$(date +%s)"
  for role in scout evaluator applier outreach; do
    id="$(jhh install run-preflight "jobhunter:probe-$role" 2>&1)" || die "$id"
    oc cron run "$id" --wait --wait-timeout 10m --json >"$TMPD/probe-$role.json" 2>&1 || true
  done
  out="$(jh selftest --offline --probe-since "$t0" 2>&1)" \
    || { say "details: $out"
         die "agent identity check failed (docs/TROUBLESHOOTING.md, Claude subscription route): $(json_message "$out"). Check your Claude login (claude auth status --text), then run ./install.sh again"; }
  say "ok    scout, evaluator, applier and outreach ran jh.py as themselves"
  qc_smoke
  jhh install probe-stamp write "${STAMP_ARGS[@]}" >/dev/null || die "could not record the probe stamp"
  done_ "agent identity checks passed"
else
  skip "agent identity checks (passed before for the same OpenClaw, claude, guard and jh.py versions; --smoke to force)"
  out="$(jh selftest --offline 2>&1)" || die "jh.py selftest --offline failed: $out"
fi

# ------------------------------------------------------------------ 15
step 15 "Stay awake (macOS, optional)"
if [ "$PLATFORM" != "macos" ]; then
  skip "not macOS"
elif [ "$STAY_AWAKE" = "1" ] || ask "Keep this Mac awake while it is on AC power, so the automations run?"; then
  p="$(jhh install render-stayawake 2>&1)" || die "$p"
  LA="$HOME/Library/LaunchAgents"
  mkdir -p "$LA"
  cp "$p" "$LA/ai.openclaw-job-hunter.stayawake.plist"
  launchctl bootout "gui/$(id -u)/ai.openclaw-job-hunter.stayawake" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$LA/ai.openclaw-job-hunter.stayawake.plist" || die "launchctl bootstrap failed"
  manifest_set '{"stay_awake": true}'
  done_ "stay-awake LaunchAgent loaded"
else
  skip "stay awake (later: ./install.sh --stay-awake)"
fi

# ------------------------------------------------------------------ 16
step 16 "Email finder (optional, off by default)"
if [ "$ASSUME_YES" = "1" ] || ! is_tty; then
  skip "email finder keys (optional; later: ./jobhunter enrich connect <provider>, docs/EMAIL-FINDER.md)"
elif [ ! -f "$REPO/private/owner_pin.json" ]; then
  skip "email finder keys (set your owner PIN first; later: ./jobhunter enrich connect <provider>)"
else
  say "Optional: the agent can look up the work email of a person on a job's hiring team, with free API keys from"
  say "accounts YOU create at Prospeo, Hunter, Tomba, GetProspect and ZeroBounce. It is off until you turn it on in"
  say "private/config.json (enrich.enabled), never scrapes LinkedIn, and stops before a free tier runs out."
  say "Never use a key you found online: it belongs to someone else. Details: docs/EMAIL-FINDER.md"
  if ask "Connect an email finder key now?"; then
    while :; do
      printf 'Provider (prospeo, hunter, tomba, getprospect, zerobounce; Enter when done): '
      prov=""; IFS= read -r prov || prov=""
      case "$prov" in
        "") break ;;
        prospeo|hunter|tomba|getprospect|zerobounce|anymailfinder|findymail|apollo)
          "$REPO/jobhunter" enrich connect "$prov" || say "NOTE: $prov was not connected; later: ./jobhunter enrich connect $prov" ;;
        *) say "unknown provider: $prov" ;;
      esac
    done
    done_ "email finder keys"
  else
    skip "email finder keys (later: ./jobhunter enrich connect <provider>)"
  fi
fi

# ------------------------------------------------------------------ 17
step 17 "Finish"
if [ "$DISPATCH_PAUSED" = "1" ]; then
  did="$(jhh install cron-id jobhunter:dispatch 2>&1)" || die "$did"
  oc cron enable "$did" >/dev/null || die "could not start the dispatcher again (automation $did)"
  DISPATCH_PAUSED=0
  say "the dispatcher runs again"
else
  say "Installed. Nothing runs yet: every automation is disabled."
fi
if [ "$CLI_TOOLS" = "native" ]; then
  red "agent mode: Claude Code's own tools (reduced protection, --cli-tools native; identity carrier argv)."
  red "  The exec allowlist and workspaceOnly do not apply to Claude Code's tools; only the guard checks them."
  red "  AskUserQuestion can wait until the run times out; your ~/.claude settings, hooks and MCP servers load into"
  red "  agent runs. For full protection run ./install.sh again without --cli-tools, or use the API key route."
else
  say "agent mode: restricted runs (Claude Code's own tools are off for every jobhunter agent; identity carriers $CARRIER)"
fi
boundary="$(jhh install identity-boundary 2>/dev/null)" || boundary=""
if [ -n "$boundary" ]; then
  while IFS= read -r line; do [ -z "$line" ] || red "$line"; done <<BOUNDARY
$boundary
BOUNDARY
  say "Agent identity cannot be forged by an agent whose shell is confined. An agent with an unconfined shell runs as you and is trusted like you."
fi
if [ "$OC" = "$HOME/.openclaw/bin/openclaw" ] && [ "$(oc_bin_show)" != "openclaw" ]; then
  rcfile="~/.bashrc"; [ "$PLATFORM" != "macos" ] || rcfile="~/.zprofile"
  say "Your terminal does not find openclaw by name yet (the docs use the short name). Add it once, then open a new terminal:"
  say "  echo 'export PATH=\"\$HOME/.openclaw/bin:\$PATH\"' >> $rcfile"
fi
say "Next steps (in this Terminal window, in this folder):"
if [ ! -f "$REPO/private/owner_pin.json" ]; then
  say "  ./jobhunter pin set           your owner PIN (not set yet: this install ran without a terminal or with --yes)"
fi
say "  ./jobhunter init              about 15 minutes: your details, resume, preferences, the sites the agent may"
say "                                use, email and your Google Sheet (each part can be skipped and done later)"
say "  ./jobhunter doctor            every line must say ok"
say "  ./jobhunter resume            start (asks for your PIN)"
say "Later, one by one: ./jobhunter browser consent (sites), ./jobhunter sheet connect (docs/GOOGLE-SHEETS.md)."
say "Email goes out from your own Gmail in the agent's browser once you allow Gmail: no password is needed."
say "Optional: ./jobhunter mail connect (Google app password route, docs/EMAIL-SETUP.md);"
say "          ./jobhunter enrich connect <provider> (email finder with your own free keys, docs/EMAIL-FINDER.md)."
say "Approval mode is human (you approve every message). LinkedIn automation is off."
done_ "install complete"
