from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

NODES = {"timelord", "farmer-1", "farmer-2", "farmer-3"}


def adapt_stack(stack: dict, plot_count: int) -> dict:
    """Preserve Kollaps' UUID, labels, paused entrypoints and DNSRR endpoints."""
    services = stack["services"]
    stack["version"] = "3.8"
    found = set()
    stack["volumes"] = {
        f"{node}-data": {"external": True, "name": f"chia-kollaps-{node}-data"} for node in NODES
    }
    stack["volumes"]["network-ca"] = {"external": True, "name": "chia-kollaps-network-ca"}
    for service in services.values():
        service["environment"] = {key: str(value) for key, value in service.get("environment", {}).items()}
        # Host-local volumes require a single manager. The supplied integration
        # targets one Swarm host, with one container/IP per Chia node.
        service.setdefault("deploy", {})["placement"] = {"constraints": ["node.role == manager"]}
        if service.get("hostname") in NODES:
            node = service["hostname"]
            found.add(node)
            if service["deploy"]["replicas"] != 1 or service["deploy"]["endpoint_mode"] != "dnsrr":
                raise ValueError(f"Expected one DNSRR task for {node}")
            service["environment"]["PLOT_COUNT"] = str(plot_count)
            service["volumes"] = [
                f"{node}-data:/data",
                "network-ca:/network-ca" + ("" if node == "timelord" else ":ro"),
            ]
            service["stop_grace_period"] = "60s"
    if found != NODES:
        raise ValueError(f"Expected exactly four Chia services, found {sorted(found)}")
    # Normalize the upstream generator's legacy external-network syntax.
    stack["networks"]["KollapsNet"] = {"external": True, "name": "kollaps_network"}
    return stack


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a persistent Chia/Kollaps Swarm stack")
    parser.add_argument("topology", type=Path)
    parser.add_argument("--plots", type=int, default=32)
    args = parser.parse_args()
    if args.plots < 1:
        parser.error("--plots must be positive")
    topology = args.topology.resolve()
    result = subprocess.run(
        ["KollapsDeploymentGenerator", str(topology), "-s"], check=True, capture_output=True, text=True
    )
    print(result.stderr, file=sys.stderr, end="")
    print(yaml.safe_dump(adapt_stack(yaml.safe_load(result.stdout), args.plots), sort_keys=False), end="")


if __name__ == "__main__":
    main()
