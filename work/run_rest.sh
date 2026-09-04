#!/bin/bash
# Strictly serial. Two writers against this API rate-limit each other badly:
# measured 12 bills/min concurrent vs 63 bills/min alone, with 144 backoff
# events in one goods-masters log. One phase at a time is FASTER overall.
cd /home/namansharma/Desktop/sage-pull
PY=.venv/bin/python
while kill -0 "$1" 2>/dev/null; do sleep 20; done
echo "[$(date '+%H:%M:%S')] AP-direct done. posted.log=$(wc -l < work/posted.log)"

$PY ./post_sage_bills.py goods-masters --all-categories --all-items --workers 8 \
    > work/logs/gm-$(date +%Y%m%d-%H%M%S).log 2>&1
echo "[$(date '+%H:%M:%S')] goods-masters exit=$? items=$($PY -c "import json;print(len(json.load(open('work/crosswalk_live.json')).get('items',{})))")"

$PY ./post_sage_bills.py goods-post --all-categories \
    > work/logs/goods-post-$(date +%Y%m%d-%H%M%S).log 2>&1
echo "[$(date '+%H:%M:%S')] goods-post exit=$? posted.log=$(wc -l < work/posted.log)"
