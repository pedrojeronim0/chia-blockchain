#!/usr/bin/env bash
set -euo pipefail

integration_dir=$(cd -- "$(dirname -- "$0")" && pwd)
plot_count=${PLOT_COUNT:-32}
if [[ -v FARMER_COUNT || -v FULL_NODE_COUNT ]]; then
  echo "Define nodes in topology.xml; FARMER_COUNT and FULL_NODE_COUNT are no longer supported" >&2
  exit 1
fi
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
if docker stack services chia-kollaps --format '{{.Name}}' 2>/dev/null | grep -q .; then
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
volume_names=$(docker run --rm --entrypoint python3 \
  -v "$integration_dir:$integration_dir:ro" \
  kollaps-deployment-generator:2.0 \
  "$integration_dir/generate.py" "$integration_dir/topology.xml" --list-volumes)
while IFS= read -r volume; do
  docker volume create "$volume" >/dev/null
done <<< "$volume_names"

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
