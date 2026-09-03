#!/usr/bin/env python
"""How many of the 393 PO-linked documents will actually itemise?

Read-only. The PO path has never run: Sage was unreachable for every previous
run, so load_po_items() returned empty and every bill logged "no PO linked".
Now that the detail is cached, this reports what classify() will decide for each
of those documents BEFORE the post run reaches them - a gate that rejects them
all would be worth knowing about now rather than in four hours.

    .venv/bin/python work/check_po_items.py
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402


def main():
    po = json.load(open(os.path.join(HERE, "work", "po_items_cache.json")))
    podocs = {(r["vendor"], m.base_invoice(r["invoice"])) for r in po}
    print("PO-linked documents in the cache: %d\n" % len(podocs))

    book = m.load_book()

    reasons = collections.Counter()
    itemised = []
    for key, bill in book.items():
        if key not in podocs:
            continue
        shape, why = m.classify(bill)
        if why:
            reasons["HELD by classify: %s" % why[:60]] += 1
            continue
        src = shape.get("item_source", "n/a")
        reasons[src[:70]] += 1
        if shape.get("items"):
            itemised.append((key, len(shape["items"]), shape["taxable"]))

    print("\n=== what classify() decides for the %d PO documents ===" % len(podocs))
    for why, n in reasons.most_common():
        print("  %5d  %s" % (n, why))

    print("\n  documents that WILL carry Sage item lines: %d" % len(itemised))
    for key, n, tx in itemised[:10]:
        print("     %-34s %2d items  taxable=%s" % ("%s|%s" % key, n, tx))


if __name__ == "__main__":
    main()
