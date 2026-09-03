#!/usr/bin/env python
"""Reconcile Sage's item master against the SMEAssist product master.

Read-only. One pass over the goods book. Answers, for the window:
  - how many distinct (item, unit) Sage actually bills
  - how many exist in SMEAssist, and how many carry a ledger
  - how many Sage prices more than once (the master keeps the latest)
  - HSN coverage
"""
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P
from decimal import Decimal as D


def main():
    book = P.load_goods_book()
    latest, when, prices = {}, {}, collections.defaultdict(set)

    for bill in book.values():                       # single pass
        bdate = P.s(bill["header"].get("bill_date"))
        for l in bill["lines"]:
            if D(str(l["qty"] or 0)) <= 0:
                continue
            it = dict(l, qty=D(str(l["qty"])), unitcost=D(str(l["unitcost"])),
                      rate=P.q2(D(str(l["rate"] or 0))), ext=P.q2(D(str(l["ext"] or 0))))
            k = P.item_key(it)
            prices[k].add(P.q2(D(str(l["unitcost"]))))
            if k not in latest or bdate >= when.get(k, ""):
                latest[k], when[k] = it, bdate

    xw_all = json.load(open(P.CROSSWALK))
    xw = xw_all.get("items", {})
    built = {k for k in latest if k in xw}
    withled = {k for k in built if xw[k].get("ledger")}
    multi = [k for k, v in prices.items() if len(v) > 1]
    no_hsn = [k for k, it in latest.items() if not P.s(it.get("hsn"))]

    w = lambda lbl, n: print("  %-42s %7d" % (lbl, n))
    print("\n" + "=" * 56)
    print("ITEM MASTER RECONCILIATION   Sage -> SMEAssist")
    print("=" * 56)
    w("Sage bills these distinct (item, unit)", len(latest))
    w("built in SMEAssist", len(built))
    w("  of those, ledger-mapped ITEM_PURCHASE", len(withled))
    w("  built but carrying NO ledger", len(built) - len(withled))
    w("NOT YET BUILT", len(latest) - len(built))
    w("burned (SKU taken, not creatable)", len(xw_all.get("burned", {})))
    print("-" * 56)
    w("Sage prices more than once (latest used)", len(multi))
    w("no HSN in Sage (hsnMissing flagged)", len(no_hsn))
    print("=" * 56)
    if len(latest) - len(built):
        print("  run: ./post_sage_bills.py goods-masters --all-categories "
              "--all-items --workers 8")


if __name__ == "__main__":
    main()
