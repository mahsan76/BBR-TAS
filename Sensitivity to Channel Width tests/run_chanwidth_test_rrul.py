#!/usr/bin/env python3
"""
BBR-TAS channel-width RRUL test runner (Reviewer 2, Comment 3)
---------------------------------------------------------------
Tests whether BBR-TAS's behaviour holds when the router's 5 GHz channel
width changes. Run ONCE PER WIDTH:

    python3 run_chanwidth_test_rrul.py 20
    python3 run_chanwidth_test_rrul.py 40
    python3 run_chanwidth_test_rrul.py 80
    python3 run_chanwidth_test_rrul.py 20 --dry-run     (prints the plan, changes nothing)

THIS COPY RUNS THE RRUL TEST ONLY (cubic, bbrv3, tas_s8_st8), always. Results go to
~/bbr-tas-chanwidth/<W>MHz_rrul/ so the single-flow / 8-stream results are never overwritten.

At the start the script PAUSES and asks you to set the router's 5 GHz
channel width by hand (same channel/band otherwise), then press Enter.

Configurations run at each width (CC set identically on client AND server):
    cubic, bbrv3,
    tas_s8_st8   <- the paper's headline pair for RRUL
Test per configuration: rrul, REPEATS runs (default 5; override with REPEATS=10).
The shift pair is deliberately NOT re-tuned per width: holding it fixed is what
tests whether the pair generalizes.

Results: ~/bbr-tas-chanwidth/<W>MHz_rrul/<config>/rrul_run<N>.flent.gz (+ .summary.txt,
.stats.txt), with manifest.log / failed_runs.log / flent_stdout.log at the top.
Re-running the same command RESUMES: configs with a .complete marker are skipped.

REQUIREMENTS: same as the other sweep scripts (tcp_bbr_tas.ko built on BOTH
machines, passwordless SSH + sudo, Flent installed). Optional:
    WIFI_IFACE=wlan0 python3 run_chanwidth_test_rrul.py 40
logs `iw dev wlan0 link` (negotiated width/MCS) -- only works if the Wi-Fi NIC
is visible to the machine running this script (not from inside a VM using a
virtual NIC); otherwise confirm the width on the router's admin page.

EDIT THE VARIABLES BELOW BEFORE RUNNING.
---------------------------------------------------------------
"""

import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------- USER CONFIGURATION ----------------------

FLENT_BIN = "/home/mahsan/flent/run-flent"

SERVER_IP = "192.168.1.169"
SERVERS = [SERVER_IP]
SSH_USER = "mahsan"

# -o ControlMaster=no: a multiplexed SSH master can pin a stale CC and block rmmod.
def ssh_prefix(ip: str) -> list:
    return ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", f"{SSH_USER}@{ip}"]

TAS_KO_PATH_CLIENT = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_KO_PATH_SERVER = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_MODULE_NAME = "tcp_bbr_tas"
TAS_CC_NAME = "tas"

REPEATS = int(os.environ.get("REPEATS", "5"))
TEST_LENGTH = 60
SETTLE_SLEEP = 5
BETWEEN_RUN_SLEEP = 3
WIFI_IFACE = os.environ.get("WIFI_IFACE", "")

# (flent test name, file prefix)
TEST_TYPES = [("tcp_upload", "tcp_upload"), ("tcp_8up", "tcp_8up")]

# (tag, kind, startup_shift, steady_shift)
CONFIGS = [
    ("cubic", "builtin:cubic", None, None),
    ("bbrv3", "builtin:bbr", None, None),
    ("tas_s7_st10", "tas", 7, 10),
    ("tas_s7_st8", "tas", 7, 8),
]

# ---------------------- END USER CONFIGURATION -------------------

args = [a for a in sys.argv[1:] if not a.startswith("--")]
DRY_RUN = "--dry-run" in sys.argv
RRUL_MODE = True   # this copy always runs the RRUL test (cubic, bbrv3, tas_s8_st8)
if len(args) != 1 or args[0] not in ("20", "40", "80"):
    sys.exit("Usage: python3 run_chanwidth_test_rrul.py <20|40|80> [--dry-run]")
WIDTH = args[0]

if RRUL_MODE:
    # RRUL headline pair from the paper is (8,8); everything else identical.
    TEST_TYPES = [("rrul", "rrul")]
    CONFIGS = [
        ("cubic", "builtin:cubic", None, None),
        ("bbrv3", "builtin:bbr", None, None),
        ("tas_s8_st8", "tas", 8, 8),
    ]

RESULTS_DIR = Path.home() / "bbr-tas-chanwidth" / (f"{WIDTH}MHz_rrul" if RRUL_MODE else f"{WIDTH}MHz")
RESUMING = RESULTS_DIR.is_dir()

if DRY_RUN:
    n = len(CONFIGS) * len(TEST_TYPES) * REPEATS
    print(f"DRY RUN -- width {WIDTH} MHz, results would go to {RESULTS_DIR}")
    for tag, kind, s, st in CONFIGS:
        print(f"  config {tag:12s} ({kind}" + (f", shifts {s}/{st}" if s else "") + ")")
    print(f"  tests: {[t for t, _ in TEST_TYPES]} x {REPEATS} repeats = {n} runs "
          f"(~{n * (TEST_LENGTH + BETWEEN_RUN_SLEEP + 10) / 60:.0f} min plus module switching)")
    sys.exit(0)

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST = RESULTS_DIR / "manifest.log"
FAILED_LOG = RESULTS_DIR / "failed_runs.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(MANIFEST, "a") as f:
        f.write(line + "\n")


def fail(msg: str) -> None:
    log(f"ERROR: {msg}")
    sys.exit(1)


def is_valid_gzip(path: Path) -> bool:
    """Flent can write a plain-text error to the raw-data path if the test failed;
    check the gzip magic bytes (1f 8b) rather than trusting the file exists."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def ssh_run(ip: str, remote_cmd: str, check=True):
    full = ssh_prefix(ip) + [remote_cmd]
    result = subprocess.run(full, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(f"SSH command failed on {ip}: {remote_cmd}\n{result.stderr}")
    return result


def run(cmd, check=True, capture=False):
    """Thin wrapper around subprocess.run with consistent error surfacing."""
    result = subprocess.run(
        cmd, shell=isinstance(cmd, str),
        capture_output=capture, text=True,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if capture else "(not captured)"
        fail(f"command failed ({result.returncode}): {cmd}\n{stderr}")
    return result


def deactivate_tas() -> None:
    """Switch away from 'tas' to cubic BEFORE rmmod, then unload the module --
    on the client AND on both local servers. Safe to call even if tas isn't
    currently loaded/active anywhere. The public host is untouched (see
    module docstring)."""
    run(["sudo", "sysctl", "-w", "net.ipv4.tcp_congestion_control=cubic"], capture=True)
    for ip in SERVERS:
        ssh_run(ip, "sudo sysctl -w net.ipv4.tcp_congestion_control=cubic >/dev/null")

    # --- client ---
    for attempt in range(1, 11):
        result = run(["sudo", "rmmod", TAS_MODULE_NAME], check=False, capture=True)
        if result.returncode == 0:
            break
        lsmod = run(["lsmod"], capture=True).stdout
        if not re.search(rf"^{re.escape(TAS_MODULE_NAME)}\b", lsmod, re.MULTILINE):
            break  # wasn't loaded to begin with -- nothing to do
        log(f"client rmmod attempt {attempt}/10 still busy, waiting...")
        time.sleep(3)
    else:
        lsmod = run(["lsmod"], capture=True).stdout
        loaded_line = next((l for l in lsmod.splitlines() if l.startswith(TAS_MODULE_NAME)), "")
        log(f"DIAGNOSTIC: client lsmod entry: {loaded_line}")
        ss_out = run(["sudo", "ss", "-tin"], check=False, capture=True).stdout
        tas_sockets = "\n".join(l for l in ss_out.splitlines() if " tas " in l)
        log(f"DIAGNOSTIC: client sockets using tas congestion control:\n{tas_sockets}")
        fail(f"could not rmmod {TAS_MODULE_NAME} on client after 10 attempts (module still in use)")

    # --- both local servers ---
    for ip in SERVERS:
        for attempt in range(1, 11):
            result = ssh_run(ip, f"sudo rmmod {TAS_MODULE_NAME}", check=False)
            if result.returncode == 0:
                break
            lsmod = ssh_run(ip, "lsmod", check=False).stdout
            if not re.search(rf"^{re.escape(TAS_MODULE_NAME)}\b", lsmod, re.MULTILINE):
                break
            log(f"{ip} rmmod attempt {attempt}/10 still busy, waiting...")
            time.sleep(3)
        else:
            lsmod = ssh_run(ip, "lsmod", check=False).stdout
            loaded_line = next((l for l in lsmod.splitlines() if l.startswith(TAS_MODULE_NAME)), "")
            log(f"DIAGNOSTIC: {ip} lsmod entry: {loaded_line}")
            fail(f"could not rmmod {TAS_MODULE_NAME} on {ip} after 10 attempts (module still in use)")


def set_builtin_cc(cc_name: str) -> None:
    deactivate_tas()

    run(["sudo", "sysctl", "-w", f"net.ipv4.tcp_congestion_control={cc_name}"], capture=True)
    for ip in SERVERS:
        ssh_run(ip, f"sudo sysctl -w net.ipv4.tcp_congestion_control={cc_name} >/dev/null")

    client_readback = run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"], capture=True).stdout.strip()
    server_readbacks = {ip: ssh_run(ip, "sysctl -n net.ipv4.tcp_congestion_control").stdout.strip()
                         for ip in SERVERS}
    log(f"Set builtin CC: client={client_readback} " +
        " ".join(f"{ip}={cc}" for ip, cc in server_readbacks.items()))

    if client_readback != cc_name:
        fail(f"CC verification mismatch on client (got={client_readback}, wanted={cc_name})")
    for ip, cc in server_readbacks.items():
        if cc != cc_name:
            fail(f"CC verification mismatch on {ip} (got={cc}, wanted={cc_name})")


def set_tas_cc(startup_shift: int, steady_shift: int) -> None:
    deactivate_tas()
    time.sleep(1)

    run([
        "sudo", "insmod", TAS_KO_PATH_CLIENT,
        f"tas_pacing_shift_startup={startup_shift}",
        f"tas_pacing_shift_steady={steady_shift}",
    ], capture=True)
    for ip in SERVERS:
        ssh_run(ip, f"sudo insmod {TAS_KO_PATH_SERVER} "
                    f"tas_pacing_shift_startup={startup_shift} "
                    f"tas_pacing_shift_steady={steady_shift}")

    run(["sudo", "sysctl", "-w", f"net.ipv4.tcp_congestion_control={TAS_CC_NAME}"], capture=True)
    for ip in SERVERS:
        ssh_run(ip, f"sudo sysctl -w net.ipv4.tcp_congestion_control={TAS_CC_NAME} >/dev/null")

    # Verify module parameters actually took, on client and both local servers
    param_dir = Path(f"/sys/module/{TAS_MODULE_NAME}/parameters")
    c_startup = (param_dir / "tas_pacing_shift_startup").read_text().strip()
    c_steady = (param_dir / "tas_pacing_shift_steady").read_text().strip()

    server_params = {}
    for ip in SERVERS:
        s_startup = ssh_run(ip, f"cat /sys/module/{TAS_MODULE_NAME}/parameters/tas_pacing_shift_startup").stdout.strip()
        s_steady = ssh_run(ip, f"cat /sys/module/{TAS_MODULE_NAME}/parameters/tas_pacing_shift_steady").stdout.strip()
        server_params[ip] = (s_startup, s_steady)

    log(f"TAS loaded: client(startup={c_startup},steady={c_steady}) " +
        " ".join(f"{ip}(startup={s},steady={st})" for ip, (s, st) in server_params.items()) +
        "")

    if int(c_startup) != startup_shift or int(c_steady) != steady_shift:
        fail(f"parameter verification mismatch on client for shifts {startup_shift}/{steady_shift}")
    for ip, (s, st) in server_params.items():
        if int(s) != startup_shift or int(st) != steady_shift:
            fail(f"parameter verification mismatch on {ip} for shifts {startup_shift}/{steady_shift}")


def run_one_flent_test(test_name: str, title: str, outdir: Path, final_name: str) -> bool:
    marker = outdir / ".marker"
    marker.touch()
    marker_mtime = marker.stat().st_mtime
    stem = final_name.replace(".flent.gz", "")

    cmd = [FLENT_BIN, test_name, "-H", SERVER_IP, "-l", str(TEST_LENGTH),
           "-t", title, "-D", str(outdir), "--socket-stats",
           "-o", str(outdir / f"{stem}.summary.txt")]
    with open(RESULTS_DIR / "flent_stdout.log", "a") as logf:
        subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    marker.unlink(missing_ok=True)

    newfile = next((f for f in outdir.glob("*.flent.gz") if f.stat().st_mtime > marker_mtime), None)
    if newfile is not None and is_valid_gzip(newfile):
        newfile.rename(outdir / final_name)
        # best-effort second pass: stats table from the same raw data (not counted as a failure)
        with open(RESULTS_DIR / "flent_stdout.log", "a") as logf:
            r = subprocess.run([FLENT_BIN, "-i", str(outdir / final_name), "-f", "stats",
                                "-o", str(outdir / f"{stem}.stats.txt")],
                               stdout=logf, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            log(f"  -> WARNING: stats post-processing failed for {title} (raw data still fine)")
        return True
    log(f"  -> no valid raw data file found for: {title}")
    if newfile is not None:
        log(f"     (found non-gzip file: {newfile}, left in place for inspection)")
    return False


def run_tests(tag: str) -> None:
    outdir = RESULTS_DIR / tag
    outdir.mkdir(parents=True, exist_ok=True)
    failures = 0
    log(f"Settling {SETTLE_SLEEP}s before testing config: {tag}")
    time.sleep(SETTLE_SLEEP)
    for run_num in range(1, REPEATS + 1):
        for test_name, prefix in TEST_TYPES:
            log(f"[{tag}] {prefix} run {run_num}/{REPEATS}")
            ok = run_one_flent_test(test_name, f"{WIDTH}MHz {tag} {prefix} run {run_num}",
                                    outdir, f"{prefix}_run{run_num}.flent.gz")
            if ok:
                log(f"[{tag}] {prefix} run {run_num}: OK")
            else:
                log(f"[{tag}] {prefix} run {run_num}: INVALID OUTPUT -- see flent_stdout.log")
                with open(FAILED_LOG, "a") as f:
                    f.write(f"{outdir / (prefix + '_run' + str(run_num) + '.flent.gz')}\n")
                failures += 1
            time.sleep(BETWEEN_RUN_SLEEP)
    if failures == 0:
        (outdir / ".complete").touch()
        log(f"Completed all runs for config: {tag} (marked complete)")
    else:
        log(f"Completed config {tag} WITH {failures} FAILURE(S) -- not marked complete, will re-run on resume")


def main() -> None:
    log(f"Channel-width run RESUMED: {WIDTH} MHz" if RESUMING else f"Channel-width run started: {WIDTH} MHz")
    log(f"Results will be saved under: {RESULTS_DIR}")
    if not shutil.which(FLENT_BIN) and not Path(FLENT_BIN).is_file():
        fail(f"FLENT_BIN not found or not executable: {FLENT_BIN}")

    print("\n>>> Set the router's 5 GHz channel width to "
          f"{WIDTH} MHz now (same channel/band otherwise), wait for the client to reconnect,")
    input(">>> then press Enter to continue... ")
    log(f"Router channel width set to {WIDTH} MHz (operator-confirmed)")
    std = input(">>> Negotiated standard on the client right now (e.g. 802.11ax, 802.11ac, 802.11n): ").strip()
    log(f"Negotiated standard at {WIDTH} MHz (operator-reported): {std or 'NOT RECORDED'}")
    if std and "ax" not in std.lower():
        log("NOTE: link is not 802.11ax -- results at this width confound channel width with PHY generation")
    if WIFI_IFACE:
        out = subprocess.run(["iw", "dev", WIFI_IFACE, "link"], capture_output=True, text=True)
        log("iw link state at start:\n" + (out.stdout or out.stderr))

    for tag, kind, startup, steady in CONFIGS:
        log(f"=== {tag} ===")
        if (RESULTS_DIR / tag / ".complete").exists():
            log(f"{tag} already complete, skipping")
            continue
        if kind.startswith("builtin:"):
            set_builtin_cc(kind.split(":", 1)[1])
        else:
            set_tas_cc(startup, steady)
        run_tests(tag)

    deactivate_tas()
    log(f"Width {WIDTH} MHz complete. All results under: {RESULTS_DIR}")
    if FAILED_LOG.exists() and FAILED_LOG.stat().st_size > 0:
        log(f"WARNING: {sum(1 for _ in open(FAILED_LOG))} run(s) produced invalid output. See: {FAILED_LOG}")
    else:
        log("All runs produced valid output files.")


if __name__ == "__main__":
    main()
