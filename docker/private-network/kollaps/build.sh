#!/usr/bin/env bash
set -euo pipefail

kollaps_source=${1:?Usage: bash build.sh /path/to/Kollaps}
kollaps_source=$(realpath "$kollaps_source")
integration_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$integration_dir/../../.." && pwd)

git -C "$kollaps_source" submodule status --recursive
if git -C "$kollaps_source" submodule status --recursive | rg -q '^[-+U]'; then
  echo "Initialize the submodules at their recorded commits before building" >&2
  exit 1
fi
docker build -f "$repo_root/docker/private-network/Dockerfile" -t chia-private:local "$repo_root"
docker build -f "$integration_dir/Dockerfile" -t chia-private-kollaps:local "$repo_root"
docker build -f "$kollaps_source/dockerfiles/Kollaps" -t kollaps:2.0 "$kollaps_source"
docker build -f "$kollaps_source/dockerfiles/DeploymentGenerator" -t kollaps-deployment-generator:2.0 "$kollaps_source"
docker build -f "$kollaps_source/examples/utils/dashboard/Dockerfile" \
  -t kollaps/dashboard:1.0 "$kollaps_source/examples/utils/dashboard"
echo "Kollaps revision: $(git -C "$kollaps_source" rev-parse HEAD)"
