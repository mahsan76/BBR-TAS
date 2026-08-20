#!/usr/bin/env python3
"""
Fixes hunk #1 of BBR-TAS.patch by replacing the two
BBR_FC_PACING_SHIFT_* #define lines directly, matched by content
instead of surrounding context (which differed from what the patch
assumed). Safe to run even though hunks 2-5 already applied via
`patch`, since this only touches the #define block.

Usage:
    python3 fix_hunk1.py tcp_bbr_tas.c
"""
import sys

REPLACEMENT = '''
/*
 * BBR-TAS: TSQ-Adaptive pacing Shift.
 * The shift values below are exposed as module parameters instead of
 * compile-time constants so that a factorial sweep of
 * (startup_shift, steady_shift) pairs can be driven from user space
 * with modprobe/insmod, without rebuilding the module between runs.
 * Defaults reproduce the BBR-BVR baseline (9, 10).
 */
static int tas_pacing_shift_startup = 9;
static int tas_pacing_shift_steady  = 10;
module_param(tas_pacing_shift_startup, int, 0644);
MODULE_PARM_DESC(tas_pacing_shift_startup,
\t\t  "TSQ pacing shift used during STARTUP and DRAIN");
module_param(tas_pacing_shift_steady, int, 0644);
MODULE_PARM_DESC(tas_pacing_shift_steady,
\t\t  "TSQ pacing shift used during ProbeBW/ProbeRTT (steady state)");
'''

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 fix_hunk1.py <path-to-tcp_bbr_tas.c>")
        sys.exit(1)

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if "BBR_FC_PACING_SHIFT_STARTUP" in line and line.lstrip().startswith("#define"):
            start_idx = i
        if start_idx is not None and "BBR_FC_PACING_SHIFT_STEADY" in line and line.lstrip().startswith("#define"):
            end_idx = i
            break

    if start_idx is None or end_idx is None:
        print("ERROR: could not find both #define lines. "
              "Either they're already replaced, or the file doesn't "
              "match what's expected. No changes made.")
        sys.exit(1)

    print(f"Found #define block at lines {start_idx + 1}-{end_idx + 1} (1-indexed):")
    print("  " + lines[start_idx].rstrip())
    print("  " + lines[end_idx].rstrip())

    new_lines = lines[:start_idx] + [REPLACEMENT.lstrip("\n") + "\n"] + lines[end_idx + 1:]

    backup_path = path + ".bak"
    with open(backup_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"Backup saved to {backup_path}")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"Replacement applied to {path}")

if __name__ == "__main__":
    main()
