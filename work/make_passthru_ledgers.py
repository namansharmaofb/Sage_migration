#!/usr/bin/env python
"""Create the three leaf ledgers the domestic GST recoverables need, and print
the PASSTHRU_GL entries to paste back.

THE PROBLEM
    Sage books four recoverable-GST accounts under 2A7T. PASSTHRU_GL maps only
    2A7TX04. Where one of the other three nets NON-ZERO on a document,
    classify() refuses the bill rather than book a balance-sheet recoverable to
    an expense head - correctly. Six bills in the Jan-Apr window are held on
    exactly that, which is the "6 need their ledger ids added to PASSTHRU_GL"
    of RUNBOOK section 6.

WHY THESE ARE THE RIGHT LEDGERS
    The 2A7T line is an ordinary distribution line carrying no tax rate of its
    own - FABI503/3103003994 is 4800.00 @5% + 1585.50 @0% + 79.00 on 2A7TX03,
    and 4800 + 1585.50 + 79 + 240 tax is exactly its AMTINVCHC of 6704.50. So it
    is a pass-through the vendor charged, belonging on a balance-sheet
    recoverable, which is precisely how 2A7TX04 is already treated.

    The org has no leaf for the domestic three. Its generic "GST Input" is a
    PARENT (leaf = 0) so nothing can post to it, and every other candidate under
    it is rate-specific ("SGST Input @ 2.50 %") which these lines are not - they
    state no rate. So the faithful mapping is one leaf per Sage account, named
    from Sage's own ACCTDESC, typed LIABILITIES and parented on GST Input,
    exactly matching the shape of the 2A7TX04 ledger already in use.

    partyId / partyType are copied from the existing GST leaves: the org id and
    "SELF". Nothing here is invented.

RUN
    .venv/bin/python work/make_passthru_ledgers.py            # dry run
    .venv/bin/python work/make_passthru_ledgers.py --apply
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

GST_INPUT_PARENT = "1202966501109047296"        # "GST Input", leaf = 0

# Sage acct -> the ACCTDESC Sage itself holds, verified against
# idedat_staging.sage_gl_acct.
WANTED = [
    ("2A7TX01", "SGST Recoverable"),
    ("2A7TX02", "CGST Recoverable"),
    ("2A7TX03", "IGST Recoverable"),
]


def existing_by_name(api):
    st, body = api.get("/financeAccount/childrenFinanceAccounts/%s" % GST_INPUT_PARENT)
    d = api.data(body)
    rows = d if isinstance(d, list) else (d or {}).get("content") or []
    return {m.s(r.get("name")).upper(): r for r in rows if isinstance(r, dict)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    api = m.Api()
    have = existing_by_name(api)
    print("existing leaves under GST Input: %d\n" % len(have))

    out = {}
    for acct, name in WANTED:
        hit = have.get(name.upper())
        if hit:
            fid = str(hit.get("financeAccountId") or hit.get("id"))
            print("  %s  already exists -> %s" % (acct, fid))
            out[acct] = (fid, name)
            continue
        if not args.apply:
            print("  %s  WOULD CREATE leaf %r under GST Input" % (acct, name))
            continue
        payload = {
            "name": name, "accountingName": name,
            "financeGroupType": "LIABILITIES",
            "parentFinanceId": GST_INPUT_PARENT,
            "partyId": m.ORG_ID, "partyType": "SELF",
            "leaf": True, "organisationId": m.ORG_ID,
        }
        st, body = api.post("/financeAccount/", payload)
        if not api.ok(st, body):
            print("  %s  CREATE FAILED: %s" % (acct, api.err(body)))
            continue
        d = api.data(body) or {}
        fid = str(d.get("financeAccountId") or d.get("id"))
        print("  %s  created -> %s  %s" % (acct, fid, name))
        out[acct] = (fid, name)

    if out:
        print("\n--- paste into PASSTHRU_GL ---")
        for acct, (fid, name) in sorted(out.items()):
            print('    "%s": ("%s", "%s"),' % (acct, fid, name))


if __name__ == "__main__":
    main()
