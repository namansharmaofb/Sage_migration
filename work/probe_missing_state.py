#!/usr/bin/env python
"""Where is the state for the 30 vendors held as "state unresolved"?

SQL_VENDORS reads only TEXTSTRE1 of APVEN's four address lines, and CODESTTE is
empty on all 30. The state may well be sitting in TEXTSTRE2/3/4 or NAMECITY.
Read-only.

    .venv/bin/python work/probe_missing_state.py
"""
import collections
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

SQL = """
SET NOCOUNT ON;
SELECT RTRIM(VENDORID) vendor, RTRIM(VENDNAME) name,
       RTRIM(TEXTSTRE1) s1, RTRIM(TEXTSTRE2) s2, RTRIM(TEXTSTRE3) s3,
       RTRIM(TEXTSTRE4) s4, RTRIM(NAMECITY) city, RTRIM(CODESTTE) st,
       RTRIM(CODEPSTL) pin, RTRIM(CODECTRY) ctry
  FROM APVEN WHERE RTRIM(VENDORID) IN (%s)
"""


def state_in(text):
    """Any state name or alias appearing in free text -> the enum value."""
    up = re.sub(r"[^A-Z]", "", (text or "").upper())
    if not up:
        return None
    for nm in m.STATE_ENUM:
        if nm in ("UNKNOWN", "OTHER_COUNTRY"):
            continue
        if re.sub(r"[^A-Z]", "", nm) in up:
            return nm
    for alias, nm in m.STATE_ALIASES.items():
        if alias in up:
            return nm
    return None


def main():
    held = json.load(open(os.path.join(HERE, "work", "contacts_held.json")))
    codes = [h["vendor"] for h in held if "unresolved" in h["reason"]]
    bills = collections.Counter()
    with open(os.path.join(HERE, "output", "bills_header.csv")) as fh:
        for r in csv.DictReader(fh):
            bills[r["vendor"]] += 1
    print("vendors held for state: %d (%d bills)\n"
          % (len(codes), sum(bills[c] for c in codes)))

    rows = {m.s(r["vendor"]): r for r in m.sage_query(
        SQL % ",".join("'%s'" % c for c in codes))}

    found, still = [], []
    for c in sorted(codes):
        r = rows.get(c)
        if not r:
            still.append((c, "no APVEN row")); continue
        blob = " | ".join(m.s(r[k]) for k in ("s1", "s2", "s3", "s4", "city"))
        hit = None
        for k in ("s4", "s3", "s2", "city", "s1"):
            hit = state_in(m.s(r[k]))
            if hit:
                hit = (hit, k); break
        pin = re.sub(r"\D", "", m.s(r["pin"]))
        pin_states = m.PIN2_STATES.get(pin[:2]) if re.match(r"^[1-9][0-9]{5}$", pin) else None
        agree = "-"
        if hit and pin_states:
            agree = "AGREES" if hit[0] in pin_states else "CONFLICTS"
        elif hit:
            agree = "no pincode"
        if hit:
            found.append((c, bills[c], hit[0], hit[1], agree, blob[:70]))
        else:
            still.append((c, blob[:70]))

    print("=== state recoverable from an address line (%d vendors, %d bills) ==="
          % (len(found), sum(f[1] for f in found)))
    print("%-9s %5s %-16s %-6s %-11s %s" % ("vendor", "bills", "state", "from", "vs pincode", "address"))
    for f in found:
        print("%-9s %5d %-16s %-6s %-11s %s" % f)

    print("\n=== still nothing (%d vendors, %d bills) ===" % (len(still), sum(bills[c] for c, _ in still)))
    for c, blob in still:
        print("  %-9s %s" % (c, blob))


if __name__ == "__main__":
    main()
