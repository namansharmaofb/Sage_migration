#!/usr/bin/env bash
# Wait for the goods-masters run given as $1 to exit, then run the AP-direct
# steps (1-4) only. Scoped to 1-4 deliberately: step 5 mints 17,129 item
# products and its two open decisions (service SAC on material items,
# RESOURCE vs RAW_MATERIAL) are still unresolved in work/RECON-BOTH-SITES.md.
set -u
cd "$(dirname "$0")/.." || exit 1
OTHER=${1:?pid to wait for}
LOG=work/logs/ap-run.log
mkdir -p work/logs
echo "$(date +%H:%M:%S) waiting for pid $OTHER (goods-masters) to finish..." | tee -a "$LOG"
while kill -0 "$OTHER" 2>/dev/null; do sleep 10; done
echo "$(date +%H:%M:%S) pid $OTHER gone - starting AP-direct steps 1-4" | tee -a "$LOG"
exec ./work/run_janapr.sh 1 4 >>"$LOG" 2>&1
