#!/usr/bin/env python
"""Does the state move ANY money on the 30 held vendors' bills?

The previous probe counted only forward-charge GST. Reverse charge is also
state-dependent - we self-assess it, and the split into IGST vs CGST+SGST
follows the party's state just the same - so a bill that is RCM-taxed is NOT
safe to post under an assumed state. This separates the three cases properly.
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402


def main():
    held = json.load(open(os.path.join(HERE, "work", "contacts_held.json")))
    codes = {h["vendor"] for h in held if "unresolved" in h["reason"]}
    book = m.load_book()

    per = collections.defaultdict(lambda: collections.Counter())
    money = collections.defaultdict(lambda: m.D(0))
    for key, bill in book.items():
        if key[0] not in codes:
            continue
        shape, why = m.classify(bill)
        v = key[0]
        if why:
            per[v]["held by classify"] += 1
            continue
        if shape["tax"] > 0:
            per[v]["RCM taxed" if shape["is_rcm"] else "forward taxed"] += 1
            money[v] += shape["tax"]
        else:
            per[v]["NO TAX AT ALL"] += 1

    tot = collections.Counter()
    for v, c in per.items():
        tot.update(c)
    print("=== all %d state-held vendors, %d bills ===" % (len(codes), sum(tot.values())))
    for k, n in tot.most_common():
        print("   %5d  %s" % (n, k))

    print("\n=== vendors with ANY tax (state moves money - must stay held) ===")
    risky = [(v, per[v], money[v]) for v in per
             if per[v]["forward taxed"] or per[v]["RCM taxed"]]
    if not risky:
        print("   (none)")
    for v, c, amt in sorted(risky, key=lambda x: -x[2]):
        print("   %-9s forward=%-3d rcm=%-3d notax=%-3d  tax Rs %s"
              % (v, c["forward taxed"], c["RCM taxed"], c["NO TAX AT ALL"], m.q2(amt)))

    safe = sorted(v for v in per if not per[v]["forward taxed"] and not per[v]["RCM taxed"])
    nb = sum(sum(per[v].values()) for v in safe)
    print("\n=== vendors where NO bill carries any tax (state moves nothing) ===")
    print("   %d vendors, %d bills" % (len(safe), nb))
    print("   " + ", ".join(safe))


if __name__ == "__main__":
    main()
