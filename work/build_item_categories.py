#!/usr/bin/env python
"""Create one catalog category per Sage ICCATG category, and write the id map.

WHY
    goods-masters is about to mint ~17,129 item products. stock_type_for()
    returns RESOURCE for every one, on a docstring premise that is now false:

      - "the org owns no catalog categories at all" - it owns 4
      - "categories cannot be created, /product/category is GET only" - true of
        that route, but POST /catalogCategory exists
      - "RESOURCE is the only value that takes a real unit and a real unit
        price" - measured false: every type stores both

    Measured on this build (work/logs/probe-stocktypes.log): RAW_MATERIAL,
    PACKAGING_ITEM, WORK_IN_PROGRESS, PRODUCT, CONSUMABLES, STORES_AND_SPARES
    and SERVICE all create, all store unit and unitPrice, and all get an item
    ledger - they just need a category. Only ASSET and RESOURCE need none, and
    ASSET can get NO ledger under any mapping, so it cannot carry a bill line.

    Devbox-wide RAW_MATERIAL is 60,986 products across 73 orgs; RESOURCE is 263.

RUN
    .venv/bin/python work/build_item_categories.py            # dry run
    .venv/bin/python work/build_item_categories.py --apply
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

OUT = os.path.join(HERE, "ref", "item_categories.json")

# The org's own catalog group, already carrying its four hand-made categories
# (Sage Migration Items, Buttons, Hanger, Other Packing Materials). Reused so
# everything this migration creates lands beside them rather than in a second
# group of its own. catalogCategory rejects a create without one.
CATALOG_GROUP = "1543236901506416640"       # "Sage Migration"

# catalogCategory also refuses a create with no attribute mapping. The org's
# four existing categories all map the same one - attributeKey SAGE_ITEM,
# label "Sage Item", a LIST of ["DEFAULT"] - which was evidently made for this
# migration on 2 Sep. Reused rather than minting a parallel attribute.
SAGE_ITEM_ATTRIBUTE = "1543237400217550848"

# Sage ICCATG code -> (its own DESC, the platform typeOfStock it should carry).
#
# The class digit carries the meaning and Sage is consistent about it:
#   1  capital items      2  maintenance      3  other items
#   4  garment inputs     5  work in progress 6  finished goods
#   7  sales              8  job work         9  trading
#   SAM sampling          SCR scrap
#
# ASSET is deliberately NOT used: it is the one type that gets no item ledger
# under ITEM_PURCHASE, ITEM_DIRECT_EXPENSE or ITEM_FIXED_ASSET, so a bill line
# pointing at one cannot be booked. The 6 in-scope class-1 items (1COMPU 4,
# 1SOFTW 1, 1FURNI 1) take STORES_AND_SPARES instead. That is a COMPROMISE, not
# a correct classification, and is flagged per product in metaData.
CATEGORY_MAP = {
    # class 1 - capital. See the ASSET note above.
    "1AIRCO": ("Air Conditioners", "STORES_AND_SPARES"),
    "1COMPU": ("Computer", "STORES_AND_SPARES"),
    "1EINST": ("Electrical Installation", "STORES_AND_SPARES"),
    "1EQUIP": ("Office Equipments", "STORES_AND_SPARES"),
    "1FFGHT": ("Fire Fighting Equipments", "STORES_AND_SPARES"),
    "1FURNI": ("Furniture & Fixtures", "STORES_AND_SPARES"),
    "1GENR":  ("Generator", "STORES_AND_SPARES"),
    "1PLMAC": ("Plant & Machinery", "STORES_AND_SPARES"),
    "1SOFTW": ("Software", "SERVICE"),
    "1VEHIC": ("Vehicles", "STORES_AND_SPARES"),
    # class 2 - maintenance
    "2MNCHE": ("Maintenance - Chemicals", "CONSUMABLES"),
    "2MNCOM": ("Maintenance - Computer", "STORES_AND_SPARES"),
    "2MNELE": ("Maintenance - Electricals", "STORES_AND_SPARES"),
    "2MNEMB": ("Maintenance - Embroidery", "STORES_AND_SPARES"),
    "2MNMEC": ("Maintenance - Mechanical", "STORES_AND_SPARES"),
    "2MNNED": ("Maintenance - Needles", "STORES_AND_SPARES"),
    "2MNOTH": ("Maintenance - Others", "STORES_AND_SPARES"),
    "2MNPRN": ("Maintenance - Printing", "STORES_AND_SPARES"),
    "2MNSER": ("Maintenance - Services", "SERVICE"),
    "2MNSOF": ("Maintenance - Software", "SERVICE"),
    # class 3 - other items
    "3OTCHG": ("Other Charges", "SERVICE"),
    "3OTGEN": ("Other Items - General", "CONSUMABLES"),
    "3OTSTN": ("Other Items - Stationery", "CONSUMABLES"),
    # class 4 - garment inputs. Packing-shaped ones are PACKAGING_ITEM; the
    # rest are the materials that go into the garment.
    "4ACCES": ("Other Accessories", "RAW_MATERIAL"),
    "4BUTON": ("Buttons", "RAW_MATERIAL"),
    "4CARTN": ("Carton Box", "PACKAGING_ITEM"),
    "4ELAST": ("Elastics", "RAW_MATERIAL"),
    "4FABRI": ("Fabric", "RAW_MATERIAL"),
    "4FAINT": ("Interlining", "RAW_MATERIAL"),
    "4FASHL": ("Shell Fabric", "RAW_MATERIAL"),
    "4HANGR": ("Hanger", "PACKAGING_ITEM"),
    "4LABEL": ("Labels", "RAW_MATERIAL"),
    "4PACK":  ("Other Packing Materials", "PACKAGING_ITEM"),
    "4POLYB": ("Poly Bag", "PACKAGING_ITEM"),
    "4THRED": ("Threads", "RAW_MATERIAL"),
    "4VELCR": ("Velcros", "RAW_MATERIAL"),
    "4ZIPER": ("Zippers", "RAW_MATERIAL"),
    # class 5/6/7/9 - own production and goods for sale
    "5WPCUT": ("WIP - Cutting", "WORK_IN_PROGRESS"),
    "5WPSEW": ("WIP - Sewing", "WORK_IN_PROGRESS"),
    "6FGOOD": ("Finished Goods", "PRODUCT"),
    "7DOMES": ("Domestic Sales", "PRODUCT"),
    "7IKEA":  ("IKEA", "PRODUCT"),
    "9TRBAG": ("Trading Bags & Pouches", "PRODUCT"),
    "9TRHFR": ("Trading Home Furnishing", "PRODUCT"),
    "9TRSTN": ("Trading Stationery", "PRODUCT"),
    # class 8 - job work is a service performed on our goods
    "8JWEMB": ("Job Work - Embroidery (REV)", "SERVICE"),
    "8JWPCW": ("Job Work - Piece Work", "SERVICE"),
    "8JWPRN": ("Job Work - Printing (REV)", "SERVICE"),
    "8JWPRO": ("Job Work - Processing", "SERVICE"),
    # sampling and scrap
    "SAMEXP": ("Sampling Goods - Export", "PRODUCT"),
    "SAMFAB": ("Sampling Fabric", "RAW_MATERIAL"),
    "SAMLCL": ("Sampling Goods - Local", "PRODUCT"),
    "SCRACC": ("Scrap Accessories", "RAW_MATERIAL"),
    "SCRFAB": ("Scrap Fabric", "RAW_MATERIAL"),
    "SCRMNT": ("Scrap Maintenance & Other Items", "CONSUMABLES"),
}


MODAL_SQL = """
WITH h AS (
  SELECT i.category cat, TRIM(g.hsn) hsn,
         ROW_NUMBER() OVER (PARTITION BY i.category
                            ORDER BY COUNT(*) DESC, TRIM(g.hsn)) rn
    FROM sage_goods_line g JOIN sage_item i ON i.item_no_fmt = g.item_no
   WHERE TRIM(COALESCE(g.hsn,'')) <> ''
   GROUP BY i.category, TRIM(g.hsn)),
u AS (
  SELECT i.category cat, TRIM(i.stock_unit) um,
         ROW_NUMBER() OVER (PARTITION BY i.category
                            ORDER BY COUNT(*) DESC, TRIM(i.stock_unit)) rn
    FROM sage_item i WHERE TRIM(COALESCE(i.stock_unit,'')) <> ''
   GROUP BY i.category, TRIM(i.stock_unit))
SELECT h.cat, h.hsn, COALESCE(u.um,'')
  FROM h LEFT JOIN u ON u.cat = h.cat AND u.rn = 1
 WHERE h.rn = 1
"""


def modal_hsn_unit():
    """-> {category: (hsn, platform unit)} - the MOST COMMON hsn and stock unit
    actually seen on that category's own items.

    catalogCategory requires an hsn and a uom, and a Sage category spans many
    of both, so one has to stand for the set. Taking the modal value keeps it
    real rather than invented, and it lands where you would want it: 4BUTON ->
    96062100 (buttons), 4THRED -> 54011000 (sewing thread), 4ZIPER -> 9607
    (slide fasteners). Two of these reproduce exactly what the org's existing
    hand-made Buttons and Hanger categories already carry, which is a useful
    check on the method.

    This is category metadata only - each PRODUCT still carries its own HSN
    from its own Sage line.
    """
    out = {}
    for row in m.staging_query(" ".join(MODAL_SQL.split())):
        if len(row) >= 3:
            out[row[0]] = (row[1], m.platform_unit(row[2]))
    return out


def existing(api):
    """-> {upper name: id} for the org's catalog categories."""
    out, page = {}, 0
    while True:
        st, body = api.get("/catalogCategory/search?pageSize=200&pageNumber=%d" % page)
        d = api.data(body)
        rows = d if isinstance(d, list) else (d or {}).get("content") or []
        if not rows:
            break
        for r in rows:
            if isinstance(r, dict) and r.get("name"):
                out[m.s(r["name"]).upper()] = str(
                    r.get("catalogCategoryId") or r.get("id"))
        if len(rows) < 200:
            break
        page += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--in-scope-only", action="store_true",
                    help="only the categories that appear on a window item")
    args = ap.parse_args()

    wanted = dict(CATEGORY_MAP)
    if args.in_scope_only:
        rows = m.staging_query("SELECT DISTINCT category FROM sage_item")
        scope = {r[0] for r in rows if r and r[0]}
        wanted = {k: v for k, v in wanted.items() if k in scope}
        print("restricting to %d categories seen on window items" % len(wanted))

    api = m.Api()
    have = existing(api)
    modal = modal_hsn_unit()
    print("catalog categories already in the org: %d" % len(have))
    print("categories with a modal hsn/unit from their own items: %d\n" % len(modal))

    out = {}
    created = reused = failed = 0
    for code, (name, stock) in sorted(wanted.items()):
        hsn, uom = modal.get(code, ("", "OTH"))
        hit = have.get(name.upper())
        if hit:
            out[code] = {"categoryId": hit, "name": name, "typeOfStock": stock}
            reused += 1
            print("  %-8s %-32s %-18s reuse %s" % (code, name, stock, hit))
            continue
        if not args.apply:
            print("  %-8s %-32s %-18s hsn=%-11s uom=%-5s WOULD CREATE"
                  % (code, name, stock, hsn, uom))
            continue
        if not hsn:
            print("  %-8s %-32s %-18s SKIPPED: no hsn on any of its items"
                  % (code, name, stock))
            failed += 1
            continue
        st, body = api.post("/catalogCategory",
                            {"name": name, "hsn": hsn, "uom": uom,
                             "catalogGroupId": CATALOG_GROUP,
                             "attributeMappingDtos": [
                                 {"attributeId": SAGE_ITEM_ATTRIBUTE,
                                  "displayOrder": 1, "isMandatory": False,
                                  "status": "ACTIVE"}],
                             "organisationId": m.ORG_ID})
        if not api.ok(st, body):
            print("  %-8s %-32s %-18s FAILED: %s"
                  % (code, name, stock, api.err(body)[:60]))
            failed += 1
            continue
        d = api.data(body) or {}
        cid = str(d.get("catalogCategoryId") or d.get("id") or d)
        out[code] = {"categoryId": cid, "name": name, "typeOfStock": stock}
        created += 1
        print("  %-8s %-32s %-18s created %s" % (code, name, stock, cid))

    if args.apply and out:
        with open(OUT, "w") as fh:
            json.dump({"_comment": "Sage ICCATG -> platform catalog category id "
                                   "and typeOfStock. Generated by "
                                   "work/build_item_categories.py.",
                       "categories": out}, fh, indent=1, sort_keys=True)
        print("\nwrote %s  (%d created, %d reused, %d failed)"
              % (OUT, created, reused, failed))
    elif not args.apply:
        print("\nDRY RUN - nothing created. Re-run with --apply.")


if __name__ == "__main__":
    main()
