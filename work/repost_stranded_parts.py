#!/usr/bin/env python
"""Revoke and rebuild the bills that posted SHORT of Sage, so `goods-post`
re-creates them whole.

WHY
    load_goods_book() used to do `book[k] = {...}` per header row, so a goods
    document split across *N receipt parts kept only the LAST part and every
    earlier one was silently dropped. That is fixed (it accumulates now), but
    the documents posted before the fix are still short in SMEAssist:

        FABI470|1173/2025-26   posted 10,183.95 against Sage's 815,867.96
        FABI470|1177/2025-26   posted 327,021.66 against Sage's 979,295.31

    posted.log records them as done, so no later run touches them. They have to
    be revoked for the corrected shape to be posted under the same document.

WHAT IT TARGETS
    Not a hand-written list: the documents work/value_recon.py actually flagged.
    It reads work/value-mismatches.json and takes the documents whose stored
    billAmount or taxableAmount differs from Sage by more than --min-delta
    (default 1.00, which is what separates a lost receipt part from the
    sub-rupee round-off and RCM-snap drift that reposting would not change).

THE SAFETY GATE
    Before revoking anything it rebuilds every target from the CURRENT code and
    refuses unless the rebuilt payload
        - passes assert_invariants(), and
        - carries Sage's own document total to the paisa.
    A bill that cannot be rebuilt correctly is left alone: revoking it would
    turn a wrong bill into a missing one.

    It also refuses to run while any post phase is live, because both write
    work/posted.log, and posted.log is backed up before it is rewritten.

    --dry-run (the default) prints the plan and changes nothing.
    --apply   revokes, soft-deletes, and drops the keys from posted.log. Then
              re-run:  ./post_sage_bills.py goods-post --all-categories
"""
import argparse, json, os, shutil, subprocess, sys, time
from decimal import Decimal as D

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import post_sage_bills as P                                    # noqa: E402

REPORT = os.path.join(P.WORK, "value-mismatches.json")


def phases_running():
    out = subprocess.run(["pgrep", "-af", r"post_sage_bills\.py"],
                         capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if "pgrep" not in ln and ln.strip()]


def targets(min_delta):
    """-> {(vendor, invoice): [reasons]} from the reconciliation report."""
    if not os.path.exists(REPORT):
        raise SystemExit("no %s - run work/value_recon.py first" % REPORT)
    rep = json.load(open(REPORT))
    out = {}
    for m in rep["mismatches"]:
        if m["check"] not in ("bill_amount", "taxable"):
            continue
        if abs(m.get("delta") or 0) <= min_delta:
            continue
        v, _, inv = m["doc"].partition("|")
        out.setdefault((v, inv), []).append(
            "%s: sage=%s smeassist=%s delta=%s"
            % (m["field"], m["sage"], m["smeassist"], m["delta"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually revoke; without it nothing is changed")
    ap.add_argument("--min-delta", type=float, default=1.00)
    args = ap.parse_args()

    busy = phases_running()
    if busy and args.apply:
        print("REFUSING: a migration phase is live - it writes work/posted.log "
              "too, and two phases against this API throttle each other.")
        for ln in busy:
            print("   " + ln)
        return 1
    if busy:
        print("NOTE: a phase is live, so --apply would refuse. Planning only.")
        for ln in busy:
            print("   " + ln)

    want = targets(args.min_delta)
    if not want:
        print("nothing over %.2f in the reconciliation report - nothing to do"
              % args.min_delta)
        return 0
    print("\n%d document(s) flagged short of Sage by more than %.2f:"
          % (len(want), args.min_delta))
    for k, why in sorted(want.items()):
        print("   %s|%s" % k)
        for w in why:
            print("        %s" % w)

    # ---------------------------------------------------- the safety gate
    print("\nrebuilding each one from the current code before touching anything")
    for attempt in range(5):
        try:
            state = P.State(); break
        except Exception as exc:                                # noqa: BLE001
            print("   crosswalk unreadable (%s), retrying" % str(exc)[:60])
            time.sleep(3)
    else:
        raise SystemExit("could not read the crosswalk")

    book = P.load_goods_book()
    api = P.Api(dry_run=True)
    plan, refused = [], []
    for key in sorted(want):
        tag = "%s|%s" % key
        bill = book.get(key)
        if bill is None:
            refused.append((tag, "not in the goods book")); continue
        shape, why = P.classify_goods(bill, None)
        if not shape:
            refused.append((tag, "classify_goods refuses: %s" % why)); continue
        if key[0] not in state.xw["contacts"]:
            refused.append((tag, "no contact for %s" % key[0])); continue
        missing = sorted({P.s(l["gl"]) for l in shape["exp"]
                          if P.s(l["gl"]) not in state.xw["products"]})
        if missing:
            refused.append((tag, "no product for GL %s" % ", ".join(missing)))
            continue
        xwi = state.xw.get("items", {})
        no_item = sorted({P.item_key(l) for l in shape["exp"]
                          if not str(l.get("CNTLINE", "")).startswith("SVC:")
                          and not (xwi.get(P.item_key(l)) or {}).get("ledger")})
        if no_item:
            refused.append((tag, "no item ledger for %s" % ", ".join(no_item[:2])))
            continue
        payload = P.build_payload(api, key, shape, state.xw["contacts"][key[0]],
                                  state.xw["products"], items=xwi)
        bad = P.assert_invariants(payload, shape)
        if bad:
            refused.append((tag, "invariants fail: %s" % "; ".join(bad))); continue
        # The whole point of the repair: the rebuild must equal Sage's own total.
        sage_total = P.q2(D(str(bill["header"]["doc_total"] or 0)))
        got = P.q2(D(str(payload["billAmount"])))
        if got != sage_total:
            refused.append((tag, "rebuild is %s, Sage says %s" % (got, sage_total)))
            continue
        rec = state.posted.get(tag, "")
        bid = rec.split("||")[1] if "||" in rec and rec.split("||")[1].isdigit() \
            else None
        if not bid:
            refused.append((tag, "no billId in posted.log (%r)" % rec)); continue
        plan.append((tag, bid, got, len(payload["lineItemDtoList"])))
        print("   %-24s rebuilds to %s in %d lines, invariants PASS"
              % (tag, got, len(payload["lineItemDtoList"])))

    for tag, why in refused:
        print("   %-24s LEFT ALONE - %s" % (tag, why))
    if not plan:
        print("\nnothing is safe to repost. Nothing changed.")
        return 1

    print("\nplan: revoke + soft-delete %d bill(s), drop them from posted.log"
          % len(plan))
    for tag, bid, total, n in plan:
        print("   %-24s billId %s  ->  reposts at %s" % (tag, bid, total))

    if not args.apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply, then:")
        print("   ./post_sage_bills.py goods-post --all-categories")
        return 0

    live = P.Api()
    done = set()
    for tag, bid, total, n in plan:
        st, b = live.call("PUT", "/bill/updateStatus/%s/REVOKED"
                          "?remarks=reposting+dropped+receipt+parts" % bid)
        rv = "ok" if live.ok(st, b) else live.err(b)[:70]
        st2, b2 = live.call("DELETE", "/bill/%s" % bid)
        dl = "ok" if live.ok(st2, b2) else live.err(b2)[:70]
        print("   %-24s revoke=%-10s delete=%s" % (tag, rv, dl))
        if live.ok(st, b):
            done.add(tag)

    if not done:
        print("\nnothing was revoked - posted.log left untouched.")
        return 1
    shutil.copy2(P.POSTED_LOG, P.POSTED_LOG + ".before-repost")
    kept = [ln for ln in open(P.POSTED_LOG)
            if ln.strip() and ln.split("||")[0] not in done]
    tmp = P.POSTED_LOG + ".tmp"
    with open(tmp, "w") as fh:
        fh.writelines(kept)
    os.replace(tmp, P.POSTED_LOG)
    print("\ndropped %d key(s) from work/posted.log (backup: %s.before-repost)"
          % (len(done), os.path.basename(P.POSTED_LOG)))
    print("now run:  ./post_sage_bills.py goods-post --all-categories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
