import sys, collections
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
import post_sage_bills as m
book = m.load_po_book()
groups = collections.Counter(); ok = 0
for key, bill in book.items():
    sh, why = m.classify_po(bill)
    if not why:
        ok += 1; continue
    if why.startswith("item "):      g = "item invoiced under >1 unit (177 items)"
    elif why.startswith("unit "):    g = "unit has no platform equivalent: " + why.split("'")[1]
    elif "!= Sage gross" in why:     g = "PO lines do not reconcile to the AP obligation"
    elif why.startswith("stated per-line tax"): g = "stated tax disagrees with document tax"
    elif "not an Indian GST slab" in why: g = "rate is not a legal GST slab"
    elif why.startswith("line") and "qty" in why: g = "qty x unitCost != EXTENDED"
    else: g = why[:52]
    groups[g] += 1
print("\n=== PO stream: %d of %d bills shape cleanly (%.1f%%) ===" % (ok, len(book), 100.0*ok/len(book)))
for g, n in groups.most_common():
    print("   %-52s %d" % (g, n))
