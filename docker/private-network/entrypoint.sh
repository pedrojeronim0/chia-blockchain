#!/usr/bin/env bash
set -euo pipefail

export ROLE=${1:?Expected role: timelord or farmer}
export NODE_NAME=${2:?Expected node name}
: "${KOLLAPS_UUID:?Kollaps must provide an experiment UUID}"

mkdir -p "$CHIA_ROOT" /data/plots/k19 /data/tmp
if [[ ! -f "$CHIA_ROOT/config/config.yaml" ]]; then
  # Each node keeps its own private CA and service certificates in /data.
  chia init
fi
python /app/docker/private-network/configure.py

if [[ "$ROLE" == farmer ]]; then
  plot_count=${PLOT_COUNT:-32}
  if [[ ! "$plot_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "PLOT_COUNT must be a positive integer" >&2
    exit 1
  fi
  shopt -s nullglob
  plots=(/data/plots/k19/plot-k19-*.plot)
  missing=$((plot_count - ${#plots[@]}))
  if ((missing > 0)); then
    python /app/docker/private-network/plot.py "$missing"
  fi
fi

chia run_daemon &
daemon_pid=$!
cleanup() {
  chia stop all -d || true
  wait "$daemon_pid" || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT

# chia start waits for the daemon to become available.
case "$ROLE" in
  timelord) chia start timelord ;;
  farmer) chia start farmer ;;
  *) echo "Unknown ROLE: $ROLE" >&2; exit 1 ;;
esac
wait "$daemon_pid"
