#!/bin/bash
# openclaw-job-hunter uninstaller (design 10.4).
#
# Usage: ./uninstall.sh [--purge] [--yes] [--help]
#   --purge   also delete local data: state/, logs/, exports/ (typed confirmation) and then, with a second
#             typed confirmation, private/ (your profile, resume copies, secrets) and the Gmail app
#             password item in the macOS Keychain
#   --yes     never ask; optional removals are skipped (the browser profile, the stay-awake agent and
#             WS_ROOT are kept). --purge still needs a terminal and the typed confirmations.
#
# Removes only what this clone installed: the jobhunter automations, the jobhunter-guard plugin, the
# jobhunter-* agents and their config entries, and the jobhunter exec approvals. It never removes OpenClaw,
# Claude Code, channel links, the Gateway service or your Google Sheet.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
JH="$REPO/scripts/jh.py"
export PATH="$HOME/.openclaw/bin:$HOME/.openclaw/tools/node/bin:$HOME/.local/bin:$PATH"
TAB="$(printf '\t')"

say()  { printf '%s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n[%s] %s\n' "$1" "$2"; }
is_tty() { [ -t 0 ] && [ -t 1 ]; }
ask() {
  if [ "$ASSUME_YES" = 1 ] || ! is_tty; then return 1; fi
  printf '%s [y/N] ' "$1"
  local ans=""
  IFS= read -r ans || return 1
  case "$ans" in y|Y|yes|YES|Yes) return 0 ;; esac
  return 1
}
typed_confirm() {
  is_tty || return 1
  printf '%s\nType %s to confirm: ' "$1" "$2"
  local ans=""
  IFS= read -r ans || return 1
  [ "$ans" = "$2" ]
}

PURGE=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

PY="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"
jh()  { "$PY" "$JH" "$@"; }
jhh() { "$PY" "$JH" --human "$@"; }
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/jh-uninstall.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

JH_HOME_EXISTS=0; JH_OC_BIN=""; JH_OC_PROFILE=""; JH_PY=""; JH_WS_ROOT=""; JH_INSTALL_ID=""
txt="$(jhh install shell-env 2>&1)" || die "cannot read private/home.json: $txt"
while IFS= read -r line; do
  case "$line" in JH_[A-Z_]*=*) eval "$line" ;; esac
done <<EOF
$txt
EOF
[ "$JH_HOME_EXISTS" = "1" ] || die "private/home.json is missing: nothing from this clone is installed"
OC="$JH_OC_BIN"
if [ -z "$OC" ] || [ ! -x "$OC" ]; then OC="$(command -v openclaw 2>/dev/null || echo "$HOME/.openclaw/bin/openclaw")"; fi
OC_ARGS=()
if [ -n "$JH_OC_PROFILE" ]; then OC_ARGS=(--profile "$JH_OC_PROFILE"); fi
oc() { "$OC" ${OC_ARGS[@]+"${OC_ARGS[@]}"} "$@"; }
FAILED=0
warn() { say "WARNING: $*"; FAILED=1; }

step 1 "Pause and remove the automations"
"$REPO/jobhunter" pause >/dev/null 2>&1 || say "NOTE: ./jobhunter pause did not complete; removing the automations anyway"
if oc cron list --all --json >"$TMPD/cron.json" 2>/dev/null; then
  ids="$(jhh install job-ids --which all --from-list "$TMPD/cron.json" 2>/dev/null)" || ids=""
  n=0
  while IFS= read -r id <&3; do
    [ -n "$id" ] || continue
    if oc cron rm "$id" >/dev/null 2>&1; then n=$((n + 1)); else warn "could not remove automation $id"; fi
  done 3<<EOF
$ids
EOF
  say "removed $n automations"
else
  warn "openclaw cron list failed; remove the jobhunter:* automations by hand (openclaw cron list --all)"
fi

step 2 "Remove the guard plugin"
oc plugins disable jobhunter-guard >/dev/null 2>&1 || say "NOTE: plugin was not enabled"
oc plugins uninstall jobhunter-guard --force >/dev/null 2>&1 || say "NOTE: plugin was not installed"

step 3 "Remove the agents"
agents="$(jhh install agents 2>/dev/null)" || agents=""
oc agents list --json >"$TMPD/agents.json" 2>/dev/null || printf '[]\n' >"$TMPD/agents.json"
while IFS="$TAB" read -r id ws model <&3; do
  [ -n "$id" ] || continue
  if grep -q "\"$id\"" "$TMPD/agents.json"; then
    if oc agents delete "$id" --force --json >/dev/null 2>&1; then say "deleted $id (workspace moved to the Trash)"
    else warn "could not delete agent $id"; fi
  fi
done 3<<EOF
$agents
EOF

step 4 "Remove the config entries and exec approvals"
p="$(jhh install render-uninstall-patch 2>&1)" || die "$p"
oc config patch --file "$p" >/dev/null 2>&1 || warn "config patch failed; remove agents.entries[jobhunter-*] and plugins.entries.jobhunter-guard by hand"
if oc config get skills.load.extraDirs --json >"$TMPD/extradirs.json" 2>/dev/null; then
  p="$(jhh install render-extradirs --current "$TMPD/extradirs.json" --remove 2>/dev/null)" || p=""
  if [ -n "$p" ]; then oc config patch --file "$p" >/dev/null 2>&1 || warn "could not remove shared-skills from skills.load.extraDirs"; fi
fi
jhh install render-shared-skills --remove >/dev/null 2>&1 || warn "could not remove the rendered shared-skills/*/SKILL.md"
if oc approvals get --json >"$TMPD/approvals.json" 2>/dev/null; then
  p="$(jhh install render-approvals --current "$TMPD/approvals.json" --remove 2>&1)" || { warn "$p"; p=""; }
  if [ -n "$p" ]; then oc approvals set --file "$p" >/dev/null 2>&1 || warn "could not remove the jobhunter exec approvals"; fi
else
  warn "openclaw approvals get failed; remove the jobhunter-* entries with openclaw approvals"
fi

step 5 "Optional removals"
if ask "Delete the jobhunter browser profile (the cookies copied or logged in for the sites you allowed)?"; then
  oc browser delete-profile --name jobhunter >/dev/null 2>&1 || warn "could not delete the browser profile"
  # the copied logins are gone with the profile: mark every site consent revoked (no PIN: it only takes access away)
  jhh install consent-revoke --all >/dev/null 2>&1 || say "NOTE: could not mark the site consents revoked in private/consent.json"
  say "deleted the jobhunter browser profile; every site consent is taken back"
else
  say "kept the jobhunter browser profile (./jobhunter browser forget --all clears its cookies and takes back every site)"
fi
# Email finder keys live in the macOS Keychain (or private/enrich_keys.json), outside what deleting the clone
# removes. ./jobhunter enrich disconnect --all deletes them; it asks for your PIN.
if [ -f "$REPO/private/enrich_keys.json" ] || [ "$(uname -s)" = "Darwin" ]; then
  if ask "Remove your email finder API keys (Keychain items or private/enrich_keys.json)?"; then
    "$REPO/jobhunter" enrich disconnect --all || warn "could not remove the email finder keys; run ./jobhunter enrich disconnect --all"
  else
    say "kept any email finder keys (./jobhunter enrich disconnect --all removes them)"
  fi
fi
PLIST="$HOME/Library/LaunchAgents/ai.openclaw-job-hunter.stayawake.plist"
if [ -f "$PLIST" ]; then
  if ask "Remove the stay-awake LaunchAgent?"; then
    launchctl bootout "gui/$(id -u)/ai.openclaw-job-hunter.stayawake" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    say "stay-awake removed"
  fi
fi
case "$JH_WS_ROOT" in
  */.openclaw-job-hunter/*)
    if [ -d "$JH_WS_ROOT" ] && ask "Delete the agent workspaces folder $JH_WS_ROOT?"; then
      rm -rf "$JH_WS_ROOT"
      say "workspaces deleted"
    fi ;;
esac

step 6 "Local data"
if [ "$PURGE" = "1" ]; then
  if typed_confirm "This deletes state/, logs/ and exports/ (the database, logs and CSV exports)." "delete"; then
    rm -rf "$REPO/state" "$REPO/logs" "$REPO/exports"
    say "deleted state/, logs/, exports/"
    if typed_confirm "Also delete private/ (profile, answers, resume copies, config, secrets, PIN)?" "delete private"; then
      # the Keychain item is named after the install id and the account recorded in private/: remove it first
      out="$(jhh install forget-mail-secret 2>&1)" || { warn "could not delete the Gmail app password from the Keychain: $out"; out=""; }
      [ -z "$out" ] || say "$out"
      rm -rf "$REPO/private"
      say "deleted private/"
    else
      say "kept private/"
    fi
  else
    say "purge cancelled; local data kept"
  fi
else
  say "kept private/, state/, logs/ and exports/ (./uninstall.sh --purge deletes them)"
fi

step 7 "Things to remove by hand (never touched here)"
say "Google Sheet: in Apps Script, Deploy > Manage deployments > Archive, or delete the sheet."
say "Gmail app password (only if you used the optional app password route): https://myaccount.google.com/apppasswords > remove \"job hunter\"."
say "Your own Chrome was never changed: the copied cookies lived only in the jobhunter browser profile."
say "Email finder accounts (if any): delete them on the provider's site if you no longer want them."
say "OpenClaw, Claude Code, chat channels and the Gateway service stay installed."
if [ "$FAILED" = "1" ]; then
  say "Uninstall finished with warnings (see above)."
  exit 1
fi
say "Uninstall complete."
