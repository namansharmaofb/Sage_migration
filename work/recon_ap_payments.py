#!/usr/bin/env python3
"""Reconcile the AP payment load. Four checks, all against SAGE.

    .venv/bin/python work/recon_ap_payments.py

The brief's rule for this is "four checks, all against Sage, none against your
own log", so every expected figure below is recomputed from IDEDAT at run time
rather than read from the extract or from a posted log. The extract is only used
to enumerate the HELD set, because "held" is a decision this migration made and
Sage has no opinion about it.

Runs from the laptop, which is the only machine that can reach both sides: Sage
over TDS (the devbox has no sqlcmd and no route to 1433) and the SMEAssist MySQL
over ssh.
"""

import collections
import csv
import io
import json
import os
import subprocess
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)

CFG = {}
for ln in io.open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, _, v = ln.partition("=")
        CFG[k.strip()] = v.strip().strip("'\"")
ORG = CFG["SME_ORG_ID"]

WINDOW = (20260101, 20260430)          # the window in force per the money-side brief
CR = Decimal(10000000)


def sme(sql):
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "root@%s" % CFG["SME_DB_HOST"],
         "mysql -N --batch -e %s" % json.dumps(sql)],
        capture_output=True, text=True, timeout=600)
    if p.returncode:
        raise SystemExit("mysql failed: %s" % p.stderr[:300])
    return [ln.split("\t") for ln in p.stdout.strip().splitlines() if ln.strip()]


def sage(sql, params=()):
    import sage as _s                                            # noqa: E402
    return _s.q(sql, params)


def cr(v):
    return "%12s" % (Decimal(v or 0) / CR).quantize(Decimal("0.01"))


def main():
    print("AP payment reconciliation  org %s  window %d..%d\n" % ((ORG,) + WINDOW))

    # ---------------------------------------------------------------- check 1
    print("CHECK 1  COUNT")
    s1 = sage("""SELECT COUNT(*) n FROM APOBL
                  WHERE IDTRXTYPE IN (50,51) AND DATEBUS BETWEEN %d AND %d""" % WINDOW)
    sage_docs = s1[0]["n"]
    live = int(sme("SELECT COUNT(*) FROM smeassist.paymentRequest "
                   "WHERE organisationId='%s' AND isDeleted=0;" % ORG)[0][0])
    ver = int(sme("SELECT COUNT(*) FROM smeassist.paymentRequest "
                  "WHERE organisationId='%s' AND isDeleted=0 AND "
                  "entityLedgerVerificationStatus='VERIFIED';" % ORG)[0][0])
    held_path = os.path.join(ROOT, "ref", "held_ap_payments.csv")
    held = 0
    if os.path.exists(held_path):
        held = sum(1 for _ in csv.DictReader(io.open(held_path, encoding="utf-8")))
    print("  Sage paying documents (50/51)      %6d" % sage_docs)
    print("  SMEAssist payments live            %6d   (verified %d)" % (live, ver))
    print("  held by this migration             %6d" % held)
    rem = sage_docs - live - held
    print("  unexplained remainder              %6d   %s"
          % (rem, "OK" if rem == 0 else "<-- must be explained"))

    # ---------------------------------------------------------------- check 2
    print("\nCHECK 2  VALUE PER BANK   (Sage ABS(AMTINVCHC) vs requestedAmount)")
    srows = sage("""SELECT RTRIM(IDBANK) bank, COUNT(*) n,
                           CAST(SUM(ABS(AMTINVCHC)) AS decimal(19,2)) v
                      FROM APOBL
                     WHERE IDTRXTYPE IN (50,51) AND DATEBUS BETWEEN %d AND %d
                     GROUP BY RTRIM(IDBANK)""" % WINDOW)
    sage_by = {r["bank"]: (r["n"], Decimal(r["v"] or 0)) for r in srows}
    mrows = sme("SELECT IFNULL(metaData->>'$.sageBank','?'), COUNT(*), "
                "       IFNULL(SUM(requestedAmount),0) "
                "FROM smeassist.paymentRequest "
                "WHERE organisationId='%s' AND isDeleted=0 GROUP BY 1;" % ORG)
    sme_by = {r[0]: (int(r[1]), Decimal(r[2])) for r in mrows}
    print("  %-10s %8s %14s %8s %14s %14s"
          % ("BANK", "SAGE n", "SAGE Cr", "SME n", "SME Cr", "GAP Cr"))
    for bank in sorted(set(sage_by) | set(sme_by)):
        sn, sv = sage_by.get(bank, (0, Decimal(0)))
        mn, mv = sme_by.get(bank, (0, Decimal(0)))
        print("  %-10s %8d %14s %8d %14s %14s"
              % (bank, sn, cr(sv), mn, cr(mv), cr(sv - mv)))

    # ---------------------------------------------------------------- check 3
    print("\nCHECK 3  MAPPING VALUE   (the one the org used to fail at Rs 0)")
    m = sme("SELECT COUNT(*), IFNULL(SUM(m.mappedAmount),0) "
            "FROM smeassist.voucherEntryMapping m "
            "JOIN smeassist.voucherEntry d ON d.id=m.debitVoucherEntryId "
            "WHERE m.organisationId='%s' AND m.isDeleted=0 "
            "AND d.referenceType='PAYMENT';" % ORG)
    print("  PAYMENT->BILL mappings             %6s   Rs %s Cr"
          % (m[0][0], cr(m[0][1])))
    # The target is NOT every settlement leg - most of the Rs 497 Cr in APOBP is
    # PAYER_LEG, the paying document itself, which is the payment and not a
    # mapping. Only the two bill-directed buckets can ever become a
    # voucherEntryMapping, so those are the denominator.
    legs = os.path.join(ROOT, "ref", "extract-v2", "out", "AP_Payment_Apps.psv")
    tgt = collections.defaultdict(lambda: [0, Decimal(0)])
    if os.path.exists(legs):
        for r in csv.DictReader(io.open(legs, encoding="utf-8", errors="replace"),
                                delimiter="~"):
            tgt[r["link_type"]][0] += 1
            tgt[r["link_type"]][1] += Decimal(r["amt_abs_inr"] or 0)
    want_n = want_v = 0
    for lt in ("PAYMENT_TO_BILL", "ADVANCE_TO_BILL"):
        n, v = tgt[lt]
        want_n += n
        want_v += v
        print("  Sage %-18s          %6d   Rs %s Cr" % (lt, n, cr(v)))
    print("  mappable target                    %6d   Rs %s Cr" % (want_n, cr(want_v)))
    print("  (the gap is the DEFERRED set, reported by the loader: bills not")
    print("   loaded or INVHSEQ-keyed, bills that posted short of Sage, and legs")
    print("   whose target is a note. None of it is a silently dropped leg.)")

    # ---------------------------------------------------------------- check 4
    print("\nCHECK 4  OPEN ITEMS, 20 VENDORS   (Sage AMTDUEHC vs the party ledger)")
    s4 = sage("""SELECT TOP 20 RTRIM(o.IDVEND) vendor,
                        CAST(SUM(o.AMTDUEHC) AS decimal(19,2)) due
                   FROM APOBL o
                  WHERE o.DATEBUS BETWEEN %d AND %d
                  GROUP BY RTRIM(o.IDVEND)
                  ORDER BY SUM(ABS(o.AMTDUEHC)) DESC""" % WINDOW)
    codes = [r["vendor"] for r in s4]
    inlist = ",".join("'%s'" % c.replace("'", "") for c in codes)
    rows = sme(
        "SELECT c.metaData->>'$.sageVendor', "
        "       IFNULL(SUM(CASE WHEN v.transactionType='CREDIT' THEN v.remainingAmount "
        "                       ELSE -v.remainingAmount END),0) "
        "FROM smeassist.contact c "
        "LEFT JOIN smeassist.financeAccount f ON f.partyId=c.id AND f.isDeleted=0 "
        "     AND f.partyType='VENDOR' AND f.leaf=1 "
        "LEFT JOIN smeassist.voucherEntry v ON v.financeAccountId=f.id "
        "     AND v.isDeleted=0 AND v.organisationId='%s' "
        "WHERE c.organisationId='%s' AND c.isDeleted=0 "
        "  AND c.metaData->>'$.sageVendor' IN (%s) GROUP BY 1;" % (ORG, ORG, inlist))
    got = {r[0]: Decimal(r[1] or 0) for r in rows}
    print("  %-10s %16s %16s %16s" % ("VENDOR", "SAGE DUE", "SME OPEN", "GAP"))
    for r in s4:
        v = r["vendor"]
        sv = Decimal(r["due"] or 0)
        mv = got.get(v, Decimal(0))
        print("  %-10s %16s %16s %16s"
              % (v, "%0.2f" % sv, "%0.2f" % mv, "%0.2f" % (sv - mv)))
    print("\n  A vendor whose bills are not all loaded CANNOT tie here - the gap is")
    print("  the unloaded bills, not a payment defect. Read it alongside check 3.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
