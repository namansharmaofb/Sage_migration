#!/usr/bin/env python3
"""Build PO payloads offline and assert every invariant. Writes nothing."""
import sys, json, collections
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
from decimal import Decimal as D
import post_sage_bills as m

book = m.load_po_book()
shaped, skips = [], collections.Counter()
for key, bill in book.items():
    sh, why = m.classify_po(bill)
    if why:
        skips[why if len(why) < 50 else why[:50] + " ..."] += 1
    else:
        shaped.append((key, sh))
print("\nshapeable PO bills (ignoring masters): %d of %d" % (len(shaped), len(book)))
print("top holds:")
for w, n in skips.most_common(6):
    print("   %-56s %d" % (w, n))

picks = m.po_pilot_pick(shaped, 10)
print("\npilot selection: %d bills" % len(picks))

class FakeApi:
    dry_run = True
    def get(self, p): return 200, {"data": {"value": "1"}}
    @staticmethod
    def data(b): return b.get("data")
    @staticmethod
    def ok(s, b): return True

stub_contact = {"contactId": "C1", "name": "STUB VENDOR", "ledger": "L1",
                "addressId": "A1", "state": "KARNATAKA", "city": "Bengaluru",
                "pinCode": "560001"}
bad_total = 0
for key, sh, label in picks:
    items = {m.s(l["item"]): {"productId": "P" + m.s(l["item"]), "skuCode": "SKU",
                              "name": m.s(l["item_name"]) or m.s(l["item"]),
                              "ledger": "IL1",
                              "unit": m.UNIT_MAP[m.s(l["unit"]).upper()]}
             for l in sh["lines"]}
    payload = m.build_po_payload(FakeApi(), key, sh, stub_contact, items)
    problems = m.assert_invariants(payload, sh)
    bad_total += len(problems)
    print("\n  %-34s %s" % ("%s|%s" % key, label))
    print("     taxable=%s tax=%s bill=%s lines=%d  %s"
          % (sh["taxable"], sh["tax"], sh["bill_amount"], len(sh["lines"]),
             "OK" if not problems else "INVARIANT FAIL: " + "; ".join(problems)))
    for li in payload["lineItemDtoList"][:3]:
        print("       %-30s qty=%-9s %-4s @ %-12s = %-12s @%s%%"
              % (li["description"][:30], li["quantity"], li["unit"],
                 li["unitPrice"], li["taxableAmount"], li["gstPercentage"]))
print("\ninvariant failures across pilot: %d" % bad_total)
if picks:
    k, sh, _ = picks[0]
    items = {m.s(l["item"]): {"productId": "P", "skuCode": "SKU",
                              "name": m.s(l["item_name"]), "ledger": "IL",
                              "unit": m.UNIT_MAP[m.s(l["unit"]).upper()]}
             for l in sh["lines"]}
    pl = m.build_po_payload(FakeApi(), k, sh, stub_contact, items)
    print("\nsample line item payload:")
    print(json.dumps(pl["lineItemDtoList"][0], indent=1))
