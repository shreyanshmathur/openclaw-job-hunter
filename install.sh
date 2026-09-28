#!/bin/bash
# openclaw-job-hunter installer (design 10.3). Safe to re-run: every step prints DONE or SKIP.
#
# Usage: ./install.sh [options]
#   --api-key                 use an Anthropic API key instead of your Claude CLI login
#   --profile <name>          use a separate OpenClaw profile (recorded; one clone is one install)
#   --openclaw-version <v>    OpenClaw version to install when it is missing
#   --chat-control            add shared-skills (the /jh help skill) to your OpenClaw skill folders
#   --upload-root <dir>       OpenClaw's browser upload folder, when it is not /tmp/openclaw/uploads
#   --stay-awake              macOS: keep the Mac awake on AC power (otherwise asked at a terminal)
#   --no-smoke                skip the one-line model test turns at the end
#   --yes                     never ask; take the safe default for every optional step
#   --help                    show this help
#
# Nothing here sends mail, applies to jobs or touches LinkedIn. All automations are created disabled;
# ./jobhunter resume turns them on after ./jobhunter init and ./jobhunter doctor.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
JH="$REPO/scripts/jh.py"
ORIG_PATH="$PATH"   # the person's own PATH, to print openclaw commands they can type as shown
export PATH="$HOME/.openclaw/bin:$HOME/.openclaw/tools/node/bin:$HOME/.local/bin:$PATH"
TAB="$(printf '\t')"

say()   { printf '%s\n' "$*"; }
die()   { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step()  { STEP_NAME="$2"; printf '\n[%s] %s\n' "$1" "$2"; }
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
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

ROUTE="cli"; PROFILE=""; PROFILE_SET=0; OC_VERSION=""; CHAT_CONTROL=0; STAY_AWAKE=0; ASSUME_YES=0; SMOKE=1
UPLOAD_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --api-key) ROUTE="api_key" ;;
    --profile) [ $# -ge 2 ] || die "--profile needs a value"; PROFILE="$2"; PROFILE_SET=1; shift ;;
    --openclaw-version) [ $# -ge 2 ] || die "--openclaw-version needs a value"; OC_VERSION="$2"; shift ;;
    --chat-control) CHAT_CONTROL=1 ;;
    --upload-root) [ $# -ge 2 ] || die "--upload-root needs a value"; UPLOAD_ROOT="$2"; shift ;;
    --stay-awake) STAY_AWAKE=1 ;;
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

PY="$(command -v python3 2>/dev/null || true)"
OC="$(command -v openclaw 2>/dev/null || true)"
[ -n "$OC" ] || OC="$HOME/.openclaw/bin/openclaw"
OC_ARGS=()
oc()  { "$OC" ${OC_ARGS[@]+"${OC_ARGS[@]}"} "$@"; }
jh()  { "$PY" "$JH" "$@"; }
jhh() { "$PY" "$JH" --human "$@"; }
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/jh-install.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

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
  JH_HOME_EXISTS=0; JH_OC_BIN=""; JH_OC_PROFILE=""; JH_PY=""; JH_WS_ROOT=""; JH_INSTALL_ID=""
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
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is needed"
command -v git >/dev/null 2>&1 || die "git is missing"
out="$(jhh install check-sqlite 2>&1)" || die "$out"
out="$(jhh install check-path 2>&1)" || die "$out"
load_home_env
if [ "$JH_HOME_EXISTS" = "1" ]; then
  if [ "$PROFILE_SET" = "1" ] && [ "$PROFILE" != "$JH_OC_PROFILE" ]; then
    die "this clone is installed for OpenClaw profile '${JH_OC_PROFILE:-default}'. One clone is one install; clone the repo again for a test install."
  fi
  PROFILE="$JH_OC_PROFILE"
fi
if [ -n "$PROFILE" ]; then OC_ARGS=(--profile "$PROFILE"); fi
say "system $PLATFORM, python $PY, OpenClaw profile ${PROFILE:-default}, model route $ROUTE"
done_

# ------------------------------------------------------------------ 2
step 2 "OpenClaw"
if "$OC" --version >/dev/null 2>&1; then
  skip "OpenClaw is installed"
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

# ------------------------------------------------------------------ 3
step 3 "Model route"
if [ "$ROUTE" = "cli" ]; then
  CLAUDE="$(command -v claude 2>/dev/null || true)"
  if [ -z "$CLAUDE" ]; then
    say "Claude Code is not installed. Install it, sign in, then run ./install.sh again:"
    say "  curl -fsSL https://claude.ai/install.sh | bash"
    say "  claude auth login"
    say "Or use an Anthropic API key instead: ./install.sh --api-key"
    exit 1
  fi
  if ! "$CLAUDE" auth status --text >/dev/null 2>&1; then
    say "Claude Code is not signed in. Run: claude auth login   then ./install.sh again."
    exit 1
  fi
  done_ "Claude CLI is signed in"
else
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    is_tty || die "set ANTHROPIC_API_KEY in the environment (it is never written to the repo)"
    printf 'Anthropic API key (input hidden): '
    IFS= read -r -s ANTHROPIC_API_KEY || true
    printf '\n'
    [ -n "$ANTHROPIC_API_KEY" ] || die "no API key given"
  fi
  done_ "Anthropic API key route"
fi

# ------------------------------------------------------------------ 4
step 4 "OpenClaw onboarding"
if oc config get gateway.mode >/dev/null 2>&1; then
  skip "OpenClaw is already set up for this profile (never reset)"
else
  if [ "$ROUTE" = "cli" ]; then
    oc onboard --non-interactive --accept-risk --mode local --auth-choice anthropic-cli \
      --gateway-bind loopback --install-daemon --daemon-runtime node \
      --skip-channels --skip-search --skip-skills --json >"$TMPD/onboard.json" 2>&1 \
      || { cat "$TMPD/onboard.json" >&2; die "openclaw onboard failed"; }
  else
    # The key reaches onboarding through the environment of that one command, never on its command line
    # (process arguments are visible to every user of the computer). OpenClaw's non-interactive onboarding
    # reads ANTHROPIC_API_KEY when --anthropic-api-key is absent [verify on new OpenClaw versions].
    if ! ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" oc onboard --non-interactive --accept-risk --mode local \
      --auth-choice apiKey --secret-input-mode plaintext \
      --gateway-bind loopback --install-daemon --daemon-runtime node \
      --skip-channels --skip-search --skip-skills --json >"$TMPD/onboard.json" 2>&1; then
      if grep -q -- '--anthropic-api-key' "$TMPD/onboard.json"; then
        # an OpenClaw that only takes the flag: one retry with it, and say so
        say "NOTE: this OpenClaw version needs the key as an argument; it is visible to local processes while onboarding runs."
        ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" oc onboard --non-interactive --accept-risk --mode local \
          --auth-choice apiKey --anthropic-api-key "$ANTHROPIC_API_KEY" --secret-input-mode plaintext \
          --gateway-bind loopback --install-daemon --daemon-runtime node \
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

# ------------------------------------------------------------------ 8
step 8 "Agents"
oc agents list --json >"$TMPD/agents.json" 2>/dev/null || die "openclaw agents list failed"
missing="$(jhh install agents --missing-from "$TMPD/agents.json")" || die "$missing"
if [ -z "$missing" ]; then
  skip "all jobhunter agents exist"
else
  while IFS="$TAB" read -r id ws model <&3; do
    [ -n "$id" ] || continue
    oc agents add "$id" --workspace "$ws" --model "$model" --non-interactive --json >/dev/null \
      || die "openclaw agents add $id failed"
    oc agents set-identity --agent "$id" --workspace "$ws" --from-identity >/dev/null 2>&1 \
      || say "NOTE: could not set the identity of $id from IDENTITY.md"
    if [ "$ROUTE" = "cli" ]; then
      oc models auth login --provider anthropic --method cli --agent "$id" >/dev/null \
        || die "openclaw models auth login failed for $id"
    fi
    say "added $id"
  done 3<<EOF
$missing
EOF
  done_ "agents added"
fi

# ------------------------------------------------------------------ 9
step 9 "Agent config and exec approvals"
p="$(jhh install render-agents-patch --route "$ROUTE" 2>&1)" || die "$p"
oc config patch --file "$p" --dry-run --json >"$TMPD/dry.json" 2>&1 \
  || { cat "$TMPD/dry.json" >&2; die "the agents config patch did not validate; nothing was changed"; }
oc config patch --file "$p" >/dev/null || die "openclaw config patch failed"
oc config validate >/dev/null || die "openclaw config validate failed after the patch; run: $(oc_show) config validate"
oc approvals get --json >"$TMPD/approvals.json" 2>/dev/null \
  || die "openclaw approvals get failed; exec approvals are required (see docs/TROUBLESHOOTING.md)"
p="$(jhh install render-approvals --current "$TMPD/approvals.json" 2>&1)" || die "$p"
oc approvals set --file "$p" >/dev/null || die "openclaw approvals set failed"
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
done_ "per-agent config and exec approvals applied"

# ------------------------------------------------------------------ 10
step 10 "Guard plugin"
PLUGIN_DIR="$REPO/openclaw/plugins/jobhunter-guard"
[ -f "$PLUGIN_DIR/openclaw.plugin.json" ] || die "the guard plugin is missing from this clone ($PLUGIN_DIR)"
ACCEPT=()
if [ "$ASSUME_YES" = "1" ]; then ACCEPT=(--accept-capabilities); fi
oc plugins list --json >"$TMPD/plugins.json" 2>/dev/null || printf '{}\n' >"$TMPD/plugins.json"
if grep -q '"jobhunter-guard"' "$TMPD/plugins.json"; then
  skip "jobhunter-guard is installed (linked to this clone)"
else
  oc plugins install --link "$PLUGIN_DIR" >/dev/null || die "openclaw plugins install --link failed"
  say "jobhunter-guard linked"
fi
oc plugins enable jobhunter-guard ${ACCEPT[@]+"${ACCEPT[@]}"} >/dev/null || die "openclaw plugins enable jobhunter-guard failed"
p="$(jhh install render-guard-config 2>&1)" || die "$p"
oc config patch --file "$p" >/dev/null || die "writing the guard config failed"
oc plugins reload jobhunter-guard ${ACCEPT[@]+"${ACCEPT[@]}"} >/dev/null 2>&1 \
  || say "NOTE: plugins reload failed; waiting for the Gateway to load the plugin"
out="$(jhh install wait-guard --timeout 60 2>&1)" \
  || { say "$out"; die "the guard did not report in. Check: $(oc_show) logs (look for jobhunter-guard), then run ./install.sh again"; }
say "$out"
done_ "guard plugin active"

# ------------------------------------------------------------------ 11
step 11 "Browser profile"
oc browser profiles >"$TMPD/profiles.txt" 2>/dev/null || : >"$TMPD/profiles.txt"
if grep -qw jobhunter "$TMPD/profiles.txt"; then
  skip "browser profile jobhunter exists"
else
  oc browser create-profile --name jobhunter >/dev/null || die "openclaw browser create-profile failed"
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
  say "NOTE: openclaw browser --browser-profile jobhunter doctor reported a problem"
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
lines="$(jhh install render-crons 2>&1)" || die "$lines"
RENDERED_COUNT=0
run_rendered "$lines" "cron add"
added="$RENDERED_COUNT"
oc cron list --all --json >"$TMPD/cron.json" || die "openclaw cron list failed"
out="$(jhh install manifest --set "$TMPD/cron.json" 2>&1)" || die "$out"
say "$out"
alerts="$(jhh install render-crons --alerts 2>&1)" || die "$alerts"
run_rendered "$alerts" "cron edit"
say "failure alerts: $RENDERED_COUNT"
done_ "$added automations declared, all disabled until ./jobhunter resume"

# ------------------------------------------------------------------ 14
step 14 "Smoke tests"
if [ "$SMOKE" = "1" ]; then
  printf 'Reply with the word OK and nothing else.\n' >"$TMPD/smoke.txt"
  ts="$(date +%Y%m%d%H%M%S)"
  agents="$(jhh install agents)" || die "$agents"
  while IFS="$TAB" read -r id ws model <&3; do
    [ -n "$id" ] || continue
    if oc agent --agent "$id" --session-key "smoke-$ts" --message-file "$TMPD/smoke.txt" --json --timeout 120 \
        >"$TMPD/smoke-$id.json" 2>&1 && grep -q 'OK' "$TMPD/smoke-$id.json"; then
      say "ok    $id answers"
    else
      tail -n 5 "$TMPD/smoke-$id.json" >&2 || true
      die "the test turn for $id failed (model login or Gateway problem; see docs/TROUBLESHOOTING.md)"
    fi
  done 3<<EOF
$agents
EOF
else
  skip "model test turns (--no-smoke)"
fi
if oc sandbox explain --agent jobhunter-qc >"$TMPD/qc.txt" 2>&1; then
  say "jobhunter-qc tool policy (must show no tools):"
  sed -n '1,12p' "$TMPD/qc.txt"
fi
out="$(jh selftest --offline 2>&1)" || die "jh.py selftest --offline failed: $out"
oc cron list --all --json >"$TMPD/cron.json" || die "openclaw cron list failed"
out="$(jhh install manifest --set "$TMPD/cron.json" 2>&1)" || die "$out"
done_ "smoke tests passed"

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
say "Installed. Nothing runs yet: every automation is disabled."
if [ "$(oc_bin_show)" != "openclaw" ]; then
  rcfile="~/.bashrc"; [ "$PLATFORM" != "macos" ] || rcfile="~/.zprofile"
  say "Your terminal does not find openclaw by name yet (the docs use the short name). Add it once, then open a new terminal:"
  say "  echo 'export PATH=\"\$HOME/.openclaw/bin:\$PATH\"' >> $rcfile"
fi
say "Next steps:"
say "  ./jobhunter init              your details, resume, preferences, then which sites the agent may use"
say "  ./jobhunter browser consent   allow sites one by one (Gmail, job boards); each defaults to No"
say "  ./jobhunter sheet connect     your Google Sheet (docs/GOOGLE-SHEETS.md; the wizard asks too)"
say "  ./jobhunter doctor            every check must be green"
say "  ./jobhunter resume            start (asks for your PIN)"
say "Email goes out from your own Gmail in the agent's browser once you allow Gmail: no password is needed."
say "Optional: ./jobhunter mail connect (Google app password route, docs/EMAIL-SETUP.md);"
say "          ./jobhunter enrich connect <provider> (email finder with your own free keys, docs/EMAIL-FINDER.md)."
say "Approval mode is human (you approve every message). LinkedIn automation is off."
done_ "install complete"
