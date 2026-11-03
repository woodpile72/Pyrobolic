#!/bin/bash
set -e
run_home=$(mktemp -d /tmp/pyrobolic-ui.XXXXXX)
mkdir -m 700 "$run_home/runtime"
export HOME="$run_home" XDG_RUNTIME_DIR="$run_home/runtime" XDG_CONFIG_HOME="$run_home/config" XDG_DATA_HOME="$run_home/data" XDG_CACHE_HOME="$run_home/cache" XDG_STATE_HOME="$run_home/state" GTK_A11Y=none GSK_RENDERER=cairo
unset GTK_THEME
exec xvfb-run -a -s '-screen 0 1280x1000x24' dbus-run-session -- bash -c '
if [ "$1" = "wm" ]; then
 metacity --sm-disable >"$HOME/wm.log" 2>&1 & wm_pid=$!
 trap '\''kill "$wm_pid" 2>/dev/null || true'\'' EXIT
fi
shift
exec_args=("$@")
"${exec_args[@]}"
' bash "$@"
