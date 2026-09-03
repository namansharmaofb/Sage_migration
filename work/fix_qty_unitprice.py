#!/usr/bin/env python3
"""Rewrite the quantity and unit price of already-posted bills.

The bills posted before the PO item detail existed carry the flat shape:
quantity 1 at the whole line amount. Where po_detail() now reconciles the
purchase order to the paise, the same bill can carry the item Sage actually
ordered, at its real quantity and unit cost.

Only presentation changes. taxableAmount, tax, roundOff and billAmount are
untouched - assert_invariants() is re-run on every payload before it is sent,
so a bill whose totals would move is not sent at all.

    ./work/fix_qty_unitprice.py            # preview every affected bill
    ./work/fix_qty_unitprice.py --apply    # PUT /bill/{id}, then re-verify
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P

APPLY = "--apply" in sys.argv

state = P.State()
book  = P.load_book()
cands, _ = P.eligible(book, state, state.xw["contacts"])
shapes = {"%s|%s" % k: (k, sh) for k, sh in cands}

todo = []
for tag, rec in state.posted.items():
    if tag not in shapes:
        continue
    key, sh = shapes[tag]
    if not sh.get("items"):
        continue
    parts = rec.split("||")
    bid = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
    if bid is None:
        # Logged as 'preexisting': a previous run created it but the id never
        # reached the log. The bill is still ours to correct, so resolve it by
        # its number - which is unique per org here - rather than skip it.
        rows = P.mysql("SELECT id FROM bill WHERE organisationId=%s "
                       "AND billNumber='%s' AND isDeleted+0=0"
                       % (P.ORG_ID, key[1].replace("'", "''")))
        if len(rows) != 1:
            print("   %-28s skipped: %d bills match number %r"
                  % (tag, len(rows), key[1]))
            continue
        bid = rows[0][0]
    todo.append((tag, bid, key, sh))

print("\n=== %d posted bill(s) can carry real quantity and unit price ===" % len(todo))
api = P.Api()
ok = fail = 0
for tag, bid, key, sh in todo:
    pay = P.build_payload(api, key, sh, state.xw["contacts"][key[0]],
                          state.xw["products"])
    bad = P.assert_invariants(pay, sh)
    print("\n%-30s billId=%s" % (tag, bid))
    print("   %d distribution line(s) -> %d item line(s), taxable %s unchanged"
          % (len(sh["exp"]), len(pay["lineItemDtoList"]), sh["taxable"]))
    if bad:
        # Never send a payload whose own arithmetic does not hold.
        print("   INVARIANT FAIL, not sent: %s" % "; ".join(bad))
        fail += 1
        continue
    for li in pay["lineItemDtoList"][:4]:
        print("     qty=%-10s unitPrice=%-10s taxable=%-11s %s"
              % (li["quantity"], li["unitPrice"], li["taxableAmount"],
                 li["description"][:36]))
    if len(pay["lineItemDtoList"]) > 4:
        print("     ... %d more" % (len(pay["lineItemDtoList"]) - 4))
    if not APPLY:
        continue
    pay["billId"] = bid
    pay["id"] = bid
    st, body = api.call("PUT", "/bill/%s" % bid, pay)
    if not api.ok(st, body):
        print("   PUT FAILED (bill unchanged): %s" % api.err(body))
        fail += 1
        continue
    vst, vbody = api.call("POST", "/bill/%s/verify" % bid)
    print("   updated%s" % ("  RE-VERIFIED" if api.ok(vst, vbody)
                            else "  VERIFY FAIL: %s" % api.err(vbody)))
    ok += 1

print("\n%s  affected=%d updated=%d failed=%d"
      % ("APPLIED" if APPLY else "PREVIEW ONLY - nothing sent", len(todo), ok, fail))
