# Two-site reconciliation of the 18 posted bills, 2 Sep 2026 ~14:40

Sage (source) against SMEAssist (target), including the ledger heads and voucher
legs each bill produced. Measured, not inferred. Independent of
`classify()`/`build_payload()` — this checks the result, it does not re-run the
logic that produced it.

Sources used:
- Sage headers: `Bills_JanApr_Header.psv` on the devbox. **The live SQL box was
  down for this run** (<sage-host>:1433 — no route, no ICMP; it answered at
  14:20 and stopped by 14:25, the Wi-Fi DHCP drop `load_headers` documents).
  Cross-checked against local `output/bills_header.csv`: 11,256 rows both sides,
  **0 gross values differing**.
- Sage lines: `idedat_staging.sage_ap_dist` (69,969 rows) — the only copy
  carrying `RATETAX`/`AMTTAX`.
- SMEAssist: `mysql smeassist` on <smeassist-host>, org <org-id>.

19 bills live in the org: 18 ACTIVE/VERIFIED, all traced to a Sage document,
plus the REVOKED `SMOKE/2026/1` (no voucher, no Sage source — expected).

## What reconciles exactly, on all 18

| check | result |
|---|---|
| `taxableAmount` vs Sage 4E* distribution | exact, all 18 |
| bill date vs `DATEINVC` | exact, all 18 |
| expense amount per Sage GL account | exact, all 18 |
| GST vs Sage, forward-charge bills | exact, all 11 |
| posted rate vs Sage's stated `RATETAX1+2` | exact, all forward bills |
| `gstPercentage` values in use | 0.00, 5.00, 18.00 — all legal slabs |
| GST authority split | CGST+SGST intra / IGST inter, halves equal, all 18 |
| voucher balances (DR − CR) | 0.00, all 18 |
| party legs | exactly one per bill, on the bill's own contact ledger |
| `voucherNumber` == `billNumber` | all 18; one voucher per bill |
| line `financeAccountId` | never NULL; always the ledger mapped to the line's product |
| lines sum to `taxableAmount` | all 18 |
| `isRcmEnabled` vs Sage's 1L8TX booking | agrees on every line |
| orphan voucher legs / duplicate bill numbers / bills without lines | none |

The 0% line on `107` is genuine: Sage states `RATETAX 0.0000` on the ₹20,000
"Rent amount" line, and 514,697 × 5% = 25,734.85 = `AMTTAXHC`.

The RCM party leg is credited the **taxable only** (not the grossed-up total),
with the tax on the RCM payable head. That is correct — Sage's `AMTINVCHC` on an
RCM document is the taxable, `AMTTAXHC` is 0, and the tax exists only as the
equal-and-opposite `2A7TX` input / `1L8TX` payable pair.

## Three real findings

### 1. Eight bills sit on the duplicate expense head — ₹1,775,411

Sage GL `4E2ME07` is split across two SMEAssist ledger heads:

| SKU | ledger head | bills | lines | amount |
|---|---|---:|---:|---:|
| `SAGE-4E2ME07` | `Job Work Expenses_SAGE-4E2ME07 Expense` | 3 | 3 | 203,253.00 |
| `SAGE-4E2ME07-R2` | `Job Work Expenses_SAGE-4E2ME07-R2 Expense` | 8 | 39 | **1,775,411.00** |

On the duplicate: `AE-3, 105, 107, 108, 109, 006., 186, 232`.
On the correct head: `007., AAQ-113/25-26, AAQ-114/25-26`.

Every **amount** is right — the recon flags only the head. Both products are
named "Job Work Expenses" and neither is deleted, so the expense is fragmented
across two heads for the same Sage account. Cause is the one documented in
`find_product_by_sku`'s own docstring (fuzzy `searchKey` paging). Known as
FINDINGS §5; `work/cleanup_pilot.py` removes these and has not been run.

### 2. RCM tax is recomputed from the snapped rate, so it no longer equals Sage's booked tax

6 of the 7 RCM bills carry a GST amount that differs from what Sage booked:

| invoice | taxable | Sage `1L8TX` | posted (taxable × 5%) | drift | Sage's implied rate |
|---|---:|---:|---:|---:|---:|
| 36220 | 110,000.00 | 5,500.00 | 5,500.00 | 0.00 | 5.0000% |
| JPS/2025-26/539 | 144,496.00 | 7,224.00 | 7,224.80 | +0.80 | 4.9994% |
| JPS/2025-26/540 | 144,248.00 | 7,212.00 | 7,212.40 | +0.40 | 4.9997% |
| JPS/2025-26/552 | 201,343.00 | 10,068.00 | 10,067.15 | −0.85 | 5.0004% |
| JPS/2025-26/565 | 141,181.00 | 7,058.00 | 7,059.05 | +1.05 | 4.9993% |
| JPS/2025-26/580 | 204,388.00 | 10,220.00 | 10,219.40 | −0.60 | 5.0003% |
| JPS/2025-26/599 | 41,523.00 | 2,078.00 | 2,076.15 | −1.85 | 5.0045% |

Net −1.05 across the six. Both the input debit and the RCM payable credit carry
the recomputed figure, so the vouchers still balance and the drift is invisible
to every balance check.

This follows from the recorded derive-and-snap decision (FINDINGS §2) — but that
decision was about the **rate label**, and the amount consequence looks
unrecorded. Sage's `2A7TX` input and `1L8TX` payable rows are equal and opposite
and carry the exact rupee figure; the snapped rate could label the line while the
booked amount is passed through verbatim, which would make RCM reconcile to the
paisa. Worth a decision either way rather than leaving it implicit.

### 3. `JOBW258 | 108` is ₹1.00 below Sage — the round-off we send is discarded

    Sage    308,406.00 + 15,420.30 + 0.70 (4E1M016)  = 323,827.00
    SMEAssist 308,406.00 + 15,420.30 − 0.30 (server) = 323,826.00

The poster sends `roundOffAmount: 0.70` and `assert_invariants` passes, because
the payload is self-consistent. The server then **discards it and recomputes**
nearest-rupee on `taxable + gst`: 323,826.30 → 323,826.00, i.e. −0.30. Sage had
rounded the same .30 fraction *up*, by adding 0.70.

Checked across all 18: **SMEAssist's `roundOffAmount` always equals the server's
own nearest-rupee figure, never the `4E1M016` line we send.** On the other 7
forward bills the two happen to coincide to the paisa, which is why this has not
shown up before. Bill `108` is the one where Sage rounded the other way.

Consequence: the vendor is credited ₹1.00 less than the invoice Sage holds. It is
a data difference, not an imbalance, so nothing in the current DOD catches it.
The cheap guard is a DOD check comparing `bill.billAmount` back to Sage's
`AMTINVCHC` on forward-charge documents.

## The DOD block itself has two dead queries and one false positive

Not edited — `post_sage_bills.py` grew 74,644 → 89,457 bytes and was modified at
14:39 while this ran, so the other session owns the file. Line numbers as of
89,457 bytes. All three corrected forms below were run and return what is stated.

**1. line ~1768 — `gstPercentage` is ambiguous, so check 4.1 never ran**
(`ERROR 1052: Column 'gstPercentage' in field list is ambiguous` — `bill` has the
column too). Qualify it:

```sql
SELECT DISTINCT li.gstPercentage FROM billLineItem li JOIN bill b ON b.id=li.billId
 WHERE b.organisationId={org} AND li.isDeleted+0=0 ORDER BY 1
```
Returns `0.00, 5.00, 18.00` — clean, no 5.01/2.49, the rate defect has not
returned.

**2. line ~1784 — there is no `voucher` table** (`ERROR 1146`). The voucher
header lives on `voucherEntry` (`voucherId`, `voucherNumber`, `voucherType`):

```sql
SELECT COUNT(*) mismatched FROM (
  SELECT DISTINCT ve.referenceId FROM voucherEntry ve JOIN bill b ON b.id=ve.referenceId
   WHERE b.organisationId={org} AND ve.isDeleted+0=0 AND ve.voucherNumber<>b.billNumber) x
```
Returns 0.

**3. line ~1778 — check 4.2 reports 9, but nothing was lost.** All 9 are
`4E4SD03` lines on `JPS/2025-26/540, 552, 565, 599` where **Sage's own `TEXTDESC`
is the empty string**, so the loader correctly fell back to the GL account name
("Carriage Outwards"). The check can only mean something where Sage supplied a
description, so it needs the staging join to exclude blank `TEXTDESC` — as
written it will keep reporting a number that looks like a defect and isn't.

## Still open from FINDINGS §5

`SMOKE/2026/1` is REVOKED but still live in the org (`isDeleted=0`), and the
duplicate counter rows per series+FY are unchanged.

---

# Update, ~15:05 — 10 more bills landed, and they are a different population

`work/posted.log` grew 18 → 28 while the above was being written (ACCI018 ×5,
ACCL005 ×5). Sage came back up, so this part is measured against **live APOBL**,
not the extract.

**None of the 10 is an AP-direct document.** All are `SRCEAPPL='PO'`
(PO-matched), which per the recorded division of work is `emit/bills.py`'s
population, not this loader's. They are absent from `SQL_HEADERS` for a second,
independent reason: the purity filter excludes them, because their only
distribution line is not `4E*`/`2A7T*`/`1L8TX14-16`.

## The account they post to is a balance-sheet account, booked as expense

Every one of the 10 distributes to a single line on **`1L6TA07`**. Sage's own
chart of accounts:

| code | description | `ACCTTYPE` | |
|---|---|---|---|
| `1L6TA07` | A/ P Clearing  -  Accessories | **B** | balance sheet |
| `1L8TX14` | SGST Payable - RCM | B | balance sheet |
| `2A7TX01` | SGST Recoverable | B | balance sheet |
| `4E2ME07` | Job Work Expenses | I | P&L |
| `4E4SD03` | Carriage Outwards | I | P&L |

`1L6TA07` is the same account *type* as the RCM payable and the GST recoverable —
a clearing account. In Sage a PO-matched invoice **debits** A/P Clearing to
reverse the accrual raised when the goods were received; the expense or inventory
was booked at GRN, not here.

SMEAssist has minted a product `SAGE-1L6TA07` named "A/ P Clearing  -
Accessories" whose ledger head is:

    A/ P Clearing  -  Accessories_SAGE-1L6TA07 Expense     financeGroupType = EXPENSE

and every voucher **debits it as an expense**:

    DE-2233/25-26   DEBIT  A/ P Clearing - Accessories_SAGE-1L6TA07 Expense   910.00
                    DEBIT  CGST Input @ 2.50 %                                 22.75
                    DEBIT  SGST Input @ 2.50 %                                 22.75
                    CREDIT Gst_29XXXPX0007X1Z1_<vendor-5>                     955.50

So a clearing account has become a P&L expense head. Consequences: the clearing
account never clears, P&L is overstated by the full value of every bill posted
this way, and the amount is double-counted against the GRN-side entry if that is
ever migrated.

Posted so far: **₹21,726.98** across 10 bills — small, but the population behind
it is not:

    Jan-Apr 2026, IDTRXTYPE=12, distributions to 1L6T* clearing accounts
        SRCEAPPL = PO     16,459 documents    ₹1,359,255,719
        SRCEAPPL = AP         11 documents    ₹733,628

The amounts themselves do tie out (taxable and GST match Sage on all 10, bar
sub-paisa: SMEAssist stores 3 decimals where Sage stores 2 — `ALD/8814` is
42.336 vs Sage 42.34 — and `DE-2229` is 1 paisa low, 6,998.25 vs 6,998.26). The
defect is the classification, not the arithmetic.

Worth confirming with whoever owns the PO-matched path before more of these go
in: a PO-matched invoice has no expense leg to migrate, so it may not belong in
`bill` at all.

## Revised verdict across all 28

| | bills | |
|---|---:|---|
| reconcile to Sage exactly — amounts, ledgers, voucher | **4** | `007.`, `AAQ-113/25-26`, `AAQ-114/25-26`, `36220` |
| correct amounts, wrong expense head (`-R2` duplicate) | 8 | ₹1,775,411 on a duplicate head |
| RCM tax recomputed from the snapped rate | 6 | −1.85 to +1.05, net −1.05 |
| round-off discarded, ₹1.00 below Sage | 1 | `108` (also in the `-R2` set) |
| wrong population **and** balance-sheet account booked as expense | 10 | ₹21,726.98 |

Every taxable amount matches Sage on all 28. Every voucher balances. The
differences are in *where* the money is booked, not in how much of it there is —
except the 7 totals noted above (6 RCM, 1 round-off).

---

# Readiness: what happens if the whole Jan-Apr 2026 AP-direct window is posted

Measured by running `load_book()` + `classify()` + the real `assert_invariants`
condition over all 11,256 documents against live Sage. Not a sample.

## Outcome per document

| | documents | |
|---|---:|---|
| **land on Sage's numbers exactly** | **10,206** | **90.7%** |
| RCM tax off by the snap (≤ ₹1.95) | 795 | 73.3% of the 1,084 RCM docs; ₹495.32 total |
| total off by the round-off override (≤ ₹1.06) | 101 | 18.8% of the 536 docs carrying a `4E1M016` line; ₹91.07 total |
| **refused** by `assert_invariants` | 136 | 1.2% — ₹705,072 of invoices that will not migrate |
| held by `classify()` | 18 | Sage's own data problems; correct to refuse |

Value of the shapeable set: ₹720,372,088. Only three GST rates occur in the whole
window — 0, 5 and 18 — all legal slabs.

**The round-off finding is smaller than the 28-bill sample suggested.**
`build_payload` sends `hasRoundOff` only when `roundoff != 0`; where it is 0 the
server keeps the exact figure. `AE-3` and every RCM and PO bill posted so far
kept their paise, which proves it. So the override can only bite the 536
documents that carry a `4E1M016` line, and it actually changes the total on 101
of them — not on all 10,566 without one.

## The 136 refusals are import documents

All 136 fail for one reason: a `2A7T` GST-recoverable distribution line that
`classify()` ignores, so `AMTINVCHC != 4E + AMTTAXHC + roundoff`.

| account | description | lines | amount |
|---|---|---:|---:|
| `2A7TX04` | IGST Recoverable on Imports | 171 | **698,169.00** |
| `2A7TX03` | IGST Recoverable | 4 | 137.00 |
| `2A7TX02` | CGST Recoverable | 2 | 3,383.00 |
| `2A7TX01` | SGST Recoverable | 2 | 3,383.00 |

99% of the gap is IGST paid at customs, concentrated in two vendors: `SELD236`
(91 documents) and `OTH359` (33). The invariant catches every one and skips it,
so **nothing is mis-posted** — this is the guard working. But the documents do
not migrate.

SMEAssist already has the right home for these: the org has `billOfEntry` /
`billOfEntryLineItem` tables, and `IGST Input (Import) @ 0.00 / 0.10 / 0.25 /
1.00 / 1.50 / 3.00 / 5.00 / 12.00 / 18.00 / 28.00%` ledger heads already exist.
Imports have a destination; they are just not wired to it.

## Two pieces of good news

**AP-direct cannot hit the `1L6TA07` classification error.** All 188 GL accounts
the window would mint products for are `ACCTTYPE=I` (P&L) — zero balance-sheet
accounts, and every one has a `GLAMF` name, so no product would be named after
its own code. The purity filter is what protects this; the PO path bypasses it.

**The `-R2` duplicate will not keep catching new bills.** `work/crosswalk_live.json`
maps `4E2ME07` to the *correct* product:

    4E2ME07 -> productId 1543849941553676288, skuCode SAGE-4E2ME07,
               ledger 1543849942543532032, adopted: true

So the 8 bills already on `-R2` are historical damage, not an ongoing leak.

## What actually gates the run

**407 vendor contacts.** The crosswalk currently holds 10 contacts and 48
products. The earlier run skipped 11,170 bills for "no contact built for vendor" —
that, not any of the above, is the blocker on volume.

## Verdict

Posting the window today would be **correct for ~90.7% of it, safe for the rest,
and incomplete**: nothing gets mis-posted (the invariant refuses the 136 that
would be), but 896 documents land within a rupee or two of Sage rather than on
it, and 154 do not land at all. In priority order:

1. Build the 407 contacts — nothing scales without them.
2. Decide the RCM tax question (§2 above): pass Sage's `1L8TX` amount through
   verbatim and let the snapped rate be only a label. Fixes 795 documents.
3. Route the 136 import documents to the bill-of-entry flow, or accept them as
   out of scope and record that.
4. Send no round-off and let the exact figure stand, or teach the server Sage's
   direction. Fixes 101 documents.
5. Keep the PO population out of this path entirely until the clearing-account
   classification is settled.

---

# Item master: pulled into the bill lines, never into a product  (checked ~16:45)

**The item master has not been built.** The code for it exists and is sound; it
has simply never run.

    products in the org                105   (all SAGE-* skus)
      carrying meta.sageItem             0   <- no product came from ICITEM
      carrying meta.sageAccount          0   <- not even the GL pseudo-items
      typeOfStock                       68 SERVICE, 36 CHARGE, 1 RESOURCE
      unitPrice = 0                    104 of 105
    crosswalk_live.json "items" key   absent  <- ensure_item_products never ran
    distinct units across all 71 bill lines: OTH

The 105 products were minted by an earlier loader that stamped no metaData; the
current code only *adopts* them (48 are `adopted: true`).

## The staging pull itself is fine

    sage_item                       17,129 rows
      = distinct items referenced by the 32,586 goods lines, exactly
      missing from sage_item              0
      blank item_no_raw / stock_unit / category   0 / 0 / 0
    real units of measure              18  (NOS 8,297, MTRS 6,204, YRDS 1,771,
                                            ROLLS 490, BOX 93, KGS 83, ...)
      platform_unit() maps them to     13  (NOS, MTR, YDS, ROL, BOX, KGS, ...)

HSN, quantity, unit cost and rate already reach the bill **line** correctly - the
12 GOODS lines carry hsn 5807 / 54011000 / 55081000 and real unit prices 0.26 to
155.00. None of it lands on a **product**, because those bills point at the GL
pseudo-item `SAGE-1L6TA07`. So all 18 units of measure collapse to `OTH`, the
item master is priced at zero, and anything keyed on the product - stock, price
lists, purchase history, HSN-by-item reporting - has nothing to read.

`ensure_item_products()` fixes exactly this and its docstring says so. Three
things to settle before running it:

**1. Every item would be created as `RESOURCE`, not `RAW_MATERIAL`.** Documented
and knowing: RAW_MATERIAL and PRODUCT are both refused ("Category is mandatory in
case of Raw Material"), this org owns no catalog categories, and they cannot be
created - `/product/category` and `/product/categories` are GET,HEAD,OPTIONS
only. Sage has the real categories (4THRED 3,554, 4PACK 3,446, 4FASHL 2,097,
4LABEL 2,023, 4ACCES 1,231, 4CARTN 1,124, ...) and they would survive only in
`meta.sageCategory`. Getting categories created in the org first is the
difference between an item master with a category structure and one without.

**2. 1,563 of the 17,129 items have no HSN on any goods line** and would be
created with `EXPENSE_SAC = 996719` - a *service* SAC on material items. 47
products in the org already carry it. The constant's own comment calls it a
DEFAULT awaiting finance sign-off; on goods it is a GST-reporting defect, not
just a placeholder.

**3. It is create-once, never updated.** `ensure_item_products` skips anything
already in the crosswalk, so a later Sage price, HSN or UoM change never reaches
SMEAssist. If the item master is meant to stay in step with Sage, that needs a
reconcile-and-PATCH pass, which does not exist.

Also: 2 item SKUs are already burned - `SAGE-4E2ME02-14` and `SAGE-4E5O024-01` -
taken by rows adoption cannot see, so those two items can never get a product.

Scope note: 17,129 items is what the Jan-Apr window references. Sage's `ICITEM`
holds **1,196,108** rows. Correct for this migration, but it is not the whole
item master.

## UPDATE ~16:55 — the item master IS being built, and it is correct

Supersedes the section above. A `goods-masters --limit 70` run is **in flight in
the other session** (pid 78737, `work/goods_masters2.log`); the crosswalk grew
31 → 44 items while this was being written, so the numbers below move.

I verified every item product that exists against Sage — price, unit, GST rate
and HSN, keyed the way `item_key()` keys them (`item_no_fmt|unit`):

    item products checked                44
    matching Sage on price               44
    matching Sage on unit                44
    matching Sage on GST rate            44
    matching Sage on HSN                 44
    -> every item product matches Sage exactly

    RESOURCE products      45, all 45 priced   (was 1, unpriced)
    units in use           NOS, ROL, BOX, KGS  (was OTH for everything)
    carrying the 996719 SERVICE sac         0
    run needs                             153 distinct (item, unit) products

Spot values, all confirmed against `sage_goods_line` + `sage_item`: cello tape
ROL @ 10.00 hsn 39191000 @18%, thread NOS @ 91.00 hsn 54011000 @5%, wash-care
label NOS @ 0.26 hsn 5807 @5%, pens BOX @ 275.00 hsn 9608 @18%, yarn NOS @
155.00 hsn 55081000 @5%.

So my earlier "the item master has not been built" was true when measured and is
now out of date. Prices, units, HSNs and rates are all coming through correctly.

### The one thing still worth fixing before the full run

None of these 70 documents needed the HSN default, which is why 996719 does not
appear yet. Across the whole window **1,563 of 17,129 items have no HSN on any
goods line**, and `ensure_item_products` falls back to
`EXPENSE_SAC = "996719"` — a *service* SAC — on a material item. DB-wide that
code appears on 86 CHARGE and 20 SERVICE products and on **no goods product**,
so it would be an anomaly here too.

`hsnCode` is nullable (195 products DB-wide have it null), so the fix is to omit
it rather than substitute a service code:

```python
# in ensure_item_products, replacing: "hsnCode": s(it.get("hsn")) or EXPENSE_SAC
"hsnCode": s(it.get("hsn")) or None,
...
"metaData": {..., "hsnMissing": "true" if not s(it.get("hsn")) else "false"},
```

**Not applied** — `post_sage_bills.py` is being run by the other session right
now, and it edited the file at 16:07. This one is theirs to take.

Also unchanged from above: `typeOfStock` is `RESOURCE` for every item. The
docstring's reason ("this organisation owns no catalog categories at all") is
**no longer true** — the org owns 4, created 29-31 Aug:

    1543237860483694592  SAGE_MIG_ITEMS  Sage Migration Items   (68 products use it)
    1543838234915667968  4BUTON          Buttons
    1543838235347681280  4HANGR          Hanger
    1543838235817443328  4PACK           Other Packing Materials

Three of them mirror Sage categories. `/product/category` and
`/product/categories` both return `[]`, so they are not reachable that way, but
`product.categoryId` does point at `catalogCategory`. Whether `RAW_MATERIAL` now
works with one of those ids is untested — I tried a single probe product and the
write was blocked, so it remains an open question rather than a measured fact.
Platform-wide `RESOURCE` is rare (204) against `RAW_MATERIAL` 60,986 and
`CONSUMABLES` 109,895, so it is worth one test before 17,129 items are minted
under it.
