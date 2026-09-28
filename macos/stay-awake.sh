#!/bin/bash
# Keep the Mac awake while it is on AC power, so the automations run (design 10.3 step 15).
# Started by the LaunchAgent ai.openclaw-job-hunter.stayawake (installed only when you agree during
# ./install.sh or with ./install.sh --stay-awake). On battery it does nothing, so the battery is not drained.
# Remove: ./uninstall.sh (asks), or launchctl bootout gui/$(id -u)/ai.openclaw-job-hunter.stayawake
set -u
INTERVAL=300
on_ac_power() {
  /usr/bin/pmset -g ps 2>/dev/null | /usr/bin/grep -q "AC Power"
}
while true; do
  if on_ac_power; then
    # -i prevents idle sleep; -t ends the assertion after INTERVAL seconds, then power is checked again
    /usr/bin/caffeinate -i -t "$INTERVAL"
  else
    /bin/sleep "$INTERVAL"
  fi
done
