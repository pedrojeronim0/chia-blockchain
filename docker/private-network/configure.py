from __future__ import annotations

import hashlib
import os
from pathlib import Path

from chia.cmds.init_funcs import check_keys
from chia.util.config import load_config, save_config
from chia.util.keychain import Keychain, generate_mnemonic


def main() -> None:
    root = Path(os.environ["CHIA_ROOT"])
    role = os.environ["ROLE"]
    config = load_config(root, "config.yaml")
    network = "private-docker"
    challenge = hashlib.sha256(b"chia-private-docker-v1").hexdigest()
    config["network_overrides"]["constants"][network] = {
        "GENESIS_CHALLENGE": challenge,
        "AGG_SIG_ME_ADDITIONAL_DATA": challenge,
        "MIN_PLOT_SIZE_V1": 19,
        "DIFFICULTY_CONSTANT_FACTOR": 33554432,
        "DIFFICULTY_STARTING": 1024,
        "SUB_SLOT_ITERS_STARTING": 1048576,
        "NUMBER_ZERO_BITS_PLOT_FILTER_V1": 1,
        "TESTNET": True,
        "HARD_FORK_HEIGHT": 0,
    }
    config["network_overrides"]["config"][network] = {
        "address_prefix": "txch",
        "default_full_node_port": 8444,
    }
    config["selected_network"] = network
    # Keep localhost for daemon/RPC and local harvester connections. P2P
    # listeners bind independently to all interfaces in ChiaServer.
    config["self_hostname"] = "localhost"
    for section in config.values():
        if isinstance(section, dict) and "selected_network" in section:
            section["selected_network"] = network
            section["network_overrides"] = config["network_overrides"]
    config["logging"]["log_stdout"] = True
    config["logging"]["log_level"] = "INFO"
    node = config["full_node"]
    node["enable_upnp"] = False
    node["dns_servers"] = []
    node["introducer_peer"] = None
    node_name = os.environ.get("NODE_NAME", "timelord")
    experiment = os.environ.get("KOLLAPS_UUID")
    node["full_node_peers"] = [
        {"host": f"{host}-{experiment}" if experiment else host, "port": 8444}
        for host in ("timelord", "farmer-1", "farmer-2", "farmer-3")
        if host != node_name
    ]
    config["farmer"]["full_node_peers"] = [{"host": "localhost", "port": 8444}]
    config["farmer"]["solver_peers"] = []
    config["wallet"]["full_node_peers"] = [{"host": "localhost", "port": 8444}]
    config["wallet"]["connect_to_unknown_peers"] = False
    config["wallet"]["dns_servers"] = []
    config["wallet"]["introducer_peer"] = None
    config["timelord"]["full_node_peers"] = [{"host": "localhost", "port": 8444}]
    config["timelord"]["bluebox_mode"] = False
    config["timelord_launcher"]["process_count"] = 3
    config["harvester"]["plot_directories"] = ["/data/plots/k19"]
    save_config(root, "config.yaml", config)
    if role == "farmer":
        keychain = Keychain()
        if not keychain.get_all_private_keys():
            keychain.add_key(generate_mnemonic())
        check_keys(root)


if __name__ == "__main__":
    main()
