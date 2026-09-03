#!/usr/bin/env bash
# Load the whole Jan-Apr 2026 window: AP-direct then goods/PO.
#
#   ./work/run_janapr.sh              run every step from the beginning
#   ./work/run_janapr.sh 4            start at step 4 (steps below)
#   ./work/run_janapr.sh 4 4          run ONLY step 4
#
# Every step resumes: `post` skips anything already in work/posted.log and the
# masters phases skip anything already in the crosswalk, so re-running a step
# costs nothing but the document that was in flight. Safe to kill and restart.
#
# Uses the SME_TOKEN already in .env.
#
#   1 AP masters      188 GL products + 407 contacts   needs Sage up   ~0.3 h
#   2 AP dry run      shapes 11,232, posts nothing                     ~2 min
#   3 AP post 50      first batch, then gated                          ~2 min
#   4 AP post rest    11,232 bills                                     ~4.1 h
#   5 goods masters   every (item, unit) product, parallel                 hours
#   6 goods dry run   posts nothing                                    ~2 min
#   7 goods post 50   first batch, then gated                          ~2 min
#   8 goods post rest 10,378 documents                                 ~3.8 h
#   9 verify          DOD checks + failure summary
set -u -o pipefail
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
FROM=${1:-1}
TO=${2:-9}
# goods-masters builds item products in parallel (the other session added
# --all-items/--workers on 2 Sep). 6 is the script's own default.
WORKERS=${WORKERS:-6}
mkdir -p work/logs
STAMP=$(date +%Y%m%d-%H%M%S)

say()  { printf '\n\033[1m== %s\033[0m  %s\n' "$*" "$(date +%H:%M:%S)"; }
die()  { printf '\n\033[31mSTOP: %s\033[0m\n' "$*"; exit 1; }
run()  { local n=$1 name=$2; shift 2
         local log="work/logs/${STAMP}-step${n}-${name}.log"
         say "step $n: $name"
         echo "   log: $log"
         "$@" >"$log" 2>&1
         local rc=$?
         tail -25 "$log"
         [ $rc -eq 0 ] || die "step $n exited $rc - see $log"
         echo "   step $n done"; }
want() { [ "$FROM" -le "$1" ] && [ "$TO" -ge "$1" ]; }

# ---------------------------------------------------------------- preflight
say "preflight"
OTHER=$(pgrep -af "post_sage_bills\.py" | grep -v "bash -c" | grep -v "^$$ " || true)
if [ -n "$OTHER" ]; then
  echo "$OTHER"
  die "another post_sage_bills.py is running. Both write work/crosswalk_live.json
     and mint SKUs; two at once race on both. Wait for it, then re-run."
fi
echo "   no other run in progress"
$PY - <<'EOF' || exit 1
import sys
sys.path.insert(0, ".")
import post_sage_bills as m
api = m.Api()
st, body = api.get("/product/products?searchKey=SAGE-4E2ME07&pageSize=1&pageNumber=0")
if not api.ok(st, body):
    print("   TOKEN REJECTED (status %s). A 403 here is indistinguishable from"
          "\n   rate limiting once the run starts, so fix it now." % st)
    sys.exit(1)
print("   token ok (status %s)" % st)
EOF
[ $? -eq 0 ] || die "SME_TOKEN is not usable"

SAGE_UP=1
$PY -c "import sys;sys.path.insert(0,'.');import post_sage_bills as m;m.sage_query('SELECT 1 x')" \
  >/dev/null 2>&1 || SAGE_UP=0
if [ $SAGE_UP -eq 1 ]; then echo "   Sage reachable"
else echo "   Sage DOWN - fine for every step except 1 (masters reads vendor and"
     echo "               GL names straight from Sage, no staging fallback)"; fi

# ------------------------------------------------------------------ AP-direct
if want 1; then
  [ $SAGE_UP -eq 1 ] || die "step 1 needs Sage up. Wait for it, or start at step 2
     if masters is already built."
  run 1 ap-masters $PY post_sage_bills.py masters
  $PY - <<'EOF' || die "masters did not build enough. Contacts are the gate: the
     earlier run skipped 11,170 bills for 'no contact built for vendor'.
     Check the step 1 log and work/contacts_held.json before going on."
import json, sys
x = json.load(open("work/crosswalk_live.json"))
p, c = len(x.get("products", {})), len(x.get("contacts", {}))
print("   crosswalk: %d products, %d contacts" % (p, c))
sys.exit(0 if (p >= 150 and c >= 300) else 1)
EOF
fi

want 2 && run 2 ap-dryrun $PY post_sage_bills.py dryrun

if want 3; then
  run 3 ap-post-50 $PY post_sage_bills.py post --limit 50
  $PY - <<'EOF' || die "the first AP batch shows drift against Sage. Look at
     work/failures.json before posting the remaining 11,000."
import json, os, sys
f = "work/failures.json"
if not os.path.exists(f):
    print("   no failures.json - clean"); sys.exit(0)
rows = json.load(open(f))
drift = [r for r in rows if "readback" in r.get("why", "")]
print("   %d failures, %d of them read-back drift against Sage" % (len(rows), len(drift)))
for r in rows[:10]:
    print("      %-34s %s" % (r["doc"], r["why"][:90]))
sys.exit(1 if drift else 0)
EOF
fi

want 4 && run 4 ap-post-rest $PY post_sage_bills.py post

# ----------------------------------------------------------------- goods / PO
if want 5; then
  echo
  echo "   NOTE step 5 mints up to 17,129 item products and is the biggest single"
  echo "   write here. Two things are still open (work/RECON-BOTH-SITES.md):"
  echo "     - 1,563 items have no HSN and will get EXPENSE_SAC 996719, a SERVICE"
  echo "       sac, on material items"
  echo "     - every item is created as RESOURCE, not RAW_MATERIAL"
  echo "   Running --all-items --all-categories with $WORKERS workers."
  run 5 goods-masters $PY post_sage_bills.py goods-masters \
        --all-items --all-categories --workers "$WORKERS"
fi

want 6 && run 6 goods-dryrun $PY post_sage_bills.py goods-dryrun

if want 7; then
  run 7 goods-post-50 $PY post_sage_bills.py goods-post --limit 50
  [ -f work/goods_failures.json ] && { echo "   goods failures:";
    head -40 work/goods_failures.json; }
fi

want 8 && run 8 goods-post-rest $PY post_sage_bills.py goods-post

# --------------------------------------------------------------------- verify
if want 9; then
  run 9 verify $PY post_sage_bills.py verify
  echo
  say "expected and not worth chasing"
  echo "   ~795 RCM TAX VARIANCE lines -> work/rcm_variance.json"
  echo "        Sage rounds reverse-charge tax to the rupee on 1,081 of 1,084"
  echo "        documents; the slab figure is the exact one. <= Rs 1.95 each."
  echo "   +/- Rs 0.01 on 503 forward documents, from Sage truncating each GST"
  echo "        authority separately. readback_drift tolerates it deliberately."
  echo
  say "24 documents will not have loaded"
  echo "   18 are Sage-side data problems - work/ERRORS-JanApr-2026.csv"
  echo "    6 need their ledger ids added to PASSTHRU_GL (Rs 6,903)"
  for f in work/posted.log work/failures.json work/rcm_variance.json; do
    [ -f "$f" ] && printf '   %-28s %s lines\n' "$f" "$(wc -l < "$f")"
  done
fi

say "finished steps $FROM..$TO"
