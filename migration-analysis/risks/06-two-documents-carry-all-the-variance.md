# The whole per-document value variance is two documents, ₹1,457,957.66 — root cause confirmed

**Class:** confirmed defect in live data · **Severity: HIGH** (it is the entire value error in
the migration) · **Already fixed in code, not in data** · **Evidence:** DATABASE VERIFIED +
SCRIPT VERIFIED + DOCUMENTED · **Confidence: VERIFIED**

Across 9,371 documents compared against Sage, **6,668 match to the paise** and 2,693 more
differ by ≤ ₹0.01. **Only 3 differ by ≥ ₹1 — and 2 of those 3 account for ₹1,457,957.66,
which is essentially the whole variance.** That is the signature of a systematic bug with a
small blast radius, not of a broken mapping.

---

## 1. What Sage holds

Sage splits one vendor invoice into one document per goods receipt, suffixed `*1`, `*2`, …
Both affected documents have exactly two parts:

| vendor \| raw document | logical invoice | `doc_total` (₹) |
|---|---|---:|
| FABI470 \| `1173/2025-26*1` | `1173/2025-26` | **805,684.01** |
| FABI470 \| `1173/2025-26*2` | `1173/2025-26` | 10,183.95 |
| FABI470 \| `1177/2025-26*1` | `1177/2025-26` | **652,273.65** |
| FABI470 \| `1177/2025-26*2` | `1177/2025-26` | 327,021.66 |

Correct logical totals: **₹815,867.96** and **₹979,295.31**.

## 2. What SMEAssist holds

| bill | status | `billAmount` (₹) | `taxableAmount` (₹) |
|---|---|---:|---:|
| `1173/2025-26` | ACTIVE | **10,183.95** | 9,699.00 |
| `1177/2025-26` | ACTIVE | **327,021.66** | 311,449.20 |

**In both cases only the `*2` part posted — the *last* one.** The `*1` part, which is the
larger in both documents, is simply absent.

| document | Sage total | posted | **short by** |
|---|---:|---:|---:|
| `1173/2025-26` | 815,867.96 | 10,183.95 | **805,684.01** |
| `1177/2025-26` | 979,295.31 | 327,021.66 | **652,273.65** |
| | | **total** | **1,457,957.66** |

That matches the independently-computed variance exactly.

---

## 3. Root cause — identified, and already fixed in the code

The project's own agent brief names the mechanism:

> *"This is the signature of the `load_goods_book()` bug where `book[k] = {...}` per header row
> kept only the last part; **it accumulates now**, but documents posted before the fix are
> still short and `posted.log` marks them done so no run revisits them."*

The dictionary was assigned per header row instead of accumulated, so each `*N` part
overwrote the previous one and only the last survived. **"Only the last part posted" is
exactly what the data shows, in both documents.** The code now accumulates correctly.

**The data was never repaired.** Both bills are `ACTIVE` in the target at the wrong value,
and both are recorded in `posted.log`, so no resumable run will revisit them.

---

## 4. The heuristic that finds these, slightly corrected

The brief suggests a Sage/SMEAssist ratio near a small integer means whole parts are missing,
and that *"a ratio of ~3.0 means one of three `*N` parts posted."*

The mechanism is right; the arithmetic is looser than stated. These two documents have
**two** parts each, yet their ratios are **80.11** and **2.99** — because the ratio reflects
the *value* proportion of the surviving part, not the number of parts. A document whose parts
are 805,684 and 10,184 gives a ratio of 80, not 2.

**Use the ratio to spot the shape, then count the parts in `sage_bill_hdr` to size the loss.**
Reading the ratio as a part count would have understated `1173/2025-26` by a factor of 40.

---

## 5. Why this pattern is the audit's recurring theme

This is the **fourth** instance of the same shape:

| defect | fixed in code | data left behind |
|---|---|---|
| mis-headed item ledgers | 3 Sep | ₹8.72 Cr still under the wrong head (`risks/01`) |
| burned-SKU blocklist | commit `c06303a` | 75 products still locked out (`risks/05`) |
| `load_goods_book()` part merge | — | **these 2 documents, ₹1.46 M** |
| product lookup | commit `c06303a` | the blocklist above, fossilised |

**A resumable loader guarantees this.** `posted.log` exists so an interrupted run of 11,000
bills continues rather than repeats — which is correct and necessary. The cost is that every
defect fixed *after* posting begins leaves a cohort that no future run will ever revisit.
**Each fix needs a named repair cohort and a repair pass, every time**, or the fix improves
only the future and silently abandons the past.

---

## 6. There is already a tool for this

`work/repost_stranded_parts.py` was written for exactly this case — commit `01c9ef0`,
*"Repost the bills that posted short of Sage"* — and the script audit rates its gating the
strongest in the tree: a full rebuild, `assert_invariants`, and a Sage-total match before it
will revoke and re-post anything.

**It has evidently not been run against these two**, since both are still short in the target
as of the most recent reconciliation. Whether that is deliberate (they may be excluded by its
₹1.00 drift threshold logic, or held for review) or simply outstanding, I could not
determine — I did not run it, and its logs do not say.

---

## 7. Recommendation — REPORTED, NOT IMPLEMENTED

1. **Establish why `repost_stranded_parts.py` has not repaired these two**, before writing
   anything new. The tool exists and is well-gated; the gap is more likely operational than
   technical.
2. ~~Sweep for the whole cohort~~ — **done; the exposure is exactly these two.** See §7b.
3. **Add a permanent multi-part completeness check** to the reconciliation: for every logical
   invoice, assert that the posted amount equals the sum over its parts. This is the check
   that would have caught it on the first run.


---

## 7b. I swept for the wider cohort. There isn't one.

Rather than leave "the exposure may be wider" as a worry, I measured it — every multi-part
logical invoice that has actually posted, Sage total vs posted `billAmount`:

```sql
WITH logical AS (
  SELECT vendor_code, inv_number_base, COUNT(*) parts, SUM(doc_total) sage_total
  FROM idedat_staging.sage_bill_hdr
  WHERE inv_date BETWEEN 20260101 AND 20260430 AND currency='INR'
  GROUP BY vendor_code, inv_number_base HAVING COUNT(*)>1),
posted AS (
  SELECT JSON_UNQUOTE(JSON_EXTRACT(metadata,'$.sageVendor')) v, billNumber bn, billAmount amt
  FROM smeassist.bill WHERE organisationId=<org> AND isDeleted=0)
SELECT COUNT(*) posted_multipart,
       SUM(ABS(l.sage_total - p.amt) < 0.05)  tie,
       SUM(ABS(l.sage_total - p.amt) >= 0.05) mismatched,
       ROUND(SUM(CASE WHEN ABS(l.sage_total-p.amt)>=0.05 THEN l.sage_total-p.amt END),2) shortfall
FROM logical l JOIN posted p ON p.v=l.vendor_code AND p.bn=l.inv_number_base;
```

| posted multi-part documents | tie | mismatched | shortfall (₹) |
|---:|---:|---:|---:|
| **2** | 0 | **2** | **1,457,957.66** |

**Only two multi-part documents have posted at all, and both are these two.** DATABASE
VERIFIED, confidence VERIFIED.

**Why the exposure is bounded:** the goods stream has posted just 128 of 14,603 documents, so
almost none of the 975 multi-part documents in Sage has been attempted yet. The accumulator
bug was caught while the goods stream was still barely running.

**The remaining 973 multi-part documents are not at risk from this defect** — the fix is in
the code and they have not been posted. They are, however, squarely at risk from the currency
defect in `risks/04`, which is *not* fixed. When the goods stream runs at scale, the
multi-part merge should behave; the currency handling will not.

So this finding closes cleanly: **a bounded, historical, two-document repair**, not a
systemic value problem. It is worth fixing precisely because it is small — it is the entire
per-document variance in the migration, and repairing it would take the value reconciliation
to effectively zero.
