#!/usr/bin/env python
"""Create a few REAL Sage items through the loader's own _item_payload(), check
what the platform stored, then delete them.

The point is to prove the new typeOfStock/category mapping end to end - right
type, right category, real unit, real unit price, and an ITEM_PURCHASE ledger -
on actual items rather than a synthetic probe, before goods-masters mints
~17,129 of them.

Touches neither the crosswalk nor posted.log, so it is safe beside a post run.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

SQL = """
SELECT item_no_fmt, item_no_raw, descr, category, stock_unit, uom,
       unit_cost, rate_sum, hsn FROM (
  SELECT i.item_no_fmt, COALESCE(i.item_no_raw,'') item_no_raw,
         i.description descr, i.category,
         COALESCE(i.stock_unit,'') stock_unit, COALESCE(g.uom,'') uom,
         g.unit_cost, COALESCE(g.rate_sum,0) rate_sum, COALESCE(g.hsn,'') hsn,
         ROW_NUMBER() OVER (PARTITION BY i.category ORDER BY i.item_no_fmt) rn
    FROM sage_item i
    JOIN sage_goods_line g ON g.item_no = i.item_no_fmt
   WHERE i.category IN ('4THRED','4PACK','4FABRI','2MNMEC','3OTSTN','4CARTN')
     AND COALESCE(g.hsn,'') <> '' AND g.unit_cost > 0) t
 WHERE rn = 1
"""

COLS = ["item", "item_raw", "descr", "category", "stock_um", "um",
        "unitcost", "rate", "hsn"]


def main():
    api = m.Api()
    rows = m.staging_query(" ".join(SQL.split()))
    made = []
    for r in rows:
        if len(r) < len(COLS):
            continue
        it = dict(zip(COLS, r))
        unit = m.platform_unit(it["um"] or it["stock_um"])
        sku = "PROBE-ITEM-%s" % it["item"]
        payload = m._item_payload(it, unit, sku, "PROBE " + it["descr"])
        st, body = api.post("/product/", payload)
        print("\n--- %s  (Sage category %s) ---" % (it["item"], it["category"]))
        print("    %s" % it["descr"][:66])
        print("    sent: type=%-18s cat=%-20s unit=%-5s price=%s hsn=%s"
              % (payload["typeOfStock"], payload.get("categoryId"), unit,
                 payload["unitPrice"], payload["hsnCode"]))
        if not api.ok(st, body):
            print("    -> REFUSED: %s" % api.err(body))
            continue
        pid = str((api.data(body) or {}).get("productId"))
        led = m.item_ledger_for(api, pid, m.ITEM_LEDGER_MAPPING)
        db = m.mysql("SELECT typeOfStock,unitOfMeasurement,unitPrice,hsnCode,"
                     "categoryId,categoryName FROM product WHERE id='%s'" % pid,
                     ["ts", "um", "up", "hsn", "cat", "catname"])
        d = db[0] if db else {}
        print("    -> stored type=%-18s unit=%-5s price=%-10s hsn=%-10s cat=%s"
              % (d.get("ts"), d.get("um"), d.get("up"), d.get("hsn"),
                 d.get("catname")))
        print("       ITEM_PURCHASE ledger = %s" % led)
        made.append(pid)

    print("\n=== cleanup ===")
    for pid in made:
        st, b = api.call("DELETE", "/product/delete/%s" % pid)
        print("  %s delete=%s" % (pid, "ok" if api.ok(st, b) else api.err(b)[:50]))


if __name__ == "__main__":
    main()
