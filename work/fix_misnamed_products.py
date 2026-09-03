#!/usr/bin/env python3
"""Remove the two products that were named after their own GL account code.

Both were created before the GL-name lookup was fixed to key on ACCTFMTTD
instead of ACCTID, so they carry '4E2ME02-14' where Sage says 'Power Charges,
IDEPL-14'. The product name propagates into the expense ledger name, so the
chart of accounts inherits the same wrong label.

Neither is referenced by any bill line (checked: 0 lines each), so deleting and
letting ensure_products() recreate them is safe and cheaper than fighting the
update endpoint. They are named explicitly - nothing is matched by pattern.
"""
import sys
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
import post_sage_bills as m

api = m.Api()
TARGETS = [("1544577130968416256", "SAGE-4E2ME02-14", "Power Charges, IDEPL-14"),
           ("1544577911201234944", "SAGE-4E5O024-01", "Telephone And Mobile Expenses, IDEPL - 1")]

for pid, sku, should_be in TARGETS:
    st, body = api.call("DELETE", "/product/delete/%s" % pid)
    print("delete %-16s (should have been %r): %s"
          % (sku, should_be, "ok" if api.ok(st, body) else api.err(body)[:120]))

import json
xw = json.load(open(m.CROSSWALK))
for acct in ("4E2ME02-14", "4E5O024-01"):
    xw["products"].pop(acct, None)
json.dump(xw, open(m.CROSSWALK, "w"), indent=1, sort_keys=True)
print("crosswalk entries dropped; re-run 'masters --pilot' to recreate them correctly")
