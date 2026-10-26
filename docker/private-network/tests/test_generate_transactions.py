from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import math
import os
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.util.bech32m import encode_puzzle_hash

spec = importlib.util.spec_from_file_location("generator", Path(__file__).resolve().parents[1] / "generate_transactions.py")
assert spec is not None and spec.loader is not None
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def coin(index: int, amount: int = 1000) -> dict:
    return Coin(bytes32(index.to_bytes(32, "big")), bytes32(b"p" * 32), uint64(amount)).to_json_dict()


def arguments(**changes) -> argparse.Namespace:
    return argparse.Namespace(**{
        "tps": 40, "duration": 0.1, "amount_mojos": 1000, "fee_mojos": 0,
        "timeout": 1, "poll_interval": 0.005, "prepare_batch_size": 2,
        "recipient": encode_puzzle_hash(bytes32(b"r" * 32), "txch"),
        "max_inflight": 8, "drain_timeout": 1, **changes,
    })


class SubmissionClient:
    def __init__(self, delay=0, fail=False, reorg=False):
        self.delay, self.fail, self.reorg = delay, fail, reorg
        self.inputs = []
        self.reads = {}

    async def fetch(self, method, request):
        if method == "send_transaction_multi":
            self.inputs.append(request["coins"][0])
            tx_id = str(len(self.inputs))
            await asyncio.sleep(self.delay)
            if self.fail:
                raise TimeoutError("unknown submission outcome")
            return {"transaction_id": tx_id}
        if method == "get_transaction":
            tx_id = request["transaction_id"]
            self.reads[tx_id] = self.reads.get(tx_id, 0) + 1
            return {"transaction": {
                "confirmed": not self.reorg or self.reads[tx_id] == 1,
                "confirmed_at_height": 12, "sent_to": [["peer", 1, None]],
            }}
        raise AssertionError(method)


class PreparationClient:
    def __init__(self):
        self.address_index = 0
        self.outputs = []
        self.requests = []
        self.transactions = {}

    async def fetch(self, method, request):
        if method == "get_sync_status":
            return {"synced": True}
        if method == "get_wallet_balance":
            return {"wallet_balance": {"spendable_balance": 10**9}}
        if method == "get_next_address":
            self.address_index += 1
            return {"address": encode_puzzle_hash(bytes32(self.address_index.to_bytes(32, "big")), "txch")}
        if method == "send_transaction_multi":
            self.requests.append(request)
            expected_excluded = {Coin.from_json_dict(c).name().hex() for c in self.outputs}
            assert set(request["excluded_coin_ids"]) == expected_excluded
            tx_id = str(len(self.requests))
            outputs = [Coin(bytes32(len(self.requests).to_bytes(32, "big")),
                            bytes32.from_hexstr(a["puzzle_hash"]), uint64(a["amount"])).to_json_dict()
                       for a in request["additions"]]
            self.outputs.extend(outputs)
            self.transactions[tx_id] = {"confirmed": True, "additions": outputs + [coin(100, 9999)]}
            return {"transaction_id": tx_id}
        if method == "get_transaction":
            return {"transaction": self.transactions[request["transaction_id"]]}
        if method == "get_spendable_coins":
            return {"confirmed_records": [{"coin": c} for c in self.outputs]}
        raise AssertionError(method)


class WorkloadTests(unittest.IsolatedAsyncioTestCase):
    async def run_workload(self, client, args):
        events = []
        result = await generator.generate(client, args,
            [coin(i + 1) for i in range(math.ceil(args.tps * args.duration))],
            lambda event, **details: events.append((event, details)))
        return result, events

    async def test_preparation_reserves_prior_outputs_and_waits_for_spendability(self):
        client = PreparationClient()
        pool = await generator.prepare_coins(client, arguments(tps=10, duration=0.3), lambda *a, **k: None)
        self.assertEqual(len(pool), 3)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(len({Coin.from_json_dict(c).name() for c in pool}), 3)
        self.assertTrue(all(c["amount"] == 1000 for c in pool))

    async def test_parallel_submission_and_confirmation(self):
        client = SubmissionClient(delay=0.06)
        result, events = await self.run_workload(client, arguments())
        self.assertGreater(result["submitted"], 1)
        self.assertEqual(result["confirmed_final_verified"], result["submitted"])
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["attempted"] + result["skipped"], result["planned"])
        attempts = [details for event, details in events if event == "attempted"]
        self.assertTrue(all(a["actual_offset"] < 0.1 for a in attempts))

    async def test_capacity_limit_skips_slots_instead_of_bursting(self):
        result, events = await self.run_workload(SubmissionClient(delay=0.15), arguments(max_inflight=1))
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["skipped"], 3)
        self.assertTrue(any(event == "schedule_skipped" for event, _ in events))

    async def test_ambiguous_rpc_failure_never_retries_a_coin(self):
        client = SubmissionClient(fail=True)
        result, _ = await self.run_workload(client, arguments())
        self.assertEqual(result["submission_errors"], result["attempted"])
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(len(client.inputs), len({Coin.from_json_dict(c).name() for c in client.inputs}))

    async def test_final_verification_detects_revoked_confirmation(self):
        result, events = await self.run_workload(SubmissionClient(reorg=True), arguments(tps=10))
        self.assertEqual(result["confirmed_final_verified"], 0)
        self.assertEqual(result["pending"], 1)
        self.assertTrue(any(event == "confirmation_revoked" for event, _ in events))

    def test_cli_rejects_missing_recipient_and_nonfinite_rate(self):
        for argv in (["generate_transactions.py"], ["generate_transactions.py", "--print-address", "--tps", "nan"]):
            with patch("sys.argv", argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                generator.parse_args()

    def test_global_slots_are_partitioned_without_tripling_rate(self):
        assignments = [list(generator.assigned_slots(arguments(tps=10, duration=1, sender_count=3, sender_index=i)))
                       for i in range(3)]
        self.assertEqual(sorted(slot for group in assignments for slot in group), list(range(10)))
        self.assertEqual([len(group) for group in assignments], [4, 3, 3])

    def test_recipient_plan_is_balanced_and_reproducible(self):
        addresses = [encode_puzzle_hash(bytes32(bytes([i]) * 32), "txch") for i in (1, 2)]
        args = arguments(recipient=addresses, seed=123, sender_index=0)
        first = generator.recipient_plan(args, 100)
        self.assertEqual(first, generator.recipient_plan(args, 100))
        self.assertEqual(first.count(addresses[0]), 50)
        self.assertNotEqual(first, generator.recipient_plan(arguments(recipient=addresses, seed=456), 100))

    async def test_distributed_sender_uses_only_its_slots(self):
        args = arguments(tps=30, duration=0.2, sender_index=1, sender_count=3)
        events = []
        result = await generator.generate(SubmissionClient(), args, [coin(1), coin(2)],
            lambda event, **details: events.append((event, details)), start_at=time.monotonic() + 0.02)
        self.assertEqual(result["planned"], 2)
        sequences = [details["global_sequence"] for event, details in events if event == "attempted"]
        self.assertEqual(sequences, [2, 5])

    async def test_coordinated_barrier_waits_for_start_and_stops_on_eof(self):
        read_fd, write_fd = os.pipe()
        ready = asyncio.Event()
        args = arguments(sender_index=0)
        with os.fdopen(read_fd, "r") as reader, os.fdopen(write_fd, "w") as writer:
            with patch.object(generator.sys, "stdin", reader), patch.object(
                generator, "prepare_coins", return_value=[coin(1)]
            ):
                worker = generator.create_referenced_task(generator.coordinated_run(
                    SubmissionClient(), args,
                    lambda event, **details: ready.set() if event == "ready" else None))
                await asyncio.wait_for(ready.wait(), timeout=1)
                self.assertFalse(worker.done())
                writer.close()
                with self.assertRaisesRegex(RuntimeError, "disconnected before start"):
                    await worker

    async def test_coordinated_start_runs_after_barrier(self):
        import json

        read_fd, write_fd = os.pipe()
        ready = asyncio.Event()
        events = []
        args = arguments(tps=10, sender_index=0)

        def emit(event, **details):
            events.append((event, details))
            if event == "ready":
                ready.set()

        with os.fdopen(read_fd, "r") as reader, os.fdopen(write_fd, "w") as writer:
            with patch.object(generator.sys, "stdin", reader), patch.object(generator, "prepare_coins", return_value=[coin(1)]):
                worker = generator.create_referenced_task(generator.coordinated_run(SubmissionClient(), args, emit))
                await asyncio.wait_for(ready.wait(), timeout=1)
                start = time.monotonic() + 0.02
                writer.write(json.dumps({"command": "start", "start_monotonic": start}) + "\n")
                writer.flush()
                await asyncio.wait_for(worker, timeout=1)
        self.assertTrue(any(event == "summary" for event, _ in events))
        attempted = [details for event, details in events if event == "attempted"]
        self.assertTrue(all(details["actual_offset"] >= 0 for details in attempted))


if __name__ == "__main__":
    unittest.main()
