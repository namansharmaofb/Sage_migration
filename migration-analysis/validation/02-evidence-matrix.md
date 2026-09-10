# Evidence matrix — why I believe each thing

One row per load-bearing conclusion. The point of this table is not the conclusion; it is
the **shape of the evidence behind it**. A claim resting on one column is weaker than one
resting on four, regardless of how confident it sounds.

Legend: **✔** = this source confirms it · **✘** = this source contradicts it ·
**—** = this source cannot speak to it · **(–)** = source not consulted for this claim.

> **Note on the "Documentation" column.** There is **no Sage 300 vendor documentation
> anywhere in this environment** — only internal handover notes. So a ✔ there means *an
> internal document asserts it*, never *the vendor manual says so*. It is the weakest
> column in this table and is never sufficient alone.

---

## A. Facts about Sage

| # | Claim | Sage DB | SME DB | Backend | Docs | Script | Confidence |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| A1 | Vendor GSTIN lives in `APVEN.BRN`; `TAXNBR`/`IDTAXREGI1` are empty on all 4,752 vendors | ✔ | — | — | ✔ | ✔ | **VERIFIED** |
| A2 | `GLAMF` is the chart of accounts (`ACCTID` → `ACCTDESC`) | ✔ | — | — | ✔ | ✔ | **VERIFIED** |
| A3 | `APIBD` is the AP distribution table; it carries `IDGLACCT` + `AMTDIST`/`AMTDISTHC` and has **no** vendor or invoice column | ✔ | — | — | ✔ | ✔ | **VERIFIED** |
| A4 | Reverse charge is booked *only* as negative lines on `1L8TX14`/`15`/`16`, with header tax zero | ✔ | ✔ | — | ✔ | ✔ | **VERIFIED** |
| A5 | Stock moves at **receipt**, not invoice (`PORCPL.POSTEDTOIC=1` on 32,971/32,971; `POINVL.POSTEDTOIC=0` on 32,586/32,586) | ✔ | — | — | — | — | **VERIFIED** |
| A6 | 88% of POs originate in a requisition — a hop the textbook chain omits | ✔ | — | — | ✘ | — | **VERIFIED** |
| A7 | The fiscal year is Apr–Mar labelled by ending year, so the Jan–Apr window **straddles two fiscal years** | ✔ | — | — | ✔ | ✔ | **VERIFIED** |
| A8 | 6,421 of 18,047 PO invoice headers carry a `*N` receipt-split suffix → 14,602 logical bills | ✔ | ✔ | — | ✔ | ✔ | **VERIFIED** |
| A9 | 1,079 type-12 rows with no distribution are **loan repayments**, not invoices | ✔ | — | — | — | — | **HIGH** |
| A10 | `idedat_staging.sage_ap_obl` matches live `APOBL` exactly for the window (12,781 / ₹1,763,950,260.97) | ✔ | ✔ | — | — | — | **VERIFIED** |
| A11 | `sage_bill_hdr.doc_total` is **source** currency; `ap_amount_hc` is home currency | ✔ | ✔ | — | ✔ | ✔ | **VERIFIED** |
| A12 | CIN/LLPIN does not exist anywhere in Sage | ✔ | — | — | ✔ | ✔ | **VERIFIED** *(value-shape scan over all 48 char columns of `APVEN` returned zero CIN-shaped values)* |

## B. Facts about SMEAssist

| # | Claim | Sage DB | SME DB | Backend | Docs | Script | Confidence |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| B1 | The server **recalculates and unconditionally overwrites** `billAmount` and `gstAmount`; a mismatch is a warning, never a rejection | — | ✔ | ✔ | — | ✔ | **VERIFIED** |
| B2 | Ids are always generated; **no first-class external-reference column exists** for bill/contact/product | — | ✔ | ✔ | — | ✔ | **VERIFIED** |
| B3 | CGST/SGST vs IGST is decided by **billing-address state**, not the GSTIN | — | ✔ | ✔ | — | — | **VERIFIED** |
| B4 | `@NotNull` on the bill and contact DTOs is inert — no `@Valid` on the controller, no `@Validated` on the service | — | — | ✔ | — | — | **VERIFIED** |
| B5 | The deployed jar does **not** contain the migration patch; every ticket 1–6 defect is live | — | ✔ | ✔ | ✘ | — | **VERIFIED** |
| B6 | The `.patch` file is missing 12 of the 32 reverted files | — | — | ✔ | ✘ | — | **HIGH** |
| B7 | `POST /product` accepts `metaData` and does not persist it (`product.meta` NULL on all 17,450) | — | ✔ | — | ✘ | ✔ | **VERIFIED** |
| B8 | Declared foreign keys: 23 across 285 tables; **none** on `bill`, `product`, `contact`, `voucherEntry`, `financeAccount` | — | ✔ | ✔ | — | — | **VERIFIED** |
| B9 | SMEAssist has **no cost-centre dimension** anywhere in the accounting layer | — | ✔ | ✔ | ✔ | — | **HIGH** |

## C. Facts about the migration as it stands

| # | Claim | Sage DB | SME DB | Backend | Docs | Script | Confidence |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| C1 | Values are right: of 9,371 documents compared, only **3** differ from Sage by ≥ ₹1 | ✔ | ✔ | — | — | ✔ | **VERIFIED** |
| C2 | Completeness is not: **34.2% by count, 13.5% by value** has crossed | ✔ | ✔ | — | — | ✔ | **VERIFIED** |
| C3 | The goods stream is **128 of 14,603 posted (0.9%)**; AP-direct is 82.2% | ✔ | ✔ | — | — | ✔ | **VERIFIED** |
| C4 | `extract.sql` keeps 88.1% of documents but **40.8% of value**; ₹104.43 Cr never enters | ✔ | — | — | ✔ | ✔ | **VERIFIED** |
| C5 | 18,006 unposted = 11,179 reported + 5,302 silently dropped + 1,525 never extracted — **exactly** | ✔ | ✔ | — | — | ✔ | **VERIFIED** |
| C6 | 289 vendor contacts block 10,209 documents; 157 held for a missing CIN/LLPIN | ✔ | ✔ | — | ✔ | ✔ | **VERIFIED** |
| C7 | ₹8.72 Cr of expense sits under the wrong head; **no voucher touches both heads** (misclassification, not double-count) | — | ✔ | ✔ | ✔ | ✔ | **VERIFIED** |
| C8 | The goods path posts source-currency amounts as INR — ₹48.03 Cr armed, **₹38.18 Cr on the next run** | ✔ | ✔ | — | — | ✔ | **VERIFIED** |
| C9 | 531 of 531 discount documents tie once the discount is applied; **0** tie without it | ✔ | — | — | — | ✔ | **VERIFIED** |
| C10 | 304 live bills share 152 statutory document numbers | — | ✔ | ✔ | — | — | **VERIFIED** |
| C11 | The 75 "burned" SKUs are complete, ledgered, unused products locked out by a stale blocklist | — | ✔ | — | ✘ | ✔ | **VERIFIED** |
| C12 | Opening balances, payments, allocations, credit/debit notes, prepayments and journals are **not migrated** (0 rows each) | ✔ | ✔ | — | ✔ | ✔ | **VERIFIED** |
| C13 | The prior PowerShell cohort (1,387 bills + 13,069 legs) is fully soft-deleted — no double-count | — | ✔ | — | ✔ | — | **VERIFIED** |
| C14 | `posted.log` contains no phantom and no duplicate entries | — | ✔ | — | — | ✔ | **VERIFIED** |
| C15 | Every migrated voucher balances; `bill_lines_null_ledger = 0` | — | ✔ | — | — | ✔ | **VERIFIED** |
| C16 | 60 illegal-GST-rate ledgers were created **and cleaned up** — all soft-deleted, all zero balance | — | ✔ | ✔ | — | — | **VERIFIED** |

---

## Where the sources disagree — and how each was settled

| Conflict | Resolution | Why that source wins here |
|---|---|---|
| Docs say placeholder products carry `hsnIsDefault`; the DB says `product.meta` is NULL on all 17,450 | **DB wins** | The loader demonstrably *sends* the flag (`:2083`) and the bill path *keeps* its metadata, so the field is being dropped server-side. Observed state beats stated intent. |
| Backend agent: "~45 illegal GST rates **in the live** chart of accounts"; my query: all 60 soft-deleted, zero balance | **DB wins — claim corrected** | The code reading was right (the mechanism is unguarded); the data claim was stale. Severity drops from *books corrupted* to *guard rail missing*. |
| Target agent: "43 foreign-currency invoices **stamped INR/DOMESTIC**"; script auditor + me: those 43 are **excluded** by `extract.sql` | **Excluded — claim corrected** | Two independent funnels put them in the exclusion column, and a provenance join returns INR-only bills in the target. Same theme, two different problems. |
| README: mirror is "missing 469 vendors"; measurement: mirror is *set-identical* to the 357 goods vendors, missing 377 of 412 AP-direct | **Measurement wins** | "469" could not be reproduced against any baseline. The structural fact — the mirror covers one population — is the useful statement. |
| `reconcile.py`: 375 amount mismatches; financial agent: only 3 differ by ≥ ₹1 | **Both right, different tests** | `reconcile.py` compares exactly with no tolerance for Sage's per-authority tax truncation. The 372 difference is method, not data. |
| `reconcile.py` denominator 12,781 vs `value_recon.py` 11,256 | **Both right, different denominators** | The 1,525 gap is exactly the extraction funnel. Neither is wrong; neither states which it is using. |
| Extract comment: "154 documents carry leading/trailing whitespace"; flow analyst: 8 in-window, 386 all-time | **Unresolved** | Trailing whitespace is undetectable in a `char` column, so neither figure can be proven. Recorded as unproven. |
| Ticket 2: "a rejected product create burns the SKU"; backend agent: `@Transactional` rolls it back, zero orphans; my check: all 75 exist and are complete | **Ticket 2 is not what happened here** | The 75 were blocklisted by a *lookup* failure, since fixed. The ticket describes a real hazard that did not cause this. |

---

## Caveat on the absence claims

A12 asserts data is **absent from Sage**, and this project has been wrong about exactly
that before — an earlier pass declared *"vendor GSTIN is not in the database"* when it was
sitting in `APVEN.BRN`, because the search looked for a column *name* rather than a value
*shape*.

**A12 has therefore been settled by the method that catches that mistake**: a value-shape
scan for the CIN pattern (21 chars, `LNNNNNAANNNNAAANNNNNN`) and the LLPIN pattern across
all 48 character columns of `APVEN`, plus `APVENO` and `APVENC`. **Zero CIN-shaped values.**
The 10 LLPIN-shaped hits are invoice numbers in `IDINVCHI` (`GST-2367`, `INV-1064`, …).
Upgraded to **VERIFIED**. See `risks/02 §3b`.

**The second absence claim has now had the same treatment.** The handover states that
vendor bank details exist nowhere in Sage and that this blocks any payment flow. I scanned
for the 11-character IFSC shape (`AAAA0NNNNNN`) across every character column of `APVEN`
(48), `APBTA` (40,463 rows), `BKACCT`, `APVCM` and `APTCR` (312,560 rows, 43 columns):

| table | rows | IFSC-shaped values |
|---|---:|---|
| `APVEN` | 4,752 | **none** |
| `APBTA` | 40,463 | **none** |
| `BKACCT` | 74 | **none** |
| `APVCM` | 2,670 | **none** |
| `APTCR` | 312,560 | 1, in `NAMERMIT` — a remittance *name* column, a false positive |

**Vendor bank details are genuinely absent from Sage.** Upgraded to DATABASE VERIFIED,
confidence VERIFIED. This is not a mapping gap that better analysis can close: any payment
flow needs that data sourced from outside Sage entirely.
