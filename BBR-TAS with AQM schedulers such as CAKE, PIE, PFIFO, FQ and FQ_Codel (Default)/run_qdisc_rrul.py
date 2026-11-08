#!/usr/bin/env python3
"""
BBR-TAS sender/receiver qdisc test runner -- RRUL only (Reviewer 2, Comment 5)
------------------------------------------------------------------------------
Question answered: do the BBR-TAS results hold when the root queueing
discipline (qdisc) on the CLIENT and SERVER hosts is changed from the
Ubuntu default (fq_codel) to other disciplines?

    python3 run_qdisc_rrul.py --dry-run              # prints the plan, changes nothing
    python3 run_qdisc_rrul.py                        # all qdiscs below
    python3 run_qdisc_rrul.py default pfifo          # only the named ones

Qdiscs (tag -> what is installed on BOTH hosts' test interface):
    default  -> `tc qdisc del dev IF root` : restores the system default, i.e. exactly
                the paper's baseline (checked to contain fq_codel)
    fq       -> fq
    pfifo    -> pfifo limit 1000
    pie      -> pie           (needs sch_pie)
    cake     -> cake          (needs sch_cake; no bandwidth shaping, default settings)
A qdisc whose kernel module is missing is SKIPPED and logged, not faked.

Per qdisc, per congestion control: cubic, bbrv3, tas_s8_st8 (the paper's RRUL headline pair),
REPEATS runs (default 3) of `flent rrul`. The CC is set IDENTICALLY on client and server and
read back from both before every config (the script aborts on any mismatch). The qdisc is set
on both hosts and read back from both before every config (abort on mismatch).

Results: ~/bbr-tas-qdisc/<qdisc>/<config>/rrul_run<N>.flent.gz (+ .summary.txt, .stats.txt),
plus tc_stats.log (drops/backlog per config). Re-running RESUMES: configs with a .complete
marker are skipped. The default qdisc is restored on both hosts when the script exits.

IMPORTANT LIMITS (state them in the paper):
  * This changes the ROOT qdisc of the test interface on the hosts. On Wi-Fi, mac80211
    drivers also keep their own per-station fq_codel queues below the root qdisc; that layer
    is NOT changed here and cannot be disabled from tc.
  * Non-fq qdiscs mean BBR/TAS use the kernel's internal pacing instead of sch_fq pacing.
  * Router/AP-side AQM is not tested.

Interfaces are auto-detected (route to SERVER_IP); override with
    CLIENT_IFACE=wlp3s0 SERVER_IFACE=enp2s0 python3 run_qdisc_rrul.py

EDIT THE VARIABLES BELOW BEFORE RUNNING.
------------------------------------------------------------------------------
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

def ssh_prefix(ip: str) -> list:
    return ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", f"{SSH_USER}@{ip}"]

TAS_KO_PATH_CLIENT = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_KO_PATH_SERVER = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_MODULE_NAME = "tcp_bbr_tas"
TAS_CC_NAME = "tas"

REPEATS = int(os.environ.get("REPEATS", "3"))
TEST_LENGTH = 60
SETTLE_SLEEP = 5
BETWEEN_RUN_SLEEP = 3
CLIENT_IFACE = os.environ.get("CLIENT_IFACE", "")
SERVER_IFACE = os.environ.get("SERVER_IFACE", "")

TEST_TYPES = [("rrul", "rrul")]
CONFIGS = [
    ("cubic", "builtin:cubic", None, None),
    ("bbrv3", "builtin:bbr", None, None),
    ("tas_s8_st8", "tas", 8, 8),
]
# tag -> (tc arguments after "root", module to modprobe or None, name that must appear in `tc qdisc show`)
QDISCS = {
    "default": (None, None, "fq_codel"),
    "fq":      ("fq", "sch_fq", "fq"),
    "pfifo":   ("pfifo limit 1000", None, "pfifo"),
    "pie":     ("pie", "sch_pie", "pie"),
    "cake":    ("cake", "sch_cake", "cake"),
}
# ---------------------- END USER CONFIGURATION -------------------

DRY_RUN = "--dry-run" in sys.argv
requested = [a for a in sys.argv[1:] if not a.startswith("--")]
for q in requested:
    if q not in QDISCS:
        sys.exit(f"Unknown qdisc '{q}'. Choose from: {', '.join(QDISCS)}")
QDISC_LIST = requested or list(QDISCS)

RESULTS_DIR = Path.home() / "bbr-tas-qdisc"
MANIFEST = RESULTS_DIR / "manifest.log"
FAILED_LOG = RESULTS_DIR / "failed_runs.log"

if DRY_RUN:
    n = len(QDISC_LIST) * len(CONFIGS) * REPEATS
    print(f"DRY RUN -- results would go to {RESULTS_DIR}")
    print(f"  qdiscs: {QDISC_LIST}")
    print(f"  configs: {[c[0] for c in CONFIGS]}   test: rrul x {REPEATS} repeats")
    print(f"  total {n} runs (~{n * (TEST_LENGTH + BETWEEN_RUN_SLEEP + 10) / 60:.0f} min plus switching)")
    sys.exit(0)

RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(MANIFEST, "a") as f:
        f.write(line + "\n")


def fail(msg: str) -> None:
    log(f"ERROR: {msg}")
    restore_default_qdisc(quiet=True)
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

    # Verify the ACTIVE congestion control is "tas" on client AND server
    # (not just that the module loaded), so RRUL's download direction also runs tas.
    client_cc = run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"], capture=True).stdout.strip()
    server_ccs = {ip: ssh_run(ip, "sysctl -n net.ipv4.tcp_congestion_control").stdout.strip() for ip in SERVERS}
    log(f"Active CC after TAS load: client={client_cc} " + " ".join(f"{ip}={cc}" for ip, cc in server_ccs.items()))
    if client_cc != TAS_CC_NAME:
        fail(f"active CC on client is {client_cc}, wanted {TAS_CC_NAME}")
    for ip, cc in server_ccs.items():
        if cc != TAS_CC_NAME:
            fail(f"active CC on {ip} is {cc}, wanted {TAS_CC_NAME}")

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




# ---------------------- qdisc handling ----------------------

def detect_ifaces() -> tuple:
    global CLIENT_IFACE, SERVER_IFACE
    r = subprocess.run(["ip", "-o", "route", "get", SERVER_IP], capture_output=True, text=True).stdout
    m_dev = re.search(r"\bdev (\S+)", r)
    m_src = re.search(r"\bsrc (\S+)", r)
    if not CLIENT_IFACE:
        if not m_dev:
            fail("could not auto-detect CLIENT_IFACE; set CLIENT_IFACE=...")
        CLIENT_IFACE = m_dev.group(1)
    if not SERVER_IFACE:
        if not m_src:
            fail("could not auto-detect SERVER_IFACE; set SERVER_IFACE=...")
        out = ssh_run(SERVER_IP, f"ip -o route get {m_src.group(1)}", check=False).stdout
        m = re.search(r"\bdev (\S+)", out)
        if not m or m.group(1) == "lo":
            fail("could not auto-detect SERVER_IFACE; set SERVER_IFACE=...")
        SERVER_IFACE = m.group(1)
    log(f"Interfaces: client={CLIENT_IFACE} server={SERVER_IFACE}")


def tc_show(side: str) -> str:
    if side == "client":
        return run(["tc", "qdisc", "show", "dev", CLIENT_IFACE], capture=True).stdout
    return ssh_run(SERVER_IP, f"tc qdisc show dev {SERVER_IFACE}").stdout


def apply_qdisc(tag: str) -> bool:
    args, module, needle = QDISCS[tag]
    if module:
        ok_c = run(["sudo", "modprobe", module], check=False, capture=True).returncode == 0
        ok_s = ssh_run(SERVER_IP, f"sudo modprobe {module}", check=False).returncode == 0
        if not (ok_c and ok_s):
            log(f"SKIP {tag}: kernel module {module} unavailable (client ok={ok_c}, server ok={ok_s})")
            return False
    if args is None:
        run(["sudo", "tc", "qdisc", "del", "dev", CLIENT_IFACE, "root"], check=False, capture=True)
        ssh_run(SERVER_IP, f"sudo tc qdisc del dev {SERVER_IFACE} root", check=False)
    else:
        run(f"sudo tc qdisc replace dev {CLIENT_IFACE} root {args}", capture=True)
        ssh_run(SERVER_IP, f"sudo tc qdisc replace dev {SERVER_IFACE} root {args}")
    time.sleep(2)
    c, s = tc_show("client"), tc_show("server")
    log(f"Qdisc readback [{tag}] client:\n{c.strip()}\nserver:\n{s.strip()}")
    for side, out in (("client", c), ("server", s)):
        if not re.search(rf"\bqdisc {re.escape(needle)}\b", out):
            fail(f"qdisc verification failed on {side}: expected '{needle}' for tag {tag}")
    return True


def restore_default_qdisc(quiet=False) -> None:
    try:
        if CLIENT_IFACE:
            subprocess.run(["sudo", "tc", "qdisc", "del", "dev", CLIENT_IFACE, "root"], capture_output=True)
        if SERVER_IFACE:
            subprocess.run(ssh_prefix(SERVER_IP) + [f"sudo tc qdisc del dev {SERVER_IFACE} root"], capture_output=True)
        if not quiet:
            log("Default qdisc restored on client and server")
    except Exception as e:  # best effort
        print(f"restore failed: {e}")


def log_tc_stats(tag: str, cfg: str) -> None:
    c = run(["tc", "-s", "qdisc", "show", "dev", CLIENT_IFACE], check=False, capture=True).stdout
    s = ssh_run(SERVER_IP, f"tc -s qdisc show dev {SERVER_IFACE}", check=False).stdout
    with open(RESULTS_DIR / tag / "tc_stats.log", "a") as f:
        f.write(f"=== {tag} / {cfg} client ===\n{c}\n=== server ===\n{s}\n")


def run_tests(qtag: str, tag: str) -> None:
    outdir = RESULTS_DIR / qtag / tag
    outdir.mkdir(parents=True, exist_ok=True)
    failures = 0
    log(f"Settling {SETTLE_SLEEP}s before testing: qdisc={qtag} config={tag}")
    time.sleep(SETTLE_SLEEP)
    for run_num in range(1, REPEATS + 1):
        log(f"[{qtag}/{tag}] rrul run {run_num}/{REPEATS}")
        ok = run_one_flent_test("rrul", f"qdisc={qtag} {tag} rrul run {run_num}",
                                outdir, f"rrul_run{run_num}.flent.gz")
        if ok:
            log(f"[{qtag}/{tag}] run {run_num}: OK")
        else:
            log(f"[{qtag}/{tag}] run {run_num}: INVALID OUTPUT -- see flent_stdout.log")
            with open(FAILED_LOG, "a") as f:
                f.write(f"{outdir / ('rrul_run' + str(run_num) + '.flent.gz')}\n")
            failures += 1
        time.sleep(BETWEEN_RUN_SLEEP)
    log_tc_stats(qtag, tag)
    if failures == 0:
        (outdir / ".complete").touch()
        log(f"Completed {qtag}/{tag} (marked complete)")
    else:
        log(f"Completed {qtag}/{tag} WITH {failures} FAILURE(S) -- will re-run on resume")


def main() -> None:
    log("Qdisc RRUL run started: " + ", ".join(QDISC_LIST))
    if not shutil.which(FLENT_BIN) and not Path(FLENT_BIN).is_file():
        fail(f"FLENT_BIN not found or not executable: {FLENT_BIN}")
    detect_ifaces()
    try:
        for qtag in QDISC_LIST:
            log(f"##### qdisc: {qtag} #####")
            (RESULTS_DIR / qtag).mkdir(parents=True, exist_ok=True)
            if all((RESULTS_DIR / qtag / c[0] / ".complete").exists() for c in CONFIGS):
                log(f"{qtag} already complete, skipping")
                continue
            if not apply_qdisc(qtag):
                continue
            for tag, kind, startup, steady in CONFIGS:
                log(f"=== {qtag} / {tag} ===")
                if (RESULTS_DIR / qtag / tag / ".complete").exists():
                    log(f"{qtag}/{tag} already complete, skipping")
                    continue
                if kind.startswith("builtin:"):
                    set_builtin_cc(kind.split(":", 1)[1])
                else:
                    set_tas_cc(startup, steady)
                run_tests(qtag, tag)
    finally:
        deactivate_tas()
        restore_default_qdisc()
    log(f"Done. Results under: {RESULTS_DIR}")
    if FAILED_LOG.exists() and FAILED_LOG.stat().st_size > 0:
        log(f"WARNING: invalid runs listed in {FAILED_LOG}")
    else:
        log("All runs produced valid output files.")


if __name__ == "__main__":
    main()
