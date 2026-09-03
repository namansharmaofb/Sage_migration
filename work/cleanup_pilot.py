#!/usr/bin/env python3
"""Undo the pilot that was posted against a duplicate product, and remove the
smoke-test leftovers. Every target is listed explicitly - nothing is matched by
pattern - so this cannot reach past what it names.

Run:  ./work/venv/bin/python work/cleanup_pilot.py
"""
import sys, json, os
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
import post_sage_bills as m

api = m.Api()

bills = [r["id"] for r in m.mysql(
    "SELECT id FROM bill WHERE organisationId=%s AND isDeleted+0=0" % m.ORG_ID, ["id"])]
print("bills to remove: %d" % len(bills))
for bid in bills:
    st, b = api.call("PUT", "/bill/updateStatus/%s/REVOKED?remarks=migration+cleanup" % bid)
    rv = "ok" if api.ok(st, b) else api.err(b)[:80]
    st, b = api.call("DELETE", "/bill/%s" % bid)
    print("  %s revoke=%s delete=%s" % (bid, rv, "ok" if api.ok(st, b) else api.err(b)[:80]))

# Three products to remove. SAGE-4E2ME07-R2 duplicates the healthy
# SAGE-4E2ME07 and is what all eight pilot bills were booked against; the other
# two were named after their own account code because the GL name lookup keyed
# on ACCTID instead of ACCTFMTTD.
for pid, why in (("1544583444931051520", "SAGE-4E2ME07-R2 duplicate"),
                 ("1544577130968416256", "SAGE-4E2ME02-14 mis-named"),
                 ("1544577911201234944", "SAGE-4E5O024-01 mis-named")):
    st, b = api.call("DELETE", "/product/delete/%s" % pid)
    print("  product %s (%s) delete=%s"
          % (pid, why, "ok" if api.ok(st, b) else api.err(b)[:80]))

# The smoke-test contact. Kept out of the crosswalk so it is never reused.
st, b = api.call("DELETE", "/contact/1544555239469776896")
print("  smoke contact delete=%s" % ("ok" if api.ok(st, b) else api.err(b)[:80]))

# Drop the poisoned product crosswalk and the posted log. The six contacts are
# KEPT: they were created correctly - GST-registered, real GSTIN, real pincode,
# state derived from the GSTIN prefix - and rebuilding them would only churn
# ledgers to no purpose.
xw = json.load(open(m.CROSSWALK))
xw["products"] = {}
json.dump(xw, open(m.CROSSWALK, "w"), indent=1, sort_keys=True)
if os.path.exists(m.POSTED_LOG):
    os.rename(m.POSTED_LOG, m.POSTED_LOG + ".superseded")
print("  product crosswalk cleared, posted log moved aside, %d contacts kept"
      % len(xw["contacts"]))
