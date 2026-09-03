#!/usr/bin/env bash
# Wait for the other session's run (given as $1) to exit, then load the whole
# Jan-Apr 2026 window. Waiting rather than racing: both runs write
# work/crosswalk_live.json and mint SKUs, and that race is what produced the
# SAGE-4E2ME07-R2 duplicate still sitting in the org.
set -u
cd "$(dirname "$0")/.." || exit 1
OTHER=${1:?pid to wait for}
LOG=work/logs/full-run.log
mkdir -p work/logs
echo "$(date +%H:%M:%S) waiting for pid $OTHER to finish..." | tee -a "$LOG"
while kill -0 "$OTHER" 2>/dev/null; do sleep 20; done
echo "$(date +%H:%M:%S) pid $OTHER gone - starting the full load" | tee -a "$LOG"
exec ./work/run_janapr.sh >>"$LOG" 2>&1
