#!/bin/bash
# I/O latency probe for watchdog(8)'s test-binary directive.
#
# Writes+syncs+reads a small file on the root filesystem (the SD card).
# Nonzero exit (or running past watchdog.conf's test-timeout, which SIGKILLs
# us) tells watchdogd to stop petting /dev/watchdog, letting the hardware
# watchdog force a reset. This targets the exact failure mode diagnosed on
# 2026-08-07: sshd blocked in D-state reading authorized_keys off a stalled
# SD card, hanging forever with no timeout of its own. systemd's own
# RuntimeWatchdogSec couldn't catch that because PID1 stayed responsive the
# whole time -- only an actual storage I/O check can.
set -e

PROBE_DIR=/var/lib/watchdog-io-test
PROBE_FILE="$PROBE_DIR/probe"
TMP_FILE="$PROBE_FILE.$$"

echo "$$ $(date +%s.%N)" > "$TMP_FILE"
sync -- "$TMP_FILE"
mv -f -- "$TMP_FILE" "$PROBE_FILE"
sync --data -- "$PROBE_DIR" 2>/dev/null || true
[ -s "$PROBE_FILE" ]
cat "$PROBE_FILE" > /dev/null
