#!/usr/bin/env python3
"""
analyze_bbr_internal_bias.py

Aggregates per-run CSVs produced by sample_bbr_internals.py across two (or
more) shift settings and tests whether BBR-v3's OWN internal mrtt / bw
estimates differ systematically with the TSQ shift value -- i.e. whether
the shift biases BBR-v3's bandwidth/RTT sampling, as Reviewer 2 asked.

Expected input layout (one CSV per run, named/grouped by shift value):

    results/
      shift7/run1.csv  run2.csv  run3.csv  run4.csv  run5.csv
      shift10/run1.csv run2.csv run3.csv run4.csv run5.csv

Usage:
    python3 analyze_bbr_internal_bias.py \
        --group shift7  results/shift7/*.csv \
        --group shift10 results/shift10/*.csv \
        --out report.txt

For each run (CSV), computes the per-run MEAN mrtt_ms and MEAN bw_mbps
(restricted to the app_limited-free, steady portion if you pass
--skip-seconds to drop ramp-up samples). Then runs a Welch's two-tailed
t-test comparing the per-run means across the two groups for mrtt and for
bw, mirroring the paper's existing Welch's-t-test methodology (n=runs per
group, not n=samples, to avoid pseudo-replication).
"""
import argparse
import csv
import sys
from collections import defaultdict
from statistics import mean, stdev

try:
    from scipy import stats
except ImportError:
    print("ERROR: this script needs scipy (pip install --break-system-packages scipy)",
          file=sys.stderr)
    sys.exit(1)


def load_run_means(path, skip_seconds=0.0):
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r["bw_mbps"] == "" or r["mrtt_ms"] == "":
                continue
            rows.append((float(r["timestamp"]), float(r["mrtt_ms"]), float(r["bw_mbps"])))

    if not rows:
        return None

    t0 = rows[0][0]
    rows = [r for r in rows if (r[0] - t0) >= skip_seconds]
    if not rows:
        return None

    mrtts = [r[1] for r in rows]
    bws = [r[2] for r in rows]
    return {
        "n_samples": len(rows),
        "mrtt_mean": mean(mrtts),
        "bw_mean": mean(bws),
    }


def welch_report(label, a, b, group_a_name, group_b_name):
    t, p = stats.ttest_ind(a, b, equal_var=False)
    lines = [
        f"--- {label}: {group_a_name} (n={len(a)}) vs {group_b_name} (n={len(b)}) ---",
        f"{group_a_name} mean = {mean(a):.6g}" + (f", sd = {stdev(a):.4g}" if len(a) > 1 else ""),
        f"{group_b_name} mean = {mean(b):.6g}" + (f", sd = {stdev(b):.4g}" if len(b) > 1 else ""),
        f"Welch's t = {t:.4f}, two-tailed p = {p:.4g}"
        + ("  <-- significant at alpha=0.05" if p < 0.05 else "  (not significant at alpha=0.05)"),
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", nargs="+", action="append", required=True,
                     metavar=("NAME", "CSV"),
                     help="--group <name> <csv1> <csv2> ... ; pass twice, once per shift setting")
    ap.add_argument("--skip-seconds", type=float, default=5.0,
                     help="drop samples within this many seconds of run start (ramp-up), default 5s")
    ap.add_argument("--out", default=None, help="optional path to also write the report to a file")
    args = ap.parse_args()

    if len(args.group) != 2:
        print("ERROR: pass exactly two --group blocks (two shift settings to compare)", file=sys.stderr)
        sys.exit(1)

    groups = {}
    for block in args.group:
        name = block[0]
        csvs = block[1:]
        if not csvs:
            print(f"ERROR: no CSVs given for group {name}", file=sys.stderr)
            sys.exit(1)
        per_run = []
        for c in csvs:
            summary = load_run_means(c, skip_seconds=args.skip_seconds)
            if summary is None:
                print(f"WARN: no usable samples in {c} (skipped)", file=sys.stderr)
                continue
            per_run.append((c, summary))
        groups[name] = per_run

    (name_a, runs_a), (name_b, runs_b) = list(groups.items())

    out_lines = []
    out_lines.append(f"BBR-v3 internal-estimator bias check: {name_a} vs {name_b}")
    out_lines.append(f"(skip_seconds={args.skip_seconds}; one data point per run = mean over that run's samples)")
    out_lines.append("")

    out_lines.append(f"{name_a} per-run means:")
    for c, s in runs_a:
        out_lines.append(f"  {c}: mrtt_mean={s['mrtt_mean']:.4f} ms, bw_mean={s['bw_mean']:.2f} Mbps, n_samples={s['n_samples']}")
    out_lines.append(f"\n{name_b} per-run means:")
    for c, s in runs_b:
        out_lines.append(f"  {c}: mrtt_mean={s['mrtt_mean']:.4f} ms, bw_mean={s['bw_mean']:.2f} Mbps, n_samples={s['n_samples']}")
    out_lines.append("")

    mrtt_a = [s["mrtt_mean"] for _, s in runs_a]
    mrtt_b = [s["mrtt_mean"] for _, s in runs_b]
    bw_a = [s["bw_mean"] for _, s in runs_a]
    bw_b = [s["bw_mean"] for _, s in runs_b]

    if len(mrtt_a) < 2 or len(mrtt_b) < 2:
        out_lines.append("NOTE: fewer than 2 runs in one group -- t-test needs >=2 per group for a variance estimate.")
    else:
        out_lines.append(welch_report("Internal min-RTT estimate (BBR mrtt)", mrtt_a, mrtt_b, name_a, name_b))
        out_lines.append(welch_report("Internal bandwidth estimate (BBR bw)", bw_a, bw_b, name_a, name_b))

    report = "\n".join(out_lines)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"\nReport also written to {args.out}")


if __name__ == "__main__":
    main()
