#!/usr/bin/env python
"""If the 30 state-held vendors got a contact, would their bills actually post?

Resolving a state is only worth doing if the bill then passes eligible(). These
vendors have no GSTIN - that is precisely why the state could not be read off
one - so they build as WITHOUT_PAN_OR_GST, and eligible() deliberately skips a
URP vendor whose bill carries FORWARD-charge GST: an unregistered person cannot
legally charge it, so the tax means the GSTIN is missing from Sage, and posting
would claim input credit against a URP ledger. RCM is different - we self-assess
that tax - so a URP RCM bill is fine.

Read-only.
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
    print("state-held vendors: %d\n" % len(codes))

    vend = m.vendors_master()
    book = m.load_book()

    agg = collections.Counter()
    bills = collections.Counter()
    detail = collections.defaultdict(list)
    for key, bill in book.items():
        if key[0] not in codes:
            continue
        bills[key[0]] += 1
        shape, why = m.classify(bill)
        if why:
            agg["held by classify()"] += 1
            continue
        reg, _no = m.registration_of(vend.get(key[0], {}))
        if reg == "WITHOUT_PAN_OR_GST" and not shape["is_rcm"] and shape["tax"] > 0:
            agg["WOULD STILL SKIP - URP with forward-charge GST"] += 1
            detail["urp"].append(key[0])
        else:
            agg["WOULD POST once the state is known"] += 1
            detail["ok"].append("%s|%s" % key)

    print("bills belonging to those vendors: %d" % sum(bills.values()))
    for k, n in agg.most_common():
        print("  %5d  %s" % (n, k))

    if detail["ok"]:
        print("\npostable if state resolved (%d): %s%s"
              % (len(detail["ok"]), ", ".join(detail["ok"][:10]),
                 " ..." if len(detail["ok"]) > 10 else ""))
    if detail["urp"]:
        c = collections.Counter(detail["urp"])
        print("\nblocked as URP regardless of state (top vendors):")
        for v, n in c.most_common(8):
            print("   %-9s %d bills" % (v, n))


if __name__ == "__main__":
    main()
