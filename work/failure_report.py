#!/usr/bin/env python
"""Everything that is NOT posted, or posted wrongly, as one JSON.

Read-only. Writes work/failures-report.json.

Sections
  not_posted.ap_direct / not_posted.goods   documents the shaper refuses,
                                            with the reason and the amount
  blockers.contacts_held                    vendors with no contact, by reason
  blockers.burned_skus                      SKUs taken by a row adoption
                                            cannot see
  blockers.items_without_ledger             item products carrying no ledger
  posted_but_wrong.unverified               bills created but never verified
                                            (no voucher -> zero accounting)
"""
import collections, datetime, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P
from decimal import Decimal as D

OUT = os.path.join(P.WORK, "failures-report.json")


def bucket(why):
    w = (why or "").lower()
    if "no contact" in w:            return "no_vendor_contact"
    if "no product" in w:            return "no_product_for_gl"
    if "no item product" in w:       return "no_item_product"
    if "cin" in w or "llpin" in w:   return "vendor_needs_cin"
    if "state" in w:                 return "vendor_state_unresolved"
    if "recoverable" in w:           return "unmapped_balance_sheet_leg"
    if "slab" in w or "rate" in w:   return "tax_rate_not_a_slab"
    if "disagree" in w or "!=" in w: return "amount_does_not_tie"
    if "mixes goods" in w or "heads" in w: return "distribution_shape"
    if "categor" in w:               return "category_filter"
    if "bill type" in w:             return "no_bill_type_for_account"
    return "other"


def money(v):
    try:    return float(P.q2(D(str(v or 0))))
    except Exception: return 0.0


def main():
    rep = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
           "summary": {}, "not_posted": {}, "blockers": {}, "posted_but_wrong": {}}

    posted = set()
    unver = []
    for ln in open(P.POSTED_LOG):
        ln = ln.strip()
        if not ln:
            continue
        posted.add(ln.split("||")[0])
        if ln.endswith("UNVERIFIED"):
            p = ln.split("||")
            unver.append({"doc": p[0], "billId": p[1] if len(p) > 1 else None})

    # ---- AP-direct -------------------------------------------------------
    ap, apc = [], collections.Counter()
    book = P.load_book()
    contacts = json.load(open(P.CROSSWALK)).get("contacts", {})
    for key, bill in book.items():
        tag = "%s|%s" % key
        if tag in posted:
            continue
        sh, why = P.classify(bill)
        if not why and key[0] not in contacts:
            why = "no contact built for vendor"
        if not why:
            continue
        amt = money(sum(D(str(h["gross"] or 0)) for h in bill["headers"]))
        apc[bucket(why)] += 1
        ap.append({"vendor": key[0], "invoice": key[1], "amount": amt,
                   "category": bucket(why), "reason": why[:160]})

    # ---- goods -----------------------------------------------------------
    gd, gdc = [], collections.Counter()
    gbook = P.load_goods_book()
    for key, bill in gbook.items():
        tag = "%s|%s" % key
        if tag in posted:
            continue
        sh, why = P.classify_goods(bill, None)
        if not why and key[0] not in contacts:
            why = "no contact built for vendor"
        if not why:
            continue
        gdc[bucket(why)] += 1
        gd.append({"vendor": key[0], "invoice": key[1],
                   "amount": money(bill["header"]["doc_total"]),
                   "parts": bill.get("parts", 1),
                   "category": bucket(why), "reason": why[:160]})

    rep["not_posted"]["ap_direct"] = sorted(ap, key=lambda r: -r["amount"])
    rep["not_posted"]["goods"] = sorted(gd, key=lambda r: -r["amount"])

    # ---- blockers --------------------------------------------------------
    xw = json.load(open(P.CROSSWALK))
    held = json.load(open(os.path.join(P.WORK, "contacts_held.json"))) \
        if os.path.exists(os.path.join(P.WORK, "contacts_held.json")) else []
    rep["blockers"]["contacts_held"] = held
    rep["blockers"]["contacts_held_by_reason"] = dict(
        collections.Counter(bucket(h.get("reason")) for h in held))
    rep["blockers"]["burned_skus"] = xw.get("burned", {})
    rep["blockers"]["items_without_ledger"] = sorted(
        k for k, v in xw.get("items", {}).items() if not v.get("ledger"))

    rep["posted_but_wrong"]["unverified_no_voucher"] = unver

    rep["summary"] = {
        "documents_posted": len(posted),
        "ap_direct_not_posted": len(ap),
        "ap_direct_value_blocked": round(sum(r["amount"] for r in ap), 2),
        "ap_direct_by_reason": dict(apc),
        "goods_not_posted": len(gd),
        "goods_value_blocked": round(sum(r["amount"] for r in gd), 2),
        "goods_by_reason": dict(gdc),
        "contacts_held": len(held),
        "burned_skus": len(rep["blockers"]["burned_skus"]),
        "items_without_ledger": len(rep["blockers"]["items_without_ledger"]),
        "unverified_no_voucher": len(unver),
    }

    with open(OUT, "w") as fh:
        json.dump(rep, fh, indent=1, sort_keys=False)
    print("\nwrote %s" % OUT)
    print(json.dumps(rep["summary"], indent=1))


if __name__ == "__main__":
    main()
