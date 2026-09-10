# RISK (HIGH) — the loader hardcodes discount to zero; Sage's discount columns are never read

**Class:** confirmed defect · **Status:** currently *protective* (blocks rather than
corrupts) · **Blocked:** 531 documents, ₹65.38 lakh · **Evidence:** DATABASE VERIFIED +
SCRIPT VERIFIED · **Confidence: VERIFIED**

This one is fully proven: **531 of 531 discount-bearing documents reconcile to the paisa
when the discount is applied, and 0 of 531 reconcile when it is ignored.**

---

## 1. How I found it

`work/failures-report.json` reports 562 goods documents blocked with
`amount_does_not_tie`, ₹1.24 Cr. The reason strings looked arithmetically odd rather than
random:

```
goods total 3900.00 + tax  175.50 != document total 3685.50
goods total 5200.00 + tax  234.00 != document total 4914.00
goods total 7200.00 + tax 1166.40 != document total 7646.40
goods total 6092.00 + tax  986.90 != document total 6469.70
```

Each resolves cleanly under one hypothesis — **a trade discount, with tax charged on the
discounted amount**:

| gross | discount | net | tax on net | doc total | matches? |
|---:|---:|---:|---:|---:|---|
| 3,900.00 | 10% → 390.00 | 3,510.00 | 5% → 175.50 | 3,685.50 | ✔ exact |
| 5,200.00 | 10% → 520.00 | 4,680.00 | 5% → 234.00 | 4,914.00 | ✔ exact |
| 7,200.00 | 10% → 720.00 | 6,480.00 | 18% → 1,166.40 | 7,646.40 | ✔ exact |
| 6,092.00 | 10% → 609.20 | 5,482.80 | 18% → 986.90 | 6,469.70 | ✔ exact |

The loader sums the **gross** line amounts, adds the tax, compares against the **net**
document total, and refuses to post.

---

## 2. The data has the discount. The loader does not read it.

Both extracted staging tables carry it:

```
idedat_staging.sage_goods_line : ... extended, discount, tax_base, tax_amount ...
idedat_staging.sage_bill_hdr   : ... tax_total, doc_total, hdr_discount ...
```

But the loader's column lists omit both — `post_sage_bills.py:1643-1647`:

```python
GOODS_HDR_COLS  = ["invhseq","vendor","invoice_raw","invoice","bill_date",
                   "due_date","doc_total","tax_total","po_number"]          # no hdr_discount
GOODS_LINE_COLS = ["invhseq","invlseq","item","item_raw","descr","um",
                   "stock_um","category","qty","unitcost","ext","rate","hsn"] # no discount
```

And the only occurrences of the word in the whole 3,943-line file are two **hardcoded
zeroes** in the payload builder — `post_sage_bills.py:2578` and `:2618`:

```python
"discount": 0, "itemDiscount": 0,
```

**SCRIPT VERIFIED. Confidence VERIFIED.** The field is extracted, delivered to the loader's
doorstep, and dropped.

---

## 3. Proof across the whole population

```sql
SELECT COUNT(*) docs,
       SUM(ABS(err_ignoring)<0.01) ties_ignoring_discount,
       SUM(ABS(err_using)<0.01)    ties_using_discount,
       ROUND(SUM(doc_total),2)     value_at_stake
FROM (SELECT h.invhseq, h.doc_total,
             SUM(l.extended)             + h.tax_total - h.doc_total AS err_ignoring,
             SUM(l.extended)-SUM(l.discount) + h.tax_total - h.doc_total AS err_using
      FROM sage_bill_hdr h JOIN sage_goods_line l ON l.invhseq=h.invhseq
      WHERE h.hdr_discount <> 0
      GROUP BY h.invhseq, h.tax_total, h.doc_total) t;
```

| docs | ties ignoring discount | **ties using discount** | value at stake (₹) |
|---:|---:|---:|---:|
| 531 | **0** | **531** | 6,538,284.61 |

Per-document, on the eight largest:

| vendor \| invoice | gross lines | discount | net | tax | doc total | err ignoring | **err using** |
|---|---:|---:|---:|---:|---:|---:|---:|
| FABL105 \| 1052583062*1 | 447,321.00 | 96,550.00 | 350,771.00 | 17,538.56 | 368,309.56 | 96,550.00 | **0.00** |
| ACCI054 \| JMB-2526-02408 | 177,070.00 | 956.18 | 176,113.82 | 31,700.49 | 207,814.31 | 956.18 | **0.00** |
| ACCI054 \| JMB-2526-02445 | 169,383.00 | 863.85 | 168,519.15 | 30,333.45 | 198,852.60 | 863.85 | **0.00** |
| FABI435 \| RSP2500794*1 | 166,382.00 | 3.00 | 166,379.00 | 8,318.95 | 174,697.95 | 3.00 | **0.00** |
| ACCI054 \| JMB-2526-02348 | 141,680.00 | 736.74 | 140,943.26 | 25,369.79 | 166,313.05 | 736.74 | **0.00** |
| ACCI054 \| JMB-2526-01897 | 136,500.00 | 1,819.95 | 134,680.05 | 24,242.41 | 158,922.46 | 1,819.95 | **0.00** |
| MNTL583 \| INV0000388/2026 | 116,294.00 | 5,814.70 | 110,479.30 | 19,886.28 | 130,365.58 | 5,814.70 | **0.00** |
| MNTL583 \| INV0000378/2026 | 114,582.00 | 5,729.10 | 108,852.90 | 19,593.52 | 128,446.42 | 5,729.10 | **0.00** |

**The error when ignoring the discount *is* the discount, to the paisa, on every document.**

Scale: **1,987 discounted lines across 531 documents, ₹417,158.28 of discount.** Header and
line discounts agree exactly (₹417,158.28 both ways), so `hdr_discount` is the sum of its
lines — no separate document-level discount to model.

---

## 4. The good news, and it is worth saying clearly

**This defect currently blocks; it does not corrupt.** `assert_invariants` / the shaper's
tie-out check catches every affected document and refuses to post it. That is exactly the
behaviour the project's stated rule demands — *"Nothing is guessed… Cases with no honest
answer are held for a decision and reported."*

I checked the failure mode that would have been far worse: because the tie-out check fires
on **every** discounted document (0 of 531 tie without the discount), **no discounted
document can have slipped through and posted at the wrong value.** The population is
cleanly quarantined.

So the cost is a **531-document, ₹65.38 lakh backlog**, not a silent misstatement. That is
a substantially better position than the mis-headed ledgers in
`risks/01-mis-headed-item-ledgers.md`, where the damage is already written.

---

## 5. Scope note — 531 of 562, not all of it

`amount_does_not_tie` covers 562 documents. 531 are explained here. The remaining ~31 have
another cause and should not be assumed to share it. The largest single item in the whole
`amount_does_not_tie` bucket is **`FABI408 | PX2505828`, ₹49.95 lakh, `parts=73`** — a
73-part receipt-split document, which is the *other* known shape (the `*N` suffix merge)
and belongs with the multi-part analysis, not here.

---

## 6. Proposed fix — REPORTED, NOT IMPLEMENTED

1. Add `discount` to `GOODS_LINE_COLS` and `hdr_discount` to `GOODS_HDR_COLS`, and carry
   them through `load_goods_book()`.
2. Decide **how SMEAssist should represent the discount** before writing any of it. The
   payload already has `discount` and `itemDiscount` fields sitting at zero — establish
   from the backend whether they are (a) applied per line before tax, (b) applied at
   document level, and (c) whether the server *recalculates* the total from them or trusts
   the caller. Feeding a discount into a field the server treats differently would turn a
   blocking defect into a silent one, which is strictly worse than today.
3. Only then re-run the affected 531 documents. They are not in `posted.log`, so they will
   be picked up naturally — no repair pass needed, unlike the mis-headed ledgers.
4. Check the **AP-direct** stream for the same gap. AP-direct lines come from `APIBD` and
   carry `AMTDIST` only, so a discount there would already be netted into the distribution
   amount — but that should be confirmed rather than assumed.
