import sys, collections
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
import post_sage_bills as m
from decimal import Decimal as D

book = m.load_book()
ok, groups = [], collections.Counter()
for key, bill in book.items():
    sh, why = m.classify(bill)
    if not why:
        ok.append((key, sh)); continue
    if why.startswith("reverse charge"):        g = "RCM: derived rate not within 0.05 of a slab"
    elif "disagrees with the document" in why:  g = "stated per-line tax != document tax"
    elif "not an Indian GST slab" in why:       g = "stated rate is not a legal slab"
    elif why.startswith("line") and "implying" in why: g = "stated rate vs stated amount mismatch"
    else:                                        g = why[:52]
    groups[g] += 1

print("\n=== AP-direct, Jan-Apr 2026 ===")
print("shapeable: %d of %d bills (%.1f%%)" % (len(ok), len(book), 100.0*len(ok)/len(book)))
print("\nheld:")
for g, n in groups.most_common():
    print("   %-52s %d" % (g, n))

rcm = sum(1 for _, sh in ok if sh["is_rcm"])
print("\nof the shapeable set:")
print("   reverse charge            %d" % rcm)
print("   forward charge            %d" % (len(ok) - rcm))
print("   carrying a round-off      %d" % sum(1 for _, sh in ok if sh["roundoff"]))
print("   multi-line                %d" % sum(1 for _, sh in ok if len(sh["exp"]) > 1))
print("   rate source stated        %d" % sum(1 for _, sh in ok if sh["rate_source"].startswith("stated")))
print("   rate source derived (RCM) %d" % sum(1 for _, sh in ok if not sh["rate_source"].startswith("stated")))
rates = collections.Counter()
for _, sh in ok:
    for r in sh["rates"].values():
        rates[str(r)] += 1
print("   distinct gstPercentage values sent: %s" % sorted(rates, key=lambda x: D(x)))
print("   value of shapeable set: %s" % sum(sh["taxable"] + sh["tax"] for _, sh in ok))
print("   distinct vendors needing a contact: %d" % len({k[0] for k, _ in ok}))
print("   distinct GL accounts needing a product: %d"
      % len({m.s(l["gl"]) for _, sh in ok for l in sh["exp"]}))
