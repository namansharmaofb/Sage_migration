# Fix proposal for the 1,050 documents that miss Sage

Everything below was checked against the live SMEAssist DB and live Sage, not
reasoned from the API docs. Nothing has been applied — `post_sage_bills.py` is
being edited by the other session.

## The constraint everything follows from

The server **ignores the `gstAmount` and `roundOffAmount` we send** and enforces
its own identity. Measured on all 29 bills in the org, no exceptions:

    gstAmount      = SUM(line taxableAmount x line gstPercentage / 100)
    billAmount     = taxableAmount + gstAmount + roundOffAmount
    roundOffAmount = the server's own nearest-rupee figure when hasRoundOff is
                     true; 0 when it is false (then the exact paise survive)

`build_payload` already sends Sage's true figures — `"gstAmount":
float(shape["tax"])` is 7224.00 on `JPS/2025-26/539` — and the DB holds 7224.80.
The value is discarded on the way in. `assert_invariants` cannot see this: it
compares the payload against itself, so it passes and the divergence appears only
after the post.

**Consequence: the only levers are per-line `taxableAmount` and `gstPercentage`.**
Every fix has to work through the lines. Two of the three problems fix cleanly
that way; one does not.

---

## Fix 1 — Imports: add the `2A7T` amount as a 0% line  ·  136 docs, ₹705,072

**Closes 136 of 136 exactly. Zero residue.** Verified over the whole window.

The refused vendors are couriers and customs agents:

    OTH359    international courier, imports arm        33 docs
    SELD236   domestic courier (GST-registered)         91 docs
    FABL026   international courier, transportation
    SELD292   domestic courier

They paid import IGST to Customs on the company's behalf and are being
reimbursed. So the `2A7TX04` amount **is** owed to the vendor — it belongs in
`billAmount` — but it carries no GST of its own. A line at 0% is exactly right:

    SELD236 | 238819       lines                          taxable      rate
      4E2ME33  Job work                                  73,688.00      0%
      4E4SD05  Carriage                                   5,000.00     18%
      2A7TX04  IGST Recoverable on Imports              133,845.00      0%   <- new
                                             taxable    212,533.00
                                             gst            900.00   (18% x 5,000)
                                             total      213,433.00  = Sage AMTINVCHC

**The one trap.** `2A7TX04` is `ACCTTYPE=B` — a balance-sheet account. Minting
its ledger through `item_ledger_for()`, which calls
`/financeAccountReferenceMapping/item/getOrCreate/ITEM_DIRECT_EXPENSE`, would
create an **EXPENSE** head and repeat the `1L6TA07` error exactly: a recoverable
asset booked to P&L. Point the line's ledger at the recoverable head instead —
the org already has `IGST Input (Import) @ 0.00 / 0.10 / 0.25 / 1.00 / 1.50 /
3.00 / 5.00 / 12.00 / 18.00 / 28.00 %` under Current Assets → Balance with
Government Authorities.

`build_payload` already separates the two: the PO branch does
`dict(i_pr, ledger=led_pr["ledger"])`, so a line can carry one product and a
different ledger. Use that.

Changes: `classify()` currently drops `inp` on the floor — carry it into the
shape (it already computes `[l for l in ll if s(l["gl"]).startswith("2A7T")]` in
the reconciler; `classify` needs the same). `build_payload` emits one 0% line per
`2A7T` line, ledger = recoverable head, description = `TEXTDESC`.

---

## Fix 2 — Round-off: make `4E1M016` a 0% line, stop sending `hasRoundOff`  ·  536 docs

**Closes 536 of 536 exactly**, including the **188 whose round-off is negative**.

Negative line items are accepted — 8 already exist in this DB, down to
`-30,294.00` at 0% ("Other Charges") and `-214.32` at 12% ("Freight Charge").

    JOBW258 | 108      taxable    308,406.00        (4E2ME07 x 3 lines)
                       4E1M016         +0.70  @0%   <- new line, not a bill field
                       taxable    308,406.70
                       gst         15,420.30
                       total      323,827.00  = Sage AMTINVCHC   (posts 323,826.00 today)

Send `hasRoundOff: false` and `roundOffAmount: 0` so the server stops recomputing
and the exact figure survives — proved by `AE-3` and every RCM and PO bill, all
of which kept their paise because `roundoff` was 0.

This means revisiting the `ROUNDOFF_GL = "4E1M016"  # 4.5 bill field, never a
line` decision. Sage books it **as a distribution line**, and `4E1M016` is
`ACCTTYPE=I` ("Round Off Value on Purchases") — a real P&L account. So a line
with a normal expense-style product and ledger mirrors Sage exactly, and unlike
Fix 1 there is no account-type trap here.

It also removes the silent override that put bill `108` ₹1.00 below Sage.

---

## Fix 3 — RCM: not fixable in code. Reclassify it.  ·  795 docs, ₹495.32

`gstAmount = SUM(taxable x rate)` is enforced, so to land both Sage's taxable
(₹144,496) and Sage's tax (₹7,224.00) the rate would have to be **4.9994%** — a
non-slab rate, i.e. precisely defect 4.1 that was fixed. There is no rate that
satisfies both.

Splitting into ₹144,480 @ 5% + ₹16.00 @ 0% hits both numbers exactly, and should
be **rejected**: it fabricates a nil-rated purchase line that does not exist in
Sage and would misstate the GST return.

**The better reading is that this is not a defect at all.** Sage rounds
hand-keyed RCM tax to whole rupees on **1,081 of 1,084** documents. The legally
correct GST at 5% on ₹144,496 is ₹7,224.80; Sage booked ₹7,224.00. SMEAssist is
the more accurate figure, and the variance is Sage's rounding.

Proposal: accept it, state the bound (≤ ₹1.95 per document, ₹495.32 across all
795), and log the Sage figure against the posted figure per document so the ITC
difference is auditable. The **3 documents where Sage does carry paise** —
`ACCI295` `ARL/721/25-26`, `ARL/785/25-26`, `ARL/699/25-26` — are the exceptions
and should be looked at by hand.

---

## Fix 4 — The 18 held: manual, in Sage

Not code. `work/ERRORS-JanApr-2026.csv` group 1 lists them:

- **9 RCM documents with no readable rate** — derived 4.76 / 4.86 / 4.91 / 4.92 /
  5.32%, plus outliers at 1.25% and 2.50%. They mix rates and Sage states none,
  so there is genuinely nothing to read. Needs `APIBD.RATETAX` populated, or a
  per-document decision.
- **8 where the stated per-line tax disagrees with the document tax** — ₹180.00,
  ₹100.00, ₹558.35, ₹1,149.22, ₹2,700.00, ₹3,444.62, ₹4,350.00, ₹9,775.52,
  ₹11,027.25.
- **1 with no `4E` expense line** — `FABL159 | VTP/870/25-26.`

---

## Add the guard that is missing

Nothing in the run compares SMEAssist back to Sage, which is why bill `108` went
₹1.00 light unnoticed. `assert_invariants` cannot do it — it only sees the
payload. After each successful post, read the bill back and assert:

    bill.billAmount   == AMTINVCHC        (forward charge)
    bill.taxableAmount == SUM(4E) + SUM(2A7T) + 4E1M016
    bill.gstAmount    == AMTTAXHC         (forward charge; log the delta for RCM)

and fail the document loudly if not. Cheap, and it is the only thing that would
have caught Fixes 1–3 at run time rather than in reconciliation.

## Result

| | today | after Fixes 1 and 2 |
|---|---:|---:|
| land on Sage's numbers exactly | 10,206 (90.7%) | **10,443 (92.8%)** |
| refused, never migrate | 136 | **0** |
| total off by the round-off override | 101 | **0** |
| RCM tax variance (Sage's rounding, ≤₹1.95) | 795 | 795 — accepted and logged |
| held by `classify()` | 18 | 18 — manual |

The 1,050 error documents are 1,050 distinct documents; no document appears in
two groups, so the arithmetic adds up cleanly.

**Still separate and still unresolved: the PO-matched population** (16,459
documents, ₹1.36bn) must stay out of this path until the `1L6TA07`
balance-sheet-as-expense classification is settled — and Fix 1 shows the same
trap is one careless `getOrCreate/ITEM_DIRECT_EXPENSE` away from recurring.

---

# IMPLEMENTED, 2 Sep 2026 ~16:20

Applied to `post_sage_bills.py` (96,925 → 111,255 bytes). Backup:
`post_sage_bills.py.before-fixes-20260902-155033`. `pyflakes` clean.

## Result, measured by running the patched code over all 11,256 documents

| | before | after |
|---|---:|---:|
| `billAmount` == Sage `AMTINVCHC` | 10,206 (90.7%) | **11,232 (99.8%)** |
| refused by `assert_invariants`, never migrate | 136 | **0** |
| total wrong from the round-off override | 101 | **0** |
| held by `classify()` | 18 | 24 |
| RCM tax variance (logged, not a defect) | 795 | 795 |

`assert_invariants` and an independent re-check of the built payload both pass on
**all 11,232** shapeable documents, and on all **10,378** shapeable goods
documents. 666 documents now carry a zero-rated line (536 round-off + 130
import).

## What changed

**`PASSTHRU_GL`** (new constant) maps `2A7TX04` → the org's own
`IGST Input (Import) @ 0.00%` leaf, with the reasoning and the 1L6TA07 warning in
the comment.

**`SQL_GL`** now also selects `2A7T` accounts so their names resolve from GLAMF.

**`classify()`** collects `2A7T` lines and emits a `zero` list of zero-rated
lines — Sage's `4E1M016` round-off always, and the import reimbursement on
forward charge only. It returns `taxable_all` and refuses any document where
`taxable_all + tax != AMTINVCHC`, so an unmapped distribution can never again
post silently short.

Two subtleties that cost a round trip each and are now encoded:

- **2A7T is netted per account, never line by line.** 35 documents book the same
  recoverable twice, `+X` and `−X` (e.g. `OTHL493|62163/25-26` has `2A7TX01`
  +3,263.69 and −3,263.69, and `2A7TX02` the same). Line-by-line handling held
  all 35 for want of a ledger they never use. Netting leaves them untouched.
- **On reverse charge the `2A7T` line is the input leg of the self-assessed
  pair** and equals the tax already — 1,084 of 1,084 documents. Emitting it there
  would double-count the tax into taxable, so it is forward-charge only.

**`build_payload()`** emits the zero-rated lines, sends
`taxableAmount = taxable_all`, and now sends `roundOffAmount: 0.0 /
hasRoundOff: False` always. A `2A7T` line overrides its ledger to the
recoverable head; the round-off keeps its own `4E` ledger, which is correct.

**`assert_invariants()`** drops the `roundOff` term (it is inside `taxable_all`
now) and adds the check that was missing all along: `billAmount` must equal
Sage's own `AMTINVCHC`.

**`ensure_products()`** no longer skips `ROUNDOFF_GL` — it is a line now and
needs a product and ledger like any other `4E` account.

**`classify_goods()`** got the same round-off treatment, for consistency and
because it shares `build_payload`/`assert_invariants`. Without it, a goods
document with a residual round-off would have started failing the invariant. As
it happens none of the 10,378 currently carries one.

**`readback_drift()`** (new) reads each posted bill back and compares it with
Sage — the guard that did not exist. It carries a deliberate tolerance, see
below. Wired into `phase_run`; drift is reported per document and recorded in
`work/failures.json`.

**RCM variance logging**: each RCM document prints `RCM TAX VARIANCE: Sage 1L8TX
x, posting y (delta z)` and the set is written to `work/rcm_variance.json`.

**The three DOD queries** are fixed: `li.gstPercentage` (4.1 had never run, error
1052), `voucherNumber` against `voucherEntry` (there is no `voucher` table, error
1146), and 4.2 now joins `idedat_staging.sage_ap_dist` through the `metaData` we
stamp on each line, so it counts only descriptions Sage actually supplied.
All seven checks now run; 4.2 returns **0** where the bare count said 9.

## One thing found while testing, and its tolerance

The server derives `gstAmount` as `SUM(line taxable x line rate)` while **Sage
truncates each authority separately** (handoff fact 4). So the two disagree on
**503 forward documents — every single one by exactly ₹0.01, ₹5.03 in total**
across the whole window. It is not caused by these fixes; it was always there and
only shows on multi-authority documents.

`readback_drift()` therefore applies the same tolerance `classify()` already uses
for its stated-tax gate, `max(0.05, 0.01 x lines x 2)`. Without it every
multi-line bill would report a spurious one-paisa failure. Genuine drift still
reports.

## The 6 new holds — a decision, not a defect

`2A7TX01/02/03` (domestic SGST/CGST/IGST Recoverable) net non-zero on 6
documents worth **₹6,903**. The org's domestic input heads are all rate-labelled
and this line is 0%, so there is no honest mapping. They are **held with an
explicit reason** rather than booked to a guessed head — they were being refused
before anyway, so nothing regressed; the message is just actionable now.

To clear them, add the right head to `PASSTHRU_GL`:

    "2A7TX03": ("<IGST input head id>", "IGST Input @ ..."),
    "2A7TX01": ("<SGST input head id>", "SGST Input @ ..."),
    "2A7TX02": ("<CGST input head id>", "CGST Input @ ..."),

## Not fixed, and why

- **The 18 original holds** are Sage-side data (9 RCM with no readable rate, 8
  stated-vs-document tax disagreements, 1 with no `4E` line). Listed in
  `work/ERRORS-JanApr-2026.csv`. They need a person in Sage.
- **The 795 RCM variances** cannot be fixed: no legal slab satisfies both Sage's
  taxable and Sage's rupee-rounded tax. Logged instead.
- **The 28 bills already posted** are untouched. `JOBW258|108` still sits ₹1.00
  below Sage and 8 bills still sit on the `SAGE-4E2ME07-R2` duplicate head; both
  need a revoke-and-repost, which is destructive and has not been run.
- **The PO-matched population** still books `1L6TA07`, a balance-sheet clearing
  account, as an expense. Untouched — that path is `emit/bills.py`'s and the
  classification needs settling first.
