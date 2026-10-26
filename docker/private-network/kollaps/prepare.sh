#!/usr/bin/env bash
set -euo pipefail

integration_dir=$(cd -- "$(dirname -- "$0")" && pwd)
plot_count=${PLOT_COUNT:-32}
if [[ ! "$plot_count" =~ ^[1-9][0-9]*$ ]]; then
  echo "PLOT_COUNT must be a positive integer" >&2
  exit 1
fi
if [[ $(docker info --format '{{.Swarm.LocalNodeState}}') != active || \
      $(docker info --format '{{.Swarm.ControlAvailable}}') != true ]]; then
  echo "Run this on a Swarm manager; initialize a dedicated host with docker swarm init" >&2
  exit 1
fi
if [[ $(docker node ls --format '{{.ID}}' | wc -l) -ne 1 ]]; then
  echo "This integration currently requires a single-node Swarm" >&2
  exit 1
fi
if docker stack services chia-kollaps --format '{{.Name}}' 2>/dev/null | rg -q .; then
  echo "Remove the existing chia-kollaps stack before preparing another experiment" >&2
  exit 1
fi
if ! docker network inspect kollaps_network >/dev/null 2>&1; then
  # Let Docker choose a non-overlapping subnet.
  docker network create --driver overlay --attachable --internal kollaps_network
fi
if [[ $(docker network inspect kollaps_network --format '{{.Driver}}') != overlay || \
      $(docker network inspect kollaps_network --format '{{.Internal}}') != true ]]; then
  echo "kollaps_network must be an internal overlay network" >&2
  exit 1
fi
for node in timelord farmer-1 farmer-2 farmer-3; do
  docker volume create "chia-kollaps-$node-data" >/dev/null
done
docker volume create chia-kollaps-network-ca >/dev/null

# Provision the timelord's CA before all four applications start concurrently.
docker run --rm --network none --entrypoint bash \
  -v chia-kollaps-timelord-data:/data -v chia-kollaps-network-ca:/network-ca \
  chia-private-kollaps:local -c '
    set -euo pipefail
    if [[ ! -f "$CHIA_ROOT/config/config.yaml" ]]; then chia init; fi
    cp "$CHIA_ROOT/config/ssl/ca/private_ca.crt" /network-ca/
    cp "$CHIA_ROOT/config/ssl/ca/private_ca.key" /network-ca/
    chmod 600 /network-ca/private_ca.key
  '

generated_stack=$(mktemp "$integration_dir/stack.XXXXXX")
trap 'rm -f "$generated_stack"' EXIT
docker run --rm --entrypoint python3 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$integration_dir:$integration_dir:ro" \
  kollaps-deployment-generator:2.0 \
  "$integration_dir/generate.py" "$integration_dir/topology.xml" --plots "$plot_count" > "$generated_stack"
docker stack config -c "$generated_stack" >/dev/null
mv "$generated_stack" "$integration_dir/stack.yaml"
echo "Prepared $integration_dir/stack.yaml; ready for docker stack deploy"
