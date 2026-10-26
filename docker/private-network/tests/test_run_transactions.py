from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("launcher", Path(__file__).resolve().parents[1] / "run_transactions.py")
assert spec is not None and spec.loader is not None
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def args(self, output):
        return argparse.Namespace(tps=10, duration=1, seed=42, amount_mojos=1000, fee_mojos=0,
            wallet_timeout=1, drain_timeout=1, max_inflight=8, prepare_batch_size=100,
            stack="chia-kollaps", output_dir=Path(output), preparation_timeout=2,
            completion_timeout=2, start_delay=0.02)

    def test_commands_share_rate_and_exclude_self(self):
        args = self.args("unused")
        addresses = {name: f"txch1{index}" for index, name in enumerate(launcher.FARMERS)}
        for index, name in enumerate(launcher.FARMERS):
            command = launcher.worker_command(args, name, index, addresses, "run")
            recipients = [command[i + 1] for i, item in enumerate(command) if item == "--recipient"]
            self.assertEqual(set(recipients), set(addresses.values()) - {addresses[name]})
            self.assertEqual(command[command.index("--tps") + 1], "10")
            self.assertEqual(command[command.index("--sender-index") + 1], str(index))

    def test_launcher_coordinates_real_pipes_and_aggregates_without_docker(self):
        # Actual subprocess pipes exercise ready/start/EOF ordering, including
        # a worker exiting while the other workers are still reporting.
        worker = '''
import json,sys,time
index=int(sys.argv[1])
print(json.dumps({"event":"ready","prepared":4 if index==0 else 3}),flush=True)
command=json.loads(sys.stdin.readline())
assert command["command"]=="start"
time.sleep(max(0,command["start_monotonic"]-time.monotonic()))
time.sleep(index*0.01)
count=4 if index==0 else 3
print(json.dumps({"event":"summary",**{k:count for k in ("planned","attempted","submitted","confirmed_final_verified")},"skipped":0,"submission_errors":0,"pending":0}),flush=True)
'''
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            index = command[command.index("--sender-index") + 1]
            return real_popen([sys.executable, "-u", "-c", worker, index], **kwargs)

        def address(command, **kwargs):
            return f"txch1{command[2]}\n"

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(launcher, "discover_containers", return_value={name: name for name in launcher.FARMERS}), \
                 patch.object(launcher.subprocess, "check_output", side_effect=address), \
                 patch.object(launcher.subprocess, "Popen", side_effect=popen):
                output = launcher.run(self.args(directory))
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["planned"], 10)
            self.assertEqual(summary["successful_submission_tps"], 10)
            self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "completed")
            self.assertTrue(all((output / f"{name}.jsonl").exists() for name in launcher.FARMERS))


if __name__ == "__main__":
    unittest.main()
