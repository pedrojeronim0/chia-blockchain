from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import aiohttp
from chia_rs import Coin
from chia_rs.sized_ints import uint16

from chia.rpc.rpc_client import RpcClient
from chia.util.bech32m import decode_puzzle_hash
from chia.util.config import load_config
from chia.util.task_referencer import create_referenced_task

Emit = Callable[..., None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare independent coins and generate a timed Chia workload")
    parser.add_argument("--root-path", type=Path, default=Path(os.environ.get("CHIA_ROOT", "/data/chia")))
    parser.add_argument("--tps", type=float, default=10)
    parser.add_argument("--duration", type=float, default=60, help="Submission window in seconds, excluding preparation")
    parser.add_argument("--recipient", "--address", dest="recipient", action="append", help="Destination txch address; repeat to vary recipients")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sender-index", type=int, default=0)
    parser.add_argument("--sender-count", type=int, default=1)
    parser.add_argument("--run-id", default=None, help="Shared experiment identifier")
    parser.add_argument("--coordinated", action="store_true", help="Wait for the launcher's start command on stdin")
    parser.add_argument("--amount-mojos", type=int, default=1_000_000)
    parser.add_argument("--fee-mojos", type=int, default=0)
    parser.add_argument("--fingerprint", type=int)
    parser.add_argument("--print-address", action="store_true", help="Print a receive address and exit without spending")
    parser.add_argument("--timeout", type=float, default=600, help="Timeout for each preparation wait")
    parser.add_argument("--drain-timeout", type=float, default=120, help="Confirmation observation window after sending")
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--max-inflight", type=int, default=8, help="Maximum concurrent submission RPC calls")
    parser.add_argument("--prepare-batch-size", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("/data/transactions.jsonl"))
    args = parser.parse_args()
    for name in ("tps", "duration", "timeout", "drain_timeout", "poll_interval"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    if not math.isfinite(args.tps * args.duration):
        parser.error("tps * duration must be finite")
    if args.sender_count < 1 or not 0 <= args.sender_index < args.sender_count:
        parser.error("sender-index must be between zero and sender-count minus one")
    if args.sender_count > math.ceil(args.tps * args.duration):
        parser.error("the workload must contain at least one transfer per sender")
    if args.amount_mojos < 1 or args.fee_mojos < 0 or args.amount_mojos + args.fee_mojos >= 2**64:
        parser.error("amount must be positive and fee non-negative; their sum must fit in uint64")
    if args.max_inflight < 1 or not 1 <= args.prepare_batch_size <= 500:
        parser.error("max-inflight must be positive; prepare-batch-size must be between 1 and 500")
    if math.ceil(args.tps * args.duration) * (args.amount_mojos + args.fee_mojos) >= 2**64:
        parser.error("the total preparation amount must fit in uint64")
    if not args.print_address:
        try:
            if not args.recipient:
                raise ValueError("recipient required")
            for recipient in args.recipient:
                if not recipient.startswith("txch1"):
                    raise ValueError("expected txch prefix")
                decode_puzzle_hash(recipient)
        except ValueError:
            parser.error("--recipient must be a valid txch address")
    return args


def assigned_slots(args: argparse.Namespace) -> range:
    return range(getattr(args, "sender_index", 0), math.ceil(args.tps * args.duration),
                 getattr(args, "sender_count", 1))


def recipient_plan(args: argparse.Namespace, count: int) -> list[str]:
    recipients = sorted(set([args.recipient] if isinstance(args.recipient, str) else args.recipient))
    rng = random.Random(f"{getattr(args, 'seed', 42)}:{getattr(args, 'sender_index', 0)}")
    plan = []
    while len(plan) < count:
        batch = recipients.copy()
        rng.shuffle(batch)
        plan.extend(batch)
    return plan[:count]


async def login(client: RpcClient, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            await client.healthz()
            break
        except (aiohttp.ClientError, OSError, TimeoutError):
            if time.monotonic() >= deadline:
                raise TimeoutError("Wallet RPC unavailable; wait for plotting and wallet startup")
            await asyncio.sleep(args.poll_interval)
    fingerprints = (await client.fetch("get_public_keys", {}))["public_key_fingerprints"]
    fingerprint = args.fingerprint
    if fingerprint is None:
        if len(fingerprints) != 1:
            raise ValueError("Expected one key; select one with --fingerprint")
        fingerprint = fingerprints[0]
    if fingerprint not in fingerprints:
        raise ValueError("Fingerprint not present in this container")
    if (await client.fetch("get_logged_in_fingerprint", {}))["fingerprint"] != fingerprint:
        await client.fetch("log_in", {"fingerprint": fingerprint})
    return fingerprint


async def wait_for_funds(client: RpcClient, args: argparse.Namespace, required: int, emit: Emit) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        status = await client.fetch("get_sync_status", {})
        balance = (await client.fetch("get_wallet_balance", {"wallet_id": 1}))["wallet_balance"]
        if status["synced"] and balance["spendable_balance"] >= required:
            return
        emit("preparation_wait", synced=status["synced"], spendable=balance["spendable_balance"], required=required)
        if time.monotonic() >= deadline:
            raise TimeoutError("Wallet needs sync and spendable farming rewards or a transfer")
        await asyncio.sleep(args.poll_interval)


async def wait_confirmed(client: RpcClient, tx_id: str, args: argparse.Namespace) -> dict:
    deadline = time.monotonic() + args.timeout
    while True:
        transaction = (await client.fetch("get_transaction", {"transaction_id": tx_id}))["transaction"]
        if transaction["confirmed"]:
            return transaction
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Preparation transaction {tx_id} is still pending; it has not been cancelled")
        await asyncio.sleep(args.poll_interval)


async def prepare_coins(client: RpcClient, args: argparse.Namespace, emit: Emit) -> list[dict]:
    count = len(assigned_slots(args))
    coin_amount = args.amount_mojos + args.fee_mojos
    await wait_for_funds(client, args, count * coin_amount, emit)
    pool: list[dict] = []
    while len(pool) < count:
        batch = min(args.prepare_batch_size, count - len(pool))
        # Distinct puzzle hashes avoid duplicate (parent, puzzle_hash, amount)
        # outputs when all prepared coins have the same amount.
        hashes = []
        for _ in range(batch):
            address = (await client.fetch("get_next_address", {"wallet_id": 1, "new_address": True}))["address"]
            hashes.append(decode_puzzle_hash(address).hex())
        if len(set(hashes)) != batch:
            raise ValueError("Wallet returned duplicate preparation addresses")
        response = await client.fetch(
            "send_transaction_multi",
            {
                "wallet_id": 1,
                "additions": [{"puzzle_hash": ph, "amount": coin_amount} for ph in hashes],
                "excluded_coin_ids": [Coin.from_json_dict(coin).name().hex() for coin in pool],
                "fee": 0,
                "push": True,
            },
        )
        tx_id = response["transaction_id"]
        emit("preparation_submitted", transaction_id=tx_id, outputs=batch)
        transaction = await wait_confirmed(client, tx_id, args)
        outputs = [
            coin for coin in transaction["additions"]
            if coin["puzzle_hash"].removeprefix("0x") in hashes and coin["amount"] == coin_amount
        ]
        if len(outputs) != batch:
            raise ValueError("Preparation transaction did not produce the requested independent coins")
        pool.extend(outputs)
        emit("preparation_confirmed", transaction_id=tx_id, prepared=len(pool), target=count)
    # Confirmation can be observed before all coin-state updates reach the wallet.
    deadline = time.monotonic() + args.timeout
    expected = {Coin.from_json_dict(coin).name() for coin in pool}
    while True:
        response = await client.fetch("get_spendable_coins", {"wallet_id": 1})
        available = {Coin.from_json_dict(record["coin"]).name() for record in response["confirmed_records"]}
        if expected <= available:
            return pool
        if time.monotonic() >= deadline:
            raise TimeoutError("Prepared coins have not all become spendable in the wallet")
        await asyncio.sleep(args.poll_interval)


async def generate(client: RpcClient, args: argparse.Namespace, coins: list[dict], emit: Emit,
                   start_at: float | None = None) -> dict:
    pending: dict[str, dict] = {}
    confirmed: dict[str, dict] = {}
    jobs: set[asyncio.Task] = set()
    failures = 0
    attempted = 0
    skipped = 0
    sending_done = asyncio.Event()
    slots = assigned_slots(args)
    if len(slots) != len(coins):
        raise ValueError("Prepared coin count does not match assigned global slots")
    recipients = recipient_plan(args, len(coins))
    sender_count = getattr(args, "sender_count", 1)
    sender_index = getattr(args, "sender_index", 0)
    if start_at is not None and time.monotonic() >= start_at:
        raise ValueError("Coordinated start deadline already passed; refusing an unsynchronized run")
    started = time.monotonic() if start_at is None else start_at
    end = started + args.duration

    async def submit(sequence: int, coin: dict, scheduled: float) -> None:
        nonlocal failures, attempted, skipped
        before = time.monotonic()
        if before >= end:
            skipped += 1
            emit("schedule_skipped", sequence=sequence, reason="submission_window_closed")
            return
        attempted += 1
        recipient = recipients[sequence - 1]
        emit("attempted", sequence=sequence, global_sequence=slots[sequence - 1] + 1,
             recipient=recipient, scheduled_offset=scheduled - started,
             actual_offset=before - started, scheduling_delay_seconds=before - scheduled,
             coin_id=Coin.from_json_dict(coin).name().hex())
        try:
            # One explicit, pre-funded input per transfer. Never reuse an input,
            # even if RPC fails after the wallet may have accepted the request.
            response = await client.fetch("send_transaction_multi", {
                "wallet_id": 1, "coins": [coin],
                "additions": [{"puzzle_hash": decode_puzzle_hash(recipient).hex(), "amount": args.amount_mojos}],
                "fee": args.fee_mojos, "push": True,
            })
            tx_id = response["transaction_id"]
            pending[tx_id] = {"sequence": sequence, "submitted": before, "accepted": False}
            emit("submitted", sequence=sequence, global_sequence=slots[sequence - 1] + 1,
                 recipient=recipient, transaction_id=tx_id, rpc_seconds=time.monotonic() - before)
        except Exception as error:
            failures += 1
            emit("submission_error", sequence=sequence, error=str(error), outcome="unknown_or_rejected_no_retry")

    async def monitor() -> None:
        while True:
            for tx_id, info in list(pending.items()):
                try:
                    tx = (await client.fetch("get_transaction", {"transaction_id": tx_id}))["transaction"]
                    if not info["accepted"] and any(entry[1] == 1 for entry in tx.get("sent_to", [])):
                        info["accepted"] = True
                        emit("mempool_accepted_observed", sequence=info["sequence"], transaction_id=tx_id)
                    if tx["confirmed"]:
                        confirmed[tx_id] = info
                        pending.pop(tx_id)
                        emit("confirmed_observed", sequence=info["sequence"], transaction_id=tx_id,
                             height=tx["confirmed_at_height"], elapsed_seconds=time.monotonic() - info["submitted"])
                except Exception as error:
                    emit("observation_error", transaction_id=tx_id, error=str(error))
            if sending_done.is_set() and not pending:
                return
            await asyncio.sleep(args.poll_interval)

    observer = create_referenced_task(monitor())
    emit("measurement_started", global_target_tps=args.tps, sender_index=sender_index,
         sender_count=sender_count, seed=getattr(args, "seed", 42),
         start_monotonic=started, duration_seconds=args.duration, planned=len(coins))
    try:
        slot = 0
        while slot < len(coins):
            scheduled = started + slots[slot] / args.tps
            await asyncio.sleep(max(0, scheduled - time.monotonic()))
            now = time.monotonic()
            if now >= end:
                skipped += len(coins) - slot
                break
            # Do not compensate for delays with a burst of old scheduled work.
            global_slot = int((now - started) * args.tps)
            current = min(len(coins) - 1, max(0, (global_slot - sender_index) // sender_count))
            if current > slot:
                skipped += current - slot
                emit("schedule_missed", first_sequence=slot + 1, count=current - slot)
                slot = current
                scheduled = started + slots[slot] / args.tps
            if len(jobs) >= args.max_inflight:
                skipped += 1
                emit("schedule_skipped", sequence=slot + 1, reason="submission_rpc_capacity")
            else:
                job = create_referenced_task(submit(slot + 1, coins[slot], scheduled))
                jobs.add(job)
                job.add_done_callback(jobs.discard)
            slot += 1
        await asyncio.sleep(max(0, end - time.monotonic()))
        emit("submission_window_ended", attempted=attempted, skipped=skipped)
        if jobs:
            await asyncio.gather(*list(jobs))
        sending_done.set()
        try:
            await asyncio.wait_for(observer, timeout=args.drain_timeout)
        except TimeoutError:
            emit("drain_timeout", pending=len(pending))
        # Recheck confirmations at the end to detect reorgs or observation errors.
        verified = 0
        for tx_id in list(confirmed):
            try:
                tx = (await client.fetch("get_transaction", {"transaction_id": tx_id}))["transaction"]
                verified += bool(tx["confirmed"])
                if not tx["confirmed"]:
                    pending[tx_id] = confirmed.pop(tx_id)
                    emit("confirmation_revoked", transaction_id=tx_id)
            except Exception as error:
                emit("final_verification_error", transaction_id=tx_id, error=str(error))
        summary = {"planned": len(coins), "attempted": attempted, "skipped": skipped,
                   "submitted": len(pending) + len(confirmed), "submission_errors": failures,
                   "confirmed_observed": len(confirmed), "confirmed_final_verified": verified,
                   "pending": len(pending), "global_target_tps": args.tps,
                   "sender_index": sender_index, "sender_count": sender_count,
                   "target_tps": len(coins) / args.duration,
                   "attempted_tps": attempted / args.duration,
                   "successful_submission_tps": (len(pending) + len(confirmed)) / args.duration,
                   "observation_seconds": time.monotonic() - started}
        emit("summary", **summary)
        return summary
    finally:
        observer.cancel()
        for job in list(jobs):
            job.cancel()
        await asyncio.gather(observer, *list(jobs), return_exceptions=True)


async def coordinated_run(client: RpcClient, args: argparse.Namespace, emit: Emit) -> None:
    """A pipe barrier avoids races between wallet preparation and measurement."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    command = create_referenced_task(reader.readline())
    preparation = create_referenced_task(prepare_coins(client, args, emit))
    workload = None
    disconnect = None
    try:
        done, _ = await asyncio.wait({command, preparation}, return_when=asyncio.FIRST_COMPLETED)
        if command in done:
            raise RuntimeError("Launcher disconnected or sent start before preparation was ready")
        coins = preparation.result()
        emit("ready", prepared=len(coins), sender_index=args.sender_index)
        line = await command
        if not line:
            raise RuntimeError("Launcher disconnected before start")
        message = json.loads(line)
        start_at = message["start_monotonic"]
        if message.get("command") != "start" or not isinstance(start_at, (int, float)) or not math.isfinite(start_at):
            raise ValueError("Invalid coordinated start command")
        workload = create_referenced_task(generate(client, args, coins, emit, start_at=start_at))
        disconnect = create_referenced_task(reader.readline())
        done, _ = await asyncio.wait({workload, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if workload not in done:
            raise RuntimeError("Launcher disconnected; workload stopped")
        await workload
    finally:
        tasks = [task for task in (command, preparation, workload, disconnect) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()


async def main(args: argparse.Namespace) -> None:
    root = args.root_path.resolve()
    config = load_config(root, "config.yaml")
    if config["selected_network"] != "private-docker":
        raise ValueError("This generator only runs on the private-docker network")
    config["rpc_timeout"] = min(30, args.timeout)
    async with RpcClient.create_as_context("localhost", uint16(config["wallet"]["rpc_port"]), root, config) as client:
        fingerprint = await login(client, args)
        if args.print_address:
            print((await client.fetch("get_next_address", {"wallet_id": 1, "new_address": False}))["address"])
            return
        args.output.parent.mkdir(parents=True, exist_ok=True)
        run_id = args.run_id or str(uuid.uuid4())
        with args.output.open("a", encoding="utf-8", buffering=1) as output:
            def emit(event: str, **details: Any) -> None:
                record = {"time": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns(),
                          "run_id": run_id, "node": os.environ.get("NODE_NAME", os.environ.get("HOSTNAME", "unknown")),
                          "event": event, **details}
                line = json.dumps(record)
                print(line, flush=True)
                output.write(line + "\n")
            emit("started", fingerprint=fingerprint, recipient=args.recipient,
                 amount_mojos=args.amount_mojos, fee_mojos=args.fee_mojos)
            try:
                if args.coordinated:
                    await coordinated_run(client, args, emit)
                else:
                    coins = await prepare_coins(client, args, emit)
                    await generate(client, args, coins, emit)
            except BaseException as error:
                emit("failed", error=str(error), note="Already submitted transactions are not cancelled")
                raise


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        print("Stopped; previously submitted transactions remain pending or confirmed", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"Transaction generator failed: {error}", file=sys.stderr)
        sys.exit(1)
