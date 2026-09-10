# Reconciliation strategy — how we will eventually prove this migration is correct

> **Provenance note.** A dedicated reconciliation agent was dispatched for this and was
> stopped before it reported. This is written by the lead from evidence already established.
> The design and the structural gaps are covered; a per-check SQL specification for all
> ~25 checks is **not**. Marked `NOT COVERED` where that applies.

---

## 1. What the existing suite genuinely proves

The project already has five reconcilers, and they are better than most migrations have.
`value_recon.py` alone runs 18 checks. What they legitimately establish:

- Per-document value agreement on what posted — **6,668 exact, 2,693 within ₹0.01**
- Every voucher balances; `unbalanced_vouchers = 0`
- No line posted without a ledger; `bill_lines_null_ledger = 0`
- Nothing invented: `in_smeassist_not_in_sage = 0`, zero duplicate postings
- Rate snapping never moved a rate more than 0.10 percentage points

That is real assurance and should not be understated.

## 2. What it structurally *cannot* prove — the part that matters

Six blind spots, each confirmed in this audit. These are not tuning problems; each is a class
of error the suite cannot detect **by construction**.

| # | Blind spot | Proof it matters |
|---|---|---|
| 1 | **Both sides read the same column.** `reconcile.py` takes the Sage side from `doc_total` and compares it to a `billAmount` derived from `doc_total`. A wrong column nets to zero | ₹48.03 Cr currency defect is invisible (`risks/04`) |
| 2 | **Presence is checked, not correctness.** Every ledger check asks "is a finance account attached?", never "is it under the right head" | ₹8.72 Cr misfiled while all three checks were green (`risks/01`) |
| 3 | **Different denominators, unstated.** `reconcile.py` uses 12,781; `value_recon.py` uses 11,256 | the 1,525 gap is the extraction funnel; neither report says which base it used |
| 4 | **The SMEAssist side is built from `posted.log`**, so `in_smeassist_not_in_sage` is **structurally always 0** | it cannot detect a bill the loader did not create |
| 5 | **No per-line GST rate check for goods.** `value_recon.py` keys on `metaData.sageLine`; goods lines are stamped `PO:<item>` / `SVC:…`, which never matches | 14,603 goods documents have **no** rate reconciliation |
| 6 | **Excluded documents are invisible.** The `extract.sql` filters run before Python, so those documents never enter `book`, never reach `classify()`, and cannot appear in any report | ₹104.43 Cr silently out of scope (`sage/00`) |

Plus one inconsistency that will scale: a `||preexisting` line is counted as **posted** by
`failure_report.py:55`, **not posted** by `value_recon.py:326` and `reconcile.py:87`, and
**both** by `reconcile.py:123`.

---

## 3. The organising principle: four populations that must sum

Any honest reconciliation must classify **every** Sage document into exactly one bucket, and
assert that the four sum to the Sage total. The current tooling conflates the last two and
hides the fourth.

```
Sage documents in window
├── posted-and-correct        ← value ties within tolerance
├── posted-and-wrong          ← posted, value does not tie
├── not-posted-but-loadable   ← shapes fine, has a contact, simply not run
└── deliberately-excluded     ← filtered in SQL, with a named reason
                                 ─────────────────────────────────
                                 must equal the Sage total, exactly
```

**Measured baseline, 2026-09-05** (this decomposition is DATABASE VERIFIED and closes exactly
— see `contradictions/01`):

| bucket | documents | value |
|---|---:|---:|
| posted (AP-direct 9,250 + goods 128) | 9,378 | ₹498,776,137.99 active |
| of which **posted-and-wrong** | **3** (2 material) | ₹1,457,957.66 |
| refused by the shaper, with a reason | 11,179 | ₹139.92 Cr |
| **not-posted-but-loadable** (invisible today) | **5,302** | ₹28.51 Cr |
| **deliberately-excluded** (invisible today) | **1,525** | ₹104.43 Cr |

`11,179 + 5,302 + 1,525 = 18,006` = `reconcile.py`'s `in_sage_not_posted`. ✔

**Make this equality an assertion, not a coincidence.** A run that cannot balance its four
buckets should fail loudly.

---

## 4. The checks to add, ranked by value

| # | Check | Question | Tolerance | Why it matters |
|---|---|---|---|---|
| **C1** | **Currency basis** | does the Sage side read `ap_amount_hc` (home) rather than `doc_total`? | exact | the only check that can detect `risks/04`. **Must exist before `goods-post` runs** |
| **C2** | **Ledger head** | is each item ledger under an *expected accounting group* for its mapping? | exact | `master_recon.py`'s version sees only **197 of 17,417** records — 98.9% unchecked |
| **C3** | **Goods-line GST rate** | does each posted line rate equal Sage's stated `RATETAX1+2`? | exact after slab snap | 14,603 documents currently unchecked |
| **C4** | **Multi-part completeness** | for each logical invoice, does the posted amount equal Σ over its `*N` parts? | ≤ ₹0.05 | would have caught `risks/06` on the first run |
| **C5** | **Scope completeness** | do the four buckets sum to the Sage total? | exact | closes blind spots 3 and 6 together |
| **C6** | **Series-number uniqueness** | is `billSeriesNumber` unique among live bills? | exact | 152 duplicates today |
| **C7** | **Accounting existence** | does every ACTIVE bill have ≥ 2 voucher legs? | exact | 2 ACTIVE bills currently have none |
| **C8** | **Discount** | is Σ line `extended` − Σ `discount` + tax = `doc_total`? | ≤ ₹0.05 | 531 documents |
| **C9** | **Place of supply** | does the intra/inter split match the vendor's GSTIN state? | exact | 7 wrong today; nets to right, legally wrong |
| **C10** | **Payables tie** | does the target creditor balance equal Sage's? | exact | **cannot pass** until opening balances and payments load — that is the point |

## 5. Acceptance thresholds, and their justification

| class | tolerance | why |
|---|---|---|
| Document counts, bucket sum, series uniqueness, voucher existence, debits = credits | **exact — zero** | these are structural. Any drift is a defect, never noise |
| Per-document value | **≤ ₹0.05** | Sage truncates tax per authority; the server recomputes from slabs. Sub-paise disagreement is arithmetic, not error |
| Total tax by head | **≤ ₹0.10 per document, and net drift must not trend** | the loader's own note — *"Tax is NOT exact, and cannot be"* — is true. Net drift is currently −₹1.38 across 2,693 documents, 1,290 up / 1,403 down: **not accumulating**, which is the test that matters |
| Rate snapping | **≤ 0.10 percentage points** | measured maximum today; a larger move means the source rate was not a slab and needs a decision |

**A tolerance chosen to make a known defect pass is not a tolerance.** Two `gst_amount` rows
currently flagged *high severity* are ±₹1.50 rate-snapping artefacts — they should be
**reclassified**, not tolerated away, so that a genuine ₹8 lakh variance still stands out.

## 6. Sequencing

**Before `goods-post` runs at scale:** C1 (currency), C3 (goods rate), C4 (multi-part), C8
(discount). All four cover the goods stream specifically, and the goods stream is 99% unrun.

**Before any production cutover:** C2, C5, C6, C7, C9 — plus a decision on the four
exclusions in `sage/00`.

**Only once journals and opening balances enter scope:** C10, and the GL double-count rule
(per source module, GL history **or** the real entity, never both — `AP IN`, `AP PY`,
`AP PP`, `AP CR`, `PO RC`, `PO IN` must be excluded from any GL load).

---

## 7. `NOT COVERED`

- Per-check SQL for all ten checks. C1 and C4 have working SQL in `risks/04` and `risks/06`;
  the rest are specified but not written.
- A referential-integrity sweep of the document chain on the Sage side using the proven keys
  (`DRILLDWNLK`, `RCPHSEQ`, `PORHSEQ`). The keys are established in
  `business-flows/01-…`; the sweep was not run.
- Re-running the existing reconcilers to produce a fresh dated baseline. The figures above
  are from the artefacts generated 2026-09-05T11:25:31, plus the lead's own queries.
