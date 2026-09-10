# Lead agent — independent verification log

*Checks I ran myself, before and alongside the specialist agents, so that their
conclusions could be tested rather than accepted. Each entry states the claim, its
source, what I did, and the verdict.*

---

## V1 — "Vendor GSTIN is in `APVEN.BRN`, not the standard tax columns"
**Source:** `ref/SAGE-TO-SMEASSIST-HANDOVER.txt`, and the archived prompt that records an
earlier pass getting this wrong.

```sql
SELECT COUNT(*) vendors,
       SUM(CASE WHEN LEN(RTRIM(BRN))=15 THEN 1 ELSE 0 END)       brn_len15,
       SUM(CASE WHEN LEN(RTRIM(TAXNBR))>0 THEN 1 ELSE 0 END)     taxnbr_nonblank,
       SUM(CASE WHEN LEN(RTRIM(IDTAXREGI1))>0 THEN 1 ELSE 0 END) idtaxregi1_nonblank
FROM APVEN;
```
→ `4752 · 2677 · 0 · 0`

**CONFIRMED.** DATABASE VERIFIED, confidence VERIFIED. Corollary worth carrying forward:
**2,075 vendors (43.7%) have no GSTIN-shaped `BRN`** — the unregistered/foreign
population that needs identity handling other than a registration number.

---

## V2 — "Reverse charge is booked explicitly to `1L8TX14/15/16`"
```sql
SELECT RTRIM(ACCTID), RTRIM(ACCTDESC) FROM GLAMF
 WHERE RTRIM(ACCTID) IN ('1L8TX14','1L8TX15','1L8TX16');
```
→ `SGST Payable - RCM` · `CGST Payable -RCM` · `IGST Payable - RCM`
Usage in `APIBD`: 17,204 / 17,216 / 3,403 lines, all **negative** amounts.

**CONFIRMED.** DATABASE VERIFIED, confidence VERIFIED. Also settles two table meanings
without guesswork: **`GLAMF` is Sage's chart of accounts** and **`APIBD` is the AP
distribution table**.

---

## V3 — Is the `idedat_staging` mirror trustworthy?
The README warns the mirror is missing 469 vendors that live `APVEN` has. I checked
whether that warning generalises to the *document* data.

| `IDTRXTYPE=12, SRCEAPPL='AP'`, Jan–Apr 2026 | live `APOBL` | `idedat_staging.sage_ap_obl` |
|---|---:|---:|
| documents | 12,781 | 12,781 |
| gross `AMTINVCHC` | 1,763,950,260.97 | 1,763,950,260.97 |

**Exact agreement to the paisa.** DATABASE VERIFIED, confidence VERIFIED.

**Verdict: the staleness warning is table-specific and must not be generalised.** The
vendor mirror is stale; the AP-obligation mirror is faithful for this window. Anyone
reasoning from "the mirror is stale" about document counts would be wrong.

---

## V4 — What the extraction discards
Measured `extract.sql @@bills_header` as a funnel against live `APOBL`:

| stage | documents | gross (₹) |
|---|---:|---:|
| base | 12,781 | 1,763,950,260.97 |
| + has `APIBD` lines | 11,702 | 1,468,842,701.21 |
| + INR only | 11,659 | 1,214,597,827.13 |
| + not pre-GST tax group | 11,541 | 1,208,928,547.13 |
| + **purity filter** → final | **11,256** | **719,631,371.43** |

**88.1% of documents, 40.8% of value.** ₹104.43 crore never enters the pipeline; the
purity filter alone removes ₹48.93 Cr on just 285 documents (Difference Adjustment
Control ₹28.78 Cr, Bonus Payable ₹5.46 Cr, Accumulated Depreciation on ROU ₹2.77 Cr,
plain CGST Payable ₹0.86 Cr, term loans, deposits).

**NEW FINDING**, DATABASE + SCRIPT VERIFIED, confidence HIGH. The filter's logic is
defensible — these are *"journals wearing an invoice's clothes"* — but a ₹104 Cr scope
exclusion should be an accepted decision, not a side effect of a `WHERE` clause. Detail
in `project-map/02-documentation-analysis.md §6`.

---

## V5 — The two reports disagree by 6,827 documents
`reconcile-report.json: in_sage_not_posted = 18,006` vs
`failures-report.json: 1,962 + 9,217 = 11,179`.

I reproduced `failure_report.py`'s own population and found the branch it discards
(`work/failure_report.py:71` — `if not why: continue`): a document that **shapes cleanly,
has a contact, and simply was never posted** is reported nowhere.

| | documents | value (₹) |
|---|---:|---:|
| AP-direct silently dropped | 44 | 14.07 Cr |
| Goods silently dropped | 5,258 | 14.44 Cr |
| **total** | **5,302** | **28.51 Cr** |

Gap closes **exactly**: `11,179 + 5,302 + 1,525 (never extracted) = 18,006`. ✔

**CONFIRMED, fully reconciled.** SCRIPT + DATABASE VERIFIED, confidence VERIFIED.
Full write-up: `contradictions/01-the-two-reports-disagree.md`.

---

## V6 — The goods stream has barely started
From the same sweep: of **14,603** goods documents in the book, **128 are posted (0.9%)**,
9,217 are reported as failures, 5,258 are silently dropped. AP-direct is at **9,250 of
11,256 (82.2%)**.

**NEW FINDING.** DATABASE + SCRIPT VERIFIED, confidence VERIFIED. Reporting the two
streams as one aggregate ("9,378 posted") makes a nearly-complete stream and a
barely-started one look like uniform two-thirds progress.

---

## V7 — Mis-headed item ledgers hold ₹8.72 crore
132 products carry both an `ITEM_DIRECT_EXPENSE` and an `ITEM_IN_DIRECT_EXPENSE` ledger,
each with posted value; the split is purely by posting date, side-stepped by
`item_ledger_for()` minting rather than remapping. **No voucher touches both heads (0),
so this is misclassification, not double-counting.**

**CONFIRMED.** Full write-up with the queries: `risks/01-mis-headed-item-ledgers.md`.

---

## V8 — The failure report OVERSTATES one risk (a correction in the project's favour)
`failure_report.py`'s docstring describes `posted_but_wrong.unverified` as *"bills created
but never verified (**no voucher → zero accounting**)"*, and reports 7 of them.

I looked all 7 up in the target:

| bill | status | amount (₹) | voucher legs |
|---|---|---:|---:|
| AE-3 | ACTIVE | 115,962 | 4 |
| 105 | ACTIVE | 355,363 | 6 |
| 107 | ACTIVE | 560,432 | 7 |
| 108 | ACTIVE | 323,826 | 7 |
| 109 | ACTIVE | 72,344 | 5 |
| 006. | ACTIVE | 162,156 | 6 |
| 186 | ACTIVE | 168,132 | 22 |

**All 7 have accounting.** The `UNVERIFIED` marker records that the loader's *readback*
step did not complete — not that the bill lacks vouchers.

**The docstring's stated consequence is wrong for every one of these rows.** The risk is
real but different: these bills were never checked against Sage, so their *values* are
unconfirmed. That is a verification gap, not an accounting hole.
DATABASE VERIFIED, confidence VERIFIED.

---

## V9 — Health signals that are genuinely green
Worth recording, because a report that only lists problems misrepresents the work:

| check | value | meaning |
|---|---|---|
| `unbalanced_vouchers` | **0** | every migrated voucher balances |
| `bill_lines_null_ledger` | **0** | the handover's CRITICAL silent-data-loss defect #1 is clean |
| `items_without_ledger` | **0** | no item product is missing a ledger |
| `in_smeassist_not_in_sage` | **0** | nothing invented on the target side |
| staging vs live Sage (V3) | exact | the extract is faithful for AP obligations |

The caveat from V7 applies to the second and third rows: they test *presence*, not
*correctness of head*.

---

## V10 — Vendor contacts are the real bottleneck (289, not 10,209)
`no_vendor_contact` accounts for **10,209 of 11,179** reported failures (91.3%). Behind it:

| population | vendors | have contact | missing |
|---|---:|---:|---:|
| AP-direct (post-filter) | 412 | 319 | **93** |
| PO / goods | 357 | 161 | **196** |
| **union** | **734** | 455 | **289** |

185 of the 289 are explicitly held, and **157 of those 185 (85%) are held for one reason**:
a GSTIN 6th character of `C` or `F` requiring a CIN/LLPIN that does not exist in Sage. I
confirmed the absence rather than assuming it — `APVEN.TAXNBR` and `APVEN.IDTAXREGI1` are
empty for all 4,752 vendors and there is no CIN column.

**NEW FINDING**, DATABASE + SCRIPT VERIFIED, confidence VERIFIED. Full write-up:
`risks/02-the-real-blocker-is-289-vendors.md`.

---

## V11 — The staging vendor mirror covers the goods population *only*
The README says the mirror is *"missing 469 of the vendors live APVEN has"*. What is
actually true is more specific and more useful:

`idedat_staging.sage_vendor` (357) is **set-identical** to the PO/goods vendor population
(357) — zero missing. But **377 of the 412 AP-direct vendors (91.5%) are absent from it.**

So it is not a degraded copy of `APVEN`; it is a complete copy of one population.
Under the documented Sage-unreachable fallback, AP-direct vendor lookups fail almost
entirely — which is exactly the symptom `no_vendor_contact` describes.

I could not reproduce the figure 469 against any baseline (937 − 357 = 580;
412 − 35 = 377). **That number should not be quoted until its baseline is stated.**
DATABASE VERIFIED, confidence VERIFIED for the structural fact.

---

## V12 — The 9999 placeholder-HSN count is exactly right
| hsnCode | products |
|---|---:|
| `9999` (goods placeholder) | **1,419** |
| `996719` (expense SAC default) | 172 |
| total products in org | 17,450 |

**CONFIRMED to the unit.** 1,591 products (9.1%) carry a default rather than a real code.
DATABASE VERIFIED, confidence VERIFIED. The project's own documentation is accurate here,
and both defaults are deliberate, visible placeholders carrying `hsnIsDefault` /
`sacIsDefault` in `metaData` — a known gap awaiting a real code, not a silent error.

---

## V13 — The previous generation's data was cleaned up properly (no double-count)
The handover records that a PowerShell pipeline previously posted 1,092 AP-direct + 295
PO-matched bills into this same org. If those survived, the current run would be posting
on top of them.

Bills in the target org by creation date:

| created | bills | of which deleted | value (₹) |
|---|---:|---:|---:|
| 2026-08-31 | 1,387 | **1,387** | 37,989,514.46 |
| 2026-09-01 | 5 | **5** | 147,329.92 |
| 2026-09-02 | 39 | 0 | 3,348,009.67 |
| 2026-09-03 | 7,936 | 0 | 393,960,975.72 |
| 2026-09-04 | 1,402 | 0 | 101,480,438.45 |

**1,387 = 1,092 + 295 exactly** — the prior cohort, and every one of it is soft-deleted.
And their accounting went with them: all 13,069 voucher legs (₹7.63 Cr) carry
`isDeleted = 1`.

Two things worth noting:
- **No contamination.** The `cleanup` phase did its job; ₹3.8 Cr of prior bills are not
  being double-counted.
- **An internal consistency check falls out for free**: voucher legs total ₹7.63 Cr against
  bills of ₹3.81 Cr — almost exactly 2×, which is what symmetric double-entry should
  produce. Corroborates `unbalanced_vouchers = 0`.

DATABASE VERIFIED, confidence VERIFIED. It also corroborates the handover's own counts to
the document, which raises my confidence in its other unverified claims.

---

## V14 — Cross-check of the backend agent's series-number claim (CONFIRMED, and sharpened)
The backend analyst reported *"3,025 bill rows share a colliding `billSeriesNumber`,
including ACTIVE-vs-ACTIVE pairs."* I re-ran it independently rather than accept it.

**Confirmed exactly: 3,025 rows across 1,481 distinct colliding numbers.**

I then split the collisions by how many rows in each group are still live, because that is
the difference between cosmetic history and a live books-of-account defect:

| live bills in the group | groups | live rows |
|---:|---:|---:|
| 1 (the others soft-deleted) | 1,329 | 1,329 |
| **2 (both live)** | **152** | **304** |

**The material figure is 152 pairs / 304 live bills sharing a statutory document number**,
not 3,025. The remaining 1,329 collisions are the soft-deleted PowerShell cohort (V13)
colliding with their replacements — expected, and harmless.

`billSeriesNumber` is the organisation's own sequential document number, so 304 live bills
carrying 152 numbers is a real defect in the books, but it is an order of magnitude smaller
than the raw collision count suggests. **DATABASE VERIFIED, confidence VERIFIED.**

*Method note: this is the kind of adjustment the lead role exists for — the agent's number
was correct and its framing was right, but the actionable subset needed separating from the
historical noise before anyone sizes the remediation.*

---

## V15 — Cross-check of the backend agent's illegal-GST-rate claim (PARTIALLY REFUTED)

The backend analyst reported: *"SMEAssist does not validate GST rates and auto-creates a
ledger account per distinct rate it sees. The live chart of accounts now contains ~45
illegal GST rates (4.76%, 4.99%, 5.01%, 2.29%, 6.71%, 9.01% …) created by this migration.
**Revoking the bills did not remove the accounts.**"*

The mechanism is real and the rates are real. **The stated consequence is not.**

```sql
SELECT CAST(isDeleted AS UNSIGNED) del, COUNT(*) accounts,
       SUM(netBalance<>0) with_balance, ROUND(SUM(ABS(netBalance)),2) abs_value
FROM financeAccount
WHERE organisationId=<org> AND name REGEXP '@ *[0-9]' AND name REGEXP '(CGST|SGST|IGST)'
GROUP BY del;
```

| `isDeleted` | rate ledgers | with a balance | absolute value (₹) |
|---:|---:|---:|---:|
| 0 (live) | 24 | 13 | 37,286,041.34 |
| **1 (deleted)** | **60** | **0** | **0.00** |

And every one of the 13 **live** ledgers carrying a balance is at a **legal slab**:

| ledger | net balance (₹) |
|---|---:|
| IGST Input @ 5.00 % | −12,027,974.26 |
| IGST Input @ 18.00 % | −10,402,623.67 |
| CGST Input @ 9.00 % / SGST Input @ 9.00 % | −4,711,588.62 each |
| CGST Input @ 2.50 % / SGST Input @ 2.50 % | −1,604,234.91 each |
| IGST Input (Import) @ 0.00% | −691,279.00 |
| CGST/SGST Payable RCM @ 2.50 % | 509,350.07 each |
| CGST/SGST Payable RCM @ 9.00 % | 163,595.16 each |
| IGST Payable RCM @ 18.00 % | 122,400.00 |
| IGST Payable RCM @ 5.00 % | 64,226.90 |

**Corrected finding.** The illegal-rate ledgers (4.76%, 4.86%, 4.92%, 4.95%, 4.99%, 5.01%,
5.02%, 5.45%, 0.32%, 0.35%, 1.25%, 1.34%, 2.29%, 2.46–2.53%, 2.66%, 6.71%, 9.01% …) were
created — 60 of them — **and then cleaned up**. All 60 are soft-deleted and **every one
holds a zero balance**. No illegal rate carries value, and the live GST position sits
entirely on legal slabs.

**What survives of the finding, and it is still worth acting on:**
- The **mechanism is confirmed and unguarded** — SMEAssist mints a ledger per distinct rate
  string with no slab validation. The next run that emits a fractional rate will create more.
- The **rows persist**, soft-deleted. If ledger names are unique-constrained, those 60 names
  are consumed — the same "burned identity" pattern as the 75 burned SKUs.
- The cleanup was evidently manual. Nothing automated detects or prevents this.

**Severity: downgraded from *live chart-of-accounts corruption* to *an unguarded mechanism
with the damage already reversed*.** DATABASE VERIFIED, confidence VERIFIED.

*This is why the lead does not accept an agent's conclusion unchecked. The agent's code
reading was right, its data claim was stale, and the difference is between "the books are
wrong" and "the books are clean but the guard rail is missing" — different urgency,
different owner, different fix.*

---

## V16 — `posted.log` vs the target: the 2-document gap, resolved (benign)
The resume ledger and the target database appeared to disagree. Since `posted.log` is what
makes an interrupted 11,000-bill run survivable, a document it *wrongly* believes posted is
one that will never be retried — so this was worth closing rather than rounding away.

| measure | value |
|---|---:|
| lines in `posted.log` | 9,378 |
| distinct bill ids in it | 9,371 |
| duplicate document keys in it | **0** |
| live bills in the target | 9,377 |
| **in `posted.log` but NOT live in the target** | **0** |
| live in the target but not in `posted.log` | 6 |

**Nothing is phantom and nothing is duplicated** — the dangerous directions are both clean.

The 7-line difference is 7 records ending `||preexisting` — bills the loader found already
present rather than creating, so it never learned their id:
```
JOBW308|232||preexisting          SELD403|103326913568||preexisting
OTHL209|KA-B1-157425684||preexisting   SELD403|103326940170||preexisting
MNTL523|ST2273/25-26||preexisting  MNTL523|ST2280/25-26||preexisting
ACCI043|C/25-26/45186||preexisting
```
Five of those account for five of the six "untracked" bills; the sixth is
`SMOKE/2026/1` (₹13,285.86), a smoke test, already `REVOKED`.

**Verdict: `posted.log` is sound.** DATABASE VERIFIED, confidence VERIFIED.

**One caveat stands**, raised by the script auditor and worth carrying into the
reconciliation design: a `||preexisting` line is counted as **posted** by
`failure_report.py:55`, as **not posted** by `value_recon.py:326` and `reconcile.py:87`, and
as **both** by `reconcile.py:123`. Seven documents is immaterial today; the inconsistency is
not, because it will scale with any future run that adopts existing records.

---

## V17 — Bills with NO accounting at all: 2 ACTIVE, and my V8 framing needed refining
The script auditor reported *"2 live bills with zero voucher legs"* via the `preexisting`
path. I checked the whole org rather than that path alone:

```sql
SELECT b.id, b.billNumber, b.billStatus, ROUND(b.billAmount,2) amt
FROM bill b WHERE b.organisationId=<org> AND b.isDeleted=0
  AND NOT EXISTS (SELECT 1 FROM voucherEntry v
                  WHERE v.referenceId=b.id AND v.organisationId=<org> AND v.isDeleted=0);
```

| bill | status | amount (₹) | vendor |
|---|---|---:|---|
| `SMOKE/2026/1` | REVOKED | 13,285.86 | smoke test |
| `KA-B1-157425684` | **ACTIVE** | **1,709.82** | OTHL209 |
| `103326913568` | **ACTIVE** | **1,978.01** | SELD403 |

**Exactly 2 ACTIVE bills carry no accounting entry whatsoever — ₹3,687.83.** CONFIRMED.
DATABASE VERIFIED, confidence VERIFIED.

### This refines V8 rather than contradicting it

The two are different populations, and it matters that they are not conflated:

| population | count | have vouchers? | meaning |
|---|---:|---|---|
| lines tagged `\|\|UNVERIFIED` | 7 | **all 7 do** (4–22 legs each) | readback did not complete; the accounting is fine |
| lines tagged `\|\|preexisting` | 7 | **2 do not** | the loader adopted a bill it did not create and never verified it |

So V8 stands — the failure report's `unverified_no_voucher` bucket does *not* mean "zero
accounting", and its docstring overstates that risk. But **a genuinely accounting-less bill
does exist**, and it arrives through the *other* tag, which no report watches.

### Why the mechanism matters more than the ₹3,687.83

The loader treats `"Bill number already exists"` as benign — correctly, since it usually
means the document is already posted. It writes `||preexisting` and moves on **without
verifying**. The repair path that would fix an unverified bill matches on `UNVERIFIED`, not
on `preexisting`, so these documents are **unreachable by any existing repair** and will stay
that way.

The mechanism fires on **every interrupted run** — and interruptions are normal here, given
that the Sage host is a Wi-Fi laptop whose address has moved four times in five days. Today's
exposure is trivial; the exposure scales with the number of interruptions, and nothing
detects it.

**Compounding factor:** a `||preexisting` line is already counted three different ways by the
three reconcilers (posted / not-posted / both). A document that is invisible, unverified and
inconsistently counted is the exact shape of a defect that survives to production.

---

## V18 — The duplicate-document-number mechanism, verified in the backend source
The script auditor proposed that sending `value: None` would fix 54 of the 152 collisions.
I read the code rather than take that on trust, because it is the only *concrete code fix*
proposed anywhere in this audit and it touches statutory numbering.

**`CounterServiceImpl.java:311-330`** (`purchaseManagement`), the client-driven branch:

```java
int presentCounterValueInt = Integer.parseInt(counter.getValue());

if (presentCounterValueInt > counterValueInt) {
    log.info("update should not be possible");
    return CounterSeries.Builder.counterSeries()
        ...
        .withValue(counterValue)      // <-- returns the CALLER'S stale value
        ...
        .build();                     // <-- and does NOT persist it
}
counter.setValue(counterValue);
```

When a caller supplies a number **lower than** the counter's current value, the service:
1. logs `"update should not be possible"` — **a log line, not an exception**;
2. **returns the caller's stale number anyway**;
3. **does not advance the stored counter**.

So the bill is issued the stale number *and* the counter stays where it was — which means the
next caller can be issued the same number again. **BACKEND VERIFIED, confidence VERIFIED.**

And the caller makes it likely. `post_sage_bills.py:2478-2490` (`series_number`) does:
```python
st, body = api.get("/counter/series/values?series=...")
value = str(int(d["value"]) + 1) if d and d.get("value") is not None else "1"
```
— a **read-then-write with no reservation**. Two concurrent posts, or a retry after a timeout,
read the same value and both send `current + 1`. Nothing on either side detects the clash.

**Verdict: the mechanism is real and both halves contribute.** The server will not refuse a
duplicate, and the client cannot avoid proposing one.

**One caveat on the proposed fix.** Sending `value: None` only helps if the server allocates a
number when the field is absent — I did **not** verify that branch exists and behaves that
way. Before that fix is adopted, someone should confirm the server-allocated path, because
the failure mode if it does not exist is worse than today: a bill with no series number at
all. Reported, not implemented.
