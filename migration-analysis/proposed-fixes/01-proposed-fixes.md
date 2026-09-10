# Proposed fixes — ranked. NONE OF THIS HAS BEEN IMPLEMENTED.

This is a list of things that should eventually change. It is **not** a work order, and
nothing in it has been done. Several items are business decisions, not engineering tasks,
and are marked as such. Each links to the finding that justifies it.

Ranking is by **expected value of acting**, which is not the same as severity: a large
problem that is already contained ranks below a smaller one that is about to happen.

---

## Tier 0 — before the pipeline is run again

### F1. Stop the goods path posting foreign currency as rupees
→ `risks/04` · **₹38.18 crore on the next run** · defect is armed, not yet fired

`SQL_GOODS_HDR` (`post_sage_bills.py:1605`) selects `h.doc_total, h.tax_total` — the
**source-currency** columns — and `build_payload` sends `"conversionRate": 1.0,
"currencyDto": {"currency": "INR"}`. The home-currency columns `ap_amount_hc` / `ap_tax_hc`
sit unused in the same staging row.

1. Switch the goods reader to `ap_amount_hc` / `ap_tax_hc`.
2. Hold, do not default, the ~132 headers where the `APOBL` join yields no home-currency
   figure.
3. Add a currency guard that refuses any non-INR document lacking a home-currency amount.
4. **Fix the line level too** — `sage_goods_line.extended` / `unit_cost` are also source
   currency, and the server *recalculates the header from the lines*, so a header-only fix
   would be undone by the server.

**Until F1 lands, do not run `goods-post`.** The AP-direct path is unaffected.

### F2. Make the reconciler capable of detecting F1
→ `risks/04 §6`

`reconcile.py` reads `doc_total` on the Sage side and compares it to a `billAmount` derived
from `doc_total`. **Both sides share the error, so it nets to zero.** Any reconciliation
whose two sides draw on the same column cannot detect that column being wrong. Add a check
that reads `ap_amount_hc` independently.

---

## Tier 1 — highest leverage, mostly not engineering

### F3. Source the CIN/LLPIN file for 157 vendors  *(business action)*
→ `risks/02` · unblocks the majority of 10,209 stalled documents

157 of 185 held vendors have a GSTIN 6th character of `C` (company) or `F` (LLP) and need a
CIN or LLPIN. **The data is genuinely not in Sage** — `APVEN.TAXNBR` and `APVEN.IDTAXREGI1`
are empty across all 4,752 vendors and there is no CIN column. This is a registry lookup or
a vendor-master extract, and no amount of engineering will produce it.

The remaining 132 of the 289 missing contacts: 21 need pincode corrections, 4 need a country
or identity decision, and ~104 have simply never been attempted and need no decision at all.

### F4. Decide the four scope exclusions  *(business action)*
→ `sage/00-the-exclusion-funnel-attributed.md` · **₹104.43 crore**

Four separate decisions currently encoded as one `WHERE` clause:

| exclusion | docs | ₹ Cr | recommendation |
|---|---:|---:|---|
| loan repayments (no distribution) | 1,079 | 29.51 | confirm — these are not bills |
| foreign-currency AP-direct | 43 | 25.42 | **decide** — real payables, excluded for convenience |
| pre-GST legacy tax groups | 118 | 0.57 | confirm |
| purity filter | 285 | 48.93 | **investigate `1L9O` (₹28.78 Cr) and `1L8T` (₹0.86 Cr)** |

### F5. Clear the stale burned-SKU blocklist
→ `risks/05` · 75 healthy products, trivially recoverable

All 75 "burned" SKUs are live, complete products with categories, HSNs and ledgers, and none
has ever been used on a bill. They were blocklisted by a product-lookup bug that has since
been fixed (`c06303a`). Verify the current lookup finds them, then clear `burned` and re-run
`goods-masters`. Give the blocklist an expiry so it cannot fossilise again.

---

## Tier 2 — correct what is already wrong in the data

### F6. Reclassify ₹8.72 crore of expense  *(business decision, then a journal)*
→ `risks/01`

132 products carry both an `ITEM_DIRECT_EXPENSE` and an `ITEM_IN_DIRECT_EXPENSE` ledger.
Which head a voucher landed on depends only on whether it posted before or after the
3 September correction. No voucher touches both heads, so this is misclassification, not
double-counting — total expense is right, the gross-profit line is not.

1. Decide the correct head per product **with finance**. Do not assume the later mapping is
   right merely because it is later.
2. **Move the value with a reclassification journal, do not revoke and re-post** 5,317
   vouchers. The bills are correct; only the ledger pointer is wrong.
3. Retire the emptied ledgers so the chart of accounts stops carrying 132 duplicate heads.

### F7. Resolve 152 duplicate document numbers
→ `validation/00 V14`, `backend/01`

**304 live bills share 152 `billSeriesNumber` values.** (The raw collision count of 3,025 is
mostly the soft-deleted prior cohort and is not the actionable figure.) Root causes are in
the backend: the counter returns numbers it never reserved, and `SAGE`+`272` and `SAGE27`+`2`
both render `SAGE272`. A statutory document number should be unique; this needs a backend
fix, not a loader workaround.

### F8. Read the discount
→ `risks/03` · 531 documents, ₹65.38 lakh, currently blocked not corrupted

Add `discount` to `GOODS_LINE_COLS` and `hdr_discount` to `GOODS_HDR_COLS`. **Establish with
the backend first** how `discount` / `itemDiscount` are applied and whether the server
recalculates from them — feeding a discount into a field the server treats differently would
turn a blocking defect into a silent one, which is strictly worse than today.

---

## Tier 3 — make the tooling able to see its own gaps

### F9. Make the two reports reconcile to zero by construction
→ `contradictions/01`

`failure_report.py` silently drops documents that shape cleanly, have a contact and were
never posted — **5,302 documents, ₹28.51 crore**, invisible in the file `run_all.sh` names as
the record of outstanding work. Add a *not attempted* bucket, surface the SQL-level
exclusions as a counted category, and make the run fail loudly when the reconciler's
`in_sage_not_posted` and the failure report's total disagree.

### F10. Check ledger **heads**, not just ledger presence
→ `risks/01 §3`, `migration-scripts/01 §D.2`

`items_without_ledger: 0` and `bill_lines_null_ledger: 0` were both green throughout the
period when ₹8.72 Cr was being misfiled, because every check tests *presence*. Worse,
`master_recon.py`'s `ledger_head` check can only see records carrying `itemMapping` —
**197 of 17,417, so 98.9% of the minted chart of accounts is never head-checked.**

### F11. Reconcile goods-line GST rates
→ `migration-scripts/01 §D.2`

No script anywhere compares Sage's stated goods-line GST rate to the posted line rate:
`value_recon.py` keys on `metaData.sageLine`, but goods lines are stamped `PO:<item>` /
`SVC:…`, which never matches. **14,603 goods documents have no per-line tax-rate
reconciliation** — and the goods stream is the one about to run at scale.

### F12. Get the crosswalk off one laptop
→ `smeassist/01`, `migration-scripts/01 §F.7`

`work/crosswalk_live.json` (4.1 MB) is the migration's only identity map — 17,220 items,
455 contacts, 197 GL accounts, 75 burned — and it is a **gitignored file on one machine with
a hard-coded path and no database fallback**, hand-repaired at least four times.
`idedat_staging.crosswalk` exists and holds **0 rows**. Losing that file loses the ability to
resume, to adopt, and to know what came from where.

### F13. Stop `find_sage.py` rewriting `.env` non-atomically
→ `migration-scripts/01 §D.1`

A crash between truncate and write loses the SQL password and the auth token. Write to a
temp file and rename.

### F14. Neutralise `work/cleanup_pilot.py`
→ `migration-scripts/01 §D.1`

It revokes and deletes **every ACTIVE bill in the org**, with no `--apply` flag and no dry
run. It was written when the org held 8 pilot bills; it now holds 9,376. This is a loaded
gun in the working directory. Also fix `repost_po_items.py`, where the `DELETE` runs even
when the `REVOKE` failed.

---

## Tier 4 — scope not yet addressed at all

These are not defects; they are work never started. Listed because the migration cannot be
called complete without a decision on each.

| Concept | Sage volume in scope | Target state | Consequence of omitting |
|---|---|---|---|
| **Opening balances** | `GLAFS`, 3,222 rows; trial balance ties (249 accounts, ₹769.05 Cr, difference 0.0000) | **0 rows** | the target's ledger starts at zero; payables cannot tie |
| **Payments / allocations** | `APOBP` 1.54 M rows | **0 rows** | every migrated bill shows unpaid; 97.8% of source documents are settled in Sage |
| **Credit / debit notes** | 2,066 CN + 137 DN in window | 96 records, all revoked and deleted | ₹398 M of credit notes unrepresented |
| **Prepayments / advances** | 1,729 in window, ₹1.55 bn | no concept | vendor balances not net of advances |
| **Journal vouchers** | manual GL is the largest FY2026 block by value (₹8,375 Cr on 7,881 legs) | none | no path exists; also the double-count hazard |
| **Chart of accounts** | 1,610 Sage accounts → 386 natural | not loaded | the journal path resolves ledgers by code and cannot run without it |
| **TDS / TCS withholding** | `APOBL.OAMTWHT1..5TC` | all NULL | payables are gross of TDS |
| **Cheque payment mode** | 14,851 cheque payments, 55% of the total | no representation | already accepted as a fidelity compromise |
| **Cost centre / unit-wise P&L** | 18 Wages ledgers by unit | **SMEAssist has no cost-centre dimension anywhere** | unit-wise P&L stops existing unless ledgers are split |

---

## What I would not do

- **Do not re-post the 5,317 mis-headed vouchers.** A reclassification journal is safer,
  cheaper and leaves an audit trail.
- **Do not "fix" the purity filter by widening it** until the four exclusions in F4 have been
  decided. Three of them are correct.
- **Do not re-apply `sage-migration-backend-changes.patch`.** It is missing 12 of the 32
  reverted files, and it contains an older RCM-column reader that silently ignores a typo —
  which books a bill under reverse charge. The current code rejects the typo.
- **Do not raise a reconciliation tolerance to make a check pass.** Two of the currently
  "high severity" GST rows are ±₹1.50 rate-snapping artefacts and should be *reclassified*,
  not tolerated away — the distinction matters when a real ₹8 lakh variance appears.
