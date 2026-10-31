"""Benchmark farmer/harvester signage point handling.

Feeds three batches of signage points through the real farmer and harvester
code paths:

* ``no_results``: no plot passes the plot filter
* ``filtered_no_proof``: at least one plot passes the plot filter, but no proof
  is produced
* ``proof``: at least one plot passes the plot filter and a proof is produced

The farmer and harvester are created directly, without starting any services or
opening any network connections.  A fake peer is used for the harvester and
farmer protocol messages.

Run with::

    python benchmarks/better-farmer.py
"""

from __future__ import annotations

import asyncio
import pathlib
import random
import statistics
import time
from dataclasses import dataclass

from chia_rs import ConsensusConstants
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint32, uint64

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.farmer.farmer_api import FarmerAPI
from chia.farmer.start_farmer import SERVICE_NAME, create_farmer_service
from chia.harvester.harvester import Harvester
from chia.harvester.harvester_api import HarvesterAPI
from chia.plotting.util import PlotInfo
from chia.protocols import farmer_protocol, harvester_protocol
from chia.protocols.farmer_protocol import FarmingInfo
from chia.protocols.outbound_message import Message
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.server.start_service import async_run
from chia.util.chia_logging import initialize_service_logging
from chia.util.config import load_config, load_config_cli
from chia.util.harvester_config import add_plot_directory
from chia.util.hash import std_hash
from chia.util.keychain import Keychain
from chia.wallet.derive_keys import master_sk_to_farmer_sk, master_sk_to_pool_sk

BUCKET_SIZE = 100
MAX_ATTEMPTS = 2_000_000
CHALLENGE_HASH = std_hash(b"better-farmer benchmark challenge")
REWARD_CHAIN_SP = std_hash(b"better-farmer benchmark reward chain sp")
PEAK_HEIGHT = uint32(10)
LAST_TX_HEIGHT = uint32(9)
SIGNAGE_POINT_INDEX = uint8(0)
# Large enough that any quality is eligible with this network's difficulty.
PROOF_SUB_SLOT_ITERS = uint64(2**50)

BUCKETS = ("no_results", "filtered_no_proof", "proof")


class FakePeer:
    """Minimal peer that records messages instead of sending them over a socket."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.peer_node_id = bytes32.zeros

    async def send_message(self, message: Message) -> None:
        self.messages.append(message)


@dataclass
class Sample:
    bucket: str
    sp_hash: str
    difficulty: int
    sub_slot_iters: int
    passed: int
    proofs: int
    lookup_us: int
    farmer_sp_s: float
    harvester_s: float
    farmer_proof_s: float
    total_s: float


def make_harvester_sp(
    sp_hash: bytes32, difficulty: uint64, sub_slot_iters: uint64
) -> harvester_protocol.NewSignagePointHarvester2:
    return harvester_protocol.NewSignagePointHarvester2(
        challenge_hash=CHALLENGE_HASH,
        difficulty=difficulty,
        sub_slot_iters=sub_slot_iters,
        signage_point_index=SIGNAGE_POINT_INDEX,
        sp_hash=sp_hash,
        pool_difficulties=[],
        peak_height=PEAK_HEIGHT,
        last_tx_height=LAST_TX_HEIGHT,
        filter_challenge=CHALLENGE_HASH,
    )


def make_farmer_sp(
    sp_hash: bytes32, difficulty: uint64, sub_slot_iters: uint64
) -> farmer_protocol.NewSignagePoint:
    return farmer_protocol.NewSignagePoint(
        challenge_hash=CHALLENGE_HASH,
        challenge_chain_sp=sp_hash,
        reward_chain_sp=REWARD_CHAIN_SP,
        difficulty=difficulty,
        sub_slot_iters=sub_slot_iters,
        signage_point_index=SIGNAGE_POINT_INDEX,
        peak_height=PEAK_HEIGHT,
        last_tx_height=LAST_TX_HEIGHT,
        filter_challenge=CHALLENGE_HASH,
    )


def any_plot_passes_filter(
    harvester_api: HarvesterAPI, plot_infos: list[PlotInfo], sp_hash: bytes32
) -> bool:
    # The filter does not depend on difficulty or sub_slot_iters; use arbitrary
    # values for the probe.
    probe = make_harvester_sp(sp_hash, uint64(1), PROOF_SUB_SLOT_ITERS)
    return any(harvester_api._plot_passes_filter(plot_info, probe) for plot_info in plot_infos)


def classify(info: FarmingInfo) -> str:
    if info.passed == 0:
        return "no_results"
    if info.proofs == 0:
        return "filtered_no_proof"
    return "proof"


async def run_sample(
    farmer_api: FarmerAPI,
    harvester_api: HarvesterAPI,
    harvester_peer: FakePeer,
    farmer_peer: FakePeer,
    sp_hash: bytes32,
    difficulty: uint64,
    sub_slot_iters: uint64,
) -> Sample:
    farmer_sp = make_farmer_sp(sp_hash, difficulty, sub_slot_iters)
    harvester_sp = make_harvester_sp(sp_hash, difficulty, sub_slot_iters)

    farmer_sp_start = time.perf_counter()
    await farmer_api.new_signage_point(farmer_sp)
    farmer_sp_end = time.perf_counter()

    harvester_peer.messages.clear()
    harvester_start = time.perf_counter()
    await harvester_api.new_signage_point_harvester(harvester_sp, harvester_peer)
    harvester_end = time.perf_counter()

    farming_info: FarmingInfo | None = None
    proofs: list[harvester_protocol.NewProofOfSpace] = []
    for message in harvester_peer.messages:
        if message.type == ProtocolMessageTypes.farming_info.value:
            farming_info = FarmingInfo.from_bytes(message.data)
        elif message.type == ProtocolMessageTypes.new_proof_of_space.value:
            proofs.append(harvester_protocol.NewProofOfSpace.from_bytes(message.data))

    if farming_info is None:
        raise RuntimeError(f"harvester did not send farming_info for SP {sp_hash.hex()}")

    farmer_proof_start = time.perf_counter()
    for proof in proofs:
        await farmer_api.new_proof_of_space(proof, farmer_peer)
    farmer_proof_end = time.perf_counter()

    return Sample(
        bucket=classify(farming_info),
        sp_hash=sp_hash.hex(),
        difficulty=int(difficulty),
        sub_slot_iters=int(sub_slot_iters),
        passed=int(farming_info.passed),
        proofs=int(farming_info.proofs),
        lookup_us=int(farming_info.lookup_time),
        farmer_sp_s=farmer_sp_end - farmer_sp_start,
        harvester_s=harvester_end - harvester_start,
        farmer_proof_s=farmer_proof_end - farmer_proof_start,
        total_s=farmer_proof_end - farmer_sp_start,
    )


def percentile(values: list[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((percentage / 100.0) * (len(ordered) - 1))
    return ordered[index]


def print_report(buckets: dict[str, list[Sample]]) -> None:
    print()
    print(
        f"{'bucket':<20} {'n':>4} {'passed':>8} {'proofs':>8} "
        f"{'farmer-sp ms':>13} {'harvester ms':>13} {'farmer-proof ms':>16} {'total ms':>11}"
    )
    print("-" * 105)

    for bucket in BUCKETS:
        samples = buckets[bucket]
        if not samples:
            continue

        harvester_times = [sample.harvester_s * 1000 for sample in samples]
        total_times = [sample.total_s * 1000 for sample in samples]
        farmer_sp_times = [sample.farmer_sp_s * 1000 for sample in samples]
        farmer_proof_times = [sample.farmer_proof_s * 1000 for sample in samples if sample.proofs > 0]

        farmer_proof_text = f"{statistics.mean(farmer_proof_times):.3f}" if farmer_proof_times else "-"

        print(
            f"{bucket:<20} {len(samples):>4} "
            f"{statistics.mean([sample.passed for sample in samples]):>8.2f} "
            f"{statistics.mean([sample.proofs for sample in samples]):>8.2f} "
            f"{statistics.mean(farmer_sp_times):>13.4f} "
            f"{statistics.mean(harvester_times):>13.3f} "
            f"{farmer_proof_text:>16} "
            f"{statistics.mean(total_times):>11.3f}"
        )

        print(
            f"{'':<20} {'':>4} {'':>8} {'':>8} "
            f"median={statistics.median(farmer_sp_times):.4f}  "
            f"median={statistics.median(harvester_times):.3f}  "
            f"p95={percentile(harvester_times, 95):.3f}  "
            f"p95={percentile(total_times, 95):.3f}"
        )


async def wait_for_plots(harvester: Harvester) -> None:
    harvester.plot_manager.trigger_refresh()
    harvester.plot_manager.start_refreshing(sleep_interval_ms=10)
    try:
        while harvester.plot_manager.needs_refresh():
            await asyncio.sleep(0.05)
    finally:
        harvester.plot_manager.stop_refreshing()


def find_filter_passing_sp(
    harvester_api: HarvesterAPI, plot_infos: list[PlotInfo], rng: random.Random
) -> bytes32:
    while True:
        sp_hash = bytes32(rng.randbytes(32))
        if any_plot_passes_filter(harvester_api, plot_infos, sp_hash):
            return sp_hash


async def warm_up(
    farmer_api: FarmerAPI,
    harvester_api: HarvesterAPI,
    harvester_peer: FakePeer,
    farmer_peer: FakePeer,
    plot_infos: list[PlotInfo],
    constants: ConsensusConstants,
    rng: random.Random,
) -> None:
    """Warm the plot cache before collecting measurements."""
    print("warming up plot cache...")
    for _ in range(3):
        sp_hash = find_filter_passing_sp(harvester_api, plot_infos, rng)
        await run_sample(
            farmer_api,
            harvester_api,
            harvester_peer,
            farmer_peer,
            sp_hash,
            constants.DIFFICULTY_STARTING,
            PROOF_SUB_SLOT_ITERS,
        )


async def main(root_path: pathlib.Path) -> int:
    config = load_config(root_path, "config.yaml", fill_missing_services=True)
    service_config = load_config_cli(root_path, "config.yaml", SERVICE_NAME)
    service_config.setdefault("logging", {})
    service_config["logging"]["log_stdout"] = True
    service_config["logging"]["log_level"] = "INFO"
    config[SERVICE_NAME] = service_config

    config_pool = load_config_cli(root_path, "config.yaml", "pool")
    config["pool"] = config_pool

    initialize_service_logging(service_name=SERVICE_NAME, config=config, root_path=root_path)

    keychain = Keychain()
    all_keys = keychain.get_all_private_keys()
    if not all_keys:
        print("No private keys found in the keychain; cannot match plots.")
        return 1

    farmer_pks = [master_sk_to_farmer_sk(sk).get_g1() for sk, _ in all_keys]
    pool_pks = [master_sk_to_pool_sk(sk).get_g1() for sk, _ in all_keys]

    plot_dir = root_path / "plots" / "final"
    plot_directories = config["harvester"].get("plot_directories") or []
    if str(plot_dir.resolve()) not in plot_directories:
        add_plot_directory(root_path, str(plot_dir))
        config["harvester"]["plot_directories"] = plot_directories + [str(plot_dir.resolve())]

    service = create_farmer_service(root_path, config, config_pool, DEFAULT_CONSTANTS, keychain=keychain)
    farmer_api = service._api
    assert farmer_api is not None
    constants = service._node.constants

    harvester = Harvester(root_path, config["harvester"], constants)
    harvester.plot_manager.set_public_keys(farmer_pks, pool_pks)
    harvester.plot_manager.set_refresh_callback(lambda event, result: None)

    await wait_for_plots(harvester)

    plot_infos = list(harvester.plot_manager.plots.values())
    print(f"loaded {len(plot_infos)} plot(s)")
    if not plot_infos:
        print("No plots loaded. The plot keys are probably not in the current keychain.")
        return 1

    print(
        f"constants: difficulty={constants.DIFFICULTY_STARTING}, "
        f"sub_slot_iters={constants.SUB_SLOT_ITERS_STARTING}, "
        f"plot_filter_bits={constants.NUMBER_ZERO_BITS_PLOT_FILTER_V1}"
    )

    harvester_api = HarvesterAPI(harvester)
    harvester_peer = FakePeer()
    farmer_peer = FakePeer()
    rng = random.Random(0)

    await warm_up(farmer_api, harvester_api, harvester_peer, farmer_peer, plot_infos, constants, rng)

    buckets: dict[str, list[Sample]] = {bucket: [] for bucket in BUCKETS}
    attempts = 0

    while any(len(samples) < BUCKET_SIZE for samples in buckets.values()):
        attempts += 1
        if attempts > MAX_ATTEMPTS:
            print(f"gave up after {MAX_ATTEMPTS} attempts")
            break

        sp_hash = bytes32(rng.randbytes(32))
        filter_passes = any_plot_passes_filter(harvester_api, plot_infos, sp_hash)

        if not filter_passes:
            if len(buckets["no_results"]) >= BUCKET_SIZE:
                continue
            difficulty = constants.DIFFICULTY_STARTING
            sub_slot_iters = constants.SUB_SLOT_ITERS_STARTING
        else:
            need_no_proof = len(buckets["filtered_no_proof"]) < BUCKET_SIZE
            need_proof = len(buckets["proof"]) < BUCKET_SIZE

            if need_no_proof and (not need_proof or rng.random() < 0.5):
                difficulty = constants.DIFFICULTY_STARTING
                sub_slot_iters = constants.SUB_SLOT_ITERS_STARTING
            elif need_proof:
                difficulty = constants.DIFFICULTY_STARTING
                sub_slot_iters = PROOF_SUB_SLOT_ITERS
            else:
                continue

        sample = await run_sample(
            farmer_api,
            harvester_api,
            harvester_peer,
            farmer_peer,
            sp_hash,
            difficulty,
            sub_slot_iters,
        )

        if len(buckets[sample.bucket]) < BUCKET_SIZE:
            buckets[sample.bucket].append(sample)
            collected = sum(len(samples) for samples in buckets.values())
            if collected % 10 == 0:
                print(
                    f"collected {collected}/{BUCKET_SIZE * len(BUCKETS)} "
                    f"(no_results={len(buckets['no_results'])}, "
                    f"filtered_no_proof={len(buckets['filtered_no_proof'])}, "
                    f"proof={len(buckets['proof'])})"
                )

    harvester.executor.shutdown(wait=True)

    print(f"\nattempts: {attempts}")
    print_report(buckets)
    return 0


if __name__ == "__main__":
    async_run(main(pathlib.Path("/home/franciscomartins/.chia/my_testnet")))
