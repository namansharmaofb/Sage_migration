#!/bin/bash
# goods-post after goods-masters. Both post loops are append-only to
# posted.log (O_APPEND + fsync); only goods-masters writes the crosswalk.
cd /home/namansharma/Desktop/sage-pull
while kill -0 "$1" 2>/dev/null; do sleep 20; done
echo "[$(date '+%H:%M:%S')] goods-masters done; items=$(python3 -c "import json;print(len(json.load(open('work/crosswalk_live.json')).get('items',{})))")"
.venv/bin/python ./post_sage_bills.py goods-post --all-categories \
  > work/logs/goods-post-$(date +%Y%m%d-%H%M%S).log 2>&1
echo "[$(date '+%H:%M:%S')] goods-post exit=$? posted.log=$(wc -l < work/posted.log)"
