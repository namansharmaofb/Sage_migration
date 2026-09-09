#!/usr/bin/env python
"""Re-type the migrated products that were created as RESOURCE.

WHY
    stock_type_for() falls back to RESOURCE when a Sage ICCATG category has no
    catalog category in ref/item_categories.json. 176 live products carried it,
    and RESOURCE is wrong for every one. Two visible consequences:

      - smeassist-fe hides the Mapped Ledgers tab for RESOURCE items
        (Item/components/Details/DetailsCard.js:280), so a mapping that IS
        present cannot be seen or edited from the item page;
      - item search on invoice/bill create shows them as RESOURCE, so finished
        goods cannot be picked as the goods they are.

    The ledgers are NOT the problem and this script never touches one. 175 of
    the 176 carry a live ITEM* mapping, and all 104 finished-goods items sit on
    ITEM_SALE under Sales Accounts, which is the correct head. Only typeOfStock
    and categoryId change here.

WHY NO CATEGORY IS NEEDED
    An earlier version of this script refused every product whose Sage category
    had no catalog category, on the strength of post_sage_bills.py:3659
    ("Category is mandatory in case of Raw Material"). That applies to the
    CREATE path only: the check lives in createOrUpdateProduct
    (ProductServiceImpl.java:233), which POST /product/update does not call.
    updateProduct (:373) runs preCheckBeforeSaving only - SKU uniqueness and a
    4/6/8 digit HSN. So a re-type needs no catalog category and no invented HSN.

    CAVEAT, stated rather than hidden: products re-typed off RESOURCE without a
    categoryId end up in a state the create path would reject (goods-and-service
    type, null category). That is strictly better than RESOURCE for search and
    for ledger visibility, but the catalog category should still be created once
    finance supplies a real HSN per Sage category - Sage carries none
    (ICITEM.TARIFFCODE is empty for all 11,222 6FGOOD and 4,070 7DOMES items),
    and the products carry GOODS_HSN_DEFAULT '9999' as a flagged placeholder.

HOW
    POST /product/update rebuilds the product from the DTO
    (ProductServiceImpl.java:385 productDtoToProduct), so a partial payload
    would blank hsnCode, skuCode and attributes. Every update here is a full
    read-modify-write: GET the product, change what is needed, send it all back.

RUN
    .venv/bin/python work/retype_resource_items.py            # dry run
    .venv/bin/python work/retype_resource_items.py --apply
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "work"))

import post_sage_bills as m                                    # noqa: E402
import sage                                                    # noqa: E402
from build_item_categories import CATEGORY_MAP                 # noqa: E402

CATMAP = os.path.join(HERE, "ref", "item_categories.json")


def cell(row, key):
    """m.mysql() splits raw mysql CLI output, so a SQL NULL arrives as the
    literal string 'NULL' - four characters, which silently passed the 4/6/8
    HSN length test and sent two doomed updates. Normalise it here."""
    v = row.get(key)
    return "" if v is None or v == "NULL" else v.strip()


def plan():
    """-> (fixable, refused). Reads only."""
    known = json.load(open(CATMAP))["categories"]
    q = ("SELECT id, skuCode, productName, hsnCode FROM product "
         "WHERE organisationId=%s AND isDeleted+0=0 AND typeOfStock='RESOURCE'"
         % m.ORG_ID)
    rows = m.mysql(q, ["id", "skuCode", "productName", "hsnCode"])

    by_item = {}
    for r in rows:
        if r["skuCode"].startswith("SAGE-"):
            by_item[r["skuCode"][5:].rsplit("-", 1)[0]] = r
    lst = ",".join("'%s'" % i.replace("'", "''") for i in by_item)
    sage_cat = {x["ITEMNO"].strip(): x["CATEGORY"].strip()
                for x in sage.q("SELECT ITEMNO, CATEGORY FROM ICITEM "
                                "WHERE ITEMNO IN (%s)" % lst)}

    fixable, refused = [], []
    for item, r in sorted(by_item.items()):
        cat = sage_cat.get(item)
        hsn = cell(r, "hsnCode")
        if "PROBE" in r["skuCode"].upper():
            refused.append((r, cat, "leftover probe product - delete, do not re-type"))
        elif cat is None:
            refused.append((r, cat, "no such item in Sage ICITEM"))
        elif cat not in CATEGORY_MAP:
            refused.append((r, cat, "Sage category %s is not in CATEGORY_MAP" % cat))
        elif len(hsn) not in (4, 6, 8):
            refused.append((r, cat, "HSN %r is not 4/6/8 digits" % hsn))
        else:
            fixable.append((r, cat, {
                "typeOfStock": CATEGORY_MAP[cat][1],
                # only when the catalog category actually exists
                "categoryId": (known.get(cat) or {}).get("categoryId"),
            }))
    return fixable, refused


def retype(api, row, target):
    """Full read-modify-write. -> (ok, message)."""
    st, body = api.get("/product/product/%s" % row["id"])
    if not api.ok(st, body):
        return False, "GET failed: %s" % api.err(body)[:70]
    dto = api.data(body)
    if not isinstance(dto, dict):
        return False, "GET returned no product dto"

    before = dto.get("typeOfStock")
    dto["productId"] = row["id"]
    dto["typeOfStock"] = target["typeOfStock"]
    if target["categoryId"]:
        dto["categoryId"] = target["categoryId"]

    st, body = api.post("/product/update", dto)
    if not api.ok(st, body):
        return False, api.err(body)[:70]
    return True, "%s -> %s" % (before, target["typeOfStock"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    fixable, refused = plan()
    print("RESOURCE products: %d fixable, %d refused\n" % (len(fixable), len(refused)))

    if refused:
        print("REFUSED (%d) - reported, not touched:" % len(refused))
        for r, cat, why in refused:
            print("  %-30s %-8s %s" % (r["skuCode"][:30], cat or "-", why))
        print()

    print("FIXABLE (%d):" % len(fixable))
    tally = {}
    for r, cat, tgt in fixable:
        k = (cat, tgt["typeOfStock"], "cat" if tgt["categoryId"] else "NO category")
        tally[k] = tally.get(k, 0) + 1
    for (cat, stock, has), n in sorted(tally.items()):
        print("  %-8s -> %-16s %-12s %3d products" % (cat, stock, has, n))

    if not args.apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply.")
        return

    api = m.Api()
    done = failed = 0
    print()
    for r, cat, tgt in fixable:
        ok, msg = retype(api, r, tgt)
        if ok:
            done += 1
        else:
            failed += 1
            print("  FAIL %-30s %s" % (r["skuCode"][:30], msg))
    print("\nre-typed %d, failed %d, refused %d" % (done, failed, len(refused)))
    if done:
        verify([r["id"] for r, _, _ in fixable])


def verify(ids):
    """Prove the update did not do collateral damage.

    POST /product/update rebuilds from the DTO, so the failure mode to rule out
    is a blanked field, not a wrong type. Also checks the products stayed ACTIVE
    (updateProduct can push one to APPROVAL_PENDING) and kept their ledgers -
    this script must never cost a mapping."""
    lst = ",".join("'%s'" % i for i in ids)
    q = ("SELECT COUNT(*) AS n, SUM(status<>'ACTIVE') AS not_active, "
         "SUM(hsnCode IS NULL OR hsnCode='') AS no_hsn, "
         "SUM(skuCode IS NULL OR skuCode='') AS no_sku, "
         "SUM(unitOfMeasurement IS NULL OR unitOfMeasurement='') AS no_uom, "
         "SUM(typeOfStock='RESOURCE') AS still_resource "
         "FROM product WHERE id IN (%s)" % lst)
    r = m.mysql(q, ["n", "not_active", "no_hsn", "no_sku", "no_uom",
                    "still_resource"])[0]
    led = m.mysql("SELECT COUNT(DISTINCT referenceNumber) AS n FROM "
                  "financeAccountReferenceMapping WHERE isDeleted=0 "
                  "AND referenceType LIKE 'ITEM%%' AND referenceNumber IN (%s)"
                  % lst, ["n"])[0]["n"]
    print("\nVERIFY  %s of %s still RESOURCE | %s not ACTIVE | blanked: "
          "hsn %s sku %s uom %s | %s of %s kept a ledger"
          % (r["still_resource"], r["n"], r["not_active"], r["no_hsn"],
             r["no_sku"], r["no_uom"], led, r["n"]))


if __name__ == "__main__":
    main()
