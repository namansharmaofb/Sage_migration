# The ₹104 crore that never enters the pipeline — now attributed to causes

**Evidence:** DATABASE VERIFIED (three independent measurements agreeing exactly) ·
**Confidence: VERIFIED**

My funnel measurement, the migration-script auditor's §E.7 and the Sage business-flow
analyst's anomaly #1 were produced independently and reconcile to the rupee. Together they
turn "₹104 crore is excluded" into four separate decisions with four different answers.

---

## 1. The funnel

`extract.sql @@bills_header`, measured against live `APOBL`:

| stage | docs | value `AMTINVCHC` (₹) |
|---|---:|---:|
| raw window — `IDTRXTYPE=12`, `SRCEAPPL='AP'`, Jan–Apr 2026 | 12,781 | 1,763,950,260.97 |
| 1. dropped — no `APIBD` distribution row | 1,079 | 295,107,559.76 |
| 2. dropped — non-INR currency | 43 | 254,244,874.08 |
| 3. dropped — `CODETAXGRP` in VAT/NRVAT/NRST/NRVATST | 118 | 5,669,280.00 |
| 4. dropped — purity filter | 285 | 489,297,175.70 |
| **in the book** | **11,256** | **719,631,371.43** |

**88.1% of documents, 40.8% of value.** The four exclusions total **1,525 documents,
₹1,044,318,889.54** — *45% more money than remains in the book*.

The script auditor additionally joined all 1,525 excluded keys against `sage_bill_hdr` and
found **zero overlap**: none of them is picked up by the goods path either. They are
excluded from the migration entirely.

---

## 2. Attributing each exclusion — and they are not the same decision

### Stage 1 — 1,079 documents, ₹295,107,559.76 · **CORRECTLY EXCLUDED**

The business-flow analyst identified what these are: type-12 rows *"with no `APIBD`, no
`APIBH`, posted through a **payment** batch — **loan repayments**."*

They are `IDTRXTYPE=12` (Sage's invoice type) but they are not purchase invoices — they
have no distribution because there is nothing to distribute. `extract.sql`'s comment says
only *"a bill with no distribution lines cannot be shaped; bill create rejects an empty
line list"*, which is true but understates the case. **The right reason to exclude them is
that they are not bills.**

**Verdict: keep excluding. Document the reason properly.** ₹29.5 crore of loan repayment
does belong in the general ledger eventually, but through a journal-voucher path, not as
purchase bills — and journal vouchers are out of scope (§4).

### Stage 2 — 43 documents, ₹254,244,874.08 · **A DECISION IS OWED**

Foreign-currency AP-direct invoices. Excluded solely because `extract.sql` filters
`CODECURN = 'INR'`. These are genuine purchase invoices; ₹25.4 crore of real payables is
simply out of scope because nobody has decided how to carry an exchange rate across.

The AP path already has the right ingredient — `APOBL.AMTINVCHC` is the home-currency
amount and is populated — so the exclusion looks more like caution than necessity.

**Verdict: escalate.** This is the second-largest single exclusion and the one most likely
to be wrong. Contrast with the goods path, which has the *opposite* defect
(`risks/04-…`): AP-direct refuses foreign currency it could handle; goods silently
mishandles foreign currency it should refuse.

### Stage 3 — 118 documents, ₹5,669,280.00 · **CORRECTLY EXCLUDED**

Pre-GST legacy tax groups (VAT/NRVAT/NRST/NRVATST). Historical documents under a tax regime
that no longer exists and that SMEAssist's GST model cannot represent. ₹56.7 lakh, 0.3% of
the window. **Verdict: keep excluding**, and note it in the reconciliation as a named,
accepted category.

### Stage 4 — 285 documents, ₹489,297,175.70 · **REVIEW REQUIRED — the big one**

The purity filter rejects any document whose distribution touches an account outside
`4E*` (expense), `2A7T*` (a tolerated balance-sheet head) and `1L8TX14/15/16` (RCM
payables). What it removes:

| account | docs | amount HC (₹) | account name |
|---|---:|---:|---|
| `1L9O` | 19 | 287,774,142.35 | **Difference Adjustment Control A/C** |
| `1L9E` | 59 | 54,594,152.00 | Bonus Payable |
| `2A1F` | 114 | 27,724,833.00 | Accumulated Depreciation on ROU |
| `2A7S` | 33 | 13,976,376.00 | Advance against DDBK Receivable |
| `1L3L` | 4 | 11,111,112.00 | Axis Bank GECL Term Loan |
| `1L8T` | 44 | 8,612,488.79 | **CGST Payable** *(the non-RCM one)* |
| `2A3L` | 5 | 3,255,323.00 | Axis Bank Term Deposit Account |
| *(6 more)* | ~15 | ~1.7 m | provisions, clearing, cash |

The SQL calls these *"journals wearing an invoice's clothes — fixed assets, TDS deductions,
other liabilities"*, and for depreciation, bonus provisions and term loans that reading is
plainly right.

**Two entries deserve a human's eye before this is signed off:**

- **`1L9O` "Difference Adjustment Control A/C" — 19 documents, ₹28.78 crore.** This single
  account is **27.6% of the entire excluded value** and 59% of the purity filter's take.
  An account named "Difference Adjustment Control" holding ₹28.78 crore across 19 documents
  in four months is either a legitimate suspense mechanism or a problem in Sage's own books.
  Either way, ₹28.78 crore should not leave scope on the strength of an account-prefix test.
- **`1L8T` "CGST Payable" — 44 documents, ₹86.12 lakh.** The filter deliberately admits the
  *RCM* GST payables (`1L8TX14/15/16`) and excludes plain CGST Payable. A purchase invoice
  touching an output-GST account is unusual, but excluding tax accounts by prefix while
  admitting three siblings by name invites a mistake.

**Verdict: the filter's design is sound; two of its exclusions need confirming, not
assuming.**

---

## 3. The point that matters for governance

| exclusion | docs | value (₹ Cr) | verdict |
|---|---:|---:|---|
| loan repayments (no distribution) | 1,079 | 29.51 | correctly excluded — not bills |
| foreign currency | 43 | 25.42 | **decision owed** |
| pre-GST legacy tax groups | 118 | 0.57 | correctly excluded |
| purity filter | 285 | 48.93 | sound in design; **₹29.4 Cr needs confirming** |
| **total** | **1,525** | **104.43** | |

Roughly **₹30 crore is correctly out of scope**, **₹25 crore is awaiting a decision nobody
has been asked to make**, and **₹49 crore rests on an account-prefix rule that is right in
principle and unverified in two specific places**.

**The governance problem is not the filter. It is that none of this is visible outside the
SQL.** Because the exclusion happens in `SQL_HEADERS`, these documents never enter `book`,
never reach `classify()`, and therefore **cannot appear in `work/skipped.json` or
`work/failures-report.json`** — the file `run_all.sh:163` presents as *"anything still
outstanding"*. A ₹104 crore scope decision is currently recorded only as a `WHERE` clause
and four code comments.

---

## 4. Recommendation — REPORTED, NOT IMPLEMENTED

1. **Emit the exclusions as data.** Have the extract write an `excluded` report — document
   key, value, and which predicate rejected it — so the four categories above are counted
   in every run rather than rediscovered by audit.
2. **Put the four decisions to the business separately.** They are not one decision, and
   three of them are cheap: loan repayments (confirm), pre-GST (confirm), foreign currency
   (decide), `1L9O` + `1L8T` (investigate).
3. **Make the reconciliation denominator explicit.** `reconcile.py` counts against the
   12,781 base and `value_recon.py` against the filtered 11,256 — a 1,525-document
   difference that is exactly this funnel (see `contradictions/01-…`). Whichever is chosen,
   the excluded set must appear as a named line, not as silence.
