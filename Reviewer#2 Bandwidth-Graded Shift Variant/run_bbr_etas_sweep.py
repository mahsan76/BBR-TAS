#!/usr/bin/env python3
"""
run_bbr_etas_sweep.py

BBR-ETAS sweep runner -- Python port of run_bbr_tas_sweep-v3-with-4TCP-8TCP.sh,
adapted for the BBR-ETAS bandwidth-tiered pacing shift design.

Runs TCP upload tests (1, 4, and 8 simultaneous streams) and an RRUL_BE
Flent test across:
    - Cubic (baseline)
    - stock BBRv3 (baseline)
    - BBR-ETAS, across whatever module-parameter configs are listed in
      ETAS_CONFIGS below (edit this list for your actual sweep design --
      it currently holds only the module's default config as a placeholder)

Congestion control is set to match on BOTH client and server for every
run, since RRUL mixes upload and download flows and keeping upload/
download on the same TCP variant per test avoids a friendliness/
fairness confound between variants (same rationale as the BBR-TAS
sweep script this was ported from).

REQUIREMENTS BEFORE RUNNING:
    1. tcp_bbr_etas.ko built and present at the same path on BOTH
       client and server (built separately on each against that
       machine's own kernel headers -- do not copy the .ko itself
       between machines, rebuild on each).
    2. Passwordless SSH key auth from client -> server for the user
       running this script (needed for sudo rmmod/insmod/sysctl on
       the server without prompting mid-sweep).
    3. Flent installed on the client; the iperf3/netperf backend Flent
       needs is running/available on the server as usual.
    4. Wi-Fi channel already confirmed clear of interference.

EDIT THE CONFIGURATION CONSTANTS BELOW BEFORE RUNNING -- in particular
ETAS_CONFIGS, which is currently just the module's compiled-in
defaults and not a real factorial design.

Resume support:
    python3 run_bbr_etas_sweep.py                    Start a fresh sweep (new timestamped dir)
    python3 run_bbr_etas_sweep.py /path/to/prior/dir  Resume: any config whose folder already
                                                       contains a .complete marker (all runs
                                                       succeeded, zero failures) is skipped;
                                                       everything else re-runs from scratch.
"""

import datetime
import hashlib
import shlex
import subprocess
import sys
import time
from pathlib import Path

# ---------------------- USER CONFIGURATION ----------------------

SERVER_IP = "192.168.0.180"        # server's IP, as seen from client
SERVER_SSH_USER = "mahsan"         # SSH user for server-side commands

# -o ControlMaster=no explicitly disables connection multiplexing for
# these calls, even if the user's ~/.ssh/config enables it globally.
# A persistent multiplexed master connection can hold a long-lived
# reference to whatever congestion control was active when it was
# established, which prevents `rmmod tcp_bbr_etas` from ever succeeding
# for the lifetime of that master connection -- not just until the
# test's own sockets close.
SSH_OPTS = ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
SERVER_SSH_TARGET = f"{SERVER_SSH_USER}@{SERVER_IP}"

ETAS_KO_PATH_CLIENT = "/home/mahsan/bbr-etas/tcp_bbr_etas.ko"
ETAS_KO_PATH_SERVER = "/home/mahsan/bbr-etas/tcp_bbr_etas.ko"   # path ON the server

ETAS_MODULE_NAME = "tcp_bbr_etas"   # module name as shown in `lsmod`
ETAS_CC_NAME = "etas"               # .name field registered in tcp_congestion_ops

FLENT_BIN = "/home/mahsan/flent/run-flent"   # path to the Flent binary/wrapper to use

REPEATS = 10             # number of repeats per test per config
TEST_LENGTH = 60          # seconds
SETTLE_SLEEP = 5          # seconds to wait after switching CC before testing
BETWEEN_RUN_SLEEP = 3     # seconds between repeated runs of the same test

# Test types run every repeat, in this order: (flent_test_name, file_prefix)
TEST_TYPES = [
    ("tcp_upload", "tcp_upload"),
    ("tcp_4up", "tcp_4up"),
    ("tcp_8up", "tcp_8up"),
    ("rrul", "rrul"),
]

# BBR-ETAS module-parameter sweep. Each entry is a dict of module params
# passed straight to insmod, e.g.:
#   {"etas_bw_low_thresh_bps": 12500000, "etas_bw_high_thresh_bps": 118000000,
#    "etas_shift_min": 7, "etas_shift_max": 10, "etas_hold_rounds": 2}
#
# 3x3 factorial on (etas_shift_min, etas_shift_max), the analogue of
# BBR-TAS's (startup_shift, steady_shift) grid: shift_max E {8,9,10}
# (tight end, low-bandwidth tier) x shift_min E {5,6,7} (loose end,
# high-bandwidth tier). Bandwidth thresholds and hold_rounds are held
# at module defaults, since the shift range is the algorithm's core
# novelty and this keeps the design a clean 2-factor sweep.
ETAS_SHIFT_MAX_VALUES = [8, 9, 10]
ETAS_SHIFT_MIN_VALUES = [5, 6, 7]
ETAS_BW_LOW_THRESH_BPS = 12500000    # module default (~100 Mbps), fixed for this sweep
ETAS_BW_HIGH_THRESH_BPS = 118000000  # module default (~944 Mbps), fixed for this sweep
ETAS_HOLD_ROUNDS = 2                 # module default, fixed for this sweep

ETAS_CONFIGS = [
    {
        "etas_bw_low_thresh_bps": ETAS_BW_LOW_THRESH_BPS,
        "etas_bw_high_thresh_bps": ETAS_BW_HIGH_THRESH_BPS,
        "etas_shift_min": shift_min,
        "etas_shift_max": shift_max,
        "etas_hold_rounds": ETAS_HOLD_ROUNDS,
    }
    for shift_max in ETAS_SHIFT_MAX_VALUES
    for shift_min in ETAS_SHIFT_MIN_VALUES
    if shift_min <= shift_max  # drop inverted pairs: interpolation assumes
                               # shift decreases as bandwidth increases
]

# ---------------------- END USER CONFIGURATION -------------------

if len(sys.argv) >= 2:
    RESULTS_DIR = Path(sys.argv[1])
    if not RESULTS_DIR.is_dir():
        print(f"ERROR: resume directory does not exist: {RESULTS_DIR}", file=sys.stderr)
        sys.exit(1)
    RESUMING = True
else:
    RESULTS_DIR = Path.home() / "bbr-etas-results" / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    RESUMING = False

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST = RESULTS_DIR / "manifest.log"
FLENT_STDOUT_LOG = RESULTS_DIR / "flent_stdout.log"
FAILED_RUNS_LOG = RESULTS_DIR / "failed_runs.log"


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(MANIFEST, "a") as f:
        f.write(line + "\n")


def fail(msg: str) -> None:
    log(f"ERROR: {msg}")
    sys.exit(1)


def run(cmd, check=True, capture=True):
    """Run a local command, returning CompletedProcess. Raises on failure if check=True."""
    result = subprocess.run(
        cmd, capture_output=capture, text=True,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )
    return result


def ssh_run(remote_cmd: str, check=True):
    """Run a command on the server over SSH, returning CompletedProcess."""
    cmd = ["ssh"] + SSH_OPTS + [SERVER_SSH_TARGET, remote_cmd]
    return run(cmd, check=check)


def local_ok(cmd) -> bool:
    """Run a local command, return True/False instead of raising."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def ssh_ok(remote_cmd: str) -> bool:
    result = subprocess.run(
        ["ssh"] + SSH_OPTS + [SERVER_SSH_TARGET, remote_cmd],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def is_valid_gzip(path: Path) -> bool:
    """Flent can write a plain-text error message to the -o path instead of
    real gzip data if the test itself failed (e.g. netperf/iperf backend
    unreachable on the server), so a file existing is not proof it's valid.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as f:
        magic = f.read(2)
    return magic == b"\x1f\x8b"


def check_flent_bin() -> None:
    p = Path(FLENT_BIN)
    if not (p.exists() and (p.stat().st_mode & 0o111)):
        fail(f"FLENT_BIN not found or not executable: {FLENT_BIN}")
    log(f"Using Flent binary: {FLENT_BIN}")


def deactivate_etas() -> None:
    """Switch CC away from 'etas' to cubic BEFORE rmmod, on both hosts.

    The kernel refuses to rmmod a congestion-control module while it is
    still the active net.ipv4.tcp_congestion_control (or while any live
    socket references it) -- this must run first, every time, on both
    client and server, or rmmod fails with "module is in use".
    Safe to call even if etas isn't currently loaded/active.
    """
    if not local_ok(["sudo", "sysctl", "-w", "net.ipv4.tcp_congestion_control=cubic"]):
        fail("could not switch client CC to cubic before rmmod")
    if not ssh_ok("sudo sysctl -w net.ipv4.tcp_congestion_control=cubic"):
        fail("could not switch server CC to cubic before rmmod")

    # rmmod can still legitimately fail here if a socket from the just-
    # finished test hasn't closed yet even after switching CC away from
    # etas. Retry a few times with a short wait rather than failing
    # immediately.
    for attempt in range(1, 11):
        if local_ok(["sudo", "rmmod", ETAS_MODULE_NAME]):
            break
        if not local_ok(["bash", "-c", f"lsmod | grep -q '^{ETAS_MODULE_NAME}'"]):
            break  # wasn't loaded to begin with
        log(f"client rmmod attempt {attempt}/10 still busy, waiting...")
        time.sleep(3)
    if local_ok(["bash", "-c", f"lsmod | grep -q '^{ETAS_MODULE_NAME}'"]):
        log(f"DIAGNOSTIC: lsmod entry for {ETAS_MODULE_NAME}:")
        r = run(["bash", "-c", f"lsmod | grep '^{ETAS_MODULE_NAME}'"], check=False)
        log(r.stdout.strip())
        log(f"DIAGNOSTIC: sockets currently using {ETAS_CC_NAME} congestion control:")
        r = run(["bash", "-c", f"sudo ss -tin 2>/dev/null | grep -B1 ' {ETAS_CC_NAME} '"], check=False)
        log(r.stdout.strip())
        log("DIAGNOSTIC: check for a lingering SSH control master:")
        r = run(["ssh", "-O", "check", SERVER_SSH_TARGET], check=False)
        log((r.stdout + r.stderr).strip())
        fail(f"could not rmmod {ETAS_MODULE_NAME} on client after 10 attempts (module still in use)")

    for attempt in range(1, 11):
        if ssh_ok(f"sudo rmmod {ETAS_MODULE_NAME} 2>/dev/null"):
            break
        if not ssh_ok(f"lsmod | grep -q '^{ETAS_MODULE_NAME}'"):
            break
        log(f"server rmmod attempt {attempt}/10 still busy, waiting...")
        time.sleep(3)
    if ssh_ok(f"lsmod | grep -q '^{ETAS_MODULE_NAME}'"):
        log(f"DIAGNOSTIC: server lsmod entry for {ETAS_MODULE_NAME}:")
        r = ssh_run(f"lsmod | grep '^{ETAS_MODULE_NAME}'", check=False)
        log(r.stdout.strip())
        log(f"DIAGNOSTIC: server sockets currently using {ETAS_CC_NAME} congestion control:")
        r = ssh_run(f"sudo ss -tin 2>/dev/null | grep -B1 ' {ETAS_CC_NAME} '", check=False)
        log(r.stdout.strip())
        fail(f"could not rmmod {ETAS_MODULE_NAME} on server after 10 attempts (module still in use)")


def set_builtin_cc(cc_name: str) -> None:
    """Sets congestion control to a built-in name (cubic/bbr) on both hosts."""
    deactivate_etas()  # switch away from etas + unload it, in the correct order

    if not local_ok(["sudo", "sysctl", "-w", f"net.ipv4.tcp_congestion_control={cc_name}"]):
        fail(f"could not set client CC to {cc_name}")
    if not ssh_ok(f"sudo sysctl -w net.ipv4.tcp_congestion_control={cc_name}"):
        fail(f"could not set server CC to {cc_name}")

    client_cc = run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"]).stdout.strip()
    server_cc = ssh_run("sysctl -n net.ipv4.tcp_congestion_control").stdout.strip()
    log(f"Set builtin CC: client={client_cc} server={server_cc}")

    if not (client_cc == cc_name and server_cc == cc_name):
        fail(f"CC verification mismatch (client={client_cc} server={server_cc}, wanted={cc_name})")


def set_etas_cc(params: dict) -> None:
    """Loads BBR-ETAS with the given module params on both hosts, then activates it."""
    deactivate_etas()  # switch away from etas + unload the old-params instance
    time.sleep(1)

    param_args = [f"{k}={v}" for k, v in params.items()]

    if not local_ok(["sudo", "insmod", ETAS_KO_PATH_CLIENT] + param_args):
        fail(f"insmod failed on client for params {params}")

    remote_insmod = "sudo insmod " + ETAS_KO_PATH_SERVER + " " + " ".join(param_args)
    if not ssh_ok(remote_insmod):
        fail(f"insmod failed on server for params {params}")

    if not local_ok(["sudo", "sysctl", "-w", f"net.ipv4.tcp_congestion_control={ETAS_CC_NAME}"]):
        fail(f"could not set client CC to {ETAS_CC_NAME}")
    if not ssh_ok(f"sudo sysctl -w net.ipv4.tcp_congestion_control={ETAS_CC_NAME}"):
        fail(f"could not set server CC to {ETAS_CC_NAME}")

    # Verify module parameters actually took, on both sides
    mismatches = []
    reported = {"client": {}, "server": {}}
    for param_name in params:
        sysfs_path = f"/sys/module/{ETAS_MODULE_NAME}/parameters/{param_name}"
        c_val = run(["cat", sysfs_path]).stdout.strip()
        s_val = ssh_run(f"cat {sysfs_path}").stdout.strip()
        reported["client"][param_name] = c_val
        reported["server"][param_name] = s_val
        if c_val != str(params[param_name]) or s_val != str(params[param_name]):
            mismatches.append(param_name)

    log(f"ETAS loaded: client={reported['client']} server={reported['server']}")

    if mismatches:
        fail(f"parameter verification mismatch for {mismatches} (wanted {params})")


def run_one_flent_test(test_name: str, title: str, outdir: Path, final_name: str) -> bool:
    """Runs a single Flent test, capturing the auto-named raw data file
    Flent writes into -D and renaming it to a predictable name. Also
    enables --socket-stats during the live test, and, once the raw file
    is confirmed valid, runs a second cheap post-processing pass against
    that same saved file (-i, no live traffic re-run) to also emit a
    `-f stats` table alongside the existing summary.txt.
    """
    marker_time = time.time()

    cmd = [
        FLENT_BIN, test_name,
        "-H", SERVER_IP,
        "-l", str(TEST_LENGTH),
        "-t", title,
        "-D", str(outdir),
        "--socket-stats",
        "-o", str(outdir / f"{Path(final_name).stem.replace('.flent', '')}.summary.txt"),
    ]
    with open(FLENT_STDOUT_LOG, "a") as f:
        f.write(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}\n")
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)

    candidates = [
        p for p in outdir.glob("*.flent.gz")
        if p.stat().st_mtime > marker_time
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    newfile = candidates[0] if candidates else None

    if newfile is not None and is_valid_gzip(newfile):
        final_path = outdir / final_name
        newfile.rename(final_path)

        # Second pass: reformat the SAME already-collected raw data as
        # `stats`, into a separate file. Best-effort -- a failure here
        # does NOT count against the run itself.
        stats_out = outdir / f"{Path(final_name).stem.replace('.flent', '')}.stats.txt"
        stats_cmd = [FLENT_BIN, "-i", str(final_path), "-f", "stats", "-o", str(stats_out)]
        with open(FLENT_STDOUT_LOG, "a") as f:
            f.write(f"\n$ {' '.join(shlex.quote(c) for c in stats_cmd)}\n")
            stats_result = subprocess.run(stats_cmd, stdout=f, stderr=subprocess.STDOUT, text=True)
        if stats_result.returncode != 0:
            log(f"  -> WARNING: stats-format post-processing failed for: {title} "
                f"(raw data and summary.txt are still fine)")
        return True
    else:
        log(f"  -> no valid raw data file found for: {title}")
        if newfile is not None:
            log(f"     (found non-gzip file: {newfile}, left in place for inspection)")
        return False


def run_tests(tag: str) -> None:
    """Runs the four Flent tests REPEATS times each, for the current CC config."""
    outdir = RESULTS_DIR / tag
    outdir.mkdir(parents=True, exist_ok=True)

    tag_failures = 0

    log(f"Settling {SETTLE_SLEEP}s before testing config: {tag}")
    time.sleep(SETTLE_SLEEP)

    for run_idx in range(1, REPEATS + 1):
        for test_name, file_prefix in TEST_TYPES:
            log(f"[{tag}] {file_prefix} run {run_idx}/{REPEATS}")
            final_name = f"{file_prefix}_run{run_idx}.flent.gz"
            title = f"{tag} {file_prefix} run {run_idx}"
            if run_one_flent_test(test_name, title, outdir, final_name):
                log(f"[{tag}] {file_prefix} run {run_idx}: OK")
            else:
                log(f"[{tag}] {file_prefix} run {run_idx}: INVALID OUTPUT -- see flent_stdout.log")
                with open(FAILED_RUNS_LOG, "a") as f:
                    f.write(f"{outdir / final_name}\n")
                tag_failures += 1
            time.sleep(BETWEEN_RUN_SLEEP)

    if tag_failures == 0:
        (outdir / ".complete").touch()
        log(f"Completed all runs for config: {tag} (marked complete)")
    else:
        log(f"Completed all runs for config: {tag} WITH {tag_failures} FAILURE(S) "
            f"-- not marked complete, will re-run on resume")


def config_already_complete(tag: str) -> bool:
    return (RESULTS_DIR / tag / ".complete").exists()


def etas_tag(params: dict) -> str:
    """Builds a short, deterministic, filesystem-safe tag for a param dict."""
    parts = [f"{k.replace('etas_', '')}{v}" for k, v in params.items()]
    tag = "etas_" + "_".join(parts)
    if len(tag) > 120:  # fall back to a hash if the param set gets long
        h = hashlib.sha1(repr(sorted(params.items())).encode()).hexdigest()[:10]
        tag = f"etas_{h}"
    return tag


def main() -> None:
    if RESUMING:
        log(f"BBR-ETAS sweep RESUMED: {datetime.datetime.now()}")
    else:
        log(f"BBR-ETAS sweep started: {datetime.datetime.now()}")

    check_flent_bin()

    log(f"Results will be saved under: {RESULTS_DIR}")

    # --- Baselines ---
    log("=== Baseline: Cubic ===")
    if config_already_complete("cubic"):
        log("cubic already complete, skipping")
    else:
        set_builtin_cc("cubic")
        run_tests("cubic")

    log("=== Baseline: stock BBRv3 ===")
    if config_already_complete("bbrv3"):
        log("bbrv3 already complete, skipping")
    else:
        set_builtin_cc("bbr")
        run_tests("bbrv3")

    # --- BBR-ETAS sweep ---
    for params in ETAS_CONFIGS:
        tag = etas_tag(params)
        if config_already_complete(tag):
            log(f"=== BBR-ETAS config: {params} -- already complete, skipping ===")
            continue
        log(f"=== BBR-ETAS config: {params} ===")
        set_etas_cc(params)
        run_tests(tag)

    # --- Cleanup: switch back to cubic (unloading etas in the process) ---
    deactivate_etas()

    log(f"Sweep complete: {datetime.datetime.now()}")
    log(f"All results under: {RESULTS_DIR}")

    if FAILED_RUNS_LOG.exists() and FAILED_RUNS_LOG.stat().st_size > 0:
        fail_count = sum(1 for _ in open(FAILED_RUNS_LOG))
        log(f"WARNING: {fail_count} run(s) produced invalid output. See:")
        log(f"  {FAILED_RUNS_LOG}")
        log("Re-check server-side netperf/iperf backend and re-run just these configs.")
    else:
        log("All runs produced valid output files.")


if __name__ == "__main__":
    main()
