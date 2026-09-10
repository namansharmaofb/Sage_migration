# Sage 300 → SMEAssist — Phase 0 Audit: Final Report

**Read-only. Nothing was changed.** Every database statement was a `SELECT`/`SHOW`/`DESCRIBE`.
No migration script or phase was run — not even `dryrun`, which reaches the live API. No
backend code, script, configuration or data was modified; nothing committed, pushed or
deployed. Problems found are reported, not fixed.

Produced 2026-09-05 by a lead analyst coordinating nine specialist agents against four
independent evidence sources.

---

## 1. Executive summary

**The migration's transformation logic is good. Its completeness, its scope governance and
its safety rails are not.**

That distinction is the finding. Of 9,371 documents compared against Sage, **6,668 match to
the paise**, 2,693 more differ by ≤ ₹0.01, and **only 3 differ by ≥ ₹1** — and two of those
three are a single known bug. Every migrated voucher balances. Nothing has been invented on
the target side. The people who built this got the hard part — Indian GST, reverse charge,
rate snapping, receipt splits — substantially right.

What is wrong is everything around that core:

**One defect is armed and will fire on the next run.** The goods path reads
`sage_bill_hdr.doc_total` — the **source-currency** amount — and posts it as INR at rate 1.0.
1,468 foreign-currency documents are now contact-ready and blocked on nothing:
**₹38.18 crore understated the next time `./run_all.sh` reaches `goods-post`.** No existing
check can see it, because the reconciler reads the same wrong column on both sides.

**Only a third of the work has crossed.** 9,376 of 27,409 in-window Sage invoices —
**34.2% by count, 13.5% by value**. The goods stream is at **0.87%**. ₹3.21 billion has not
moved.

**91% of what is blocked traces to 289 vendor contacts**, and 157 of those are held for one
missing CIN/LLPIN file that genuinely does not exist in Sage — proven by value-shape scan,
not by assuming.

**₹104 crore of scope is excluded by a `WHERE` clause** that no report surfaces, and
**₹8.72 crore of expense already sits under the wrong head** in live data, invisible to
every green light the project has.

**And this is a rehearsal.** The target org is `wonderblues`, a devbox stand-in — not Indian
Designs. These numbers measure method fidelity, not a delivered migration. That is the right
way to run this, and it means the findings above are cheap to act on now and expensive later.

---

## 2. Project structure

The starting folder holds **the loader and its scripts only**. Three of the four evidence
sources live elsewhere and had to be located:

| Source | Where | Live? |
|---|---|---|
| Sage 300 | SQL Server 2025, DB `IDEDAT`, 1,110 tables (575 non-empty) | **yes** |
| SMEAssist DB | MySQL 8.0.46 over ssh — `smeassist` (285 tables), `idedat_staging` (21), `smeassist_audit`, `yoda` | **yes** |
| SMEAssist backend | `~/Desktop/PROJECTS/smeassist` — Java/Spring, ~50 Maven modules | on disk |
| Migration scripts | `~/Desktop/sage-pull` — 8,518 lines Python, 417 shell, 3,000 Markdown | here |

Key structural facts: `post_sage_bills.py` is 3,943 lines with 9 phases; `extract.sql` holds
7 `SELECT` blocks; `work/` holds reconcilers, probes and **one-off repair scripts that write
to the live system**. The extract runner `pull.py` referenced by `extract.sql` **is not in
this tree**, so `output/*.csv` cannot currently be regenerated from the folder alone.

Two *different* migration mechanisms exist historically: bulk Excel upload (Aug 2026,
largely blocked) and direct REST posting (current). The backend tickets describe work done
for the first, and that work is **currently reverted on the branch**.

Detail: `project-map/01-project-structure.md`.

---

## 3. Sage database

**Sage 300's own data dictionary is inside `IDEDAT`** — `AUVIEW` (63 rows, table → English
description), `AUFLDS` (1,057 field descriptions), `GLSRCE`, `CSAPP`, `DATADICT`, `CSOPTFD`.
No abbreviation needed expanding by intuition. *(This corrects my own earlier claim that no
Sage documentation existed — I had searched for files, not queried the database.)*

**The database declares zero foreign keys.** Every relationship in this schema is convention,
not constraint.

**The batch-vs-posted question, settled** — getting it backwards would migrate the wrong
population:

| family | what it is | evidence |
|---|---|---|
| `APIBC`/`APIBH`/`APIBD`/`APIBS` | **invoice batch (entry) side** | `AUVIEW` labels them "Invoice Batches / Invoices / Invoice Details / Invoice Payment Schedules"; PK is `(CNTBTCH, CNTITEM)`; carries `INVCSTTS`, `ERRBATCH` work-in-progress columns |
| `APOBL`/`APOBS`/`APOBP`/`APOBLJ` | **posted open-payables ledger** | not in `AUVIEW` at all (derived, not an entry view); PK is `(IDVEND, IDINVC)`; carries `POSTSEQNCE`, `SWPAID`, `AMTDUEHC`, `DATEPAID` |

**A latent fragility follows from that.** `extract.sql` takes headers from `APOBL` (posted)
and lines from `APIBD` (batch), joined on `(CNTBTCH, CNTITEM)`. That only works because this
company has never purged posted AP batches. **If posted batches were ever purged, `APOBL`
would survive and `APIBD` would vanish — the extract would silently return zero lines for
every bill.** `APOBLJ` is the purge-proof alternative.

Other proven facts: vendor GSTIN is in **`APVEN.BRN`** (`TAXNBR`/`IDTAXREGI1` empty on all
4,752 vendors); `GLAMF` is the chart of accounts; reverse charge is booked *only* as negative
lines on `1L8TX14/15/16` with header tax zero; dates are `YYYYMMDD` integers; `…HC` is home
currency and `…TC` source. The fiscal year is **Apr–Mar labelled by ending year**, so the
Jan–Apr 2026 window **straddles two fiscal years** — which is why two counter series exist.

Detail: `sage/01-sage-schema-inventory.md`, `business-flows/01-sage-actual-flow.md`.

---

## 4. SMEAssist database

285 tables, of which **only 39 are in use** for the target org. `organisation` and `employee`
live in a *different schema* (`yoda`), so tenancy is cross-schema.

**Declared foreign keys: 23 across 285 tables — and none on `bill`, `product`, `contact`,
`voucherEntry` or `financeAccount`.** There is no `@ManyToOne` in the backend either.
Referential integrity is entirely a property of the loader. Measured, the loader got it
right: zero orphans on every check.

Currently loaded: 10,769 bills (9,376 ACTIVE, 1,393 REVOKED) worth **₹498,776,137.99**;
36,459 line items; 17,875 products; 537 contacts; 19,560 finance accounts; 66,108 voucher
legs. **100% of the org's bills, contacts and line items are migration output.**

`idedat_staging` is a MySQL mirror of extracted Sage data. For AP obligations in the window
it matches live Sage **exactly** (12,781 documents / ₹1,763,950,260.97). For vendors it is
**scoped to the goods population only** — set-identical to the 357 PO vendors, missing 377
of the 412 AP-direct ones.

Detail: `smeassist/01-target-schema-and-state.md`.

---

## 5. SMEAssist backend

**Amounts are recalculated server-side and silently overwritten, never rejected.**
`BillServiceImpl.reCalculateAndValidateBillCreateDto` recomputes `itemPrice`, `cessAmount`,
`gstAmount` and the bill total, warns to the log and a Google Chat webhook on mismatch, then
overwrites. Line 2195 sets `billAmount` to the server's figure **outside the `if`** —
unconditionally. The caller gets `200 OK` with different numbers.

**Consequence for the whole migration:** Sage's stated totals **cannot be carried across as
values.** They can only be *reproduced*, by choosing per-line quantity, unit price and rate
so the server's arithmetic lands on Sage's number. The loader's author knew this
(`post_sage_bills.py:1463-1474`).

Other decisive behaviours:
- **IDs are always generated**; no create DTO exposes `id`, and **no first-class
  external-reference column exists** for bill, contact or product. Provenance survives only
  in `bill.metadata` JSON, the SKU string, and finance-account *display names* — unindexed,
  non-unique, user-renameable.
- **GST is decided by billing-address state, not the GSTIN.** Proven in live data: 4 bills
  book IGST for a vendor whose GSTIN state matches the org's.
- **`@NotNull` is decorative** — no `@Valid` on the bill/contact controllers, no `@Validated`
  on the services. Required-field enforcement is whatever `if` happens to exist.
- **The deployed jar does not contain the migration patch** (built 26 minutes after the
  revert). Every ticket 1–6 defect is live on the server being posted to.
- **The `.patch` is missing 12 of the 32 reverted files**, including the product-SKU and
  contact-CIN fixes and the entire payment-allocation feature.
- **No server-side GST slab validation** — an unvalidated rate auto-creates a ledger account.

Detail: `backend/01-smeassist-backend-analysis.md`.

---

## 6. Existing migration scripts

`post_sage_bills.py` runs **two entirely separate populations** — AP-direct and goods/PO —
with separate readers, classifiers and phases, sharing one payload builder, one crosswalk and
one `posted.log`.

What the audit found in them:

- **`extract.sql` discards 88% → 41% of value** before any Python runs (§7 below).
- **`failure_report.py` silently drops** documents that shape cleanly, have a contact and
  were never posted — **5,302 documents, ₹28.51 crore** absent from the file `run_all.sh`
  presents as the record of outstanding work.
- **The reconcilers disagree with each other** on denominators (12,781 vs 11,256), on whether
  a `||preexisting` line counts as posted, and on tolerance.
- **`work/cleanup_pilot.py` revokes and deletes every ACTIVE bill in the org**, with no
  `--apply` flag and no dry run. Written for 8 pilot bills; the org now holds 9,376.
- **`work/find_sage.py` rewrites `.env` non-atomically** — a crash loses the SQL password and
  the auth token.
- **`work/po_validate.py` and `work/po_holds.py` are dead code** — they call functions that no
  longer exist.
- The crosswalk is a **4.1 MB gitignored file on one laptop** with a hard-coded path and no
  database fallback; `idedat_staging.crosswalk` holds 0 rows.

Detail: `migration-scripts/01-script-audit.md`, `02-bugs-and-risks.md`.

---

## 7. The actual Sage business flow (proven)

```
Requisition ──88%──▶ Purchase Order ──▶ Receipt ──▶ PO Invoice ──▶ AP ──▶ Settlement
 PORQNH1/L            POPORH1/L         PORCPH1/L    POINVH1/L     APIBH   APOBP
                                            │
                                            └──▶ Inventory (ICHIST)   ◀── stock moves HERE
                                                                          not at invoice
AP ──▶ Journal ──▶ Posted GL
APPJH/D   GLJEH/D    GLPOST
```

Corrections to the textbook hypothesis, all DATABASE VERIFIED:

- **A requisition hop exists and the hypothesis omits it** — 8,549 of 9,690 POs (88%) start
  as a requisition.
- **Stock moves at receipt, not invoice.** `PORCPL.POSTEDTOIC=1` on 32,971/32,971 receipt
  lines; `POINVL.POSTEDTOIC=0` on 32,586/32,586 invoice lines. The invoice posts only a
  value-only IC adjustment.
- **The AP-direct / PO-matched split is `APOBL.SRCEAPPL`** — PO 18,046 (58.5%,
  ₹1,942,654,924.63) vs AP 12,781 (41.5%, ₹1,763,950,260.97). **Trap:** 427 AP-direct
  invoices carry a PO number; that does not make them PO-matched.
- **The reliable link from PO invoice to AP is `DRILLDWNLK`**, not the natural key — the
  natural key resolves 17,915 of 18,047; the drilldown resolves 100%.
- **6,421 of 18,047 headers carry a `*N` receipt-split suffix** → 14,602 logical vendor
  bills, 975 split across 2–36 parts. **The logical invoice is the bill.**

Accounting: `APIBH/APIBD` → `APOBL` → `APPJH/APPJD` → `GLPOST`, debits equal credits
(`SUM(GLPOST.TRANSAMT)=0` on traced documents). Notably **the PO path debits `1L6T*` A/P
Clearing, never an expense account** — 16,531 lines, ₹1,835,493,941.72.

Detail: `business-flows/01-sage-actual-flow.md` (8 full document traces).

---

## 8. The actual SMEAssist flow

A bill create produces `bill` + `billLineItem` + `billEntityMapping`, and on verify a set of
`voucherEntry` legs: Dr expense (per-item ledger), Dr input GST (per-*rate* ledger),
Cr the vendor's party ledger. Every migrated voucher balances; live Dr = Cr = ₹498,772,451.61.

Where the two models **do not correspond** (12 divergences, all DATABASE VERIFIED — the most
consequential):

| Sage | SMEAssist | Consequence |
|---|---|---|
| 252 payables control accounts by category | one leaf ledger **per party** | Sage's payables analysis by category cannot be reproduced |
| one input-tax account per tax *type* | one ledger per tax *rate* | many-to-many; one Sage account maps to N ledgers |
| one GL account = one balance | the same account can become **two** ledgers (Direct + Indirect) | **48 accounts split across two P&L groups, ₹139,577,534.48** |
| RCM: vendor payable **excludes** the tax | `billAmount` **includes** self-assessed tax | `billAmount` is not the payable on 901 bills |
| payments, notes, prepayments, opening balances | **no live rows at all** | see §12 |
| fiscal period stamped on every document | no fiscal-period field | period-close cannot be reproduced |
| one sub-ledger per vendor code | **7 contacts absorb 2–14 Sage vendor codes** | per-vendor balances unrecoverable for those |
| TDS/TCS withheld at source | `txsType`/`txsAmount` NULL on every bill | payables are gross of TDS |

---

## 9. Sage → SMEAssist mapping

Object and field mapping: `mappings/01-object-and-field-mapping.md` — lead-authored and
condensed, because the dedicated mapping agent was stopped before reporting. The object map,
the identity and amount rules and the money-carrying fields are covered; a full DTO-attribute
enumeration is not.

The two structural answers that govern every row of it:

1. **No Sage identifier can be carried across.** IDs are generated; no external-reference
   column exists. Every relationship must be re-established through the crosswalk, which
   makes that 4.1 MB local file a system-of-record, not a cache.
2. **No Sage amount can be carried across.** The server recalculates and overwrites. Amounts
   must be *reproduced* through per-line quantity × unit price × rate.

---

## 10. Validation results — what is actually proven

**Proven correct:**

| Check | Result |
|---|---|
| Per-document value, 9,371 compared | 6,668 exact · 2,693 within ₹0.01 · **3 differ by ≥ ₹1** |
| Net rounding drift | −₹1.38 across 2,693 documents (1,290 up / 1,403 down — not accumulating) |
| Voucher balance | every migrated voucher balances; `unbalanced_vouchers = 0` |
| Bill lines with no ledger | **0** — the handover's CRITICAL silent-data-loss defect is clean |
| Invented data | **0** bills in SMEAssist that Sage does not have; **0** duplicate postings |
| Place of supply | 8,842 of 8,849 taxed documents agree; **7 wrong, ₹20,022.90** |
| Rate snapping | never moved a rate more than 0.10 percentage points |
| Prior generation's data | 1,387 bills + 13,069 legs fully soft-deleted — no double-count |
| `posted.log` integrity | no phantom entries, no duplicate document keys |

**The entire per-document variance is two documents.** `FABI470|1173/2025-26` and
`|1177/2025-26` lost **₹1,457,957.66** because `base_invoice()` consolidated Sage's `*1`/`*2`
parts but only one part posted. A cluster of two, not a scatter — which is what a systematic
bug looks like, and it has a known cause.

Detail: `validation/00-lead-independent-checks.md`, `01-financial-validation.md`,
`02-evidence-matrix.md`.

**Caveat on how strongly to read these.** Adversarial validation was dispatched and stopped
before it reported, so **no agent was specifically tasked with proving these conclusions
wrong.** What the findings did survive: independent re-derivation by the lead (18 checks),
convergence from multiple agents attacking from different directions, and three corrections
that arose from that cross-checking. That is reproduction, not hostile review.

---

## 11. Reconciliation strategy

Specification: `reconciliation/01-reconciliation-strategy.md` — lead-authored and condensed,
for the same reason. Ten checks are specified and sequenced; per-check SQL exists for two.

The governing principle the current suite lacks: **every Sage document must fall into exactly
one of four buckets, and the four must sum to the Sage total** —

`posted-and-correct` · `posted-and-wrong` · `not-posted-but-loadable` · `deliberately-excluded`

Today the tooling conflates the last two and hides the fourth entirely. And two structural
blind spots must be closed regardless of anything else:

- **A reconciliation whose two sides read the same column cannot detect that column being
  wrong.** That is precisely how a ₹48 crore currency error stays invisible.
- **Presence is not correctness.** `items_without_ledger: 0` and `bill_lines_null_ledger: 0`
  were green throughout the period ₹8.72 crore was being misfiled.

---

## 12. Data problems

**In Sage (report only, do not fix):**
- 1,079 type-12 rows with no distribution — **loan repayments**, ₹295,107,559.76
- 132 PO invoices with no AP obligation; 347 receipts never invoiced; 3 invoices with no GL posting
- 18,005 invoices (58%) post on a different date from the document date (−89 to +310 days);
  3,467 show `DATEPAID` before `DATEINVC`
- 2,075 of 4,752 vendors have no GSTIN-shaped `BRN`
- **CIN/LLPIN absent entirely** — proven by value-shape scan across all 48 character columns
- **Vendor bank details absent entirely** — same method; blocks any payment flow

**In SMEAssist:**
- **304 live bills share 152 statutory document numbers**
- **138 orphaned ledgers holding ₹87,293,633** with 16,827 live vouchers still posting to them
- **516 of 537 contacts have duplicate addresses**, with the degraded one marked primary
- **`product.meta` NULL on all 17,875** — provenance dropped; 1,419 placeholder-HSN products
  are unflagged, and 172 at SAC `996719` are indistinguishable from genuine classifications
- 17,126 of 17,886 products have never appeared on a bill line
- 75 "burned" SKUs are healthy products locked out by a stale blocklist

**Not migrated at all:** opening balances (₹535,122,264.66 of creditor balances at
31 Dec 2025), payments and allocations (every bill 100% unpaid against 97.8% paid in Sage),
credit/debit notes (2,066 + 137 in window), prepayments (1,729, ₹1.55 bn), journal vouchers,
chart of accounts, TDS/TCS.

**RCM specifically:** true population 1,135 documents / ₹1,944,885.80; 901 migrated
(Δ −₹184.65, −0.012%); **234 not migrated, ₹412,183.80 — a 21% understatement of the RCM
liability.**

---

## 13. Contradictions

| Conflict | Resolution |
|---|---|
| `reconcile.py` says 18,006 unposted; `failure_report.py` says 11,179 | **Both right.** Gap closes exactly: 11,179 + 5,302 silently dropped + 1,525 never extracted = 18,006 |
| Docs say placeholder products carry `hsnIsDefault`; DB says `product.meta` is NULL on all | **DB wins.** The loader sends it; `POST /product` discards it |
| Backend agent: "~45 illegal GST rates **live** in the chart of accounts" | **Corrected.** All 60 are soft-deleted with **zero balance**; the mechanism is unguarded but the damage was reversed |
| Target agent: "43 foreign-currency invoices stamped INR/DOMESTIC" | **Corrected.** Those 43 are *excluded* by `extract.sql`, never posted |
| README: mirror "missing 469 vendors" | **Unreproducible.** The mirror is set-identical to the 357 goods vendors and missing 377 of 412 AP-direct |
| `reconcile.py` 375 mismatches vs financial agent's 3 | **Both right, different tests** — `reconcile.py` has no tolerance for Sage's per-authority tax truncation |
| Extract comment: "154 whitespace documents"; flow analyst: 8 in-window | **Unresolved** — trailing whitespace is undetectable in a `char` column |
| Ticket 2: "a rejected product create burns the SKU" | **Not what happened.** The 75 were blocklisted by a *lookup* failure, since fixed |
| README: "six field-level defects fixed" from the PowerShell loader | **Materially false as worded.** Only defect 4.1 (blended rate) was a real PowerShell defect, and it *is* genuinely fixed — the stored rate equals Sage's stated rate on all 23,322 forward-charge lines. 4.2/4.3/4.6 were already correct in the PowerShell and are inherited, not fixed. 4.4 is partial. **4.5 is deliberately inverted** — the Python does the opposite of the PowerShell, for good measured reasons. The file annotates seven defects, one mislabelled |
| `failure_report.py` docstring: unverified bills have "zero accounting" | **Overstated.** All 7 `UNVERIFIED` bills have 4–22 voucher legs. But **2 ACTIVE bills genuinely have none** — they arrive through the *`preexisting`* tag, which no report watches |
| My own claim: "no Sage documentation exists" | **Wrong, corrected.** Sage's data dictionary is inside `IDEDAT` (`AUVIEW`, `AUFLDS`, …) |

---

## 14. Risks

### CRITICAL
1. **The goods path posts source-currency amounts as INR.** ₹48.03 Cr exposure; **₹38.18 Cr
   on the next run**; 1,468 documents contact-ready and blocked on nothing. No check detects
   it. → `risks/04`
2. **`work/cleanup_pilot.py` deletes every ACTIVE bill in the org**, no dry run, no guard.
   9,376 bills at risk from one careless invocation. → `migration-scripts/01 §D.1`

### HIGH
3. **₹8.72 Cr of expense under the wrong head**, 132 products, unreversed and invisible to
   every current check. Root cause: the item ledger follows the *document's* `billType` vote
   rather than the line's own GL group. → `risks/01`
4. **The migrated payables cannot be tied to Sage's payables.** ₹497,239,932.81 is neither
   the opening (₹535,122,264.66), the closing (₹429,484,090.46), nor the movement.
5. **289 vendor contacts block 10,209 documents**; the full master needs ~1,516 CIN/LLPINs.
   → `risks/02`
6. **₹104.43 Cr excluded in SQL**, invisible to every report. → `sage/00`
7. **304 live bills share 152 statutory document numbers.** Two causes, both fixable:
   98 from the prefix extension (`SAGE` + `272` and `SAGE27` + `2` both render `SAGE272`),
   54 from `series_number()` sending an explicit value, which takes the backend's
   client-driven branch and accepts a stale number with only a log line. **Sending
   `value: None` would fix the second entirely.**
8. **The crosswalk is a gitignored file on one laptop** — losing it loses the ability to
   resume, adopt, or know what came from where.
9. **No per-line GST rate reconciliation exists for 14,603 goods documents** — the stream
   about to run at scale.

### MEDIUM
10. Discounts never read — 531 documents blocked (contained, not corrupted). → `risks/03`
11. Product provenance dropped — placeholders unflagged. → `contradictions/02`
12. 75 healthy products locked out by a stale blocklist. → `risks/05`
13. `extract.sql` depends on posted AP batches never being purged.
14. `find_sage.py` rewrites `.env` non-atomically.
15. 234 RCM documents unmigrated — 21% of the RCM liability.
16. **2 ACTIVE bills carry no accounting entry at all** (₹3,687.83). They arrive via the
    `preexisting` tag; the repair path matches only `UNVERIFIED`, so they are unreachable by
    any existing repair. The mechanism fires on **every interrupted run**.
17. **The tax head follows the *pincode*, not the GSTIN.** `OrgAddressMinUpsertDto` has no
    `state` field, so the voucher split reads a pincode-derived address. The loader's guard
    against this calls a route that does not exist, so it **has never fired**. 4 bills,
    ₹19,379.70 booked IGST against a Karnataka GSTIN.

### LOW
18. Dead code (`po_validate.py`, `po_holds.py`); `ORG_STATE` assigned twice; `||preexisting`
    counted three different ways by three reconcilers.

---

## 15. Proposed fixes

Full list, ranked, with what *not* to do: `proposed-fixes/01-proposed-fixes.md`.
**Nothing has been implemented.** Headlines:

- **Before the pipeline runs again:** fix the goods currency column (`ap_amount_hc`, at header
  *and* line level), and give the reconciler an independent column so it can detect the class
  of error.
- **Highest leverage, not engineering:** source the CIN/LLPIN file; decide the four scope
  exclusions; clear the stale burned-SKU blocklist.
- **Correct what is already wrong:** reclassify ₹8.72 Cr **with a journal, not by re-posting
  5,317 vouchers**; resolve the duplicate document numbers in the backend.
- **Do not** re-apply `sage-migration-backend-changes.patch` (missing 12 of 32 files, and it
  reintroduces a silent RCM default). **Do not** raise a tolerance to make a check pass.

---
---

# WHAT I NEED YOU TO CORRECT

Below is my understanding in plain language. I have tried to separate what I **proved**,
what I **inferred**, and what I **could not establish**. Please correct anything wrong —
especially in the last two groups, where I am most likely to have gone astray.

## What I believe, and why

**I believe `IDEDAT` is one Sage 300 company database holding Indian Designs' accounts**,
because it has a single company's chart of accounts in `GLAMF`, one vendor master of 4,752
rows in `APVEN`, and no company-code column partitioning anything.

**I believe `APOBL` is the posted open-payables ledger and `APIBH`/`APIBD` are the invoice
batch (entry) side**, because Sage's own `AUVIEW` dictionary calls `APIBH` "Invoices" and
`APIBD` "Invoice Details" and does not list `APOBL` at all; because `APIBH`'s key is
(batch, entry) while `APOBL`'s is (vendor, document); and because only `APOBL` carries the
posted-lifecycle columns `POSTSEQNCE`, `SWPAID`, `AMTDUEHC`, `DATEPAID`. **This is the one I
would most want you to confirm**, because if it is backwards the migration is reading the
wrong population, and everything downstream is wrong with it.

**I believe the purchase flow is** requisition → purchase order → receipt → PO invoice →
AP → settlement, with stock moving **at receipt**, not at invoice — because `POSTEDTOIC` is
1 on all 32,971 receipt lines and 0 on all 32,586 invoice lines. I believe 88% of POs begin
as requisitions, a step the usual description of this chain leaves out.

**I believe the accounting flow is** AP document → distribution (`APIBD`) → journal
(`APPJH`/`APPJD`) → posted GL (`GLPOST`), because I traced real documents through all four
and the postings sum to zero.

**I believe reverse charge is identifiable only by the accounts `1L8TX14`, `1L8TX15` and
`1L8TX16`**, because on those documents the header tax field is zero and the tax exists only
as negative lines on those three accounts, which `GLAMF` names "SGST/CGST/IGST Payable - RCM".

**I believe the existing migration is doing this**: pull AP-direct bills from Sage and
PO-matched goods bills from a MySQL staging mirror, create the vendor contacts and item
products each bill needs, then post bills one at a time over the REST API and verify each by
reading it back. It resumes from an append-only log and skips anything already recorded.

**I believe the mapping is broadly correct, because the numbers agree** — of 9,371 documents
compared, 6,668 match Sage to the paise and only 3 differ by ₹1 or more. That is not what a
wrong mapping looks like.

## What I believe is wrong

**I believe the goods path will understate ₹38.18 crore the next time it runs.** It reads
`doc_total`, which is the amount in the vendor's own currency, and posts it as rupees. I am
confident because the ratio of the home-currency column to `doc_total` reproduces the
exchange rate exactly in all three foreign currencies, and because the correct column,
`ap_amount_hc`, is sitting unused in the same row. **This is the thing I would most want
acted on before anything else.**

**I believe ₹8.72 crore of expense is filed under the wrong head right now**, because 132
products each have two live ledgers — one Direct, one Indirect — and which one a voucher
landed on depends only on whether it posted before or after 3 September. I checked whether
this was double-counting and it is not: no voucher touches both heads. *But I did not prove
that Indirect is the right answer for all 132.* That is a judgement for finance, and if the
September correction was itself wrong, the error points the other way.

**I believe the migration is about a third complete, not nearly done.** 34.2% by count and
13.5% by value. The goods stream is at 0.87%.

**I believe 289 missing vendor contacts are the real bottleneck**, not 10,209 individual
document problems — and that 157 of them need one file of CIN/LLPIN numbers that has to come
from outside Sage.

## What I could not prove

**I could not prove which of Sage's excluded documents *should* be excluded.** I could
attribute all ₹104.43 crore to four causes, and I am confident about two of them (loan
repayments, pre-GST tax groups). I am **not** confident about the ₹28.78 crore sitting in
"Difference Adjustment Control A/C" across 19 documents, or about 44 documents touching plain
CGST Payable. Those need a human who knows what that account is for.

**I could not prove the correct treatment of the 43 foreign-currency AP-direct invoices**
(₹25.42 crore). They are excluded because the extract filters to INR. Whether that is a
deliberate scope decision or an unexamined convenience, I do not know.

**I could not tie the migrated payables to Sage's payables.** SMEAssist's creditor balance is
₹497,239,932.81. Sage's is ₹535,122,264.66 at the start of the window and ₹429,484,090.46 at
the end. The migrated figure is neither, and I do not think it can be reconciled until
opening balances and payments are loaded.

**I could not settle whether the `1L9O` related-party accounts belong in "payables."** They
hold ₹3.26 billion. Including or excluding them moves the opening-balance gap by an order of
magnitude, and it is a question about how the business thinks, not about the data.

**I could not find where the distributions live for 1,071 AP-direct documents worth
₹289 million** that have no `APIBD` row. The business-flow analysis suggests they are loan
repayments posted through a payment batch. That is the most likely explanation, not a proven
one.

**I could not reproduce the claim that 154 documents carry leading or trailing whitespace.**
Live Sage shows 8 in the window. Trailing spaces are invisible in a `char` column, so neither
figure can be proven, and I do not know which is right.

**I could not verify that the goods staging mirror ties to Sage's PO and inventory tables.**
I only checked it against `APOBL`, where it was exact. The goods population is the one about
to be loaded at scale, so this gap matters.

## Where I found contradictions

**Between the two reports the project produces itself** — one says 18,006 documents are
outstanding, the other says 11,179. Both are right; they answer different questions, and
nothing says so. The 6,827 difference is 5,302 documents that are loadable and simply not
loaded, plus 1,525 that never entered the pipeline.

**Between the documentation and the database** — the project's own agent brief says
placeholder products carry a flag marking them as placeholders. They do not: `product.meta`
is empty on all 17,450. The loader sends the flag; the server discards it.

**Between two of my own agents** — one reported illegal GST-rate accounts sitting live in the
chart of accounts; my own check found all 60 soft-deleted with zero balances. Another
reported 43 foreign-currency invoices posted as INR; they were in fact excluded and never
posted. I have recorded the corrections rather than the original claims.

**And I contradicted myself.** I wrote early on that no Sage documentation existed in this
environment. That was wrong: Sage's own data dictionary is inside the database (`AUVIEW`,
`AUFLDS`, `DATADICT`). I had searched for files instead of querying. The correction is
recorded where the error was, because it is the same mistake this project made once before
with the vendor GSTIN — reporting an absence after searching only one place.

## What I think is missing

Opening balances, payments and allocations, credit and debit notes, prepayments, journal
vouchers, the chart of accounts, and TDS withholding — **all of these have zero rows in the
target**, and each is a decision that has not been made rather than a bug. Also missing:
any cost-centre dimension in SMEAssist at all, which means unit-wise P&L cannot exist unless
ledgers are split per unit.

## What I think may cause data loss

The crosswalk. It is a 4.1 MB file on one laptop, gitignored, hard-coded path, no database
fallback, already hand-repaired four times — and it is the only record linking Sage
identifiers to SMEAssist ids. The database table meant to hold it has zero rows. If that file
is lost, the migration cannot resume, cannot adopt existing records, and cannot tell what
came from where. Nothing else in this audit worries me as much on a long horizon.

And `work/cleanup_pilot.py`, which revokes and deletes every active bill in the organisation
with no confirmation flag. It was written when there were 8 bills. There are now 9,376.

## The one thing I would ask you first

**Is `APOBL` really the posted payables ledger, and is `APIBD` really where the expense
distribution lives?** Everything else in this audit is built on that. I have Sage's own
dictionary, the key structures, the status columns and a traced document all pointing the
same way — but it is the assumption that, if wrong, makes the rest of this report wrong too.

---
---

# The claims that most deserve a sceptical second look

Adversarial validation was dispatched and stopped before it reported, so nobody was
specifically tasked with proving this audit wrong. This is my own list of where I would
attack it. Ordered by *how much rests on the claim*, not by how likely I think it is to fail.

**1. Is `APOBL` the posted payables ledger and `APIBD` the expense distribution?**
Everything rests on this. Attack it by checking whether any AP document exists in `APIBD`
that never reaches `APOBL`, and whether `APOBLJ` (the purge-proof journal) tells a different
story from `APOBL` for the same document.

**2. Does the per-document value comparison join the right things?**
"Only 3 documents differ by ≥ ₹1" is the single most reassuring number in this report, and a
per-document comparison is only as good as its key. Attack it by testing whether the join
silently drops documents (an inner join hides its own failures), whether `*N` parts are
double-counted on one side, and whether the 9,371 compared are the same 9,371 on both sides.
**If this claim is wrong, the reassuring half of this report collapses.**

**3. Does `11,179 + 5,302 + 1,525 = 18,006` close because it is true, or because the sets
overlap conveniently?** An equality that lands exactly is either a proof or a coincidence.
Attack it by checking the three sets are genuinely disjoint — particularly whether any of the
5,302 "loadable but not attempted" documents are also inside the 1,525 never extracted.

**4. Is the later ledger mapping the *correct* one for all 132 products?**
I proved the migration gave two contradictory answers and that ₹8.72 Cr sits on the earlier
one. I did **not** prove Indirect is right. If the 3 September correction was itself wrong,
the error is the same size and points the other way — and the recommended remedy would move
value in the wrong direction.

**5. Are the 1,468 currency-affected documents genuinely unblocked?**
The ₹38.18 Cr "on the next run" figure assumes they will actually post. Attack it by checking
whether some other gate — a held contact, a missing product, a category filter — stops them
anyway. **The defect is real regardless; only the urgency depends on this.**

**6. Are the 1,079 no-distribution documents really loan repayments?**
₹29.5 Cr is excluded on this reading. It came from one analysis and I did not re-derive it.
Attack it by tracing two of them through `APOBP` and `GLPOST` to see what they actually did.

**7. Does `doc_total` behave the same way on *every* goods document?**
I proved the ratio reproduces the exchange rate in aggregate per currency. Attack it
per-document rather than per-currency — an aggregate ratio can hide a mixed population.

**8. Is the 289-vendor figure the real bottleneck, or an artefact of run ordering?**
Vendors may be "missing contacts" simply because the masters phase has not reached them.
Attack it by checking whether the 289 are genuinely held versus merely not yet attempted —
I separated 185 held from ~104 unattempted, but did not verify that the 185 are all still
blocked under current code.

**What I am most confident about**, and would expect to survive attack: the currency column
identification (`risks/04`, proven at the source-column level and by ratio), the discount
arithmetic (`risks/03`, 531 of 531 tie), the CIN and bank-detail absence (value-shape scans),
and the mis-headed ledgers *existing* (the double-mapping is a fact; only their correct head
is a judgement).

**What I would bet against myself on**: the exact attribution of the ₹104 crore exclusion.
Four causes, only two of which I would defend without help from someone who knows what
"Difference Adjustment Control A/C" is for.
