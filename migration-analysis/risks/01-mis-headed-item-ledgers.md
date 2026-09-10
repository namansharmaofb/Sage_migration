# RISK (HIGH) — ₹8.72 crore of expense is booked under the wrong head, and nothing will revisit it

**Class:** confirmed defect in live target data · **Financial impact:** moves the
gross-profit line · **Already happened:** yes · **Self-healing:** no

**Evidence:** DATABASE VERIFIED (SMEAssist MySQL) + SCRIPT VERIFIED
(`post_sage_bills.py:1934-1966`) + DOCUMENTED (`.claude/agents/sage-recon.md`).
**Confidence: VERIFIED** for the mechanism and the amounts; **HIGH** for the
consequence (see *Honest limits* below).

---

## 1. The mechanism, from the code's own docstring

`post_sage_bills.py:1934` — `item_ledger_for(api, product_id, mapping)`:

> *"Note the endpoint **MINTS A SEPARATE LEDGER per reference type; it does not remap
> an existing one.** Calling it again with a different mapping adds a correctly-headed
> ledger alongside the old one rather than moving it…"*
>
> *"`mapping` is REQUIRED and has no default. It used to default to
> `ITEM_DIRECT_EXPENSE`, and because the AP-direct path never passed anything, every one
> of its products was minted under a Direct Expenses head — 226 ledgers, with not one
> under Indirect. A default here is a silent misclassification, so there is none."*

The defect was found and the code was fixed. **The data it had already written was not.**

---

## 2. The damage, measured

### 2.1 Products carrying two live item ledgers

```sql
SELECT types, COUNT(*) AS products FROM (
  SELECT referenceNumber,
         GROUP_CONCAT(DISTINCT referenceType ORDER BY referenceType SEPARATOR ' + ') AS types
  FROM financeAccountReferenceMapping
  WHERE organisationId=<org> AND isDeleted=0 AND referenceType LIKE 'ITEM%'
  GROUP BY referenceNumber HAVING COUNT(DISTINCT referenceType) > 1) t
GROUP BY types ORDER BY 2 DESC;
```

| mapping pair | products | reading |
|---|---:|---|
| `ITEM_PURCHASE` + `ITEM_SALE` | 403 | **benign** — an item can be bought and sold |
| **`ITEM_DIRECT_EXPENSE` + `ITEM_IN_DIRECT_EXPENSE`** | **132** | **the defect** — mutually exclusive heads |
| `ITEM_DIRECT_EXPENSE` + `ITEM_INCOME` | 10 | questionable, low value |
| `ITEM_DIRECT_EXPENSE` + `ITEM_PURCHASE` | 6 | the *"first ten MATERIAL bills"* defect the docstring names |

### 2.2 Both ledgers carry posted value

| head | ledgers | voucher legs | posted value (₹) |
|---|---:|---:|---:|
| `ITEM_DIRECT_EXPENSE` *(the wrong one)* | 132 | **16,805** | **87,167,664.28** |
| `ITEM_IN_DIRECT_EXPENSE` *(the corrected one)* | 132 | 9,909 | 71,154,879.29 |

### 2.3 The timeline shows the fix landing mid-run

| posted on | head | legs | value (₹) |
|---|---|---:|---:|
| 2026-09-02 | DIRECT | 14 | 877,179.00 |
| 2026-09-03 | DIRECT | 16,791 | 86,290,485.28 |
| 2026-09-03 | INDIRECT | 4,823 | 49,901,589.99 |
| 2026-09-04 | INDIRECT | 5,086 | 21,253,289.30 |

The correction lands on **3 September**. Everything booked before it stayed where it was.

### 2.4 It is misclassification, NOT double-counting

The important disambiguation — I tested whether any single voucher touches both heads:

| shape | vouchers |
|---|---:|
| direct head only | 5,317 |
| indirect head only | 2,640 |
| **both heads on one voucher** | **0** |

**Zero.** No expense is recorded twice. Each voucher went to exactly one head; which head
depends only on **when it was posted**. So the total expense is right and the
**classification is wrong** — a materially better outcome than a double-count, and worth
stating plainly so nobody over-reacts.

---

## 3. Why it will not fix itself

Three independent mechanisms keep it in place:

1. **The endpoint cannot remap.** `/financeAccountReferenceMapping/item/getOrCreate/` mints
   alongside. There is no move operation in the loader's vocabulary.
2. **`posted.log` marks those bills done.** The loader is resumable by design — a document
   in `posted.log` is skipped forever. The 5,317 affected vouchers belong to bills that are
   recorded as complete.
3. **No existing check looks for this.** `.claude/agents/sage-recon.md` states it outright:
   > *"`value_recon.py`'s only ledger check is `null_ledger`, which asks whether a line's
   > finance account is **missing** — never whether it is under the **right head**.
   > `item_master_recon.py` counts 'how many carry a ledger' and `failure_report.py` reports
   > `items_without_ledger`. All three can report clean while every ledger sits under the
   > wrong group."*

   And they do report clean: `failures-report.json` shows `items_without_ledger: 0`,
   `reconcile-report.json` shows `bill_lines_null_ledger: 0` and `unbalanced_vouchers: 0`.
   **Every green light in the project is green while this is wrong.**

---

## 4. Why it matters in accounting terms

Direct vs indirect expense is the split that defines **gross profit**. ₹8.72 crore
classified as Direct Expenses instead of Indirect overstates direct cost and understates
indirect cost by the same amount. Revenue, total expense and net profit are unaffected;
**gross margin and every ratio derived from it are not.**

This compounds with a constraint the handover already flags as an open business decision:
SMEAssist has **no cost-centre dimension** anywhere in its accounting layer, so *"the
finance account is the only dimension"*. When the ledger is the only classification you
have, a ledger under the wrong head is the whole error.

---

## 5. Honest limits of this finding

- I proved the migration produced **two contradictory answers for the same 132 products**,
  and that the earlier answer still holds ₹8.72 Cr. I did **not** independently prove that
  *indirect* is the correct head for all 132 — that judgement rests on the loader's own
  corrected mapping and on `item_ledger_for()`'s docstring. If the September 3 correction
  was itself wrong, the error points the other way but is the same size.
- I did not check whether the 10 `ITEM_DIRECT_EXPENSE + ITEM_INCOME` and 6
  `+ ITEM_PURCHASE` products carry the same pattern; they are far smaller and were not
  measured.
- Whether the orphaned ledgers are *visible* in the SMEAssist chart-of-accounts UI was not
  tested. If they are, the chart of accounts has 132 duplicate expense heads in it.

---

## 6. Proposed fix — REPORTED, NOT IMPLEMENTED

1. **Decide the correct head per product** for the 132, with finance. Do not assume the
   later mapping is right merely because it is later.
2. **Move the value, do not re-post it.** The bills are correct; only the ledger they point
   at is wrong. A reclassification journal against the 132 pairs is far safer than
   revoking and re-posting 5,317 vouchers, and it leaves an audit trail.
3. **Retire the emptied ledgers** so the chart of accounts does not carry 132 duplicate
   heads.
4. **Add a `ledger_head` check** that asserts each item ledger sits under an expected group
   for its mapping, and a `ledger_orphan` check for products with mutually exclusive
   mappings. `master_recon.py --check ledger_head,ledger_orphan` reportedly exists — verify
   it covers this and wire it into `run_all.sh --check`, which currently runs only
   `reconcile.py` and `failure_report.py`.
5. **Generalise the lesson**: any defect fixed in the loader after posting has begun leaves
   a cohort of already-written rows that no resumable run will revisit. That cohort needs
   naming and a repair pass every time — `posted.log` guarantees it will otherwise be
   invisible.
