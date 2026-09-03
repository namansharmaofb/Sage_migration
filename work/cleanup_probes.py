#!/usr/bin/env python
"""Remove what today's probe products left behind.

Deleting a product does NOT remove the ledger and reference mapping that
item_ledger_for() minted for it, so 14 ledgers and 14 mappings were left live.
Ledgers cannot be deleted at all on this build - PUT /financeAccount/{id}/{status}
only offers ACTIVE or DISABLED - so they are DISABLED, and their mappings are
deleted outright.

EXPLICIT IDS ONLY. In particular it must not touch
    1544673475989372928  ELASTIC PROBE_SAGE-ID40827AIR01-ROL Purchase
which is a REAL Sage item that happens to be called "ELASTIC PROBE".

    .venv/bin/python work/cleanup_probes.py            # dry run
    .venv/bin/python work/cleanup_probes.py --apply
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

LEDGERS = [
    ("1544959813200412672", "PROBE RAW_MATERIAL"),
    ("1544959818141302785", "PROBE PACKAGING_ITEM"),
    ("1544959820712411136", "PROBE WORK_IN_PROGRESS"),
    ("1544959823493234688", "PROBE PRODUCT"),
    ("1544959825946902528", "PROBE CONSUMABLES"),
    ("1544959828752891904", "PROBE STORES_AND_SPARES"),
    ("1544959833274351616", "PROBE SERVICE"),
    ("1544959835585413120", "PROBE RESOURCE"),
    ("1544963082903650304", "PROBE JUKI/SIRUBA BOBBIN"),
    ("1544963088045867008", "PROBE PEN"),
    ("1544963092378583040", "PROBE 26*18*12 carton"),
    ("1544963098787479552", "PROBE INNER ROLL"),
    ("1544963102486855680", "PROBE THRD Art# 8753"),
    ("1544963505085513728", "PROBE Grey Blue MESH Pocketing"),
]

MAPPINGS = [
    "1544959813204606976", "1544959818145497088", "1544959820720799744",
    "1544959823497428992", "1544959825955291136", "1544959828757086208",
    "1544959833278545920", "1544959835589607424", "1544963082907844608",
    "1544963088054255616", "1544963092386971648", "1544963098791673856",
    "1544963102491049984", "1544963505089708032",
]

KEEP = "1544673475989372928"        # real item "ELASTIC PROBE" - never touch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    assert KEEP not in [i for i, _ in LEDGERS], "refusing: would touch a real item"
    print("%d ledgers to disable, %d mappings to delete" % (len(LEDGERS), len(MAPPINGS)))
    print("explicitly NOT touching %s (real item ELASTIC PROBE)\n" % KEEP)

    if not args.apply:
        for fid, name in LEDGERS:
            print("  WOULD DISABLE %s  %s" % (fid, name))
        print("\nDRY RUN - nothing changed.")
        return

    api = m.Api()
    for mid in MAPPINGS:
        st, b = api.call("DELETE", "/financeAccountReferenceMapping/%s" % mid)
        print("  mapping %s delete=%s"
              % (mid, "ok" if api.ok(st, b) else api.err(b)[:60]))
    for fid, name in LEDGERS:
        st, b = api.call("PUT", "/financeAccount/%s/DISABLED"
                                "?closureRemarks=probe+cleanup" % fid)
        print("  ledger  %s %-32s disable=%s"
              % (fid, name[:32], "ok" if api.ok(st, b) else api.err(b)[:60]))


if __name__ == "__main__":
    main()
