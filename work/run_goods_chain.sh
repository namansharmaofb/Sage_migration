#!/bin/bash
# Wait for a SPECIFIC pid, then goods masters -> goods post.
# Waiting on `kill -0 $PID` rather than pgrep -f: a pattern like
# 'post_sage_bills.py post' also matches the shell that runs the pgrep, so the
# pattern form both false-positives and, worse, can exit the wait early.
cd /home/namansharma/Desktop/sage-pull
PY=.venv/bin/python
WAIT_PID="$1"
TS=$(date +%Y%m%d-%H%M%S)

while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 20; done
echo "[$(date '+%H:%M:%S')] AP-direct (pid $WAIT_PID) finished; posted.log=$(wc -l < work/posted.log)"

echo "[$(date '+%H:%M:%S')] goods-masters starting"
$PY ./post_sage_bills.py goods-masters --all-categories --all-items --workers 12 \
    > work/logs/goods-masters-$TS.log 2>&1
echo "[$(date '+%H:%M:%S')] goods-masters exit=$? posted.log=$(wc -l < work/posted.log)"

echo "[$(date '+%H:%M:%S')] goods-post starting"
$PY ./post_sage_bills.py goods-post --all-categories \
    > work/logs/goods-post-$TS.log 2>&1
echo "[$(date '+%H:%M:%S')] goods-post exit=$? posted.log=$(wc -l < work/posted.log)"
