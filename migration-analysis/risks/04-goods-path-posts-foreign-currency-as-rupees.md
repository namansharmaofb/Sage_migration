# RISK (CRITICAL, LATENT) — the goods path reads a source-currency column and declares it INR

**Class:** confirmed defect, **not yet triggered** · **Exposure:** 1,998 documents,
**₹48.03 crore of understatement** · **Evidence:** SCRIPT VERIFIED + DATABASE VERIFIED +
DOCUMENTED · **Confidence: VERIFIED**

The AP-direct path handles currency correctly. The goods path does not, and the goods path
is the one that has barely run yet. **This defect is almost entirely ahead of us, not
behind us — which is the best possible time to find it.**

---

## 1. The two paths are asymmetric

**AP-direct — safe, twice over.** `post_sage_bills.py:333-359` (`SQL_HEADERS`):
```sql
AND RTRIM(b.CODECURN) = 'INR'                 -- filters non-INR out entirely
...
CAST(f.AMTINVCHC AS decimal(18,2)) gross,     -- and reads the HOME-CURRENCY column
CAST(f.AMTTAXHC  AS decimal(18,2)) header_tax
```
Sage's convention, documented in this project's own archived brief: **`…HC` = functional /
INR (always populated), `…TC` = source currency.** `AMTINVCHC` is INR by definition, and
non-INR documents are excluded anyway. Belt and braces.

**Goods — neither guard.** `post_sage_bills.py:1605-1610` (`SQL_GOODS_HDR`):
```sql
SELECT h.invhseq, h.vendor_code, h.inv_number_raw, h.inv_number_base,
       h.inv_date, h.due_date, h.doc_total, h.tax_total, h.po_number
  FROM sage_bill_hdr h
 WHERE h.inv_date BETWEEN %d AND %d      -- no currency filter
```
- **No currency predicate.**
- **`currency` and `fx_rate` are not even selected**, though `sage_bill_hdr` carries both.
- The amount used is **`doc_total`**.

And the payload hardcodes, at `post_sage_bills.py:2689`:
```python
"conversionRate": 1.0, "currencyDto": {"currency": "INR"},
```

---

## 2. `doc_total` is the source-currency amount. Proven two ways.

**By provenance.** The handover's `10_bills.sql` shows `sage_bill_hdr` is built from
`POINVH1` left-joined to `APOBL`, and names the columns in order:
```
... currency, fx_rate, taxbase1_authority1, tax_total, doc_total, hdr_discount,
    on_hold, fisc_year, fisc_period, ap_amount_hc, ap_tax_hc, ap_due_hc, ...
```
`doc_total` ← **`POINVH1.DOCTOTAL`** (no HC/TC suffix — the document's own currency).
`ap_amount_hc` ← **`APOBL.AMTINVCHC`** (home currency). Both are present in the staging
table. The loader reads the wrong one.

**By measurement.** If `doc_total` were home currency, `ap_amount_hc / doc_total` would be
≈1 for every currency. It is not — it is the exchange rate:

```sql
SELECT currency, COUNT(*) docs, ROUND(AVG(fx_rate),4) avg_fx,
       ROUND(SUM(doc_total),2) sum_doc_total, ROUND(SUM(ap_amount_hc),2) sum_ap_hc,
       ROUND(SUM(ap_amount_hc)/NULLIF(SUM(doc_total),0),4) ratio
FROM sage_bill_hdr WHERE inv_date BETWEEN 20260101 AND 20260430 GROUP BY currency;
```

| currency | docs | avg `fx_rate` | Σ `doc_total` | Σ `ap_amount_hc` (₹) | **ratio** |
|---|---:|---:|---:|---:|---:|
| INR | 16,049 | 1.0000 | 1,440,377,343.30 | 1,438,183,151.96 | **0.9985** |
| USD | 1,841 | 90.5064 | 2,688,752.54 | 242,392,995.60 | **90.1507** |
| RMB | 92 | 12.7897 | 10,886,465.28 | 138,912,052.52 | **12.7601** |
| CNY | 65 | 12.7662 | 9,592,335.59 | 122,125,197.73 | **12.7315** |

**The ratio reproduces the exchange rate to three decimal places in every currency.**
`doc_total` is in the vendor's currency. DATABASE VERIFIED, confidence VERIFIED.

---

## 3. What that means, per document and in total

A USD 10,000 invoice would post as **₹10,000** instead of **₹905,064** — understated by
about **99%**.

| currency | docs | would post as (₹) | true INR (₹) | **understatement (₹)** |
|---|---:|---:|---:|---:|
| USD | 1,841 | 2,688,752.54 | 242,392,995.60 | 239,704,243.06 |
| RMB | 92 | 10,886,465.28 | 138,912,052.52 | 128,025,587.24 |
| CNY | 65 | 9,592,335.59 | 122,125,197.73 | 112,532,862.14 |
| **total** | **1,998** | **23,167,553.41** | **503,430,245.85** | **480,262,692.44** |

**₹48.03 crore**, on 1,998 documents — 11.1% of the goods population by count.

---

## 4. It has not happened yet

I checked what actually posted, joining `bill.metadata` provenance back to the Sage header:

```sql
SELECT h.currency, COUNT(DISTINCT b.id) posted_bills FROM smeassist.bill b
JOIN idedat_staging.sage_bill_hdr h
  ON h.inv_number_raw = JSON_UNQUOTE(JSON_EXTRACT(b.metadata,'$.sageDoc'))
 AND h.vendor_code    = JSON_UNQUOTE(JSON_EXTRACT(b.metadata,'$.sageVendor'))
WHERE b.organisationId=<org> AND b.isDeleted=0 GROUP BY h.currency;
```
→ **`INR · 121 bills · posted ₹1,397,612.81 · true ₹1,397,612.92`** — and nothing else.

**Zero foreign-currency goods bills have posted.** Only 128 goods documents have posted at
all (0.9% of the population), and they happen to all be INR. The defect is fully armed and
has not fired.

**It fires the moment the goods stream runs at scale — which is the entire remaining scope
of this migration.**

---

## 4b. It is armed for the **next** run — independent corroboration

Two analyses reached this defect independently (my funnel/ratio test above, and the
migration-script auditor's §E.7). The numbers agree exactly. The auditor added the fact
that turns this from "latent" into "urgent":

```
non-INR goods documents in the window                              1,998
  ... already posted                                                   0
  ... whose vendor ALREADY has a contact in the crosswalk           1,468
      would post as INR      Rs  22,071,112.42
      true home currency     Rs 403,866,335.94
      UNDERSTATEMENT         Rs 381,795,223.52
```

**1,468 of the 1,998 are contact-ready right now.** They are blocked on nothing. The next
`./run_all.sh` reaches `goods-post --all-categories` and posts them — **₹38.18 crore
understated, on the next run.**

And the thing that armed it was a fix: commit `81ba90b` *"Post the international vendors;
stop four sources of false failure"* built the contacts that were the only remaining
obstacle. Before that commit the currency defect was unreachable because the documents
could not post at all. **A correct fix to one problem removed the accidental guard on
another.**

> **This is the single most time-critical finding in the audit.** Everything else in this
> report describes damage already done or work not yet started. This one describes damage
> that happens the next time someone runs the pipeline.

---

## 4c. Adjudication — one agent read this population differently

The target-database analyst reported: *"All 43 in-scope foreign-currency invoices (₹254m,
14.4% of scope value) stamped INR/DOMESTIC."*

**That framing is incorrect, and I am recording the correction rather than the claim.**
Those 43 documents are the **AP-direct** non-INR population, and `extract.sql` **excludes**
them at `SQL_HEADERS:346` (`AND RTRIM(b.CODECURN) = 'INR'`). They were never posted. Both
the script auditor's funnel and mine put them in the exclusion column:

| AP-direct exclusion stage | docs | value (₹) |
|---|---:|---:|
| non-INR currency | **43** | **254,244,874.08** |

I verified the target side directly: joining `bill.metadata` provenance back to the Sage
headers returns **INR only, 121 bills**. There is no foreign-currency bill in the target.

**So: ₹254m of AP-direct foreign-currency documents are *excluded*, not mis-stamped**
(a scope gap, belonging with the exclusion funnel), while **₹48.03 Cr of goods
foreign-currency documents are *armed to be mis-stamped*** (a correctness defect, belonging
here). Same currency theme, two different problems, two different owners.

---

## 5. Why the code looks correct on a first read

`post_sage_bills.py:2334-2336` explains the INR decision, and the reasoning is sound:

> *"INR, not the vendor's local currency: every amount this loader posts is Sage's
> home-currency figure (`doc_total` / `AMTINVCHC`) and the bill declares `currencyDto` INR
> at `conversionRate` 1.0."*

The policy — *post the INR figure, declare INR* — is right. The comment simply **asserts
that `doc_total` and `AMTINVCHC` are the same kind of number.** For AP-direct that is true.
For goods it is false: `doc_total` is `POINVH1.DOCTOTAL`, source currency. One clause of one
comment carries a ₹48 crore assumption, and the correct column (`ap_amount_hc`) is sitting
unused in the same staging row.

This is worth dwelling on as a review lesson: the defect is invisible in the code, invisible
in the comment, and invisible in every current test — because the affected population has
not been posted yet.

---

## 6. Why no existing check would catch it

- **`assert_invariants` / the shaper's tie-out** compares line sums against `doc_total` —
  *both in source currency*. They agree perfectly. The document ties, and is wrong.
- **`reconcile.py`** compares the posted `billAmount` against Sage — but its goods side also
  reads `doc_total`. **Both sides of the reconciliation share the same error**, so it nets
  to zero. A reconciliation that draws both sides from the same mistaken column cannot
  detect that mistake.
- **`value_recon.py`** likewise.
- The org's declared currency is INR and the posted currency is INR, so no currency check
  fires anywhere.

**Every green light in the project would stay green while ₹48 crore went missing.** That is
the same structural weakness identified in `risks/01-mis-headed-item-ledgers.md`: the checks
test internal consistency, not correspondence to the source of truth.

---

## 7. Proposed fix — REPORTED, NOT IMPLEMENTED

1. **Decide the policy explicitly**, then implement it once for both streams:
   *"every posted amount is Sage's home-currency (INR) figure; the bill declares INR at
   rate 1.0."* That policy is already correct — it is only mis-implemented for goods.
2. **Change the goods reader to `ap_amount_hc`** (and `ap_tax_hc` for tax), not `doc_total`
   / `tax_total`. Both are already in `sage_bill_hdr`; no re-extract is needed.
   **Caution:** `ap_amount_hc` comes from the `APOBL` left join and resolves 17,915 of
   18,047 headers per the handover — so ~132 headers have **no** home-currency figure and
   must be held, not defaulted to `doc_total`.
3. **Add a currency guard** to the goods reader that refuses, rather than silently posting,
   any document where `currency <> 'INR'` and no home-currency amount is available.
4. **Add a reconciliation check that reads `ap_amount_hc` on the Sage side**, so the two
   sides of the comparison no longer share a column — otherwise this class of error remains
   undetectable by construction.
5. **Verify the line level too.** `sage_goods_line.extended` / `unit_cost` are presumably
   also source-currency; the line amounts drive `quantity × unitPrice`, which the backend
   *recalculates the bill total from* (see `backend/01-…§3.5`). So the fix must reach the
   lines, not just the header — otherwise the server will recompute the header back to the
   wrong number.
