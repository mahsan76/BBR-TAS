#!/usr/bin/env python3
"""
run_cc_sweep.py

Congestion-control sweep runner covering FOUR CCAs:
    - Cubic (baseline, builtin)
    - stock BBRv3 (baseline, builtin)
    - BBR-TAS (loadable module, CC name "tas"), across whatever
      (startup_shift, steady_shift) configs are listed in TAS_CONFIGS
    - BBR-ETAS (loadable module, CC name "etas"), across whatever
      (shift_min, shift_max) configs are listed in ETAS_CONFIGS

Runs Flent's tcp_1up / tcp_4up / tcp_8up / tcp_12up tests plus RRUL,
REPEATS times each, for every CCA/config combination.

Congestion control is set to match on BOTH client and server for every
run, since RRUL mixes upload and download flows and keeping upload/
download on the same TCP variant per test avoids a friendliness/
fairness confound between variants.

REQUIREMENTS BEFORE RUNNING:
    1. tcp_bbr_tas.ko AND tcp_bbr_etas.ko built and present at the same
       paths on BOTH client and server (built separately on each
       against that machine's own kernel headers -- do not copy the
       .ko itself between machines, rebuild on each).
    2. Passwordless SSH key auth from client -> server for the user
       running this script (needed for sudo rmmod/insmod/sysctl on
       the server without prompting mid-sweep).
    3. Flent installed on the client; the iperf3/netperf backend Flent
       needs is running/available on the server as usal.
    4. Wi-Fi channel already confirmed clear of interference.
    5. Run `flent --list-tests` and confirm tcp_1up/tcp_4up/tcp_8up/
       tcp_12up/rrul are all present in your Flent version BEFORE
       starting the sweep -- tcp_12up in particular is not part of
       Flent's standard tcp_Nup series (1/2/4/8/16/32...) as of this
       writing, so it's worth verifying it actually exists rather
       than discovering a typo/version mismatch partway through an
       overnight sweep.

EDIT THE CONFIGURATION CONSTANTS BELOW BEFORE RUNNING -- in particular
TAS_CONFIGS and ETAS_CONFIGS, which are placeholders illustrating the
sweep-list shape, not necessarily your intended final factorial design.

Channel-36 min7_max8 + TAS 7/9 confirmation (8-up and RRUL only):
    Purpose: re-test ETAS min7_max8 (the original manuscript's strongest
    config: +63.5%/+68.5% latency improvement vs Cubic/BBR-v3 at 8-up;
    +14.4% throughput vs BBR-v3, +41.3% latency vs Cubic at rrul, all
    measured on the old, interfered channel) against both baselines AND
    BBR-TAS 7/9 (the config used throughout the earlier channel-36 5-run
    and 20-run sweeps), on channel 36, restricted to the two tests where
    an effect has actually appeared (tcp_8up, rrul). This lets the ETAS
    result be compared against BBR-TAS on the same clean channel, not
    just against the two baselines.
    4 configs run (cubic, bbrv3, tas_startup7_steady9, etas_min7_max8);
    REPEATS kept at 10.

Run ordering (ROUND_ROBIN = True, the default):
    Round 1 runs every config once, round 2 runs every config again, and so
    on up to REPEATS. With SHUFFLE_WITHIN_ROUND = True the config order is
    shuffled in each round (seeded from the results-dir name, so a resume
    reproduces the same order). This spreads slow Wi-Fi drift/interference
    evenly across configs instead of letting it land on whichever config
    happened to be running during a bad hour. Set ROUND_ROBIN = False for
    the old behaviour (all repeats of one config back to back).

Labels / folders:
    cubic, bbrv3, tas_startup<S>_steady<T>, etas_min<MIN>_max<MAX>
    ETAS thresholds and hold_rounds are fixed for the sweep, so they are left
    out of labels and console output. They are still passed to insmod,
    still verified via sysfs, and written to params.txt in each config folder.

Resume support:
    python3 run_cc_sweep.py                    Start a fresh sweep (new timestamped dir)
    python3 run_cc_sweep.py /path/to/prior/dir  Resume: finished (config, run) pairs are
                                                  skipped (.runN.ok marker); a config whose
                                                  folder has .complete is skipped entirely.
                                                  A partly finished (config, run) re-runs.
"""

import datetime
import hashlib
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path

# ---------------------- USER CONFIGURATION ----------------------

SERVER_IP = "192.168.1.169"        # server's IP, as seen from client
SERVER_SSH_USER = "mahsan"         # SSH user for server-side commands

# -o ControlMaster=no explicitly disables connection multiplexing for
# these calls, even if the user's ~/.ssh/config enables it globally.
# A persistent multiplexed master connection can hold a long-lived
# reference to whatever congestion control was active when it was
# established, which prevents `rmmod` from ever succeeding for the
# lifetime of that master connection -- not just until the test's own
# sockets close.
SSH_OPTS = ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
SERVER_SSH_TARGET = f"{SERVER_SSH_USER}@{SERVER_IP}"

FLENT_BIN = "/home/mahsan/flent/run-flent"   # path to the Flent binary/wrapper to use

REPEATS = 10              # number of repeats per test per config (only 3 configs, so this stays cheap)
TEST_LENGTH = 60          # seconds
SETTLE_SLEEP = 5          # seconds to wait after switching CC before testing
BETWEEN_RUN_SLEEP = 3     # seconds between repeated runs of the same test

ROUND_ROBIN = True            # True: run 1 of every config, then run 2 of every config, ...
SHUFFLE_WITHIN_ROUND = True   # shuffle config order inside each round (reproducible per results dir)

# Test types run every repeat, in this order: (flent_test_name, file_prefix)
# NOTE: tcp_12up is not part of Flent's standard tcp_Nup series (which is
# powers of 2: 1/2/4/8/16/32/64) -- confirm it exists in your Flent version
# with `flent --list-tests` before running the sweep.
TEST_TYPES = [
    # Restricted to the two tests that have shown a contention-dependent
    # effect (see docstring). Uncomment to restore the other three.
    # ("tcp_1up", "tcp_1up"),
    # ("tcp_4up", "tcp_4up"),
    ("tcp_8up", "tcp_8up"),
    # ("tcp_12up", "tcp_12up"),
    ("rrul", "rrul"),
]

# ------------------------------------------------------------------
# BBR-TAS: loadable module, CC name "tas", module name "tcp_bbr_tas".
# Fixed (startup_shift, steady_shift) pair per config -- no bandwidth
# grading, unlike BBR-ETAS. Confirmed module parameter names from the
# actual tcp_bbr_tas.c source:
#   tas_pacing_shift_startup  (TSQ shift used during STARTUP/DRAIN)
#   tas_pacing_shift_steady   (TSQ shift used during ProbeBW/ProbeRTT)
# Module compiled-in defaults are (9, 10).
# ------------------------------------------------------------------
TAS_KO_PATH_CLIENT = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_KO_PATH_SERVER = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"   # path ON the server

TAS_MODULE_NAME = "tcp_bbr_tas"     # module name as shown in `lsmod`
TAS_CC_NAME = "tas"                 # .name field registered in tcp_congestion_ops

# PLACEHOLDER factorial design -- EDIT to match your actual intended sweep.
# Mirrors the shape of ETAS_CONFIGS below: a startup_shift x steady_shift
# grid, keeping only startup_shift <= steady_shift (startup is meant to be
# the looser/lower-or-equal shift, steady the tighter/higher-or-equal one,
# same ordering convention as BBR-ETAS's shift_min <= shift_max).
# Confirmation stage (20 runs). 5-run stage used (9,9), (8,9), (8,8), (7,9).
# (startup, steady) pairs:
TAS_PAIRS = [
    (7, 9),   # same config used throughout the earlier channel-36 sweeps
]
# Previous full grid, for reference / easy revert:
# TAS_PAIRS = [(s, t) for t in (8, 9, 10) for s in (7, 8, 9) if s <= t]

TAS_CONFIGS = [
    {
        "tas_pacing_shift_startup": startup,
        "tas_pacing_shift_steady": steady,
    }
    for startup, steady in TAS_PAIRS
]

# ------------------------------------------------------------------
# BBR-ETAS: loadable module, CC name "etas", module name "tcp_bbr_etas".
# Bandwidth-graded (shift_min, shift_max) pair per config.
# ------------------------------------------------------------------
ETAS_KO_PATH_CLIENT = "/home/mahsan/bbr-etas/tcp_bbr_etas.ko"
ETAS_KO_PATH_SERVER = "/home/mahsan/bbr-etas/tcp_bbr_etas.ko"   # path ON the server

ETAS_MODULE_NAME = "tcp_bbr_etas"   # module name as shown in `lsmod`
ETAS_CC_NAME = "etas"               # .name field registered in tcp_congestion_ops

# 3x3 factorial on (etas_shift_min, etas_shift_max): shift_max E {8,9,10}
# (tight end, low-bandwidth tier) x shift_min E {5,6,7} (loose end,
# high-bandwidth tier). Bandwidth thresholds and hold_rounds are held
# at module defaults, since the shift range is the algorithm's core
# novelty and this keeps the design a clean 2-factor sweep.
# Confirmation stage (20 runs). 5-run stage used (5,9), (5,10), (6,9), (6,10), (7,8).
# (shift_min, shift_max) pairs:
ETAS_PAIRS = [
    (7, 8),   # ONLY config under test: best result in the original manuscript (old, interfered channel)
    # (6, 9), (6, 10), (5, 9), (5, 10),   # the other 4 candidates -- add back for the fuller check
]
# Previous 3x3 grid, for reference / easy revert:
# ETAS_PAIRS = [(mn, mx) for mx in (8, 9, 10) for mn in (5, 6, 7) if mn <= mx]
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
    for shift_min, shift_max in ETAS_PAIRS
    if shift_min <= shift_max  # drop inverted pairs: interpolation assumes
                               # shift decreases as bandwidth increases
]

# All custom (module_name, cc_name) pairs, used by the generic
# deactivate/cleanup logic to know what might need unloading.
CUSTOM_MODULES = [
    (TAS_MODULE_NAME, TAS_CC_NAME),
    (ETAS_MODULE_NAME, ETAS_CC_NAME),
]

# ---------------------- END USER CONFIGURATION -------------------

if len(sys.argv) >= 2:
    RESULTS_DIR = Path(sys.argv[1])
    if not RESULTS_DIR.is_dir():
        print(f"ERROR: resume directory does not exist: {RESULTS_DIR}", file=sys.stderr)
        sys.exit(1)
    RESUMING = True
else:
    RESULTS_DIR = Path.home() / "cc-sweep-results" / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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


def _rmmod_with_retry(module_name: str, cc_name: str, on_client: bool) -> None:
    """Shared retry/diagnostic logic for unloading one module on one host."""
    side = "client" if on_client else "server"
    check = (lambda: local_ok(["bash", "-c", f"lsmod | grep -q '^{module_name}'"])) if on_client \
        else (lambda: ssh_ok(f"lsmod | grep -q '^{module_name}'"))
    do_rmmod = (lambda: local_ok(["sudo", "rmmod", module_name])) if on_client \
        else (lambda: ssh_ok(f"sudo rmmod {module_name} 2>/dev/null"))

    for attempt in range(1, 11):
        if do_rmmod():
            return
        if not check():
            return  # wasn't loaded to begin with
        log(f"{side} rmmod attempt {attempt}/10 for {module_name} still busy, waiting...")
        time.sleep(3)

    if check():
        log(f"DIAGNOSTIC: {side} lsmod entry for {module_name}:")
        if on_client:
            r = run(["bash", "-c", f"lsmod | grep '^{module_name}'"], check=False)
        else:
            r = ssh_run(f"lsmod | grep '^{module_name}'", check=False)
        log(r.stdout.strip())
        log(f"DIAGNOSTIC: {side} sockets currently using {cc_name} congestion control:")
        if on_client:
            r = run(["bash", "-c", f"sudo ss -tin 2>/dev/null | grep -B1 ' {cc_name} '"], check=False)
        else:
            r = ssh_run(f"sudo ss -tin 2>/dev/null | grep -B1 ' {cc_name} '", check=False)
        log(r.stdout.strip())
        if on_client:
            log("DIAGNOSTIC: check for a lingering SSH control master:")
            r = run(["ssh", "-O", "check", SERVER_SSH_TARGET], check=False)
            log((r.stdout + r.stderr).strip())
        fail(f"could not rmmod {module_name} on {side} after 10 attempts (module still in use)")


def deactivate_custom_modules() -> None:
    """Switch CC away to cubic BEFORE rmmod, on both hosts, then unload
    WHICHEVER of the custom modules (tas, etas) is currently loaded.

    The kernel refuses to rmmod a congestion-control module while it is
    still the active net.ipv4.tcp_congestion_control (or while any live
    socket references it) -- switching to cubic first is required, every
    time, on both client and server, or rmmod fails with "module is in
    use". Safe to call even if neither custom module is currently loaded.
    """
    if not local_ok(["sudo", "sysctl", "-w", "net.ipv4.tcp_congestion_control=cubic"]):
        fail("could not switch client CC to cubic before rmmod")
    if not ssh_ok("sudo sysctl -w net.ipv4.tcp_congestion_control=cubic"):
        fail("could not switch server CC to cubic before rmmod")

    for module_name, cc_name in CUSTOM_MODULES:
        _rmmod_with_retry(module_name, cc_name, on_client=True)
        _rmmod_with_retry(module_name, cc_name, on_client=False)


def set_builtin_cc(cc_name: str) -> None:
    """Sets congestion control to a built-in name (cubic/bbr) on both hosts."""
    deactivate_custom_modules()  # switch away from any custom module + unload it

    if not local_ok(["sudo", "sysctl", "-w", f"net.ipv4.tcp_congestion_control={cc_name}"]):
        fail(f"could not set client CC to {cc_name}")
    if not ssh_ok(f"sudo sysctl -w net.ipv4.tcp_congestion_control={cc_name}"):
        fail(f"could not set server CC to {cc_name}")

    client_cc = run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"]).stdout.strip()
    server_cc = ssh_run("sysctl -n net.ipv4.tcp_congestion_control").stdout.strip()
    log(f"Set builtin CC: client={client_cc} server={server_cc}")

    if not (client_cc == cc_name and server_cc == cc_name):
        fail(f"CC verification mismatch (client={client_cc} server={server_cc}, wanted={cc_name})")


def set_module_cc(module_name: str, cc_name: str, ko_path_client: str,
                   ko_path_server: str, params: dict) -> None:
    """Loads a custom CC module with the given module params on both hosts,
    then activates it. Generic across BBR-TAS and BBR-ETAS.
    """
    deactivate_custom_modules()  # switch away + unload whatever was loaded before
    time.sleep(1)

    param_args = [f"{k}={v}" for k, v in params.items()]

    if not local_ok(["sudo", "insmod", ko_path_client] + param_args):
        fail(f"insmod failed on client for {module_name} params {params}")

    remote_insmod = "sudo insmod " + ko_path_server + " " + " ".join(param_args)
    if not ssh_ok(remote_insmod):
        fail(f"insmod failed on server for {module_name} params {params}")

    if not local_ok(["sudo", "sysctl", "-w", f"net.ipv4.tcp_congestion_control={cc_name}"]):
        fail(f"could not set client CC to {cc_name}")
    if not ssh_ok(f"sudo sysctl -w net.ipv4.tcp_congestion_control={cc_name}"):
        fail(f"could not set server CC to {cc_name}")

    # Verify module parameters actually took, on both sides
    mismatches = []
    reported = {"client": {}, "server": {}}
    for param_name in params:
        sysfs_path = f"/sys/module/{module_name}/parameters/{param_name}"
        c_val = run(["cat", sysfs_path]).stdout.strip()
        s_val = ssh_run(f"cat {sysfs_path}").stdout.strip()
        reported["client"][param_name] = c_val
        reported["server"][param_name] = s_val
        if c_val != str(params[param_name]) or s_val != str(params[param_name]):
            mismatches.append(param_name)

    if mismatches:
        # Full detail only when something is wrong.
        log(f"{cc_name.upper()} params: client={reported['client']} server={reported['server']}")
        fail(f"parameter verification mismatch for {mismatches} (wanted {params})")

    log(f"{cc_name.upper()} params verified on client and server")


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


def run_marker(tag: str, run_idx: int) -> Path:
    return RESULTS_DIR / tag / f".run{run_idx}.ok"


def run_round(tag: str, run_idx: int) -> bool:
    """Runs every Flent test type once (repeat number run_idx) for the
    currently active CC config. Returns True if all tests produced valid
    output; in that case a .runN.ok marker is written for resume."""
    outdir = RESULTS_DIR / tag
    outdir.mkdir(parents=True, exist_ok=True)

    log(f"Settling {SETTLE_SLEEP}s before testing config: {tag}")
    time.sleep(SETTLE_SLEEP)

    failures = 0
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
            failures += 1
        time.sleep(BETWEEN_RUN_SLEEP)

    if failures == 0:
        run_marker(tag, run_idx).touch()
    else:
        log(f"[{tag}] run {run_idx} had {failures} failure(s) -- will re-run on resume")
    return failures == 0


def mark_complete_if_done(tag: str) -> None:
    if all(run_marker(tag, i).exists() for i in range(1, REPEATS + 1)):
        (RESULTS_DIR / tag / ".complete").touch()
        log(f"All {REPEATS} runs complete for config: {tag}")


def config_already_complete(tag: str) -> bool:
    # Complete means every run up to the CURRENT value of REPEATS has a .runN.ok
    # marker, so raising REPEATS on an existing results dir extends it
    # automatically (no need to delete .complete by hand).
    return all(run_marker(tag, i).exists() for i in range(1, REPEATS + 1))


def _param_tag(prefix: str, params: dict, strip: str) -> str:
    """Builds a short, deterministic, filesystem-safe tag for a param dict."""
    parts = [f"{k.replace(strip, '')}{v}" for k, v in params.items()]
    tag = prefix + "_" + "_".join(parts)
    if len(tag) > 120:  # fall back to a hash if the param set gets long
        h = hashlib.sha1(repr(sorted(params.items())).encode()).hexdigest()[:10]
        tag = f"{prefix}_{h}"
    return tag


def tas_tag(params: dict) -> str:
    return _param_tag("tas", params, "tas_pacing_shift_")   # e.g. tas_startup9_steady9


def etas_tag(params: dict) -> str:
    # Only the swept parameters go in the label; thresholds and hold_rounds
    # are fixed for the sweep (see params.txt in each folder).
    return f"etas_min{params['etas_shift_min']}_max{params['etas_shift_max']}"


def write_params_file(tag: str, cc: str, params: dict) -> None:
    """Records the full parameter set (incl. fixed ones) for provenance."""
    outdir = RESULTS_DIR / tag
    outdir.mkdir(parents=True, exist_ok=True)
    lines = [f"cc={cc}"] + [f"{k}={v}" for k, v in params.items()]
    (outdir / "params.txt").write_text("\n".join(lines) + "\n")


def build_config_list():
    """Returns [(tag, header, activate_fn)] for every config in the sweep."""
    configs = [
        ("cubic", "=== Baseline: Cubic ===", lambda: set_builtin_cc("cubic")),
        ("bbrv3", "=== Baseline: stock BBRv3 ===", lambda: set_builtin_cc("bbr")),
    ]
    for p in TAS_CONFIGS:
        header = (f"=== BBR-TAS: startup={p['tas_pacing_shift_startup']}, "
                  f"steady={p['tas_pacing_shift_steady']} ===")
        configs.append((tas_tag(p), header,
                        lambda p=p: set_module_cc(TAS_MODULE_NAME, TAS_CC_NAME,
                                                  TAS_KO_PATH_CLIENT, TAS_KO_PATH_SERVER, p)))
    for p in ETAS_CONFIGS:
        header = (f"=== BBR-ETAS: shift_min={p['etas_shift_min']}, "
                  f"shift_max={p['etas_shift_max']} ===")
        configs.append((etas_tag(p), header,
                        lambda p=p: set_module_cc(ETAS_MODULE_NAME, ETAS_CC_NAME,
                                                  ETAS_KO_PATH_CLIENT, ETAS_KO_PATH_SERVER, p)))

    tags = [c[0] for c in configs]
    dupes = sorted({t for t in tags if tags.count(t) > 1})
    if dupes:
        fail(f"duplicate config labels {dupes} -- if you now sweep thresholds or "
             f"hold_rounds, add them to etas_tag()")

    for p in TAS_CONFIGS:
        write_params_file(tas_tag(p), TAS_CC_NAME, p)
    for p in ETAS_CONFIGS:
        write_params_file(etas_tag(p), ETAS_CC_NAME, p)
    return configs


def main() -> None:
    if RESUMING:
        log(f"CC sweep RESUMED: {datetime.datetime.now()}")
    else:
        log(f"CC sweep started: {datetime.datetime.now()}")

    check_flent_bin()
    log(f"Results will be saved under: {RESULTS_DIR}")

    configs = build_config_list()
    log(f"Sweep: {len(configs)} configs x {REPEATS} runs x {len(TEST_TYPES)} tests "
        f"({'round-robin' if ROUND_ROBIN else 'sequential'})")
    log("Configs: " + ", ".join(c[0] for c in configs))

    if ROUND_ROBIN:
        for run_idx in range(1, REPEATS + 1):
            order = list(configs)
            if SHUFFLE_WITHIN_ROUND:
                random.Random(f"{RESULTS_DIR.name}-{run_idx}").shuffle(order)
            todo = [c for c in order
                    if not (config_already_complete(c[0]) or run_marker(c[0], run_idx).exists())]
            if not todo:
                log(f"Round {run_idx}/{REPEATS} already done, skipping")
                continue
            log(f"########## Round {run_idx}/{REPEATS} ##########")
            log("Order: " + ", ".join(c[0] for c in order))
            if len(todo) < len(order):
                done = [c[0] for c in order if c not in todo]
                log("Already done this round, skipping: " + ", ".join(done))
            for tag, header, activate in todo:
                log(header)
                activate()
                run_round(tag, run_idx)
        for tag, _, _ in configs:
            mark_complete_if_done(tag)
    else:
        for tag, header, activate in configs:
            if config_already_complete(tag):
                log(f"{header[:-4]} -- already complete, skipping ===")
                continue
            log(header)
            activated = False
            for run_idx in range(1, REPEATS + 1):
                if run_marker(tag, run_idx).exists():
                    continue
                if not activated:
                    activate()
                    activated = True
                run_round(tag, run_idx)
            mark_complete_if_done(tag)

    # --- Cleanup: switch back to cubic (unloading any custom module in the process) ---
    deactivate_custom_modules()

    incomplete = [c[0] for c in configs if not config_already_complete(c[0])]
    log(f"Sweep complete: {datetime.datetime.now()}")
    log(f"All results under: {RESULTS_DIR}")
    if incomplete:
        log(f"Not complete (re-run with the results dir to resume): {', '.join(incomplete)}")

    if FAILED_RUNS_LOG.exists() and FAILED_RUNS_LOG.stat().st_size > 0 and not incomplete:
        log("All configs complete. Runs listed in failed_runs.log were invalid earlier "
            "and have since been re-run successfully.")
    elif FAILED_RUNS_LOG.exists() and FAILED_RUNS_LOG.stat().st_size > 0:
        fail_count = sum(1 for _ in open(FAILED_RUNS_LOG))
        log(f"WARNING: {fail_count} run(s) produced invalid output. See:")
        log(f"  {FAILED_RUNS_LOG}")
        log("Re-check server-side netperf/iperf backend, then resume with the results dir.")
    else:
        log("All runs produced valid output files.")


if __name__ == "__main__":
    main()
