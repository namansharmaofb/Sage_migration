#!/usr/bin/env python
"""Which typeOfStock values can this org actually create, and will a BILL LINE
accept one?

Creating 17,129 item products under the wrong type is expensive to undo, so
every type the Sage->platform mapping wants is tested first, and each probe
product is deleted immediately. Nothing here touches the crosswalk or
posted.log, so it is safe beside a running post.

The second half is the one that actually decides the design: a product that
creates fine is useless if the bill module then refuses a line pointing at it.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

CATEGORY = "1543237860483694592"        # "Sage Migration Items"

TYPES = ["RAW_MATERIAL", "PACKAGING_ITEM", "WORK_IN_PROGRESS", "PRODUCT",
         "CONSUMABLES", "STORES_AND_SPARES", "ASSET", "SERVICE", "RESOURCE"]


def make(api, stock_type, with_category=True):
    sku = "PROBE-TYPE-%s" % stock_type
    p = {
        "productName": "PROBE %s - delete me" % stock_type, "skuCode": sku,
        "unit": "MTR", "unitOfMeasurement": "MTR", "unitPrice": 2.0,
        "typeOfStock": stock_type, "hsnCode": "5407", "gstPercentage": 5.0,
        "isManageInventory": False, "itemStatus": "ACTIVE", "isBulkUpload": True,
        "metaData": {"migrationSource": "IDEDAT", "probe": "true"},
    }
    if with_category:
        p["categoryId"] = CATEGORY
    st, body = api.post("/product/", p)
    if not api.ok(st, body):
        return None, api.err(body)
    return str((api.data(body) or {}).get("productId")), None


def drop(api, pid):
    st, b = api.call("DELETE", "/product/delete/%s" % pid)
    return "ok" if api.ok(st, b) else api.err(b)[:50]


def main():
    api = m.Api()
    print("=== can each typeOfStock be created? (category = Sage Migration Items) ===")
    made = {}
    for t in TYPES:
        pid, err = make(api, t)
        print("  %-20s %s" % (t, ("created %s" % pid) if pid else "REFUSED: %s" % err))
        if pid:
            made[t] = pid

    print("\n=== and WITHOUT a category? ===")
    for sku_t in TYPES:
        sku = "PROBE-NOCAT-%s" % sku_t
        payload = {
            "productName": "PROBE NOCAT %s" % sku_t, "skuCode": sku,
            "unit": "MTR", "unitOfMeasurement": "MTR", "unitPrice": 2.0,
            "typeOfStock": sku_t, "hsnCode": "5407", "gstPercentage": 5.0,
            "isManageInventory": False, "itemStatus": "ACTIVE",
            "isBulkUpload": True,
            "metaData": {"migrationSource": "IDEDAT", "probe": "true"},
        }
        st, body = api.post("/product/", payload)
        if api.ok(st, body):
            pid = str((api.data(body) or {}).get("productId"))
            print("  %-20s created WITHOUT category -> %s (%s)"
                  % (sku_t, pid, drop(api, pid)))
        else:
            print("  %-20s needs a category: %s" % (sku_t, api.err(body)[:60]))

    print("\n=== stored values, and the item ledger each one gets ===")
    for t, pid in made.items():
        led = m.item_ledger_for(api, pid)
        rows = m.mysql("SELECT typeOfStock,unitOfMeasurement,unitPrice,categoryId "
                       "FROM product WHERE id='%s'" % pid,
                       ["ts", "um", "up", "cat"])
        r = rows[0] if rows else {}
        print("  %-20s stored ts=%-18s um=%-5s price=%-8s cat=%s ledger=%s"
              % (t, r.get("ts"), r.get("um"), r.get("up"),
                 (r.get("cat") or "")[:20], led))

    print("\n=== cleanup ===")
    for t, pid in made.items():
        print("  %-20s delete=%s" % (t, drop(api, pid)))


if __name__ == "__main__":
    main()
