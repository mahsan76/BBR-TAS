#!/usr/bin/env python3
"""
BBR-TAS suite for Reviewer 2, Comments 3 and 6
-----------------------------------------------------------------------------
    python3 run_suite_c3_c6.py A|B|C|all [--dry-run]

PART A  (Comment 6) Coexistence / RTT fairness on the LAN.
        Two groups of upload flows share the client's Wi-Fi link at the same time, each group
        with its OWN congestion control (iperf3 -C, per socket). Pairs: TAS vs Cubic,
        TAS vs BBRv3, BBRv3 vs Cubic (reference). BOTH groups send to SERVER_LAN (gigabit):
        group "short" to its real IP, group "long" to ALIAS_IP, a second address added on the same
        server; tc netem on the server delays only replies sourced from ALIAS_IP (+40 ms RTT).
        Every RTT case is run in both assignments (X short / X long). Flows per group: 1 and 4.
        Upload only, so only the SENDER's CC matters. No second server is used.
PART B  (Comment 3, WAN) Upload to a public Internet host (PUBLIC_HOST). Only the client CC
        matters for upload; the public host's own CC is outside our control and is NOT used
        by the upload direction. Tests: tcp_upload, tcp_8up.
PART C  (Comment 3, loss) LAN upload with EMULATED random loss on the client's egress
        (tc netem loss, 0 / 0.1 / 1 %, fq_codel kept as child qdisc). Tests: tcp_upload, tcp_8up.
        CC is set on the client AND on SERVER_LAN, read back before every run.

Loss in Part C is emulated at the sender's network interface; it is NOT measured wireless loss.
Run order is interleaved and rotated each round. Resume: runs with a valid result file are skipped.
Results: ~/bbr-tas-suite/<partA|partB|partC>/...   Env: REPEATS_A (3), REPEATS_B (5), REPEATS_C (3).
Optional RATE_MBIT=25 applies a netem rate limit on the client egress in Part A (like the old 25 Mbps test).
EDIT THE VARIABLES BELOW BEFORE RUNNING.
-----------------------------------------------------------------------------
"""
import re
import json, os, re, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path

FLENT_BIN = "/home/mahsan/flent/run-flent"
SERVER_LAN = "192.168.1.169"      # wired server (group A in Part A; Part C target)
ALIAS_IP = os.environ.get("ALIAS_IP", "192.168.1.170")   # spare unused LAN address, added to SERVER_LAN during Part A
PUBLIC_HOST = "flent-eu.bufferbloat.net"
SSH_USER = "mahsan"
SERVERS = []                      # set per part (hosts whose CC is coordinated over SSH)

SSH_USERS = {"192.168.1.169": "mahsan"}   # per-host SSH user

def ssh_prefix(ip: str) -> list:
    return ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", f"{SSH_USERS.get(ip, SSH_USER)}@{ip}"]

TAS_KO_PATH_CLIENT = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_KO_PATH_SERVER = "/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_MODULE_NAME = "tcp_bbr_tas"
TAS_CC_NAME = "tas"
TEST_LENGTH = 60
SETTLE_SLEEP = 5
BETWEEN_RUN_SLEEP = 3
REPEATS_A = int(os.environ.get("REPEATS_A", "3"))
REPEATS_B = int(os.environ.get("REPEATS_B", "5"))
REPEATS_C = int(os.environ.get("REPEATS_C", "3"))
RATE_MBIT = os.environ.get("RATE_MBIT", "")
CLIENT_IFACE = os.environ.get("CLIENT_IFACE", "")
SERVER_IFACE = os.environ.get("SERVER_IFACE", "")
ADDED_DELAY_MS = 40
LOSS_LEVELS = [0.0, 0.1, 1.0]     # percent, Part C
PAIRS = [("tas", "cubic"), ("tas", "bbr"), ("bbr", "cubic")]   # (X, Y) CC names, Part A
STREAMS_A = [1, 4]
TAS_A = (8, 8)                    # shift pair used for the TAS flows in Part A
# Parts B and C: (flent test, [(tag, kind, startup, steady)])
def cfgs(st, sd):
    return [("cubic", "builtin:cubic", None, None), ("bbrv3", "builtin:bbr", None, None),
            (f"tas_s{st}_st{sd}", "tas", st, sd)]
TESTS_BC = [("tcp_upload", cfgs(7, 10)), ("tcp_8up", cfgs(7, 8))]

args = [a for a in sys.argv[1:] if not a.startswith("--")]
DRY_RUN = "--dry-run" in sys.argv
if len(args) != 1 or args[0] not in ("A", "B", "C", "all"):
    sys.exit("Usage: python3 run_suite_c3_c6.py A|B|C|all [--dry-run]")
PARTS = ["A", "B", "C"] if args[0] == "all" else [args[0]]

ROOT = Path.home() / "bbr-tas-suite"
MANIFEST = ROOT / "manifest.log"
FAILED_LOG = ROOT / "failed_runs.log"
RESULTS_DIR = ROOT   # used by run_one_flent_test for flent_stdout.log

def cells_A():
    out = []
    for n in STREAMS_A:
        for x, y in PAIRS:
            for scn in ("equal", "asymXshort", "asymXlong"):
                out.append((x, y, scn, n))
    return out

if DRY_RUN:
    nA = len(cells_A()) * REPEATS_A; nB = sum(len(c) for _, c in TESTS_BC) * REPEATS_B
    nC = len(LOSS_LEVELS) * sum(len(c) for _, c in TESTS_BC) * REPEATS_C
    print(f"DRY RUN parts {PARTS}; results under {ROOT}")
    print(f"  A: {len(cells_A())} cells x {REPEATS_A} = {nA} runs (~{nA*72/60:.0f} min)")
    print(f"  B: {sum(len(c) for _, c in TESTS_BC)} cells x {REPEATS_B} = {nB} runs (~{nB*70/60:.0f} min)")
    print(f"  C: {len(LOSS_LEVELS)*sum(len(c) for _, c in TESTS_BC)} cells x {REPEATS_C} = {nC} runs (~{nC*70/60:.0f} min)")
    sys.exit(0)

ROOT.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(MANIFEST, "a") as f:
        f.write(line + "\n")


def fail(msg: str) -> None:
    log(f"ERROR: {msg}")
    restore_network(quiet=True)
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


def kill_tas_sockets() -> None:
    """Leftover sockets (e.g. netperf connections the remote end never closed) keep the tas module
    busy. Destroy only the sockets that are using tas (needs ss -K / INET_DIAG_DESTROY; best effort)."""
    out = run(["sudo", "ss", "-tin"], check=False, capture=True).stdout.splitlines()
    header = ""
    peers = []
    for line in out:
        if line and not line[0].isspace():
            header = line
        elif " tas " in line and header:
            tok = header.split()
            if tok and ":" in tok[-1]:
                peers.append(tok[-1])
    for peer in sorted(set(peers)):
        r = run(["sudo", "ss", "-K", "-tn", f"dst {peer}"], check=False, capture=True)
        log(f"Destroyed lingering tas socket(s) to {peer} (rc={r.returncode})")


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
        if attempt in (1, 4, 8):
            kill_tas_sockets()
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


def run_one_flent_test(test_name: str, title: str, outdir: Path, final_name: str, host: str) -> bool:
    marker = outdir / ".marker"
    marker.touch()
    marker_mtime = marker.stat().st_mtime
    stem = final_name.replace(".flent.gz", "")

    cmd = [FLENT_BIN, test_name, "-H", host, "-l", str(TEST_LENGTH),
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





# ---------------------- network helpers ----------------------

def client_iface() -> str:
    global CLIENT_IFACE
    if not CLIENT_IFACE:
        r = subprocess.run(["ip", "-o", "route", "get", SERVER_LAN], capture_output=True, text=True).stdout
        m = re.search(r"\bdev (\S+)", r)
        if not m:
            fail("cannot detect CLIENT_IFACE; set CLIENT_IFACE=...")
        CLIENT_IFACE = m.group(1)
    return CLIENT_IFACE


def server_iface() -> str:
    global SERVER_IFACE
    if not SERVER_IFACE:
        r = subprocess.run(["ip", "-o", "route", "get", SERVER_LAN], capture_output=True, text=True).stdout
        src = re.search(r"\bsrc (\S+)", r).group(1)
        out = ssh_run(SERVER_LAN, f"ip -o route get {src}", check=False).stdout
        m = re.search(r"\bdev (\S+)", out)
        if not m or m.group(1) == "lo":
            fail("cannot detect SERVER_IFACE; set SERVER_IFACE=...")
        SERVER_IFACE = m.group(1)
    return SERVER_IFACE


def restore_network(quiet=False) -> None:
    try:
        if CLIENT_IFACE:
            subprocess.run(["sudo", "tc", "qdisc", "del", "dev", CLIENT_IFACE, "root"], capture_output=True)
        if SERVER_IFACE:
            subprocess.run(ssh_prefix(SERVER_LAN) + [f"sudo tc qdisc del dev {SERVER_IFACE} root; "
                                                     f"sudo ip addr del {ALIAS_IP}/24 dev {SERVER_IFACE}"], capture_output=True)
        subprocess.run(["sudo", "sysctl", "-w", "net.ipv4.tcp_congestion_control=cubic"], capture_output=True)
        if not quiet:
            log("Network restored (netem removed, default CC cubic)")
    except Exception as e:
        print(f"restore failed: {e}")


def set_client_netem(loss_pct=None, rate_mbit="") -> None:
    """Client egress: netem root (loss and/or rate) with fq_codel as child, or plain default if nothing requested."""
    dev = client_iface()
    subprocess.run(["sudo", "tc", "qdisc", "del", "dev", dev, "root"], capture_output=True)
    opts = []
    if loss_pct:
        opts.append(f"loss {loss_pct}%")
    if rate_mbit:
        opts.append(f"rate {rate_mbit}mbit")
    if opts:
        run(f"sudo tc qdisc add dev {dev} root handle 1: netem {' '.join(opts)}", capture=True)
        run(f"sudo tc qdisc add dev {dev} parent 1:1 handle 10: fq_codel", capture=True)
    time.sleep(1)
    show = run(["tc", "qdisc", "show", "dev", dev], capture=True).stdout
    log(f"Client qdisc now: {show.strip()}")
    if opts and "netem" not in show:
        fail("netem not active on client interface")


def setup_server_alias() -> None:
    dev = server_iface()
    if subprocess.run(["ping", "-c", "2", "-W", "1", ALIAS_IP], capture_output=True).returncode == 0:
        r = ssh_run(SERVER_LAN, f"ip -o addr show dev {dev} | grep -c ' {ALIAS_IP}/'", check=False).stdout.strip()
        if r != "1":
            fail(f"{ALIAS_IP} already answers on the LAN and is not ours; set ALIAS_IP to an unused address")
    else:
        ssh_run(SERVER_LAN, f"sudo ip addr add {ALIAS_IP}/24 dev {dev}")
    time.sleep(1)
    if subprocess.run(["ping", "-c", "2", "-W", "1", ALIAS_IP], capture_output=True).returncode != 0:
        fail(f"{ALIAS_IP} did not respond after being added on the server")
    log(f"Alias {ALIAS_IP} active on {SERVER_LAN} ({dev})")


def set_path_delay(ms: int) -> None:
    """Add `ms` of RTT to flows addressed to ALIAS_IP by delaying them on the CLIENT egress (client has local root).
    Verified with ping: the alias must show >= 80% of the added delay over the real server address, else abort."""
    dev = client_iface()
    run(f"sudo tc qdisc del dev {dev} root", check=False, capture=True)
    if ms:
        rate = f"netem rate {RATE_MBIT}mbit limit 10000" if RATE_MBIT else None
        run(f"sudo tc qdisc add dev {dev} root handle 1: prio bands 2 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", capture=True)
        if rate:
            run(f"sudo tc qdisc add dev {dev} parent 1:1 handle 10: {rate}", capture=True)
            run(f"sudo tc qdisc add dev {dev} parent 10: handle 11: fq_codel", capture=True)
            run(f"sudo tc qdisc add dev {dev} parent 1:2 handle 20: netem delay {ms}ms rate {RATE_MBIT}mbit limit 10000", capture=True)
        else:
            run(f"sudo tc qdisc add dev {dev} parent 1:1 handle 10: fq_codel", capture=True)
            run(f"sudo tc qdisc add dev {dev} parent 1:2 handle 20: netem delay {ms}ms limit 10000", capture=True)
        run(f"sudo tc filter add dev {dev} parent 1: protocol ip prio 1 u32 match ip dst {ALIAS_IP}/32 flowid 1:2", capture=True)
    else:
        set_client_netem(None, RATE_MBIT)
    out = run(f"tc qdisc show dev {dev}", capture=True).stdout
    log(f"Client qdisc now: {' | '.join(l.strip() for l in out.strip().splitlines())}")
    if ms:
        def avg(host):
            o = subprocess.run(["ping", "-c", "5", "-i", "0.2", host], capture_output=True, text=True).stdout
            m = re.search(r"= [\d.]+/([\d.]+)/", o)
            return float(m.group(1)) if m else float("nan")
        a, b = avg(ALIAS_IP), avg(SERVER_LAN)
        log(f"  RTT check: alias {a:.1f} ms vs real {b:.1f} ms (wanted +{ms})")
        if not (a - b >= 0.8 * ms):
            fail(f"added delay not effective (alias {a:.1f} ms, real {b:.1f} ms); "
                 f"tc filter: {run(f'tc filter show dev {dev} parent 1:', capture=True).stdout}")

def set_server_delay(ms: int) -> None:
    """Delay only packets the server sends with source ALIAS_IP (replies of flows to the alias)."""
    dev = server_iface()
    ssh_run(SERVER_LAN, f"sudo tc qdisc del dev {dev} root", check=False)
    if ms:
        ssh_run(SERVER_LAN,
                f"sudo tc qdisc add dev {dev} root handle 1: prio bands 2 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 && "
                f"sudo tc qdisc add dev {dev} parent 1:1 handle 10: fq_codel && "
                f"sudo tc qdisc add dev {dev} parent 1:2 handle 20: netem delay {ms}ms && "
                f"sudo tc filter add dev {dev} parent 1: protocol ip prio 1 u32 match ip src {ALIAS_IP}/32 flowid 1:2")
    out = ssh_run(SERVER_LAN, f"tc qdisc show dev {dev}").stdout
    log(f"Server qdisc now: {' | '.join(l.strip() for l in out.strip().splitlines())}")
    if ms and "netem" not in out:
        fail("netem delay not active on server")


def verify_cc(want: str) -> None:
    got = {"client": run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"], capture=True).stdout.strip()}
    for ip in SERVERS:
        got[ip] = ssh_run(ip, "sysctl -n net.ipv4.tcp_congestion_control").stdout.strip()
    log("CC check: " + " ".join(f"{k}={v}" for k, v in got.items()) + f" (want {want})")
    bad = {k: v for k, v in got.items() if v != want}
    if bad:
        fail(f"CC mismatch: {bad}")


def apply_cfg(kind, startup, steady) -> str:
    if kind.startswith("builtin:"):
        cc = kind.split(":", 1)[1]
        set_builtin_cc(cc)
        return cc
    set_tas_cc(startup, steady)
    return TAS_CC_NAME


def mean_ping(path: Path) -> str:
    try:
        t = path.read_text()
        m = re.search(r"= [\d.]+/([\d.]+)/", t)
        return m.group(1) if m else ""
    except Exception:
        return ""

# ---------------------- Part A ----------------------

def part_a() -> None:
    global SERVERS
    SERVERS = []
    outroot = ROOT / "partA"; outroot.mkdir(exist_ok=True)
    if not shutil.which("iperf3"):
        fail("iperf3 not installed on client")
    for ip in (SERVER_LAN,):
        if ssh_run(ip, "which iperf3 >/dev/null", check=False).returncode != 0:
            fail(f"iperf3 is not installed on {ip}; run: sudo apt install -y iperf3")
        # an iperf3 server handles ONE test at a time, so each concurrent group needs its own server/port
        for port in (5201, 5202):
            listening = f"ss -ltn | grep -q ':{port} '"
            if ssh_run(ip, listening, check=False).returncode != 0:
                ssh_run(ip, f"iperf3 -s -D -p {port}", check=False)
                time.sleep(1)
            if ssh_run(ip, listening, check=False).returncode != 0:
                fail(f"no iperf3 server listening on {ip}:{port} (could not start it)")
    # load TAS (8,8) on client once; keep default CC cubic; allow per-socket selection
    set_tas_cc(*TAS_A)
    run(["sudo", "sysctl", "-w", "net.ipv4.tcp_congestion_control=cubic"], capture=True)
    run(["sudo", "modprobe", "tcp_bbr"], check=False, capture=True)   # stock BBR is a module; load it alongside tas
    run(["sudo", "modprobe", "tcp_cubic"], check=False, capture=True)
    avail = run(["sysctl", "-n", "net.ipv4.tcp_available_congestion_control"], capture=True).stdout.split()
    for need in ("cubic", "bbr", TAS_CC_NAME):
        if need not in avail:
            fail(f"congestion control '{need}' not available on client (available: {avail}); load it (modprobe tcp_bbr)")
    run(["sudo", "sysctl", "-w", "net.ipv4.tcp_allowed_congestion_control=" + " ".join(avail)], capture=True)
    set_client_netem(None, RATE_MBIT)
    setup_server_alias()
    log(f"Part A: shift pair for TAS flows = {TAS_A}; rate limit = {RATE_MBIT or 'none'}; added delay = {ADDED_DELAY_MS} ms")
    cells = cells_A()
    for rnd in range(1, REPEATS_A + 1):
        k = (rnd - 1) % len(cells)
        order = cells[k:] + cells[:k]
        log(f"##### Part A round {rnd}/{REPEATS_A} #####")
        for (x, y, scn, n) in order:
            tag = f"{x}_vs_{y}_{scn}_P{n}"
            outdir = outroot / tag; outdir.mkdir(exist_ok=True)
            fx, fy = outdir / f"run{rnd}_X.json", outdir / f"run{rnd}_Y.json"
            if fx.exists() and fy.exists() and fx.stat().st_size > 0 and fy.stat().st_size > 0:
                log(f"[{tag}] run {rnd} present, skipping"); continue
            delay = 0 if scn == "equal" else ADDED_DELAY_MS
            set_path_delay(delay)
            # asymXshort: X to the real IP (short RTT), Y to the alias (long RTT). asymXlong: swapped.
            host_x, host_y = (ALIAS_IP, SERVER_LAN) if scn == "asymXlong" else (SERVER_LAN, ALIAS_IP)
            ccx = TAS_CC_NAME if x == "tas" else x
            ccy = TAS_CC_NAME if y == "tas" else y
            log(f"[{tag}] run {rnd}: X={ccx}->{host_x} Y={ccy}->{host_y} P={n}")
            time.sleep(SETTLE_SLEEP)
            def cmd(cc, host, port):
                return ["sudo", "iperf3", "-c", host, "-p", str(port), "-t", str(TEST_LENGTH), "-O", "5",
                        "-P", str(n), "-C", cc, "-J"]
            ping = subprocess.Popen(["ping", "-c", str(TEST_LENGTH), "-i", "1", SERVER_LAN],
                                    stdout=open(outdir / f"run{rnd}_ping.txt", "w"), stderr=subprocess.STDOUT)
            px = subprocess.Popen(cmd(ccx, host_x, 5201), stdout=open(fx, "w"), stderr=subprocess.STDOUT)
            py = subprocess.Popen(cmd(ccy, host_y, 5202), stdout=open(fy, "w"), stderr=subprocess.STDOUT)
            px.wait(); py.wait(); ping.wait()
            ok = True
            for f, cc in ((fx, ccx), (fy, ccy)):
                try:
                    d = json.load(open(f))
                    if "error" in d or not d.get("end"):
                        raise ValueError(d.get("error", "empty result"))
                    got = d["end"].get("sender_tcp_congestion", "")
                    if got != cc:
                        log(f"  -> WARNING: {f.name} sender CC reported '{got}', wanted '{cc}'"); ok = False
                except Exception as e:
                    log(f"  -> INVALID JSON {f.name}: {e}"); ok = False
            if not ok:
                with open(FAILED_LOG, "a") as fl:
                    fl.write(f"{outdir}/run{rnd}\n")
                for f in (fx, fy):
                    f.rename(f.with_suffix(".bad"))
            else:
                log(f"[{tag}] run {rnd}: OK")
            time.sleep(BETWEEN_RUN_SLEEP)

# ---------------------- Parts B and C ----------------------

def flent_rounds(label: str, outroot: Path, host: str, repeats: int, conds, before_cond) -> None:
    """conds: list of (condname, setup_fn or None). Interleaved rotation over (cond, test, cfg) cells."""
    cells = []
    for cname, _ in conds:
        for test, cfglist in TESTS_BC:
            for cfg in cfglist:
                cells.append((cname, test, cfg))
    for rnd in range(1, repeats + 1):
        k = (rnd - 1) % len(cells)
        order = cells[k:] + cells[:k]
        log(f"##### {label} round {rnd}/{repeats} #####")
        for cname, test, (tag, kind, st, sd) in order:
            outdir = outroot / cname / test / tag; outdir.mkdir(parents=True, exist_ok=True)
            final = outdir / f"run{rnd}.flent.gz"
            if is_valid_gzip(final):
                log(f"[{cname}/{test}/{tag}] run {rnd} present, skipping"); continue
            before_cond(cname)
            cc = apply_cfg(kind, st, sd)
            verify_cc(cc)
            time.sleep(SETTLE_SLEEP)
            log(f"[{cname}/{test}/{tag}] run {rnd}/{repeats}")
            if run_one_flent_test(test, f"{label} {cname} {test} {tag} run {rnd}", outdir, final.name, host):
                log(f"[{cname}/{test}/{tag}] run {rnd}: OK")
            else:
                log(f"[{cname}/{test}/{tag}] run {rnd}: INVALID OUTPUT")
                with open(FAILED_LOG, "a") as fl:
                    fl.write(f"{final}\n")
            if host == PUBLIC_HOST:
                time.sleep(2)
                kill_tas_sockets()      # public host may leave tas sockets open; clear them now
            time.sleep(BETWEEN_RUN_SLEEP)


def part_b() -> None:
    global SERVERS
    SERVERS = []          # public host: CC cannot be coordinated; upload direction only needs the client
    (ROOT / "partB").mkdir(exist_ok=True)
    pre = subprocess.run(["ping", "-c", "5", PUBLIC_HOST], capture_output=True, text=True)
    log("WAN pre-check ping:\n" + (pre.stdout or pre.stderr).strip().splitlines()[-1] if (pre.stdout or pre.stderr).strip() else "no ping output")
    set_client_netem(None, "")
    flent_rounds("WAN", ROOT / "partB", PUBLIC_HOST, REPEATS_B, [("wan", None)], lambda c: None)


def part_c() -> None:
    global SERVERS
    SERVERS = [SERVER_LAN]
    (ROOT / "partC").mkdir(exist_ok=True)
    conds = [(f"loss{str(l).replace('.', 'p')}", l) for l in LOSS_LEVELS]
    lossmap = dict(conds)
    state = {"cur": None}
    def before(cname):
        if state["cur"] != cname:
            set_client_netem(lossmap[cname] or None, "")
            state["cur"] = cname
    flent_rounds("LOSS", ROOT / "partC", SERVER_LAN, REPEATS_C, [(c, None) for c, _ in conds], before)


def main() -> None:
    log(f"Suite started, parts: {PARTS}")
    if not shutil.which(FLENT_BIN) and not Path(FLENT_BIN).is_file():
        fail(f"FLENT_BIN not found: {FLENT_BIN}")
    client_iface()
    log(f"Client iface: {CLIENT_IFACE}")
    try:
        for p in PARTS:
            log(f"=============== PART {p} ===============")
            {"A": part_a, "B": part_b, "C": part_c}[p]()
    finally:
        deactivate_tas()
        restore_network()
    log("Suite finished. Results under " + str(ROOT))
    if FAILED_LOG.exists() and FAILED_LOG.stat().st_size > 0:
        log(f"WARNING: invalid runs listed in {FAILED_LOG}")


if __name__ == "__main__":
    main()
