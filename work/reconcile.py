#!/usr/bin/env python
"""Side-by-side reconciliation: SAGE vs SMEASSIST.

Read-only. Run any time; it is the fastest way to see what drifted.

  .venv/bin/python work/reconcile.py            # summary
  .venv/bin/python work/reconcile.py --detail   # + per-document mismatches

Writes work/reconcile-report.json.

Sage side  : idedat_staging.sage_ap_obl (AP-direct) + sage_bill_hdr (goods)
SMEAssist  : the bill / billLineItem / voucherEntry tables
Join key   : vendor + base invoice (the *N suffix stripped), the same key
             post_sage_bills.py posts under.

RCM: on a reverse-charge document Sage's AMTINVCHC is the vendor payable and
AMTTAXHC is zero, while SMEAssist grosses up to taxable + self-assessed GST.
Those are compared on taxableAmount, not billAmount, or every one looks wrong.
"""
import collections, json, os, subprocess, sys
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P

OUT = os.path.join(P.WORK, "reconcile-report.json")
DETAIL = "--detail" in sys.argv


def sme(sql):
    host = P.cfg("SME_DB_HOST")
    p = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "-o", "ConnectTimeout=10", "root@" + host,
                        "mysql smeassist -N --raw --batch -e %s" % json.dumps(sql)],
                       capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise SystemExit("smeassist query failed: %s" % p.stderr[:300])
    return [ln.split("\t") for ln in p.stdout.splitlines() if ln.strip()]


def q2(v):
    try:    return P.q2(D(str(v or 0)))
    except Exception: return D(0)


def main():
    org = P.ORG_ID
    rep = {"summary": {}, "mismatches": [], "missing_in_smeassist": [],
           "extra_in_smeassist": []}

    # ---------- SAGE ----------
    sage = {}
    for r in P._staging_rows(
            "SELECT vendor_code, inv_number_raw, amt_invc_hc, amt_tax_hc "
            "FROM sage_ap_obl WHERE trx_type=12 AND srce_appl='AP' "
            "AND inv_date BETWEEN %d AND %d" % (P.DATE_FROM, P.DATE_TO),
            ["vendor", "invoice", "gross", "tax"]):
        k = (P.s(r["vendor"]), P.base_invoice(r["invoice"]))
        g, t = sage.get(k, (D(0), D(0)))
        sage[k] = (g + q2(r["gross"]), t + q2(r["tax"]))
    ap_n = len(sage)

    for r in P._staging_rows(
            "SELECT vendor_code, inv_number_raw, doc_total, tax_total "
            "FROM sage_bill_hdr WHERE inv_date BETWEEN %d AND %d"
            % (P.DATE_FROM, P.DATE_TO),
            ["vendor", "invoice", "gross", "tax"]):
        k = (P.s(r["vendor"]), P.base_invoice(r["invoice"]))
        g, t = sage.get(k, (D(0), D(0)))
        sage[k] = (g + q2(r["gross"]), t + q2(r["tax"]))

    # ---------- SMEASSIST ----------
    rows = sme("SELECT id, billAmount, taxableAmount, gstAmount, billType, "
               "billStatus FROM bill WHERE organisationId='%s' AND isDeleted=0" % org)

    # Join on billId, NEVER on billNumber. posted.log records the exact id the
    # run created for each (vendor, invoice); billNumber alone is ambiguous -
    # short invoice numbers like "3", "007" or "188" recur across vendors, and
    # keying on them hands one vendor another vendor's amount. That produced
    # 69 fictitious "mismatches over Rs 10,000" the first time this ran.
    bill_of = {}
    for ln in open(P.POSTED_LOG):
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split("||")
        if len(parts) >= 2 and parts[1].isdigit():
            v, _, inv = parts[0].partition("|")
            bill_of[parts[1]] = (P.s(v), P.base_invoice(inv))

    by_id = {}
    active = 0
    for bid, amt, txb, gst, bt, st in rows:
        if st != "ACTIVE":
            continue
        active += 1
        by_id[bid] = (q2(amt), q2(txb), q2(gst), bt)

    smeb = {}
    for bid, key in bill_of.items():
        if bid in by_id:
            smeb[key] = by_id[bid]

    # ---------- COMPARE ----------
    matched = mism = rcmok = 0
    for k, (amt, txb, gst, bt) in smeb.items():
        if k not in sage:
            rep["extra_in_smeassist"].append({"doc": "|".join(k), "amount": float(amt)})
            continue
        sg, stx = sage[k]
        if amt == sg:
            matched += 1
        elif txb == sg:            # RCM: Sage gross IS our taxable
            rcmok += 1
        else:
            mism += 1
            if len(rep["mismatches"]) < 400:
                rep["mismatches"].append(
                    {"doc": "|".join(k), "sage": float(sg),
                     "smeassist": float(amt), "delta": float(q2(amt - sg)),
                     "billType": bt})

    posted_tags = {ln.split("||")[0] for ln in open(P.POSTED_LOG) if ln.strip()}
    missing = [k for k in sage if "|".join(k) not in posted_tags]

    # ---------- OTHER SIDES ----------
    def one(sql):
        r = sme(sql)
        return int(r[0][0]) if r and r[0] else 0

    counts = {
        "sage_documents_in_window": len(sage),
        "sage_ap_direct": ap_n,
        "sage_goods": len(sage) - ap_n,
        "smeassist_bills_active": active,
        "smeassist_contacts": one(
            "SELECT COUNT(*) FROM contact WHERE organisationId='%s'" % org),
        "smeassist_sage_products": one(
            "SELECT COUNT(*) FROM product WHERE organisationId='%s' "
            "AND skuCode LIKE 'SAGE-%%'" % org),
        "smeassist_item_ledgers": one(
            "SELECT COUNT(*) FROM financeAccountReferenceMapping WHERE "
            "organisationId='%s' AND referenceType LIKE 'ITEM%%'" % org),
        "smeassist_vouchers": one(
            "SELECT COUNT(DISTINCT voucherId) FROM voucherEntry WHERE "
            "organisationId='%s'" % org),
        "unbalanced_vouchers": one(
            "SELECT COUNT(*) FROM (SELECT voucherId, SUM(CASE WHEN "
            "transactionType='DEBIT' THEN amount ELSE -amount END) r FROM "
            "voucherEntry WHERE organisationId='%s' AND isDeleted=0 GROUP BY 1 "
            "HAVING ABS(r)>0.01) t" % org),
        "bill_lines_null_ledger": one(
            "SELECT COUNT(*) FROM billLineItem li JOIN bill b ON b.id=li.billId "
            "WHERE li.organisationId='%s' AND li.isDeleted=0 AND "
            "b.billStatus='ACTIVE' AND li.financeAccountId IS NULL" % org),
    }
    counts.update({
        "compared": len(smeb), "amount_exact": matched,
        "amount_ok_via_rcm_taxable": rcmok, "amount_MISMATCH": mism,
        "in_sage_not_posted": len(missing),
        "in_smeassist_not_in_sage": len(rep["extra_in_smeassist"]),
    })
    rep["summary"] = counts
    rep["missing_in_smeassist"] = ["|".join(k) for k in missing[:2000]]

    with open(OUT, "w") as fh:
        json.dump(rep, fh, indent=1)

    w = max(len(k) for k in counts)
    print("\n" + "=" * (w + 20))
    print("SAGE  vs  SMEASSIST".center(w + 20))
    print("=" * (w + 20))
    for k, v in counts.items():
        flag = ""
        if k in ("amount_MISMATCH", "unbalanced_vouchers",
                 "bill_lines_null_ledger", "in_smeassist_not_in_sage") and v:
            flag = "   <-- INVESTIGATE"
        print("  %-*s %10s%s" % (w, k, format(v, ","), flag))
    print("=" * (w + 20))
    if DETAIL and rep["mismatches"]:
        print("\nper-document mismatches (first 20):")
        for m in rep["mismatches"][:20]:
            print("   %-30s sage=%14.2f  sme=%14.2f  delta=%12.2f"
                  % (m["doc"], m["sage"], m["smeassist"], m["delta"]))
    print("\nwrote %s" % OUT)


if __name__ == "__main__":
    main()
