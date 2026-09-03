#!/usr/bin/env python
"""Can an ASSET product get an item ledger?

ASSET was the one type in the sweep that came back with ledger=None under
ITEM_DIRECT_EXPENSE, and a bill line needs a ledger. Only 6 of the 17,129
in-scope items are class 1 (1COMPU 4, 1SOFTW 1, 1FURNI 1), so this decides a
small tail - but if ASSET can never carry a ledger those 6 have to be typed
something else rather than silently dropped.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

CATEGORY = "1543237860483694592"


def main():
    api = m.Api()
    payload = {
        "productName": "PROBE ASSET LEDGER - delete me",
        "skuCode": "PROBE-ASSET-LEDGER",
        "unit": "NOS", "unitOfMeasurement": "NOS", "unitPrice": 100.0,
        "typeOfStock": "ASSET", "hsnCode": "8471", "gstPercentage": 18.0,
        "isManageInventory": False, "itemStatus": "ACTIVE", "isBulkUpload": True,
        "categoryId": CATEGORY,
        "metaData": {"migrationSource": "IDEDAT", "probe": "true"},
    }
    st, body = api.post("/product/", payload)
    if not api.ok(st, body):
        print("create refused: %s" % api.err(body))
        return
    pid = str((api.data(body) or {}).get("productId"))
    print("created %s" % pid)
    for mapping in ("ITEM_PURCHASE", "ITEM_DIRECT_EXPENSE", "ITEM_FIXED_ASSET"):
        led = m.item_ledger_for(api, pid, mapping)
        print("  %-22s -> %s" % (mapping, led))
    st2, b2 = api.call("DELETE", "/product/delete/%s" % pid)
    print("deleted: %s" % ("ok" if api.ok(st2, b2) else api.err(b2)[:60]))


if __name__ == "__main__":
    main()
