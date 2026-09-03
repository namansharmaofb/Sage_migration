#!/usr/bin/env python
"""Break the 30 state-held vendors into groups that can be argued separately.

Read-only. The point is to separate:
  (a) internal accounts - "Reimb IDEPL <unit>", "ONE TIME VENDOR" - which are
      the organisation reimbursing itself and have no external address at all;
  (b) vendors whose state IS recoverable, from a city/address line corroborated
      by the pincode's own postal circle (the rule already agreed for the
      GSTIN-prefix case: two independent fields agreeing is evidence);
  (c) whatever is genuinely left.

and to report, per group, whether GST is even in play - if a bill carries no
forward-charge tax, the state does not move any money.
"""
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

SQL = """
SET NOCOUNT ON;
SELECT RTRIM(VENDORID) v, RTRIM(VENDNAME) name,
       RTRIM(TEXTSTRE1) s1, RTRIM(TEXTSTRE2) s2, RTRIM(TEXTSTRE3) s3,
       RTRIM(TEXTSTRE4) s4, RTRIM(NAMECITY) city, RTRIM(CODEPSTL) pin
  FROM APVEN WHERE RTRIM(VENDORID) IN (%s)
"""

# Only city names that are unambiguous for a state AND appear in this data.
CITY_STATE = {
    "BANGALORE": "KARNATAKA", "BENGALURU": "KARNATAKA",
    "BANGARPET": "KARNATAKA", "MYSORE": "KARNATAKA",
    "KOLKATA": "WEST_BENGAL", "CALCUTTA": "WEST_BENGAL",
    "DELHI": "DELHI", "NEWDELHI": "DELHI",
    "CHENNAI": "TAMIL_NADU", "MUMBAI": "MAHARASHTRA",
    "HYDERABAD": "TELANGANA", "PUNE": "MAHARASHTRA",
}


def city_state(*texts):
    for t in texts:
        up = re.sub(r"[^A-Z]", "", (t or "").upper())
        for city, st in CITY_STATE.items():
            if city in up:
                return st, city
    return None, None


def main():
    held = json.load(open(os.path.join(HERE, "work", "contacts_held.json")))
    codes = sorted({h["vendor"] for h in held if "unresolved" in h["reason"]})
    rows = {m.s(r["v"]): r for r in m.sage_query(
        SQL % ",".join("'%s'" % c for c in codes))}

    book = m.load_book()
    stats = collections.defaultdict(lambda: {"bills": 0, "taxed": 0, "tax": m.D(0)})
    for key, bill in book.items():
        if key[0] not in codes:
            continue
        shape, why = m.classify(bill)
        s = stats[key[0]]
        s["bills"] += 1
        if not why and shape["tax"] > 0 and not shape["is_rcm"]:
            s["taxed"] += 1
            s["tax"] += shape["tax"]

    groups = collections.defaultdict(list)
    for c in codes:
        r = rows.get(c, {})
        name = m.s(r.get("name"))
        pin = re.sub(r"\D", "", m.s(r.get("pin")))
        pin_states = m.PIN2_STATES.get(pin[:2]) if re.match(r"^[1-9][0-9]{5}$", pin) else None
        st, via = city_state(m.s(r.get("city")), m.s(r.get("s4")), m.s(r.get("s3")),
                             m.s(r.get("s2")), m.s(r.get("s1")))
        internal = bool(re.match(r"(?i)\s*(reimb\b|one time vendor)", name))
        if internal:
            groups["A internal org account (no external address)"].append((c, name, "-", "-"))
        elif st and pin_states and st in pin_states:
            groups["B city + pincode AGREE"].append((c, name, st, "%s/%s" % (via, pin)))
        elif st and not pin_states:
            groups["C city only, no pincode to corroborate"].append((c, name, st, via))
        elif st and pin_states:
            groups["D city and pincode CONFLICT"].append((c, name, st, "%s vs pin %s" % (via, pin)))
        else:
            groups["E nothing at all"].append((c, name, "-", "-"))

    for g in sorted(groups):
        rowsg = groups[g]
        b = sum(stats[c]["bills"] for c, *_ in rowsg)
        t = sum(stats[c]["taxed"] for c, *_ in rowsg)
        amt = sum((stats[c]["tax"] for c, *_ in rowsg), m.D(0))
        print("\n=== %s ===" % g)
        print("    %d vendors, %d bills, %d of them carrying forward-charge GST (Rs %s)"
              % (len(rowsg), b, t, m.q2(amt)))
        for c, name, st, via in rowsg:
            print("      %-9s %-34s %-14s %-18s bills=%d taxed=%d"
                  % (c, name[:34], st, via, stats[c]["bills"], stats[c]["taxed"]))


if __name__ == "__main__":
    main()
