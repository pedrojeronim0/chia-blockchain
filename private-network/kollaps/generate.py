from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


def read_nodes(topology: Path) -> dict[str, str]:
    """Read one explicitly named Chia container per service from the original XML."""
    services = ET.parse(topology).getroot().find("services")
    if services is None:
        raise ValueError("Topology requires services")
    nodes = {}
    names = set()
    for service in services:
        name = service.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or name in names:
            raise ValueError(f"Invalid or duplicate service name: {name!r}")
        names.add(name)
        if service.get("supervisor") == "true":
            continue
        try:
            command = ast.literal_eval(service.get("command", ""))
        except (ValueError, SyntaxError) as error:
            raise ValueError(f"Expected command=['role', '{name}'] for {name}") from error
        if (
            not isinstance(command, list)
            or len(command) != 2
            or not isinstance(command[0], str)
            or command[0] not in {"farmer", "timelord", "full_node"}
            or command[1] != name
        ):
            raise ValueError(f"Expected a Chia role and matching node name in command for {name}")
        if service.get("replicas", "1") != "1":
            raise ValueError(f"Declare each Chia node separately; replicas must be 1 for {name}")
        nodes[name] = command[0]
    if not nodes:
        raise ValueError("Topology contains no Chia nodes")
    return nodes


def adapt_stack(stack: dict, plot_count: int, nodes: dict[str, str]) -> dict:
    """Preserve Kollaps' UUID, labels, paused entrypoints and DNSRR endpoints."""
    if plot_count < 1:
        raise ValueError("Plot count must be positive")
    services = stack["services"]
    stack["version"] = "3.8"
    found = set()
    stack["volumes"] = {
        f"{node}-data": {"external": True, "name": f"chia-kollaps-{node}-data"} for node in nodes
    }
    for service in services.values():
        service["environment"] = {key: str(value) for key, value in service.get("environment", {}).items()}
        # Host-local volumes require a single manager. The supplied integration
        # targets one Swarm host, with one container/IP per Chia node.
        service.setdefault("deploy", {})["placement"] = {"constraints": ["node.role == manager"]}
        if service.get("hostname") in nodes:
            node = service["hostname"]
            if node in found:
                raise ValueError(f"Multiple generated services for {node}")
            found.add(node)
            if service["deploy"]["replicas"] != 1 or service["deploy"]["endpoint_mode"] != "dnsrr":
                raise ValueError(f"Expected one DNSRR task for {node}")
            service["environment"]["PLOT_COUNT"] = str(plot_count)
            service["environment"]["CHIA_NODES"] = json.dumps(nodes, sort_keys=True)
            service["volumes"] = [f"{node}-data:/data"]
            service["stop_grace_period"] = "60s"
    if found != set(nodes):
        raise ValueError(f"Expected {len(nodes)} Chia services, found {sorted(found)}")
    # Normalize the upstream generator's legacy external-network syntax.
    stack["networks"]["KollapsNet"] = {"external": True, "name": "kollaps_network"}
    return stack


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a persistent Chia/Kollaps Swarm stack")
    parser.add_argument("topology", type=Path)
    parser.add_argument("--plots", type=int, default=32)
    parser.add_argument("--list-volumes", action="store_true", help="Print persistent volume names without generating a stack")
    args = parser.parse_args()
    if args.plots < 1:
        parser.error("--plots must be positive")
    topology = args.topology.resolve()
    nodes = read_nodes(topology)
    if args.list_volumes:
        print("\n".join(f"chia-kollaps-{name}-data" for name in nodes))
        return
    result = subprocess.run(
        ["KollapsDeploymentGenerator", str(topology), "-s"], check=True, capture_output=True, text=True
    )
    print(result.stderr, file=sys.stderr, end="")
    print(yaml.safe_dump(adapt_stack(yaml.safe_load(result.stdout), args.plots, nodes), sort_keys=False), end="")


if __name__ == "__main__":
    main()
