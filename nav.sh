#!/bin/sh
# nav.sh <left|down|up|right> — what Ctrl-H/J/K/L run (see config.toml).
#
# Herdr runs keybindings before a popup sees the key, so while the Go To picker
# is open Ctrl-K/Ctrl-J would move pane focus behind it. Here: if the picker is
# running, Ctrl-K/J move its selection (SIGUSR1 up, SIGUSR2 down); otherwise,
# and always for H/L, do what the key did before: vim-herdr-navigation when it
# is installed, else Herdr's own pane focus.

dir="${1:?usage: nav.sh <left|down|up|right>}"
pidfile="${XDG_CACHE_HOME:-$HOME/.cache}/herdr-goto/picker.pid"

if [ "$dir" = up ] || [ "$dir" = down ]; then
  pid=$(cat "$pidfile" 2>/dev/null)
  # Only signal a live picker: a stale pid reused by another process must not get USR1/2.
  if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -q 'goto\.py'; then
    if [ "$dir" = up ]; then kill -USR1 "$pid"; else kill -USR2 "$pid"; fi
    exit 0
  fi
fi

# vim-herdr-navigation's own script, newest install first; it reads HERDR_PANE_ID,
# which this action got from the same keybinding context.
script=$(ls -dt "$HOME"/.config/herdr/plugins/github-installations/vim-herdr-navigation-*/*/checkout/navigate.sh 2>/dev/null | head -n 1)
if [ -n "$script" ]; then
  exec bash "$script" "$dir"
fi
herdr="${HERDR_BIN_PATH:-herdr}"
"$herdr" plugin action invoke "$dir" --plugin vim-herdr-navigation >/dev/null 2>&1 ||
  exec "$herdr" pane focus --direction "$dir" ${HERDR_PANE_ID:+--pane "$HERDR_PANE_ID"}
