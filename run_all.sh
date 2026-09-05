#!/usr/bin/env bash
# One command for the whole migration.
#
#   ./run_all.sh            run every phase that still has work
#   ./run_all.sh --check    preflight + reconcile only, change nothing
#   ./run_all.sh --from goods-masters   start at a given phase
#
# Every phase is resumable: bills already in work/posted.log are skipped,
# masters already in work/crosswalk_live.json are skipped. Re-running after an
# interruption continues, it does not repeat.
#
# Phases run STRICTLY ONE AT A TIME. Two of them against this API throttle each
# other badly - measured 12 bills/min concurrent against 63 bills/min alone -
# so serial is not just safer here, it is faster.
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
LOGS=work/logs; mkdir -p "$LOGS"
LOCK=work/.run_all.lock
STAMP() { date '+%H:%M:%S'; }
say()   { printf '\n[%s] %s\n' "$(STAMP)" "$*"; }

# --check only reads, so it must work WHILE a run is in flight - that is
# exactly when you want to look. It deliberately sits ahead of the locks.
if [ "${1:-}" = "--check" ]; then
    echo "[$(STAMP)] CHECK ONLY - nothing will be written"
    $PY work/reconcile.py
    $PY work/failure_report.py
    exit 0
fi

# ---------------------------------------------------------------- guard rails
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "Another run_all.sh is live (pid $(cat "$LOCK")). Refusing to start a"
    echo "second one - concurrent phases corrupt work/crosswalk_live.json."; exit 1
fi
if pgrep -f '[p]ost_sage_bills\.py' >/dev/null; then
    echo "A migration phase is already running:"
    ps -eo pid,etime,args | awk '/[p]ost_sage_bills\.py/ {print "   "$1"  "$2"  "$4" "$5}'
    echo "Wait for it to finish, or stop it first."; exit 1
fi
echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT

# ------------------------------------------------------------------- preflight
say "PREFLIGHT"
[ -f .env ] || { echo "  .env missing - cp .env.example .env and fill it in"; exit 2; }
miss=""
for k in SQL_HOST SQL_USER SQL_PASSWORD SQL_DATABASE SME_BASE SME_ORG_ID \
         SME_NAMESPACE SME_ORG_GSTIN SME_ORG_PAN SME_TOKEN SME_DB_HOST; do
    grep -q "^$k=." .env || miss="$miss $k"
done
[ -n "$miss" ] && { echo "  .env is missing values for:$miss"; exit 2; }

DB=$(sed -n 's/^SME_DB_HOST=//p' .env | head -1)
SQLH=$(sed -n 's/^SQL_HOST=//p' .env | head -1)
timeout 6 bash -c "</dev/tcp/$DB/9069" 2>/dev/null \
    && echo "  SMEAssist API   reachable" \
    || { echo "  SMEAssist API   UNREACHABLE ($DB:9069) - nothing can post"; exit 3; }
# The Sage box is a Wi-Fi DHCP host, not a server, so its address MOVES - four
# times in five days, twice into a different /24. A stale address does not fail
# cleanly either: the office subnet overlaps the lease range, so an unrelated
# device answers and you get "connection refused".
#
# This used to print a warning and carry on. Carrying on is what wrote 1,419
# products with the 9999 placeholder HSN: with Sage unreachable item_hsn_map()
# returns {}, resolve_item_hsn() silently skips its ICITEMO tier, and the run
# "succeeds" while stamping a permanent misclassification that later runs skip.
# So now it relocates first, and if it cannot, it says exactly what will be
# wrong rather than burying it in one line.
if timeout 6 bash -c "</dev/tcp/$SQLH/1433" 2>/dev/null; then
    echo "  Sage SQL Server reachable ($SQLH)"
else
    echo "  Sage SQL Server not at $SQLH - relocating..."
    if $PY work/find_sage.py --write --quiet >/dev/null 2>&1; then
        SQLH=$(sed -n 's/^SQL_HOST=//p' .env | head -1)
        echo "  Sage SQL Server found at $SQLH (.env updated)"
    else
        echo "  Sage SQL Server NOT FOUND on this network."
        echo "     The Sage laptop is a Wi-Fi DHCP host; it only answers while"
        echo "     its owner is online. Running now means:"
        echo "       - item HSN from Sage's ICITEMO is UNAVAILABLE, so every"
        echo "         item without a line HSN gets the 9999 placeholder, and"
        echo "         the crosswalk will SKIP it on later runs - permanent"
        echo "         until repaired by hand."
        echo "       - vendors come from the staging mirror, which is missing"
        echo "         469 of the vendors live APVEN has."
        echo "     Re-run when Sage is up, or pass --allow-stale-sage to accept"
        echo "     the above."
        case " $* " in
            *" --allow-stale-sage "*) echo "     --allow-stale-sage given; continuing." ;;
            *) exit 3 ;;
        esac
    fi
fi
$PY - <<'PZ' || exit 3
import sys; sys.path.insert(0, ".")
import post_sage_bills as P
st, _ = P.Api(dry_run=True).call("GET", "/contact/gst/29AABCM8279K1ZR")
print("  auth token      %s" % ("valid" if st in (200, 500) else "REJECTED (%s) - refresh SME_TOKEN" % st))
sys.exit(0 if st in (200, 500) else 1)
PZ

FROM="${2:-}"; [ "${1:-}" = "--from" ] && FROM="${2:-}"
skip() { [ -n "$FROM" ] && [ "$FROM" != "$1" ] && return 0 || return 1; }
run() {                       # run <phase-name> <args...>
    local name="$1"; shift
    if skip "$name"; then echo "  (skipping $name)"; return 0; fi
    FROM=""                   # once started, run everything after it
    local log="$LOGS/${name}-$(date +%Y%m%d-%H%M%S).log"
    say "$name  ->  $log"
    $PY ./post_sage_bills.py "$@" > "$log" 2>&1
    local rc=$?
    tail -3 "$log" | sed 's/^/     /'
    [ $rc -ne 0 ] && { echo "  $name FAILED (exit $rc) - see $log"; exit $rc; }
    echo "  $name done. posted.log=$(wc -l < work/posted.log)"
}

# --------------------------------------------------------------------- phases
run masters        masters
run post           post
run goods-masters  goods-masters --all-categories --all-items --workers 6
run goods-post     goods-post --all-categories

# ------------------------------------------------------------------ verify
say "RECONCILE"
$PY work/reconcile.py
$PY work/failure_report.py
say "DONE. Anything still outstanding is listed in work/failures-report.json"
