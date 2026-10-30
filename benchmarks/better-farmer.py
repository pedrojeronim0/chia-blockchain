from chia.farmer.start_farmer import SERVICE_NAME, Service, create_farmer_service
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint8
from chia.util.config import load_config, load_config_cli
from chia.util.chia_logging import initialize_service_logging
from chia.consensus.default_constants import DEFAULT_CONSTANTS, update_testnet_overrides
from chia.server.signal_handlers import SignalHandlers
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.simulator.block_tools import BlockTools
from chia.util.keychain import Keychain
import os
import pathlib
from chia.server.start_service import RpcInfo, Service, async_run
import asyncio

async def main(root_path: pathlib.Path) -> int:
    config = load_config(root_path, "config.yaml", fill_missing_services=True)
    service_config = load_config_cli(root_path, "config.yaml", SERVICE_NAME)
    config[SERVICE_NAME] = service_config
    config_pool = load_config_cli(root_path, "config.yaml", "pool")
    config["pool"] = config_pool
    initialize_service_logging(service_name=SERVICE_NAME, config=config, root_path=root_path)
    keychain = Keychain()
    all_keys = keychain.get_all_private_keys()

    service = create_farmer_service(root_path, config, config_pool, DEFAULT_CONSTANTS)
    bt = BlockTools(
        DEFAULT_CONSTANTS,
        root_path,
        config_overrides={"simulator": {"use_current_time": True}},
        plot_dir="/home/franciscomartins/.chia/testing/plots",
    )
    if all_keys:
        farmer_sk = all_keys[0][0]  # Use the key that matches your plots
        farmer_pk = farmer_sk.get_g1()
        bt.farmer_sk = farmer_sk
        bt.farmer_pk = farmer_pk
        bt.plot_manager.set_public_keys([farmer_pk], [])
    print("something")

    bt.add_plot_directory(pathlib.Path("/home/franciscomartins/.chia/testing/plots"))

    # Trigger the PlotManager to scan for plots
    await bt.refresh_plots()
    print(all_keys)
    plot_infos = list(bt.plot_manager.plots.values())
    print(plot_infos)
    sp = bt.create_signage_point(peak_height=uint32(10), signage_point_index=uint8(0))
    await service._api.new_signage_point(sp)
    
    return 0
async_run(main(pathlib.Path("/home/franciscomartins/.chia/mainnet")))
