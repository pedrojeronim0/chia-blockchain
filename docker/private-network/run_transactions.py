from __future__ import annotations

import argparse
import json
import math
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

FARMERS = ("farmer-1", "farmer-2", "farmer-3")
SCRIPT = "/app/docker/private-network/generate_transactions.py"


def discover_containers(stack: str) -> dict[str, str]:
    ids = subprocess.check_output(
        ["docker", "ps", "--filter", f"label=com.docker.stack.namespace={stack}", "--format", "{{.ID}}"],
        text=True,
    ).split()
    if not ids:
        raise ValueError(f"No running containers found for stack {stack}")
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
    result = {}
    for container in containers:
        name = container["Config"]["Hostname"]
        if name in FARMERS:
            if name in result:
                raise ValueError(f"Multiple containers for {name}; wait for Swarm to converge")
            result[name] = container["Id"]
    if set(result) != set(FARMERS):
        raise ValueError(f"Expected {FARMERS}, found {sorted(result)}")
    return result


def worker_command(args: argparse.Namespace, container: str, index: int, addresses: dict[str, str], run_id: str) -> list[str]:
    command = [
        "docker", "exec", "-i", container, "python", SCRIPT,
        "--coordinated", "--sender-index", str(index), "--sender-count", str(len(FARMERS)),
        "--tps", str(args.tps), "--duration", str(args.duration), "--seed", str(args.seed),
        "--run-id", run_id, "--amount-mojos", str(args.amount_mojos), "--fee-mojos", str(args.fee_mojos),
        "--timeout", str(args.wallet_timeout), "--drain-timeout", str(args.drain_timeout),
        "--max-inflight", str(args.max_inflight), "--prepare-batch-size", str(args.prepare_batch_size),
        "--output", f"/data/transactions/{run_id}.jsonl",
    ]
    for farmer in FARMERS:
        if farmer != FARMERS[index]:
            command.extend(["--recipient", addresses[farmer]])
    return command


def aggregate(summaries: dict[str, dict], duration: float, tps: float, run_id: str) -> dict:
    result = {"run_id": run_id, "global_target_tps": tps, "duration_seconds": duration, "nodes": summaries}
    for field in ("planned", "attempted", "skipped", "submitted", "submission_errors", "confirmed_final_verified", "pending"):
        result[field] = sum(summary[field] for summary in summaries.values())
    result["attempted_tps"] = result["attempted"] / duration
    result["successful_submission_tps"] = result["submitted"] / duration
    return result


def run(args: argparse.Namespace) -> Path:
    containers = discover_containers(args.stack)
    addresses = {}
    for farmer in FARMERS:
        addresses[farmer] = subprocess.check_output(
            ["docker", "exec", containers[farmer], "python", SCRIPT, "--print-address",
             "--timeout", str(args.wallet_timeout)], text=True, timeout=args.wallet_timeout + 60,
        ).strip()
        if not addresses[farmer].startswith("txch1") or any(c.isspace() for c in addresses[farmer]):
            raise ValueError(f"Invalid wallet address returned by {farmer}")
    if len(set(addresses.values())) != len(FARMERS):
        raise ValueError("Farmers must have distinct wallets and receive addresses")
    run_id = str(uuid.uuid4())
    output = args.output_dir / run_id
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"run_id": run_id, "global_target_tps": args.tps, "duration_seconds": args.duration,
                "seed": args.seed, "containers": containers, "addresses": addresses,
                "amount_mojos": args.amount_mojos, "fee_mojos": args.fee_mojos,
                "sender_order": FARMERS, "status": "preparing"}
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    events: queue.Queue = queue.Queue()
    processes: dict[str, subprocess.Popen] = {}
    readers = []
    finished = set()

    def read_events(farmer: str, process: subprocess.Popen) -> None:
        try:
            with process.stdout, (output / f"{farmer}.jsonl").open("w", encoding="utf-8", buffering=1) as log:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"event": "worker_output", "text": line.rstrip()}
                    log.write(json.dumps(event) + "\n")
                    events.put((farmer, event))
        finally:
            events.put((farmer, {"event": "worker_exit"}))

    def receive(deadline: float) -> tuple[str, dict]:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for coordinated workers")
            try:
                farmer, event = events.get(timeout=min(1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if event["event"] == "worker_exit" and farmer in finished:
                continue
            if event["event"] in {"failed", "worker_exit"}:
                raise RuntimeError(f"{farmer}: {event}")
            return farmer, event

    try:
        deadline = time.monotonic() + args.preparation_timeout
        for index, farmer in enumerate(FARMERS):
            process = subprocess.Popen(worker_command(args, containers[farmer], index, addresses, run_id),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, bufsize=1)
            processes[farmer] = process
            thread = threading.Thread(target=read_events, args=(farmer, process), daemon=True)
            thread.start()
            readers.append(thread)
        ready = set()
        while len(ready) < len(FARMERS):
            farmer, event = receive(deadline)
            if event["event"] == "ready":
                ready.add(farmer)
                print(f"{farmer}: ready with {event['prepared']} coins", flush=True)
        # All containers share the dedicated Linux host's monotonic clock.
        start_at = time.monotonic() + args.start_delay
        manifest.update(status="running", start_monotonic=start_at)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        command = json.dumps({"command": "start", "start_monotonic": start_at}) + "\n"
        for process in processes.values():
            process.stdin.write(command)
            process.stdin.flush()
        print(f"Run {run_id}: {args.tps} TPS globally across three wallets; logs: {output}", flush=True)
        summaries = {}
        # Includes RPC settlement and final per-transaction verification.
        deadline = start_at + args.duration + args.drain_timeout + args.completion_timeout
        while len(summaries) < len(FARMERS):
            farmer, event = receive(deadline)
            if event["event"] == "summary":
                summaries[farmer] = event
                finished.add(farmer)
                print(f"{farmer}: {event['submitted']} submitted, {event['confirmed_final_verified']} confirmed", flush=True)
        result = aggregate(summaries, args.duration, args.tps, run_id)
        (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        manifest.update(status="completed")
        return output
    except BaseException as error:
        manifest.update(status="aborted", error=str(error))
        raise
    finally:
        # EOF cancels only the workers owned by this launcher, including workers
        # still preparing coins. Already broadcast transactions remain in flight.
        for process in processes.values():
            try:
                process.stdin.close()
            except OSError:
                pass
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for reader in readers:
            reader.join(timeout=2)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coordinate three Chia farmer wallets with a global TPS budget")
    parser.add_argument("--stack", default="chia-kollaps")
    parser.add_argument("--tps", type=float, default=30, help="Global TPS, shared by all three senders")
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--amount-mojos", type=int, default=1_000_000)
    parser.add_argument("--fee-mojos", type=int, default=0)
    parser.add_argument("--wallet-timeout", type=float, default=600)
    parser.add_argument("--preparation-timeout", type=float, default=3600)
    parser.add_argument("--drain-timeout", type=float, default=120)
    parser.add_argument("--completion-timeout", type=float, default=600)
    parser.add_argument("--start-delay", type=float, default=5)
    parser.add_argument("--max-inflight", type=int, default=8)
    parser.add_argument("--prepare-batch-size", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=Path("results/transactions"))
    args = parser.parse_args()
    for field in ("tps", "duration", "wallet_timeout", "preparation_timeout", "drain_timeout", "completion_timeout", "start_delay"):
        if not math.isfinite(getattr(args, field)) or getattr(args, field) <= 0:
            parser.error(f"{field} must be finite and positive")
    if not math.isfinite(args.tps * args.duration) or args.tps * args.duration < 3:
        parser.error("the global workload must contain at least three transfers")
    if args.amount_mojos < 1 or args.fee_mojos < 0 or args.amount_mojos + args.fee_mojos >= 2**64:
        parser.error("invalid amount or fee")
    if args.max_inflight < 1 or not 1 <= args.prepare_batch_size <= 500:
        parser.error("invalid max-inflight or prepare-batch-size")
    return args


if __name__ == "__main__":
    try:
        print(f"Results: {run(parse_args())}")
    except KeyboardInterrupt:
        print("Stopped; already submitted transactions are not cancelled", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"Coordinated workload failed: {error}", file=sys.stderr)
        sys.exit(1)
