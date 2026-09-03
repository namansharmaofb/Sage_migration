#!/usr/bin/env python
"""Can item products be created as RAW_MATERIAL on this build?

WHY IT MATTERS NOW
    goods-masters is about to mint ~17,129 item products and stock_type_for()
    returns RESOURCE for every one of them. Its docstring says RAW_MATERIAL is
    "currently unusable" because the org owns no catalog categories and
    categories cannot be created. Both halves of that are now false:

      - the org owns 4 catalog categories (Sage Migration Items, Buttons,
        Hanger, Other Packing Materials)
      - POST /catalogCategory exists; the docstring checked /product/category,
        which is a different route

    And devbox-wide, RAW_MATERIAL is 60,986 products across 73 orgs of which
    5,298 carry NO category, with real units and unit prices - while RESOURCE
    is 263 products in total. So the premise wants retesting before 17,000
    products are created under the wrong type, which would be painful to undo.

WHAT IT DOES
    Creates up to three throwaway probe products with an obvious PROBE- SKU and
    DELETES each one immediately, reporting exactly what the server said and
    what it stored. It touches neither the crosswalk nor posted.log, so it is
    safe to run beside a post run.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

SAGE_MIGRATION_CATEGORY = "1543237860483694592"     # "Sage Migration Items"


def probe(api, label, stock_type, category=None):
    sku = "PROBE-STOCKTYPE-%s" % label
    payload = {
        "productName": "PROBE %s - delete me" % label, "skuCode": sku,
        "unit": "MTR", "unitOfMeasurement": "MTR",
        "unitPrice": 2.0,
        "typeOfStock": stock_type,
        "hsnCode": "5407",
        "gstPercentage": 5.0,
        "isManageInventory": False,
        "itemStatus": "ACTIVE", "isBulkUpload": True,
        "metaData": {"migrationSource": "IDEDAT", "probe": "true"},
    }
    if category:
        payload["categoryId"] = category
    st, body = api.post("/product/", payload)
    ok = api.ok(st, body)
    print("\n--- %s: typeOfStock=%s category=%s ---"
          % (label, stock_type, category or "(none)"))
    print("    -> %s  %s" % (st, "OK" if ok else api.err(body)))
    if not ok:
        return
    pid = str((api.data(body) or {}).get("productId"))
    print("    created productId=%s" % pid)
    st2, b2 = api.get("/product/%s" % pid)
    d = api.data(b2) or {}
    if isinstance(d, dict):
        print("    stored: typeOfStock=%s unit=%s unitPrice=%s category=%s"
              % (d.get("typeOfStock"), d.get("unitOfMeasurement"),
                 d.get("unitPrice"), d.get("categoryId")))
    st3, b3 = api.call("DELETE", "/product/delete/%s" % pid)
    print("    deleted: %s" % ("ok" if api.ok(st3, b3) else api.err(b3)[:70]))


def main():
    api = m.Api()
    probe(api, "RAWMAT-NOCAT", "RAW_MATERIAL")
    probe(api, "RAWMAT-CAT", "RAW_MATERIAL", SAGE_MIGRATION_CATEGORY)
    probe(api, "RESOURCE-BASE", "RESOURCE")


if __name__ == "__main__":
    main()
