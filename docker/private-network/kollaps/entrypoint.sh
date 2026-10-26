#!/usr/bin/env bash
set -euo pipefail

export ROLE=${1:?Expected role: timelord or farmer}
export NODE_NAME=${2:?Expected node name}
: "${KOLLAPS_UUID:?Kollaps must provide an experiment UUID}"

# Swarm starts services concurrently. prepare.sh provisions this CA before
# deployment so every node can import the same certificates at startup.
if [[ ! -s /network-ca/private_ca.crt || ! -s /network-ca/private_ca.key ]]; then
  echo "Missing experiment CA; run prepare.sh before deploying the stack" >&2
  exit 1
fi

exec bash /app/docker/private-network/entrypoint.sh
