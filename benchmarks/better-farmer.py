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

Signage points are expensive to find, so they are stored in a JSON file and
replayed on later runs.  The first run (or ``--discover``) generates the store;
subsequent runs load it and skip all searching.  Use ``--bucket`` to benchmark a
single bucket.

Examples::

    # discover the default number of SPs per bucket and save them
    python benchmarks/better-farmer.py --discover

    # replay the stored SPs
    python benchmarks/better-farmer.py

    # replay only the proof bucket, four times over
    python benchmarks/better-farmer.py --bucket proof --samples 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

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

DEFAULT_ROOT = pathlib.Path("/home/franciscomartins/.chia/my_testnet")
DEFAULT_BUCKET_SIZE = 100
DEFAULT_WARMUP = 3
MAX_ATTEMPTS = 2_000_000
STORE_VERSION = 1
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
class SignagePointSpec:
    """Everything needed to replay one signage point, plus its observed result."""

    bucket: str
    sp_hash: bytes32
    difficulty: uint64
    sub_slot_iters: uint64
    challenge_hash: bytes32 = CHALLENGE_HASH
    reward_chain_sp: bytes32 = REWARD_CHAIN_SP
    peak_height: uint32 = PEAK_HEIGHT
    last_tx_height: uint32 = LAST_TX_HEIGHT
    signage_point_index: uint8 = SIGNAGE_POINT_INDEX
    # Recorded during discovery; used to sanity-check replays.
    passed: int | None = None
    proofs: int | None = None


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


def spec_to_dict(spec: SignagePointSpec) -> dict[str, Any]:
    return {
        "bucket": spec.bucket,
        "sp_hash": spec.sp_hash.hex(),
        "difficulty": int(spec.difficulty),
        "sub_slot_iters": int(spec.sub_slot_iters),
        "challenge_hash": spec.challenge_hash.hex(),
        "reward_chain_sp": spec.reward_chain_sp.hex(),
        "peak_height": int(spec.peak_height),
        "last_tx_height": int(spec.last_tx_height),
        "signage_point_index": int(spec.signage_point_index),
        "passed": spec.passed,
        "proofs": spec.proofs,
    }


def spec_from_dict(data: dict[str, Any]) -> SignagePointSpec:
    return SignagePointSpec(
        bucket=str(data["bucket"]),
        sp_hash=bytes32(bytes.fromhex(data["sp_hash"])),
        difficulty=uint64(data["difficulty"]),
        sub_slot_iters=uint64(data["sub_slot_iters"]),
        challenge_hash=bytes32(bytes.fromhex(data["challenge_hash"])),
        reward_chain_sp=bytes32(bytes.fromhex(data["reward_chain_sp"])),
        peak_height=uint32(data["peak_height"]),
        last_tx_height=uint32(data["last_tx_height"]),
        signage_point_index=uint8(data["signage_point_index"]),
        passed=None if data.get("passed") is None else int(data["passed"]),
        proofs=None if data.get("proofs") is None else int(data["proofs"]),
    )


def save_store(
    path: pathlib.Path,
    network: str,
    buckets: dict[str, list[SignagePointSpec]],
) -> None:
    """Atomically write the SP store so an interrupted discovery is still usable."""
    payload: dict[str, Any] = {
        "version": STORE_VERSION,
        "network": network,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "challenge_hash": CHALLENGE_HASH.hex(),
        "reward_chain_sp": REWARD_CHAIN_SP.hex(),
        "buckets": {bucket: [spec_to_dict(spec) for spec in specs] for bucket, specs in buckets.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    tmp_path.replace(path)


def load_store(path: pathlib.Path) -> dict[str, list[SignagePointSpec]] | None:
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(f"could not read SP store {path}: {error}")
        return None

    if payload.get("version") != STORE_VERSION:
        print(f"ignoring SP store {path}: unsupported version {payload.get('version')!r}")
        return None

    try:
        stored = payload.get("buckets", {})
        return {bucket: [spec_from_dict(entry) for entry in stored.get(bucket, [])] for bucket in BUCKETS}
    except (KeyError, TypeError, ValueError) as error:
        print(f"ignoring malformed SP store {path}: {error}")
        return None


def make_harvester_sp(spec: SignagePointSpec) -> harvester_protocol.NewSignagePointHarvester2:
    return harvester_protocol.NewSignagePointHarvester2(
        challenge_hash=spec.challenge_hash,
        difficulty=spec.difficulty,
        sub_slot_iters=spec.sub_slot_iters,
        signage_point_index=spec.signage_point_index,
        sp_hash=spec.sp_hash,
        pool_difficulties=[],
        peak_height=spec.peak_height,
        last_tx_height=spec.last_tx_height,
        filter_challenge=spec.challenge_hash,
    )


def make_farmer_sp(spec: SignagePointSpec) -> farmer_protocol.NewSignagePoint:
    return farmer_protocol.NewSignagePoint(
        challenge_hash=spec.challenge_hash,
        challenge_chain_sp=spec.sp_hash,
        reward_chain_sp=spec.reward_chain_sp,
        difficulty=spec.difficulty,
        sub_slot_iters=spec.sub_slot_iters,
        signage_point_index=spec.signage_point_index,
        peak_height=spec.peak_height,
        last_tx_height=spec.last_tx_height,
        filter_challenge=spec.challenge_hash,
    )


def any_plot_passes_filter(
    harvester_api: HarvesterAPI, plot_infos: list[PlotInfo], sp_hash: bytes32
) -> bool:
    # The filter does not depend on difficulty or sub_slot_iters; use arbitrary
    # values for the probe.
    probe = SignagePointSpec(
        bucket="probe",
        sp_hash=sp_hash,
        difficulty=uint64(1),
        sub_slot_iters=PROOF_SUB_SLOT_ITERS,
    )
    return any(harvester_api._plot_passes_filter(plot_info, make_harvester_sp(probe)) for plot_info in plot_infos)


def classify(info: FarmingInfo) -> str:
    if info.passed == 0:
        return "no_results"
    if info.proofs == 0:
        return "filtered_no_proof"
    return "proof"


def reset_farmer_state(farmer_api: FarmerAPI) -> None:
    """Make every signage point look new to the farmer.

    Replaying the same stored SP must not trip the duplicate check or the
    testnet ``max_pos_per_sp`` limit, otherwise the farmer timings would not be
    comparable between samples.
    """
    farmer = farmer_api.farmer
    farmer.sps.clear()
    farmer.proofs_of_space.clear()
    farmer.quality_str_to_identifiers.clear()
    farmer.pending_solver_requests.clear()
    farmer.number_of_responses.clear()
    farmer.cache_add_time.clear()


async def run_sample(
    farmer_api: FarmerAPI,
    harvester_api: HarvesterAPI,
    harvester_peer: FakePeer,
    farmer_peer: FakePeer,
    spec: SignagePointSpec,
) -> Sample:
    reset_farmer_state(farmer_api)
    farmer_sp = make_farmer_sp(spec)
    harvester_sp = make_harvester_sp(spec)

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
        raise RuntimeError(f"harvester did not send farming_info for SP {spec.sp_hash.hex()}")

    farmer_proof_start = time.perf_counter()
    for proof in proofs:
        await farmer_api.new_proof_of_space(proof, farmer_peer)
    farmer_proof_end = time.perf_counter()

    return Sample(
        bucket=classify(farming_info),
        sp_hash=spec.sp_hash.hex(),
        difficulty=int(spec.difficulty),
        sub_slot_iters=int(spec.sub_slot_iters),
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


def print_report(results: dict[str, list[Sample]]) -> None:
    print()
    print(
        f"{'bucket':<20} {'n':>4} {'passed':>8} {'proofs':>8} "
        f"{'farmer-sp ms':>13} {'harvester ms':>13} {'farmer-proof ms':>16} {'total ms':>11}"
    )
    print("-" * 105)

    for bucket in BUCKETS:
        samples = results.get(bucket)
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


async def discover_signage_points(
    farmer_api: FarmerAPI,
    harvester_api: HarvesterAPI,
    harvester_peer: FakePeer,
    farmer_peer: FakePeer,
    plot_infos: list[PlotInfo],
    constants: ConsensusConstants,
    rng: random.Random,
    samples_per_bucket: int,
    store_path: pathlib.Path,
    network: str,
    wanted: list[str],
    known: dict[str, list[SignagePointSpec]] | None = None,
    force: bool = False,
) -> dict[str, list[SignagePointSpec]]:
    """Search for signage points until every wanted bucket has ``samples_per_bucket`` entries."""
    buckets: dict[str, list[SignagePointSpec]] = {
        bucket: list((known or {}).get(bucket, []))[:samples_per_bucket] for bucket in BUCKETS
    }
    if force:
        # Keep the other buckets, but replace the ones we were asked to rediscover.
        for bucket in wanted:
            buckets[bucket] = []
    attempts = 0

    while any(len(buckets[bucket]) < samples_per_bucket for bucket in wanted):
        attempts += 1
        if attempts > MAX_ATTEMPTS:
            print(f"gave up after {MAX_ATTEMPTS} attempts")
            break

        sp_hash = bytes32(rng.randbytes(32))
        filter_passes = any_plot_passes_filter(harvester_api, plot_infos, sp_hash)

        if not filter_passes:
            if "no_results" not in wanted or len(buckets["no_results"]) >= samples_per_bucket:
                continue
            intended = "no_results"
            difficulty = constants.DIFFICULTY_STARTING
            sub_slot_iters = constants.SUB_SLOT_ITERS_STARTING
        else:
            need_no_proof = (
                "filtered_no_proof" in wanted and len(buckets["filtered_no_proof"]) < samples_per_bucket
            )
            need_proof = "proof" in wanted and len(buckets["proof"]) < samples_per_bucket

            if need_no_proof and (not need_proof or rng.random() < 0.5):
                intended = "filtered_no_proof"
                difficulty = constants.DIFFICULTY_STARTING
                sub_slot_iters = constants.SUB_SLOT_ITERS_STARTING
            elif need_proof:
                intended = "proof"
                difficulty = constants.DIFFICULTY_STARTING
                sub_slot_iters = PROOF_SUB_SLOT_ITERS
            else:
                continue

        spec = SignagePointSpec(
            bucket="",
            sp_hash=sp_hash,
            difficulty=difficulty,
            sub_slot_iters=sub_slot_iters,
        )
        sample = await run_sample(
            farmer_api,
            harvester_api,
            harvester_peer,
            farmer_peer,
            spec,
        )

        # Only keep the sample if it landed in the bucket we were aiming for,
        # otherwise the stored signage points would use mixed parameters.
        if sample.bucket != intended:
            continue

        if len(buckets[intended]) < samples_per_bucket:
            spec.bucket = intended
            spec.passed = sample.passed
            spec.proofs = sample.proofs
            buckets[intended].append(spec)

            collected = sum(len(samples) for samples in buckets.values())
            if collected % 10 == 0:
                save_store(store_path, network, buckets)
                print(
                    f"collected {collected}/{samples_per_bucket * len(BUCKETS)} "
                    f"(no_results={len(buckets['no_results'])}, "
                    f"filtered_no_proof={len(buckets['filtered_no_proof'])}, "
                    f"proof={len(buckets['proof'])})"
                )

    print(f"discovery attempts: {attempts}")
    save_store(store_path, network, buckets)
    return buckets


def select_specs(specs: list[SignagePointSpec], count: int) -> list[SignagePointSpec]:
    """Return ``count`` measurements, cycling through the stored signage points."""
    if not specs:
        return []
    return [specs[index % len(specs)] for index in range(count)]


def check_replay(spec: SignagePointSpec, sample: Sample) -> str | None:
    problems = []
    if sample.bucket != spec.bucket:
        problems.append(f"bucket {sample.bucket} != {spec.bucket}")
    if spec.passed is not None and sample.passed != spec.passed:
        problems.append(f"passed {sample.passed} != {spec.passed}")
    if spec.proofs is not None and sample.proofs != spec.proofs:
        problems.append(f"proofs {sample.proofs} != {spec.proofs}")
    return ", ".join(problems) if problems else None


async def main(args: argparse.Namespace) -> int:
    root_path: pathlib.Path = args.root
    network = load_config(root_path, "config.yaml").get("selected_network", "unknown")

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

    try:
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
        requested_buckets = args.buckets or list(BUCKETS)

        stored = load_store(args.store)
        if stored is None:
            print(f"no usable SP store at {args.store}; discovering signage points")
        elif args.discover:
            print(f"loaded {args.store}; --discover will create new signage points")
        else:
            print(f"loaded signage points from {args.store} (network {network!r})")

        stored_buckets = stored or {bucket: [] for bucket in BUCKETS}
        if args.discover:
            missing = [
                bucket for bucket in requested_buckets if len(stored_buckets.get(bucket, [])) < args.samples
            ]
        else:
            # Only discover when a bucket has nothing at all; otherwise the stored
            # signage points are reused (cycled) to fill --samples measurements.
            missing = [bucket for bucket in requested_buckets if not stored_buckets.get(bucket)]

        if missing:
            print(f"discovering signage points for: {', '.join(missing)}")
            rng = random.Random(args.seed)
            stored = await discover_signage_points(
                farmer_api=farmer_api,
                harvester_api=harvester_api,
                harvester_peer=harvester_peer,
                farmer_peer=farmer_peer,
                plot_infos=plot_infos,
                constants=constants,
                rng=rng,
                samples_per_bucket=args.samples,
                store_path=args.store,
                network=network,
                wanted=missing,
                known=stored,
                force=args.discover,
            )
            stored_buckets = stored
            if any(len(stored_buckets.get(bucket, [])) < args.samples for bucket in requested_buckets):
                print("not enough signage points; lower --samples or retry discovery")
                return 1

        selected: dict[str, list[SignagePointSpec]] = {}
        for bucket in requested_buckets:
            specs = stored_buckets[bucket]
            selected[bucket] = select_specs(specs, args.samples)
            if len(specs) < args.samples:
                print(
                    f"{bucket}: reusing the {len(specs)} stored signage point(s) "
                    f"for {args.samples} measurements"
                )

        # Warm up with real, already discovered signage points.
        for index in range(args.warmup):
            bucket = requested_buckets[index % len(requested_buckets)]
            await run_sample(farmer_api, harvester_api, harvester_peer, farmer_peer, selected[bucket][0])
        if args.warmup:
            print(f"warmed up plot cache with {args.warmup} signage point(s)")

        results: dict[str, list[Sample]] = {bucket: [] for bucket in BUCKETS}
        mismatches = 0
        measured = 0
        total = sum(len(specs) for specs in selected.values())

        for bucket in requested_buckets:
            for spec in selected[bucket]:
                sample = await run_sample(farmer_api, harvester_api, harvester_peer, farmer_peer, spec)
                problem = check_replay(spec, sample)
                if problem is not None:
                    mismatches += 1
                    print(f"warning: {spec.sp_hash.hex()} replayed differently: {problem}")
                results[sample.bucket].append(sample)
                measured += 1

                if measured % 10 == 0:
                    print(f"measured {measured}/{total}")

        if mismatches:
            print(f"\n{mismatches} signage point(s) behaved differently than during discovery")

        print_report(results)
        return 0
    finally:
        harvester.executor.shutdown(wait=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=DEFAULT_ROOT,
        help=f"chia root to benchmark (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--store",
        type=pathlib.Path,
        default=None,
        help="JSON file holding the signage points (default: <root>/better-farmer-sps.json)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="search for new signage points (replaces the wanted buckets in the store)",
    )
    parser.add_argument(
        "--bucket",
        action="append",
        choices=BUCKETS,
        dest="buckets",
        help="only benchmark this bucket (repeatable)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_BUCKET_SIZE,
        help=(
            "measurements per bucket (default: "
            f"{DEFAULT_BUCKET_SIZE}); stored signage points are reused when fewer are available"
        ),
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=DEFAULT_WARMUP,
        help=f"warmup signage points before measuring (default: {DEFAULT_WARMUP})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="random seed used when discovering signage points (default: 0)",
    )

    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    if args.warmup < 0:
        parser.error("--warmup must not be negative")
    if args.store is None:
        args.store = args.root / "better-farmer-sps.json"
    return args


if __name__ == "__main__":
    # ``chia.util.config.load_config_cli`` parses ``sys.argv`` itself for
    # config overrides, so hide this script's own arguments from it.
    benchmark_args = parse_args()
    sys.argv = sys.argv[:1]
    async_run(main(benchmark_args))
