"""Micro-benchmark of Chia v1 proof-of-space verification.

Reads proofs produced by proof_verif.py and times, for each proof:
  A: verify_and_get_quality_string (full acceptance path used by full nodes and farmers)
  B: chiapos Verifier.validate_proof (cryptographic check only, C++)
A and B are timed back to back on the same proof, alternating which goes first.

Usage: python benchmarks/bench_verif.py proofs.txt [output.csv]
"""

from __future__ import annotations

import csv
import gc
import random
import statistics
import sys
from time import perf_counter_ns

from chia_rs import ProofOfSpace
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint32
from chiapos import Verifier

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.proof_of_space import verify_and_get_quality_string

# Must match proof_verif.py
HEIGHT = uint32(9_500_000)
N_PASSES = 10
CONSTANTS = DEFAULT_CONSTANTS.replace(MIN_PLOT_SIZE_V1=uint8(18))

Proof = tuple[ProofOfSpace, bytes32, bytes32]


def load_proofs(path):
    proofs = []
    with open(path) as f:
        for line in f:
            pos_hex, ch_hex, sp_hex = line.split()
            pos_bytes = bytes.fromhex(pos_hex)
            pos = ProofOfSpace.from_bytes(pos_bytes)
            ch = bytes32.fromhex(ch_hex)
            sp = bytes32.fromhex(sp_hex)
            proofs.append((pos, ch, sp))
    return proofs


def level_a(pos, ch, sp):
    q = verify_and_get_quality_string(
        pos,
        CONSTANTS,
        ch,
        sp,
        height=HEIGHT,
        prev_transaction_block_height=HEIGHT,
    )
    return q


def level_b(pos, ch, sp):
    v = Verifier()
    ver_quality_str = v.validate_proof(pos.compute_plot_id(), pos.param().size_v1, pos.challenge, pos.proof)
    return ver_quality_str


def time_one(fn, pos, ch, sp):
    t0 = perf_counter_ns()
    fn(pos, ch, sp)
    t1 = perf_counter_ns()
    return t1 - t0


def measure_paired(proofs):

    for idx, (pos, ch, sp) in enumerate(proofs):
        if level_a(pos, ch, sp) is None:
            raise RuntimeError(f"level_a rejected proof {idx}")
        if level_b(pos, ch, sp) is None:
            raise RuntimeError(f"level_b rejected proof {idx}")

    records = []  # (pass, proof index, first level, a_ns, b_ns)
    rng = random.Random(42)
    indices = list(range(len(proofs)))
    step = 0
    gc.disable()

    for p in range(N_PASSES):
        rng.shuffle(indices)
        for idx in indices:
            pos, ch, sp = proofs[idx]
            if step % 2 == 0:
                a = time_one(level_a, pos, ch, sp)
                b = time_one(level_b, pos, ch, sp)
                first = "A"
            else:
                b = time_one(level_b, pos, ch, sp)
                a = time_one(level_a, pos, ch, sp)
                first = "B"
            records.append((p, idx, first, a, b))
            step += 1

    gc.enable()
    return records


def compute_stats(times):
    q = statistics.quantiles(times, n=100)
    return {
        "n": len(times),
        "median": q[49],
        "p95": q[94],
        "p99": q[98],
    }


def print_stats(name, stats):
    print(f"level {name}: n={stats['n']}\nmedian={stats['median']} ns\np95={stats['p95']} ns\np99={stats['p99']} ns\n")


def main():
    if len(sys.argv) < 2:
        sys.exit("use: python bench_verif.py proofs.txt [output.csv]")
    proofs_path = sys.argv[1]
    csv_path = sys.argv[2] if len(sys.argv) > 2 else "verif_times.csv"

    proofs = load_proofs(proofs_path)
    k = proofs[0][0].param().size_v1
    print(f"{len(proofs)} proofs (k={k}), {N_PASSES} passes, height {HEIGHT}")

    records = measure_paired(proofs)
    times_a = [r[3] for r in records]
    times_b = [r[4] for r in records]
    diffs = [a - b for a, b in zip(times_a, times_b)]

    stats_a = compute_stats(times_a)
    stats_b = compute_stats(times_b)
    stats_d = compute_stats(diffs)
    print_stats("A (total: verify_and_get_quality_string)", stats_a)
    print_stats("B (crypto only: Verifier.validate_proof)", stats_b)
    print_stats("A - B per pair (Python overhead)", stats_d)
    print(f"Python share of total (medians): {stats_d['median'] / stats_a['median']:.1%}")

    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pass", "proof", "first", "k", "a_ns", "b_ns"])
        for p, idx, first, a, b in records:
            w.writerow([p, idx, first, k, a, b])
    print(f"times written in {csv_path}")


if __name__ == "__main__":
    main()
