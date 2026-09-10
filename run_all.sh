#!/usr/bin/env bash
# One command for the whole migration.
#
#   ./run_all.sh            run every phase that still has work
#   ./run_all.sh --check    preflight + reconcile only, change nothing
#   ./run_all.sh --from goods-masters   start at a given phase
#   ./run_all.sh --with-ar  ALSO load the AR (sales) invoices
#
# The AR phases are OPT-IN. They post sales invoices, under an export
# exemption, against AR/OE column names that come from the migration brief
# rather than from the schema inventory this repo's AP work was built on - so
# widening a plain ./run_all.sh to include them would be a scope this command
# has never had. Run ar-probe first; see the AR section of the README.
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
    # The AR arithmetic self-test needs neither Sage nor a token, so it always
    # runs here: it is the only check in this repo that can prove the sales
    # conversion ties before anything is posted.
    $PY ./post_sage_invoices.py ar-selftest || echo "  AR SELF TEST FAILED - do not run --with-ar"
    # Read-only, but it needs Sage and the AR schema, so it is allowed to fail
    # without failing the check.
    $PY ./post_sage_invoices.py ar-recon 2>&1 | tail -20 \
        || echo "  (ar-recon unavailable - run ar-probe)"
    exit 0
fi

# ---------------------------------------------------------------- guard rails
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "Another run_all.sh is live (pid $(cat "$LOCK")). Refusing to start a"
    echo "second one - concurrent phases corrupt work/crosswalk_live.json."; exit 1
fi
# Both loaders, not just the payables one: they pace against ONE process-wide
# rate gate only WITHIN a process, so two of them are two unpaced clients
# throttling each other, and the AR loader reads the AP crosswalk that a live
# AP phase is still writing.
if pgrep -f '[p]ost_sage_\(bills\|invoices\)\.py' >/dev/null; then
    echo "A migration phase is already running:"
    ps -eo pid,etime,args | awk '/[p]ost_sage_(bills|invoices)\.py/ {print "   "$1"  "$2"  "$4" "$5}'
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
# Ask find_sage.py outright rather than gating on a bare TCP connect first.
# An open port is NOT proof of Sage - there is a second SQL Server on this
# network that refuses the migration's login, and the /22 means an unrelated
# device can answer on a stale lease. A TCP-only gate would accept it, skip
# relocation, and let the run proceed against nothing - writing the very 9999
# placeholders this block exists to prevent. find_sage.py already does the
# cheap path (try the configured host first) and verifies identity.
# Retried once. find_sage.py verifies identity by logging in and counting
# ICITEMO rows, so a momentary blip - a slow probe, a dropped Wi-Fi frame -
# reads the same as "the laptop is off the network". With this as the sole
# gate, one such blip would abort the whole migration; a second attempt costs
# seconds and removes that failure mode.
if $PY work/find_sage.py --write --quiet >/dev/null 2>&1 \
   || $PY work/find_sage.py --write --quiet >/dev/null 2>&1; then
    SQLH=$(sed -n 's/^SQL_HOST=//p' .env | head -1)
    echo "  Sage SQL Server confirmed at $SQLH"
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
$PY - <<'PZ' || exit 3
import sys; sys.path.insert(0, ".")
import post_sage_bills as P
st, _ = P.Api(dry_run=True).call("GET", "/contact/gst/29AABCM8279K1ZR")
print("  auth token      %s" % ("valid" if st in (200, 500) else "REJECTED (%s) - refresh SME_TOKEN" % st))
sys.exit(0 if st in (200, 500) else 1)
PZ

# Scan for --from anywhere in the arguments. This used to be
#   FROM="${2:-}"; [ "${1:-}" = "--from" ] && FROM="${2:-}"
# where $2 was taken UNCONDITIONALLY, so `--allow-stale-sage --from goods-post`
# set FROM="--from", which matches no phase name - every phase was skipped and
# the script exited 0 reporting success while doing nothing.
FROM=""
_saw_from=""
_prev=""
for _a in "$@"; do
    [ "$_a" = "--from" ] && _saw_from=1
    [ "$_prev" = "--from" ] && { FROM="$_a"; break; }
    _prev="$_a"
done
# `--from` as the LAST argument leaves FROM empty, which used to mean "run
# everything" - the opposite of what was asked, against the live ERP.
if [ -n "$_saw_from" ] && [ -z "$FROM" ]; then
    echo "  --from needs a phase name"
    echo "     expected one of: masters post goods-masters goods-post"
    echo "                      ar-masters ar-post"
    exit 2
fi
if [ -n "$FROM" ]; then
    case "$FROM" in
        masters|post|goods-masters|goods-post) ;;
        ar-masters|ar-post) ;;
        *) echo "  --from: unknown phase '$FROM'"
           echo "     expected one of: masters post goods-masters goods-post"
           echo "                      ar-masters ar-post"
           exit 2 ;;
    esac
fi
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

# ------------------------------------------------------- AR (sales), opt-in
WITH_AR=""
case " $* " in *" --with-ar "*) WITH_AR=1 ;; esac
# --from ar-* asks for the AR phases by name, which is consent enough.
case "${FROM:-}" in ar-*) WITH_AR=1 ;; esac

run_ar() {                    # run_ar <phase-name> <args...>
    local name="$1"; shift
    if skip "$name"; then echo "  (skipping $name)"; return 0; fi
    FROM=""
    local log="$LOGS/${name}-$(date +%Y%m%d-%H%M%S).log"
    say "$name  ->  $log"
    $PY ./post_sage_invoices.py "$@" > "$log" 2>&1
    local rc=$?
    tail -3 "$log" | sed 's/^/     /'
    [ $rc -ne 0 ] && { echo "  $name FAILED (exit $rc) - see $log"; exit $rc; }
    echo "  $name done. posted_ar.log=$(wc -l < work/posted_ar.log 2>/dev/null || echo 0)"
}

if [ -n "$WITH_AR" ]; then
    # Gate on the arithmetic BEFORE touching the API. It needs no network, and
    # a sales voucher that misses Sage by a paise still posts and still
    # verifies - it is only caught much later, by reconciliation.
    say "AR SELF TEST"
    $PY ./post_sage_invoices.py ar-selftest | tail -4 | sed 's/^/     /'
    if [ "${PIPESTATUS[0]}" -ne 0 ]; then
        echo "  AR self test FAILED - refusing to post sales invoices"; exit 4
    fi
    # And gate on the schema, for the same reason the Sage relocation above
    # gates the AP run: the AR/OE column names are ASSUMED, and a load that
    # proceeds on a renamed column holds every invoice at best.
    say "AR SCHEMA PROBE"
    AR_PROBE_LOG="$LOGS/ar-probe-$(date +%Y%m%d-%H%M%S).log"
    if ! $PY ./post_sage_invoices.py ar-probe > "$AR_PROBE_LOG" 2>&1; then
        echo "  ar-probe FAILED - see $AR_PROBE_LOG. Nothing was posted."; exit 4
    fi
    # THIS run's log only. A glob over $LOGS/ar-probe-*.log would also read
    # every earlier probe, so one stale run that found gaps would block the AR
    # phases for good.
    if grep -q 'GAPS' "$AR_PROBE_LOG"; then
        echo "  ar-probe reports schema GAPS. Fix SCHEMA in"
        echo "     post_sage_invoices.py (and the query beside it), then re-run."
        sed -n '/what does not match/,$p' "$AR_PROBE_LOG" | head -30 | sed 's/^/     /'
        exit 4
    fi
    echo "  AR schema matches"
    run_ar ar-masters  ar-masters
    run_ar ar-post     ar-post
else
    echo
    echo "  (AR sales invoices NOT loaded - pass --with-ar. Run"
    echo "   ./post_sage_invoices.py ar-probe first; see the README.)"
fi

# ------------------------------------------------------------------ verify
say "RECONCILE"
$PY work/reconcile.py
$PY work/failure_report.py
if [ -n "$WITH_AR" ]; then
    $PY ./post_sage_invoices.py ar-recon
fi
say "DONE. Anything still outstanding is listed in work/failures-report.json"
if [ -n "$WITH_AR" ]; then
    say "AR work items: work/ar_held.json, work/buyers_held.json, work/ar_failures.json"
fi
exit 0
