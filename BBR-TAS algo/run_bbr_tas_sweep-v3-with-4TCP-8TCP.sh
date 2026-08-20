#!/usr/bin/env bash
#
# BBR-TAS sweep runner
# ---------------------------------------------------------------
# Runs TCP upload tests (1, 4, and 8 simultaneous streams) and RRUL
# Flent tests across:
#   - Cubic (baseline)
#   - stock BBRv3 (baseline)
#   - BBR-TAS, all 9 (startup_shift x steady_shift) combinations
#
# Congestion control is set to match on BOTH client and server for
# every run, since RRUL mixes upload and download flows and Grazia's
# methodology keeps upload/download on the same TCP variant per test
# to avoid a friendliness/fairness confound between variants.
#
# REQUIREMENTS BEFORE RUNNING:
#   1. tcp_bbr_tas.ko built and present at the same path on BOTH
#      client and server (built separately on each against that
#      machine's own kernel headers -- do not copy the .ko itself
#      between machines, rebuild on each).
#   2. Passwordless SSH key auth from client -> server for the user
#      running this script (needed for sudo rmmod/insmod/sysctl on
#      the server without prompting mid-sweep).
#   3. Flent installed on the client; iperf3/netperf backend Flent
#      needs is running/available on the server as usual.
#   4. Wi-Fi channel already confirmed clear of interference.
#
# EDIT THE VARIABLES BELOW BEFORE RUNNING.
# ---------------------------------------------------------------

set -uo pipefail

# ---------------------- USER CONFIGURATION ----------------------

SERVER_IP="192.168.1.169"          # server's IP, as seen from client
SERVER_SSH_USER="mahsan"           # SSH user for server-side commands
# -o ControlMaster=no explicitly disables connection multiplexing for
# these calls, even if the user's ~/.ssh/config enables it globally.
# A persistent multiplexed master connection can hold a long-lived
# reference to whatever congestion control was active when it was
# established, which prevents `rmmod tcp_bbr_tas` from ever succeeding
# for the lifetime of that master connection -- not just until the
# test's own sockets close.
SERVER_SSH="ssh -o ControlMaster=no -o ControlPath=none ${SERVER_SSH_USER}@${SERVER_IP}"

TAS_KO_PATH_CLIENT="/home/mahsan/bbr-tas/tcp_bbr_tas.ko"
TAS_KO_PATH_SERVER="/home/mahsan/bbr-tas/tcp_bbr_tas.ko"   # path ON the server

TAS_MODULE_NAME="tcp_bbr_tas"      # module name as shown in `lsmod`
TAS_CC_NAME="tas"                  # .name field registered in tcp_congestion_ops

FLENT_BIN="/home/mahsan/flent/run-flent"   # path to the Flent binary/wrapper to use

RESULTS_DIR="$HOME/bbr-tas-results/$(date +%Y%m%d_%H%M%S)"
REPEATS=10                          # number of repeats per test per config
TEST_LENGTH=60                     # seconds, matches Grazia's 30s TCP window
SETTLE_SLEEP=5                     # seconds to wait after switching CC before testing
BETWEEN_RUN_SLEEP=3                # seconds between repeated runs of the same test

# --- Resume support ---
# Usage:
#   ./run_bbr_tas_sweep.sh                  Start a fresh sweep (new timestamped dir)
#   ./run_bbr_tas_sweep.sh /path/to/prior/results/dir
#                                            Resume an interrupted sweep: any config
#                                            whose folder already contains a .complete
#                                            marker (all runs succeeded, zero failures)
#                                            is skipped; everything else re-runs from
#                                            scratch for that config.
if [[ $# -ge 1 ]]; then
  RESULTS_DIR="$1"
  [[ -d "$RESULTS_DIR" ]] || { echo "ERROR: resume directory does not exist: $RESULTS_DIR"; exit 1; }
  RESUMING=1
else
  RESUMING=0
fi

# Factorial sweep: startup_shift steady_shift pairs (V1-V9)
TAS_CONFIGS=(
  "9 10"   # V1 - matches BBR-BVR baseline
  "9 9"    # V2
  "9 8"    # V3
  "8 10"   # V4
  "8 9"    # V5
  "8 8"    # V6
  "7 10"   # V7
  "7 9"    # V8
  "7 8"    # V9
)

# ---------------------- END USER CONFIGURATION -------------------

mkdir -p "$RESULTS_DIR"
MANIFEST="$RESULTS_DIR/manifest.log"
if [[ "$RESUMING" -eq 1 ]]; then
  echo "BBR-TAS sweep RESUMED: $(date)" | tee -a "$MANIFEST"
else
  echo "BBR-TAS sweep started: $(date)" | tee "$MANIFEST"
fi

log() {
  echo "[$(date '+%H:%M:%S')] $*" | tee -a "$MANIFEST"
}

fail() {
  log "ERROR: $*"
  exit 1
}

# Verifies a file actually starts with the gzip magic bytes (1f 8b).
# Flent can write a plain-text error message to the -o path instead of
# real gzip data if the test itself failed (e.g. netperf/iperf backend
# unreachable on the server), so a file existing is not proof it's valid.
is_valid_gzip() {
  local f="$1"
  [[ -s "$f" ]] || return 1
  local magic
  magic=$(head -c2 "$f" | od -An -tx1 | tr -d ' \n')
  [[ "$magic" == "1f8b" ]]
}

# Fail immediately if the Flent binary path is wrong, rather than
# discovering it hours into the sweep on the first test call.
[[ -x "$FLENT_BIN" ]] || fail "FLENT_BIN not found or not executable: $FLENT_BIN"
log "Using Flent binary: $FLENT_BIN"

# --- Switches CC away from "tas" to cubic BEFORE rmmod, on both hosts. ---
# The kernel refuses to rmmod a congestion-control module while it is
# still the active net.ipv4.tcp_congestion_control (or while any live
# socket references it) -- this must run first, every time, on both
# client and server, or rmmod fails with "module is in use".
# Safe to call even if tas isn't currently loaded/active: switching CC
# to cubic when cubic is already active is a harmless no-op, and rmmod
# on a module that isn't loaded is suppressed below.
deactivate_tas() {
  sudo sysctl -w net.ipv4.tcp_congestion_control=cubic >/dev/null \
    || fail "could not switch client CC to cubic before rmmod"
  $SERVER_SSH "sudo sysctl -w net.ipv4.tcp_congestion_control=cubic >/dev/null" \
    || fail "could not switch server CC to cubic before rmmod"

  # rmmod can still legitimately fail here if a socket from the just-
  # finished test hasn't closed yet even after switching CC away from
  # tas (the CC switch only affects *new* sockets/default; existing
  # connections keep whatever CC they were created with). Retry a
  # few times with a short wait rather than failing immediately.
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if sudo rmmod "$TAS_MODULE_NAME" 2>/dev/null; then
      break
    fi
    if ! lsmod | grep -q "^${TAS_MODULE_NAME}"; then
      break   # wasn't loaded to begin with -- nothing to do
    fi
    log "client rmmod attempt $attempt/10 still busy, waiting..."
    sleep 3
  done
  if lsmod | grep -q "^${TAS_MODULE_NAME}"; then
    log "DIAGNOSTIC: lsmod entry for $TAS_MODULE_NAME:"
    lsmod | grep "^${TAS_MODULE_NAME}" | tee -a "$MANIFEST"
    log "DIAGNOSTIC: sockets currently using tas congestion control:"
    sudo ss -tin 2>/dev/null | grep -B1 " tas " | tee -a "$MANIFEST"
    log "DIAGNOSTIC: check for a lingering SSH control master:"
    ssh -O check "${SERVER_SSH_USER}@${SERVER_IP}" 2>&1 | tee -a "$MANIFEST"
    fail "could not rmmod $TAS_MODULE_NAME on client after 10 attempts (module still in use)"
  fi

  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if $SERVER_SSH "sudo rmmod $TAS_MODULE_NAME 2>/dev/null"; then
      break
    fi
    if ! $SERVER_SSH "lsmod | grep -q '^${TAS_MODULE_NAME}'"; then
      break
    fi
    log "server rmmod attempt $attempt/10 still busy, waiting..."
    sleep 3
  done
  if $SERVER_SSH "lsmod | grep -q '^${TAS_MODULE_NAME}'"; then
    log "DIAGNOSTIC: server lsmod entry for $TAS_MODULE_NAME:"
    $SERVER_SSH "lsmod | grep '^${TAS_MODULE_NAME}'" | tee -a "$MANIFEST"
    log "DIAGNOSTIC: server sockets currently using tas congestion control:"
    $SERVER_SSH "sudo ss -tin 2>/dev/null | grep -B1 ' tas '" | tee -a "$MANIFEST"
    fail "could not rmmod $TAS_MODULE_NAME on server after 10 attempts (module still in use)"
  fi
}

# --- Sets congestion control to a built-in name (cubic/bbr) on both hosts ---
set_builtin_cc() {
  local cc_name="$1"

  deactivate_tas   # switch away from tas + unload it, in the correct order

  sudo sysctl -w net.ipv4.tcp_congestion_control="$cc_name" >/dev/null \
    || fail "could not set client CC to $cc_name"
  $SERVER_SSH "sudo sysctl -w net.ipv4.tcp_congestion_control=$cc_name >/dev/null" \
    || fail "could not set server CC to $cc_name"

  local client_cc server_cc
  client_cc=$(sysctl -n net.ipv4.tcp_congestion_control)
  server_cc=$($SERVER_SSH "sysctl -n net.ipv4.tcp_congestion_control")
  log "Set builtin CC: client=$client_cc server=$server_cc"

  [[ "$client_cc" == "$cc_name" && "$server_cc" == "$cc_name" ]] \
    || fail "CC verification mismatch (client=$client_cc server=$server_cc, wanted=$cc_name)"
}

# --- Loads BBR-TAS with given shifts on both hosts, then activates it ---
set_tas_cc() {
  local startup_shift="$1"
  local steady_shift="$2"

  deactivate_tas   # switch away from tas (to cubic) + unload the old-shift instance
  sleep 1

  sudo insmod "$TAS_KO_PATH_CLIENT" \
      tas_pacing_shift_startup="$startup_shift" \
      tas_pacing_shift_steady="$steady_shift" \
    || fail "insmod failed on client for shifts $startup_shift/$steady_shift"

  $SERVER_SSH "sudo insmod $TAS_KO_PATH_SERVER \
      tas_pacing_shift_startup=$startup_shift \
      tas_pacing_shift_steady=$steady_shift" \
    || fail "insmod failed on server for shifts $startup_shift/$steady_shift"

  sudo sysctl -w net.ipv4.tcp_congestion_control="$TAS_CC_NAME" >/dev/null \
    || fail "could not set client CC to $TAS_CC_NAME"
  $SERVER_SSH "sudo sysctl -w net.ipv4.tcp_congestion_control=$TAS_CC_NAME >/dev/null" \
    || fail "could not set server CC to $TAS_CC_NAME"

  # Verify module parameters actually took, on both sides
  local c_startup c_steady s_startup s_steady
  c_startup=$(cat /sys/module/${TAS_MODULE_NAME}/parameters/tas_pacing_shift_startup)
  c_steady=$(cat /sys/module/${TAS_MODULE_NAME}/parameters/tas_pacing_shift_steady)
  s_startup=$($SERVER_SSH "cat /sys/module/${TAS_MODULE_NAME}/parameters/tas_pacing_shift_startup")
  s_steady=$($SERVER_SSH "cat /sys/module/${TAS_MODULE_NAME}/parameters/tas_pacing_shift_steady")

  log "TAS loaded: client(startup=$c_startup,steady=$c_steady) server(startup=$s_startup,steady=$s_steady)"

  [[ "$c_startup" == "$startup_shift" && "$c_steady" == "$steady_shift" \
     && "$s_startup" == "$startup_shift" && "$s_steady" == "$steady_shift" ]] \
    || fail "parameter verification mismatch for shifts $startup_shift/$steady_shift"
}

# Runs a single Flent test, capturing the auto-named raw data file
# Flent writes into -D and renaming it to a predictable name. Also
# enables --socket-stats during the live test (captures per-socket
# TCP diagnostics -- cwnd, rtt, etc. -- via periodic `ss` polling,
# stored as extra data series inside the raw .flent.gz), and, once
# the raw file is confirmed valid, runs a second CHEAP post-processing
# pass against that same saved file (-i, no live traffic re-run) to
# also emit a `-f stats` table alongside the existing summary.txt.
#
# NOTE: -f/-o control *processed* output, same slot used for the
# summary.txt this script already relies on for downstream parsing --
# stats output is generated as a SEPARATE file, not a replacement,
# so existing summary.txt-based analysis scripts keep working unchanged.
run_one_flent_test() {
  local test_name="$1"     # e.g. tcp_upload or rrul
  local title="$2"
  local outdir="$3"
  local final_name="$4"    # desired final filename, e.g. tcp_upload_run1.flent.gz

  local marker
  marker=$(mktemp)

  "$FLENT_BIN" "$test_name" -H "$SERVER_IP" -l "$TEST_LENGTH" \
    -t "$title" \
    -D "$outdir" \
    --socket-stats \
    -o "$outdir/${final_name%.flent.gz}.summary.txt" \
    >> "$RESULTS_DIR/flent_stdout.log" 2>&1

  local newfile
  newfile=$(find "$outdir" -maxdepth 1 -name '*.flent.gz' -newer "$marker" 2>/dev/null | head -n1)
  rm -f "$marker"

  if [[ -n "$newfile" ]] && is_valid_gzip "$newfile"; then
    mv "$newfile" "$outdir/$final_name"

    # Second pass: reformat the SAME already-collected raw data as
    # `stats`, into a separate file. This is best-effort -- if it
    # fails, the run itself is still valid (the raw .flent.gz and
    # summary.txt, which everything else depends on, are already
    # safely written), so a stats-formatting failure does NOT count
    # against tag_failures / failed_runs.log.
    local stats_out="$outdir/${final_name%.flent.gz}.stats.txt"
    if ! "$FLENT_BIN" -i "$outdir/$final_name" -f stats -o "$stats_out" \
         >> "$RESULTS_DIR/flent_stdout.log" 2>&1; then
      log "  -> WARNING: stats-format post-processing failed for: $title (raw data and summary.txt are still fine)"
    fi

    return 0
  else
    log "  -> no valid raw data file found for: $title"
    [[ -n "$newfile" ]] && log "     (found non-gzip file: $newfile, left in place for inspection)"
    return 1
  fi
}

# --- Runs the four Flent tests REPEATS times each, for the current CC config ---
run_tests() {
  local tag="$1"    # used in output filenames, e.g. "cubic" or "tas_s9_st10"
  local outdir="$RESULTS_DIR/$tag"
  mkdir -p "$outdir"

  local tag_failures=0

  log "Settling ${SETTLE_SLEEP}s before testing config: $tag"
  sleep "$SETTLE_SLEEP"

  # Test types run every repeat, in this order: single-flow upload,
  # 4-stream upload, 8-stream upload, then RRUL. (test_name, file_prefix)
  local -a TEST_TYPES=(
    "tcp_upload tcp_upload"
    "tcp_4up tcp_4up"
    "tcp_8up tcp_8up"
    "rrul rrul"
  )

  for run in $(seq 1 "$REPEATS"); do
    for entry in "${TEST_TYPES[@]}"; do
      read -r test_name file_prefix <<< "$entry"
      log "[$tag] $file_prefix run $run/$REPEATS"
      if run_one_flent_test "$test_name" "$tag $file_prefix run $run" "$outdir" "${file_prefix}_run${run}.flent.gz"; then
        log "[$tag] $file_prefix run $run: OK"
      else
        log "[$tag] $file_prefix run $run: INVALID OUTPUT -- see flent_stdout.log"
        echo "$outdir/${file_prefix}_run${run}.flent.gz" >> "$RESULTS_DIR/failed_runs.log"
        tag_failures=$((tag_failures + 1))
      fi
      sleep "$BETWEEN_RUN_SLEEP"
    done
  done

  if [[ "$tag_failures" -eq 0 ]]; then
    touch "$outdir/.complete"
    log "Completed all runs for config: $tag (marked complete)"
  else
    log "Completed all runs for config: $tag WITH $tag_failures FAILURE(S) -- not marked complete, will re-run on resume"
  fi
}

# --- Checks whether a config was already fully completed in a prior run ---
config_already_complete() {
  local tag="$1"
  [[ -f "$RESULTS_DIR/$tag/.complete" ]]
}

# ------------------------------ MAIN ------------------------------

log "Results will be saved under: $RESULTS_DIR"

# --- Baselines ---
log "=== Baseline: Cubic ==="
if config_already_complete "cubic"; then
  log "cubic already complete, skipping"
else
  set_builtin_cc "cubic"
  run_tests "cubic"
fi

log "=== Baseline: stock BBRv3 ==="
if config_already_complete "bbrv3"; then
  log "bbrv3 already complete, skipping"
else
  set_builtin_cc "bbr"
  run_tests "bbrv3"
fi

# --- BBR-TAS factorial sweep ---
for cfg in "${TAS_CONFIGS[@]}"; do
  read -r startup steady <<< "$cfg"
  tag="tas_s${startup}_st${steady}"
  if config_already_complete "$tag"; then
    log "=== BBR-TAS config: startup=$startup steady=$steady -- already complete, skipping ==="
    continue
  fi
  log "=== BBR-TAS config: startup=$startup steady=$steady ==="
  set_tas_cc "$startup" "$steady"
  run_tests "$tag"
done

# --- Cleanup: switch back to cubic (unloading tas in the process) ---
deactivate_tas

log "Sweep complete: $(date)"
log "All results under: $RESULTS_DIR"

if [[ -s "$RESULTS_DIR/failed_runs.log" ]]; then
  fail_count=$(wc -l < "$RESULTS_DIR/failed_runs.log")
  log "WARNING: $fail_count run(s) produced invalid output. See:"
  log "  $RESULTS_DIR/failed_runs.log"
  log "Re-check server-side netperf/iperf backend and re-run just these configs."
else
  log "All runs produced valid output files."
fi
