# Transaction workload generator

`generate_transactions.py` runs inside a farmer container against its local
wallet RPC. Farmers include wallets; rebuild the images and redeploy if using
an older image that starts `farmer-no-wallet`. The generator never starts
automatically with the network.

## Run

For a coordinated workload, run this **on the dedicated Linux machine** after
starting Chia in the Kollaps dashboard:

```bash
python3 private-network/run_transactions.py \
  --stack chia-kollaps --tps 30 --duration 300 --seed 42
```

This requests **30 TPS globally**, approximately 30/N TPS from each of N wallets (10 TPS with 3 farmers).
The launcher discovers farmer containers and wallet addresses, validates the
deployed topology metadata, and sorts farmers numerically. Standalone
`full-node-*` containers participate in network propagation but are excluded
from transaction workers and recipients. At least two farmers
and one planned transfer per farmer are required. Senders
take turns in numeric order: farmer-1 through farmer-N, then
repeat. Slots are allocated without overlap, even when the total transfer
count is not divisible by N.

Each sender varies its recipient between the other N-1 wallets, using seeded,
shuffled batches. Planned destination counts differ by at most one per sender;
skipped sends can change the observed distribution. There are no self-transfers.
The seed repeats recipient choices and sender order, not keys, coin IDs or
confirmation timing.

Each worker prepares only its assigned number of coins. All workers must report
ready before the launcher releases a common start timestamp. Slots are staggered
by the global TPS interval. This relies on a single native Linux Docker host,
where the coordinator and containers share the monotonic clock. Do not use
a remote Docker context for this launcher.

By default, host results are saved under `results/transactions/<run-id>/`,
relative to the working directory; use `--output-dir` to change this:

- `manifest.json`: parameters, seed, wallets, containers, start time and status.
- `farmer-<index>.jsonl`: one worker event log per farmer.
- `summary.json`: individual totals and aggregate submission rates.

Workers also save `/data/transactions/<run-id>.jsonl` inside their containers.
Ctrl+C or a worker failure closes the control pipes and cancels the remaining
workers, including workers still preparing coins. Already broadcast transactions
are not cancelled. Wait for workers to exit before starting another run.
`--preparation-timeout` bounds the ready barrier; `--start-delay` sets the lead
time for the shared start command. `--max-inflight` remains a per-wallet limit.

### Single-sender mode

After starting Chia in the Kollaps dashboard, find the farmer containers:

```bash
docker ps --filter label=com.docker.stack.namespace=chia-kollaps
```

Obtain a receive address from the destination farmer's wallet:

```bash
docker exec <recipient-farmer-id> python /app/private-network/generate_transactions.py --print-address
```

Pass that address to the sender. For example, request 10 transactions/second
for a 300-second submission window:

```bash
docker exec <sender-farmer-id> python /app/private-network/generate_transactions.py \
  --recipient txch1... \
  --tps 10 \
  --duration 300 \
  --amount-mojos 1000000 \
  --fee-mojos 0 \
  --drain-timeout 120
```

Replace `txch1...` with the complete address printed by the first command.
Run only one generator per sender wallet. Use another farmer's address for
transfers between wallets; a self-transfer is possible only by explicitly
supplying the sender's own address. Repeat `--recipient` to vary destinations
and use `--seed` for reproducibility. Here, `--tps` applies only to this sender.

## Preparation, sending, and observation

Before the measured submission window, the generator waits for wallet sync
and enough spendable balance for its assigned transfer count times
`(amount + fee)` mojos. For a single sender, the count is `ceil(tps * duration)`.
It splits that balance into one independent coin per planned transfer. Each
coin has a distinct wallet address and contains exactly the transfer amount
plus fee, so measured transfers use one input and one output without change.
Preparation transactions are confirmed in batches before measurement; they
are logged separately and must not be counted as workload transactions.
Earlier prepared outputs are excluded from subsequent preparation spends.
Before each preparation batch, the worker waits for wallet synchronization.
If the wallet explicitly rejects the request because it became unsynced, the
worker waits and retries within `--timeout`. This rejection occurs before
transaction construction. Transport errors and other submission failures are
not retried. The final spendable-coin check also waits and retries within
`--timeout` if the wallet explicitly reports that it is not fully synced.
Synchronization waits are logged as `preparation_sync_wait`.

After preparing coins, each worker constructs and signs every measured
transfer through the wallet with `push=false`. No workload transfer is
broadcast during this stage. Each signed bundle is checked to spend exactly
its assigned input and create the expected recipient output. `transfer_signed`
events record the bundle ID and input/output coin IDs. The ready barrier is
released only after all transfers have been signed.

Farming rewards must be available first. Preparation can take several blocks
and many wallet RPC calls; it is excluded from `--duration`. Use `--timeout`
to increase the per-wait preparation limit (default: 600 seconds). A failed
preparation can leave coins and transactions in the wallet; another run makes
a new pool rather than resuming the previous one.

During measurement, a monotonic-clock schedule submits transfers at the
requested TPS using the local full node's `push_tx`, without wallet RPC calls.
`--max-inflight` limits concurrent submission RPC calls
(default: 8). Confirmation polling runs concurrently. The generator does not
wait for each transfer to confirm before sending another. Missed slots and
capacity-limited slots are logged and skipped, rather than sent in a catch-up
burst. A failed submission is never automatically retried because the full node
may already have accepted it. Each prepared input is used at most once.

After the submission window, outstanding submission RPC calls settle and
confirmation polling continues for up to `--drain-timeout` seconds. Previously
observed confirmations are checked again at the end to detect reorgs. Stopping
the generator does not cancel transactions already sent.

## Results and limitations

Events are printed as JSONL and appended to `/data/transactions.jsonl` in the
sender's persistent volume. Each run has a UUID, node identifier, UTC timestamps
and monotonic timestamps. Use `--output` for another file. Export the log with:

```bash
docker cp <sender-farmer-id>:/data/transactions.jsonl ./transactions.jsonl
```

Relevant events include `measurement_started`, `attempted`, `submitted`,
`submission_error`, `schedule_missed`, `schedule_skipped`,
`mempool_accepted_observed`, `confirmed_observed`, `submission_window_ended`,
and `summary`. The summary reports planned, attempted, skipped, successfully
submitted, errored, confirmed, and pending transfers, plus achieved attempt and
successful-submission rates. RPC failures have an unknown-or-rejected outcome;
they are not proof that the transaction was never broadcast.

These rates describe the generator, not the network's confirmed throughput.
Confirmation timestamps are full-node polling observations, with block heights;
they are not exact block arrival or inclusion times. Confirmation requires the
expected output to exist and the input to be spent at the same height. Scans
are batched and discarded if the chain peak changes during the scan.
Mempool acceptance is observed from `push_tx` returning `SUCCESS`.
Transaction IDs now identify signed spend bundles, not wallet history records.
Final verification errors
remain explicit, and a reorg after the run can still change the chain.
Use the transaction IDs and confirmation heights with canonical chain data
for throughput analysis. Block propagation instrumentation is separate.

Wallet construction/signing happens before measurement. Full-node validation,
confirmation polling, RPC capacity, and logging can still limit achieved TPS.
Compare requested TPS with actual submission rates
before attributing saturation to the network. Independent single-sender runs
add their rates; use `run_transactions.py` to coordinate a global TPS budget.
Keep preparation and workload
settings identical when comparing network configurations.

## Validation status

`tests/test_topology.py` covers topology-derived peer configuration, volumes,
and farmer discovery. Run it from the repository root with
`tools/pytest -o addopts='' private-network/tests/test_topology.py -q`.
End-to-end wallet submission and coordinated workloads must be verified on
the dedicated machine before collecting experimental results.
