#!/usr/bin/env python3
"""
sample_bbr_internals.py

Polls `ss -ti dst <remote_ip>` repeatedly during a single TCP-upload run and
logs BBR-v3's OWN internal bandwidth and min-RTT estimates (the bbr:(bw:...,
mrtt:...) block ss prints for any CC that implements .get_info), together
with cwnd / pacing_rate / delivery_rate, to a CSV.

This is meant to run IN PARALLEL with the actual data transfer (iperf3 or a
flent tcp_upload test) on the CLIENT (sending) machine, against the same
server IP used by that test, so the socket ss finds is the test flow.

Usage:
    python3 sample_bbr_internals.py <remote_ip> <duration_sec> <interval_sec> <output_csv> [--local-port PORT]

    remote_ip     - server IP the test connects to (same as ss -ti dst filter)
    duration_sec  - how long to sample for (should cover the test's length)
    interval_sec  - polling interval, e.g. 0.1 for 10 Hz
    output_csv    - path to write results
    --local-port  - optional: restrict to one local port if multiple flows
                    to the same remote_ip might be active concurrently

Output CSV columns:
    timestamp, local_port, remote_port, mrtt_ms, bw_mbps,
    pacing_rate_mbps, delivery_rate_mbps, cwnd

Notes on parsing:
    ss -ti prints each socket as two physical lines:
        ESTAB   0   0   <local_ip>:<local_port>   <remote_ip>:<remote_port>
            tas rto:... rtt:.../... ... bbr:(bw:8bps,mrtt:4.71,pacing_gain:1,cwnd_gain:2) ...
    bw can appear in bps / Kbps / Mbps / Gbps - normalized to Mbps here.
"""
import subprocess
import sys
import time
import re
import csv
import argparse

HEADER_RE = re.compile(
    r"^\S+\s+\d+\s+\d+\s+\S+:(?P<lport>\d+)\s+\S+:(?P<rport>\d+)"
)
BBR_RE = re.compile(
    r"bbr:\(bw:(?P<bw>[\d.]+)(?P<bw_unit>[GMK]?bps),"
    r"mrtt:(?P<mrtt>[\d.]+)"
)
PACING_RE = re.compile(r"pacing_rate\s+(?P<val>[\d.]+)(?P<unit>[GMK]?bps)")
DELIVERY_RE = re.compile(r"delivery_rate\s+(?P<val>[\d.]+)(?P<unit>[GMK]?bps)")
CWND_RE = re.compile(r"\bcwnd:(?P<cwnd>\d+)")

UNIT_TO_MBPS = {
    "bps": 1e-6,
    "Kbps": 1e-3,
    "Mbps": 1.0,
    "Gbps": 1e3,
}


def to_mbps(value, unit):
    return float(value) * UNIT_TO_MBPS.get(unit, 1.0)


def parse_ss_output(text, local_port_filter=None):
    """Yields dicts, one per matched socket block, for the current snapshot."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        hdr = HEADER_RE.match(line)
        if hdr and i + 1 < len(lines):
            info_line = lines[i + 1]
            lport = hdr.group("lport")
            rport = hdr.group("rport")
            if local_port_filter and lport != str(local_port_filter):
                i += 2
                continue
            bbr = BBR_RE.search(info_line)
            if bbr:
                bw_mbps = to_mbps(bbr.group("bw"), bbr.group("bw_unit"))
                mrtt_ms = float(bbr.group("mrtt"))
                pacing = PACING_RE.search(info_line)
                delivery = DELIVERY_RE.search(info_line)
                cwnd = CWND_RE.search(info_line)
                yield {
                    "local_port": lport,
                    "remote_port": rport,
                    "mrtt_ms": mrtt_ms,
                    "bw_mbps": bw_mbps,
                    "pacing_rate_mbps": (
                        to_mbps(pacing.group("val"), pacing.group("unit"))
                        if pacing else ""
                    ),
                    "delivery_rate_mbps": (
                        to_mbps(delivery.group("val"), delivery.group("unit"))
                        if delivery else ""
                    ),
                    "cwnd": cwnd.group("cwnd") if cwnd else "",
                }
            i += 2
        else:
            i += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("remote_ip")
    ap.add_argument("duration_sec", type=float)
    ap.add_argument("interval_sec", type=float)
    ap.add_argument("output_csv")
    ap.add_argument("--local-port", default=None)
    args = ap.parse_args()

    end_time = time.time() + args.duration_sec
    rows_written = 0

    with open(args.output_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "local_port", "remote_port", "mrtt_ms", "bw_mbps",
            "pacing_rate_mbps", "delivery_rate_mbps", "cwnd",
        ])

        while time.time() < end_time:
            ts = time.time()
            try:
                out = subprocess.run(
                    ["ss", "-ti", "dst", args.remote_ip],
                    capture_output=True, text=True, timeout=2,
                ).stdout
            except Exception as e:
                print(f"WARN: ss call failed: {e}", file=sys.stderr)
                time.sleep(args.interval_sec)
                continue

            for rec in parse_ss_output(out, args.local_port):
                writer.writerow([
                    f"{ts:.3f}", rec["local_port"], rec["remote_port"],
                    rec["mrtt_ms"], f"{rec['bw_mbps']:.6f}",
                    rec["pacing_rate_mbps"], rec["delivery_rate_mbps"],
                    rec["cwnd"],
                ])
                rows_written += 1
            f.flush()
            time.sleep(args.interval_sec)

    print(f"Done. {rows_written} samples written to {args.output_csv}")


if __name__ == "__main__":
    main()
