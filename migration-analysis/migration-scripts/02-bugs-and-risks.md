# 02 — Bugs, Missing Cases, Data-Loss and Financial Risks

Companion to `01-script-audit.md`. Every entry carries `file:line`, a concrete failure scenario, a
measured blast radius where measurement was possible, and **whether it has already happened in live
data**.

**Evidence labels** — `SCRIPT VERIFIED`, `DATABASE VERIFIED`, `BACKEND VERIFIED`, `DOCUMENTED`,
`INFERRED`. **Confidence** — VERIFIED / HIGH / MEDIUM / LOW / UNKNOWN.
GSTINs masked to state code + 6th character. Org `1029113552088076445`.

## Summary

| severity | count | already in live data | armed, not yet fired |
|---|---:|---|---|
| **CRITICAL** | 4 | **2** — ₹87.3 M of expense misfiled (C-2); ₹1,044.3 M never migrated and invisible to every report (C-3) | **2** — ₹381.8 M FX understatement on the next `goods-post` (C-1); a one-command wipe of 9,376 bills (C-4) |
| **HIGH** | 15 | **9** — H-1, H-2, H-8, H-10, H-11, H-12, H-13, H-14, H-15 | 6 — H-3, H-4, H-5, H-6, H-7, H-9 |
| **MEDIUM** | 14 | 6 | 7 (M-10 resolved as *safe* by backend evidence) |
| **LOW** | 8 | 5 | 3 |

Four of the HIGH findings are **backend-side and invisible from the loader**: the tax head follows the
pincode-derived address rather than the GSTIN (H-12), all six backend tickets were reverted (H-13),
five payload keys are silently dropped (H-14), and the read-back's taxable check is vacuous (H-15).
None of them can be found by reading `post_sage_bills.py` alone.

---

# CRITICAL

## C-1 — The goods path posts foreign-currency documents at face value as INR

**`post_sage_bills.py:1605-1611` (`SQL_GOODS_HDR`), `:2692` (`conversionRate`/`currencyDto`)**
`DATABASE VERIFIED / VERIFIED` · **NOT YET IN LIVE DATA — armed for the next `goods-post` run**

`SQL_GOODS_HDR` selects `h.doc_total, h.tax_total` from `idedat_staging.sage_bill_hdr` with **only an
`inv_date` predicate — no currency filter**. That table carries `currency`, `fx_rate`, **and both a
source-currency total (`doc_total`) and a home-currency total (`ap_amount_hc`)**. The loader reads the
source-currency one and then hardcodes `"conversionRate": 1.0, "currencyDto": {"currency": "INR"}`.
`sage_goods_line.extended` is likewise in source currency, so `classify_goods`'s internal identity
holds and the document sails through every gate.

The AP-direct path does not have this bug — `SQL_HEADERS:346` filters `CODECURN='INR'` and
`AMTINVCHC` is genuinely the home-currency figure. The comment at `:2687-2690` that justifies INR is
**true for the AP path and false for the goods path**, and appears to be why the goods path was never
re-examined.

**Concrete scenario.** A USD purchase invoice for $12,000 at `fx_rate` 90.5064 has
`doc_total = 12000.00` and `ap_amount_hc = 1,086,076.80`. The loader posts a bill of **₹12,000**
against a Sage liability of **₹10.86 lakh**, with `conversionRate 1.0`, and every downstream check —
`assert_invariants`, `classify_goods`, and (were it called) `readback_drift` — agrees, because they all
compare the payload against `doc_total`.

**Blast radius, measured:**

```
non-INR goods documents in the Jan-Apr window                                   1,998
  ... USD 1,841   RMB 92   CNY 65
  ... already posted (present in work/posted.log)                                   0
  ... whose vendor ALREADY has a contact in the crosswalk (i.e. postable now)   1,468
        would post as INR      Rs   22,071,112.42
        true home-currency     Rs  403,866,335.94
        UNDERSTATEMENT         Rs  381,795,223.52
```

The remaining 530 become postable as soon as their vendors are built.

**Why it is armed now.** Commit `81ba90b` ("Post the international vendors") is exactly what made 1,468
of these contact-ready. `run_all.sh:157` runs `goods-post --all-categories` unconditionally.

**Fix direction (do not apply — reported only).** Either add `AND currency='INR'` to `SQL_GOODS_HDR`
and handle the rest separately, or select `ap_amount_hc`/`ap_tax_hc` and send the real `fx_rate` as
`conversionRate` with the true `currencyDto`. Whichever is chosen, `classify_goods` must be taught that
`sage_goods_line.extended` is in the *source* currency so the identity is checked on the same basis.

---

## C-2 — ₹87.3 million of expense is filed under the wrong head, in live data

**`post_sage_bills.py:1934-1966` (`item_ledger_for`), `:2015-2034` (the re-head branch)**
`DATABASE VERIFIED / VERIFIED` · **ALREADY HAPPENED**

`item_ledger_for` calls `POST /financeAccountReferenceMapping/item/getOrCreate/{mapping}` which, as the
docstring correctly records, **mints a separate ledger per reference type — it does not remap an
existing one.** When `ensure_products:2015-2034` later re-heads a product from the old
`ITEM_DIRECT_EXPENSE` default to `ITEM_IN_DIRECT_EXPENSE`, a correctly-headed ledger appears *beside*
the old one. **Bills already posted keep pointing at the old ledger**, and nothing revokes and reposts
them.

**Blast radius, measured** (resolved every `priorLedger` in `crosswalk_live.json` against
`financeAccount` and `billLineItem`):

```
products carrying a superseded priorLedger                        138
superseded ledgers still carrying a balance                        50   |netBalance| Rs 87,293,633.44
ACTIVE bill lines still pointing at a superseded ledger        16,829   taxable  Rs 87,296,758.72
```

Every one of the 50 sits under accountingGroupName **"Direct Expenses"** while its Sage account group
says indirect:

| account | ledger | filed under | balance |
|---|---|---|---:|
| `4E4SD04` | Freight Charges-Export | Direct Expenses | −21,651,747.35 |
| `4E4SD01` | Custom Clearance & Forwarding Charges | Direct Expenses | −17,703,293.92 |
| `4E5O014` | Professional Charges | Direct Expenses | −16,549,052.00 |
| `4E4SD03` | Carriage Outwards | Direct Expenses | −9,626,522.05 |
| `4E2ME14` | Freight Charges - Imports | Direct Expenses | −5,427,925.07 |
| `4E5O036` | Donation | Direct Expenses | −2,167,168.00 |
| `1L6TA07` | A/P Clearing - Accessories (a **balance-sheet** account in P&L) | Purchase Accounts | −21,726.98 |

**Consequence.** The gross-profit line is wrong. ₹8.73 crore of selling, administrative and finance
cost is presented as direct cost of goods. `work/master_recon.py` detects it (`ledger_orphan`, 50 high-
severity rows, identical total) and `RECON-BOTH-SITES.md:203` recorded the `1L6TA07` case three days
ago. **Nothing has repaired it, and no script in `work/` can** — `repost_stranded_parts.py` targets
amount shortfalls only.

**Aggravating factor.** `master_recon.py:587`'s `ledger_head` check — the one designed to catch this —
can only run on records carrying `itemMapping`. 197 GL pseudo-items carry one; **17,220 item products
do not**. 98.9% of the chart of accounts this migration minted is never head-checked at all.

---

## C-3 — ₹1,044 million of Sage AP is excluded in SQL and is invisible to every report

**`post_sage_bills.py:333-359` (`SQL_HEADERS`)** · `DATABASE VERIFIED / VERIFIED` ·
**ALREADY HAPPENED (as an omission)**

The purity filter runs **inside the SQL**, so excluded documents never enter `book`, never reach
`classify()`, and **cannot appear in `work/skipped.json` or `work/failures-report.json`** — the file
`run_all.sh:163` calls "anything still outstanding".

```
raw window (IDTRXTYPE=12, SRCEAPPL='AP', 20260101..20260430)  12,781 docs  Rs 1,763,950,260.97
  1. no APIBD distribution row                                 1,079       Rs   295,107,559.76
  2. non-INR currency                                             43       Rs   254,244,874.08
  3. CODETAXGRP in VAT/NRVAT/NRST/NRVATST                        118       Rs     5,669,280.00
  4. purity filter: a line outside 4E / 2A7T / 1L8TX14-16        285       Rs   489,297,175.70
  IN THE BOOK                                                 11,256       Rs   719,631,371.43
```

**1,525 documents worth ₹1,044,318,889.54 — 45% more money than the book contains.** I joined all
1,525 keys against `idedat_staging.sage_bill_hdr`: **zero are covered by the goods path either.**

**Concrete scenario.** A ₹4.9 crore AP invoice carrying one distribution line to a `1L` inventory head
alongside its `4E` expense lines is dropped by predicate 4. It appears in no log, no skip count and no
failure report. An operator reading `run_all.sh --check` concludes the migration is complete.

**Compounding reporting defects:**
- `work/failure_report.py:64-76` replays only `classify()` + the contact test, missing three of
  `eligible()`'s skip branches — a further **44 documents** (27 URP, 17 no-product) are absent
  (`skipped.json` totals 2,006; the report totals 1,962).
- `work/reconcile.py:57` reads raw `sage_ap_obl` with **none** of these filters and reports
  `sage_ap_direct: 12,781`, while `value_recon.py` reports 11,256 — the two headline reports disagree
  on the denominator by exactly this 1,525, and `README.md:79-80` presents them side by side.

**Currently unposted for other reasons** (measured): 2,006 of the 11,256 in-book bills, ₹225,779,248 —
1,952 blocked by a missing vendor contact (₹84,916,191) and 54 crosswalked but unposted (₹140.9 M, of
which **four `IDGR004` documents alone are ₹133.5 M**).

---

## C-4 — `work/cleanup_pilot.py` revokes and deletes every ACTIVE bill in the organisation

**`work/cleanup_pilot.py:14-21`** · `SCRIPT + DATABASE VERIFIED / VERIFIED` ·
**NOT YET RUN — one command away**

```python
# docstring:3-5 "Every target is listed explicitly - nothing is matched by pattern -
#                so this cannot reach past what it names."
for r in mysql("SELECT id FROM bill WHERE organisationId=%s AND isDeleted+0=0" % ORG):
    api.call("PUT", "/bill/updateStatus/%s/REVOKED?remarks=migration+cleanup" % bid)
    api.call("DELETE", "/bill/%s" % bid)
```

The docstring's guarantee is true of the three products and one contact it names. **It is false of the
bills**, which come from an unbounded `SELECT`. The script was written when the org held 8 pilot bills.

**Blast radius, measured:** the org now holds **9,376 ACTIVE bills worth ₹498,776,137.99**. There is no
`--apply` flag, no dry-run default, no count confirmation and no `pgrep` guard. `:42-46` additionally
clears `xw["products"]` (all 197 entries) with a non-atomic `json.dump` and renames `posted.log` to
`.superseded`, destroying the resume state in the same breath.

`RUNBOOK-JanApr-2026.md:166` records that it has not been run. It is still executable and still sits
next to fifteen other scripts an operator is expected to run by hand.

---

# HIGH

## H-1 — `"Bill number already exists"` is recorded as success without verifying the bill

**`post_sage_bills.py:3050-3057`** (and the identical `:3707-3711` on the goods path)
`DATABASE VERIFIED / VERIFIED` · **ALREADY HAPPENED — 2 live bills**

```python
if "Bill number already exists" in api.err(body):
    print("     already posted - recording and moving on")
    state.mark(tag, "preexisting")
    continue
```

A bill created by an earlier run that then **failed `verify`** is ACTIVE with zero voucher entries — a
payable that books nothing. When the loader retries the document, the create is refused as
pre-existing, the tag is written as `preexisting`, and the bill is never verified. The repair path at
`:3000-3013` matches only `prior.rstrip().endswith("UNVERIFIED")`, which `"preexisting"` does not.
**The document can never be fixed by any future run.**

`BACKEND VERIFIED / VERIFIED` — the loader's comment that the check is "scoped to org + contact + FY"
is exactly right. `BillServiceImpl.java:611-633` calls
`existsByOrganisationIdAndBillNumberAndContactIdAndBillDateBetweenAndBillStatusIn` with
`billStatuses = [ACTIVE, APPROVAL_PENDING, BLOCKED]`. Note `isDeleted` is **not** in the predicate.

**Blast radius, measured.** 7 `preexisting` tags in `posted.log`; resolving each against the database:

| billNumber | vendor | status | verification | voucher legs | billAmount |
|---|---|---|---|---:|---:|
| `KA-B1-157425684` | OTHL209 | ACTIVE | **PENDING_VERIFICATION** | **0** | 1,709.82 |
| `103326913568` | SELD403 | ACTIVE | **PENDING_VERIFICATION** | **0** | 1,978.01 |
| `232`, `ST2273/25-26`, `ST2280/25-26`, `103326940170`, `C/25-26/45186` | — | ACTIVE | VERIFIED | 3–13 | — |

₹3,687.83 of payables with zero accounting impact, permanently unrepairable by the loader. The amount
is small; **the mechanism is not** — every future `preexisting` lands the same way, and this is the
default outcome whenever a run is interrupted between `POST /bill/` and `state.mark`.

**Fix direction**: on `"Bill number already exists"`, look the bill up, verify it, and record the real
billId — the machinery already exists in the `UNVERIFIED` branch.

## H-2 — `billSeriesNumber` is not unique: 152 ACTIVE bills share one with another bill

**`post_sage_bills.py:86` (`SERIES_BY_FY`), `:2478-2491` (`series_number`)**
`DATABASE VERIFIED / VERIFIED` · **ALREADY HAPPENED**

```
ACTIVE bills                                          9,376
distinct billSeriesNumber (the concatenated string)   9,224    -> 152 bills share one
distinct (billSeriesPrefix, billSeriesValue)          9,322    ->  54 bills share one
cross-prefix collisions                                  98
```

**Cause 1 — the series names collide by construction.** `SERIES_BY_FY = {"2025-2026": "SAGE",
"2026-2027": "SAGE27"}`. `"SAGE27"` is a prefix-extension of `"SAGE"`, so the concatenation collides
across the financial-year boundary:

```
billSeriesNumber  SAGE2710  =  SAGE|2710  (FY2025-26)  +  SAGE27|10  (FY2026-27)
                  SAGE2711  =  SAGE|2711                +  SAGE27|11
                  ... 98 such pairs
```

The comment two lines above (`:84-85`) — "one series name serves ONE financial year: the counter
uniqueness key has no FY in it, so reusing a name across years collides" — shows the author reasoning
about exactly this class of problem and then choosing a colliding name.

**Cause 2 — the counter is read, not reserved, and fails open to `1`.**
```python
d = api.data(body) if api.ok(st, body) else None
value = str(int(d["value"]) + 1) if d and d.get("value") is not None else "1"
```
A throttled or failed `GET /counter/series/values` silently yields series value **1**. Combined with
the duplicate `counter` rows `phase_cleanup:2862-2870` detects but **cannot delete through the API**
("the API resolves one of them; series values will continue from it"), this produced **54 within-`SAGE`
duplicates** — 7,964 distinct values across 8,018 bills.

**Root cause confirmed in the backend.** `BACKEND VERIFIED / VERIFIED` —
`smeassist/purchaseManagement/src/main/java/com/assist/purchaseManagement/service/impl/CounterServiceImpl.java:234-345`:

```java
public CounterSeries updateCounter(..., String counterValue, ...) {
    if (ObjectUtils.isBlankObject(counterValue)) {
        return updateCounterWithOutValue(...);   // server reads, increments, SAVES  <- atomic
    } else {
        return updateCounterWithValue(...);      // server takes the CLIENT's number
    }
}

// updateCounterWithValue, :311-330
if (presentCounterValueInt > counterValueInt) {
    log.info("update should not be possible");
    return ...withValue(counterValue)...;        // returns the client's value, does NOT save
}
counter.setValue(counterValue);                  // equal or higher: accepted verbatim
```

The whole create runs under `@RedisLock(BILL_CREATION_ORG_ID + organisationId)`
(`BillServiceImpl.java:635-640`), so `updateCounterWithOutValue` is collision-free by construction.
**`series_number:2478-2491` sends an explicit `value`, which takes the client-driven branch**, and the
server accepts a stale or equal number without complaint — it merely logs "update should not be
possible" and stamps it anyway. Sending `"value": None` would hand allocation to the server and remove
this defect entirely.

**Consequence.** The series number is the document's own reference in SMEAssist. Two bills sharing one
breaks any downstream export or audit trail keyed on it, and `voucherNumber == billNumber` (DOD query
7) does not test it.

**Aggravated by `work/fix_qty_unitprice.py:54,75`**: it calls `build_payload`, which unconditionally
re-derives a series number, and then only ever `PUT`s — so the counter never advances and **every bill
in one repair run is sent the identical series value.**

## H-3 — `KeyError` on a zero-rated line's product aborts the entire phase

**`post_sage_bills.py:2604`; missed guards at `:2781-2782` (`eligible`) and `:3662-3663`
(`phase_goods`); root cause at `:3546`** · `SCRIPT VERIFIED / HIGH` ·
**not observed in the logs; latent**

```python
for z in shape.get("zero", ()):
    pr = products[z["gl"]]        # <- bare subscript, no guard
```

`eligible:2781` checks product presence **only over `shape["exp"]`**. The round-off (`4E1M016`) and the
import-IGST pass-through (`2A7TX04`) arrive as `zero` lines and are never checked. If either product is
HELD (`:1999-2003`) or BURNED (`:2100-2106`), the lookup raises `KeyError`, which `__main__:3936-3943`
does not catch — it handles only `Stop` and `KeyboardInterrupt`. The process exits with a traceback,
`run_all.sh:150` sees a non-zero rc and **aborts the whole migration**; every phase after it is skipped.

**The goods path can reach this without any hold at all.** `phase_goods_masters:3546` calls
`ensure_products` with `{s(l["gl"]) for _, sh in scope for l in sh["exp"]}` — **`ROUNDOFF_GL` is not in
that set**, while `classify_goods:1818-1821` emits a `4E1M016` zero line whenever the residual is
non-zero. The goods masters phase therefore **never creates `SAGE-4E1M016`.** It works today only
because the AP `masters` phase happens to create it first (`main():3915-3917` includes zero-line GLs).
`./run_all.sh --from goods-masters` on a fresh crosswalk crashes.

`DATABASE VERIFIED`: `4E1M016` and `2A7TX04` are both present in the current crosswalk, so the
condition is not met right now.

## H-4 — `work/repost_po_items.py` deletes a bill whose revoke failed

**`work/repost_po_items.py:95-102`** · `SCRIPT VERIFIED / VERIFIED` ·
**not observed; the script has run at least once**

```python
st,  b  = live.call("PUT", "/bill/updateStatus/%s/REVOKED?remarks=repost+with+Sage+item+detail" % bid)
st2, b2 = live.call("DELETE", "/bill/%s" % bid)      # runs unconditionally
...
if live.ok(st, b):
    done.add(tag)                                     # tag dropped only if the REVOKE succeeded
```

Revoke fails, delete succeeds → **the bill is gone from SMEAssist while its tag stays in `posted.log`**
→ `post` skips it forever → **the document is permanently lost.** This is verbatim the failure mode
that `work/repost_stranded_parts.py:182-189` documents as fixed *there*; the fix was never back-ported.

It also has **no rebuild gate at all** — no `classify()`, no `build_payload()`, no
`assert_invariants()`, no product/contact/ledger check — where its sibling refuses to revoke anything
it cannot rebuild to the paise. And it takes no backup of `posted.log`, where the sibling does.

**Blast radius**: 16 named documents (`work/repost_needed_po_items.txt`). Also
`:77` `posted[ln.split("||")[0]] = ln.split("||")[-1]` takes the **last** field, so a
`tag||<bid>||UNVERIFIED` line yields the literal string `"UNVERIFIED"`, which is then used as the
billId in the URL.

## H-5 — The goods path is missing four of the AP path's safeguards

**`post_sage_bills.py:3611-3623` (`eligible_goods`), `:3720-3722`, `:1825`, `:1733`**
`SCRIPT VERIFIED / VERIFIED` · goods bills posted so far: **128 ACTIVE**

The goods population is the larger of the two (14,603 Sage documents against 11,256) and carries the
fewest checks:

| safeguard | AP path | goods path |
|---|---|---|
| read-back against the server | `readback_drift` at `:3077-3080` | **absent** — `:3720` goes from `VERIFIED` straight to `state.mark` |
| URP vendor with forward-charge GST is held | `eligible:2773-2780` | **absent** — `eligible_goods` checks only that a contact exists |
| RCM detected from Sage's `1L8TX` booking | `classify:1407-1410` | **absent** — `classify_goods:1825` hardcodes `"is_rcm": False`; `:1733` merely *filters out* `1L8TX*` heads. (0 goods documents carry one in this window) |
| currency | `CODECURN='INR'` at `:346` | **absent** — see C-1 |

**Blast radius, measured** (`DATABASE VERIFIED / VERIFIED`):

```
goods documents in the window from a vendor the crosswalk records WITHOUT_PAN_OR_GST,
carrying forward-charge tax  (the AP path would HOLD every one of these)
        documents  18
        doc_total  Rs 1,550,109.78
        tax        Rs    73,814.78     <- input credit claimed against a URP ledger
        already posted                    0     -> armed, not yet fired

goods documents carrying an RCM (1L8TX14/15/16) distribution
        documents   0                         -> latent design gap, no exposure today
```

So the URP gap is a live ₹73,814.78 of misclaimed input credit waiting on the next `goods-post`; the
missing RCM detection has **no exposure in this window** but is a real hole for any future one. The
absent read-back applies to the whole goods population unconditionally — the check that exists
*specifically because the server overrides `gstAmount` and `roundOffAmount`* runs on one of the two
posting paths only, and the other one is the path with C-1 in it.

## H-6 — `phase_cleanup`'s contact sweep is unbounded — and inert, because the route does not exist

**`post_sage_bills.py:2843-2852`** · `SCRIPT + BACKEND VERIFIED / VERIFIED` · **cannot fire**

```python
for c in mysql("SELECT id, accountName FROM contact WHERE organisationId=%s AND isDeleted+0=0" % ORG_ID, …):
    if state.xw["contacts"] and any(v.get("contactId") == c["id"] for v in state.xw["contacts"].values()):
        continue
    api.call("DELETE", "/contact/%s" % c["id"])
```

The `state.xw["contacts"] and …` guard is a **truthiness** test: an empty or truncated crosswalk makes
the whole condition false and the code attempts to delete **every contact in the org** (537 exist, 353
carry bills).

**It would fail.** `BACKEND VERIFIED / VERIFIED`: `ContactController.java` (520 lines) contains **no
`@DeleteMapping` at all** — `DELETE /contact/{id}` is not a route. The sweep has always been a no-op
that prints failure messages nobody reads. `DELETE /bill/{id}` is likewise always refused after a
revoke (`BillServiceImpl.java:1647-1649` allows deletion only from `APPROVAL_REJECTED`), so the SMOKE
bills the phase claims to delete are merely REVOKED.

**Kept at HIGH, with the severity moved**: the danger is not the deletion, it is that
`cleanup` reports success for three operations, two of which it cannot perform. The same non-existent
route makes the uncommitted orphan-contact rollback in §G.5 of file 01 a no-op, and
`ContactRepository.java:51-54` ignores status with no `DELETED` state — so **a contact created against
the wrong GSTIN blocks that GSTIN permanently through the public API**, with no way to remove it.

The crosswalk is a single gitignored JSON file (§F.7) written by `State.save:925` with `os.replace` but
**no `fsync` of the temp file or the directory**, and by `work/fix_misnamed_products.py:30` and
`work/cleanup_pilot.py:44` with a plain truncate-and-write.

## H-7 — The crosswalk is a single point of failure; the durable copy was never written

**`post_sage_bills.py:245` (`CROSSWALK`), `:912-946` (`State`); `idedat_staging.crosswalk`**
`DATABASE VERIFIED / VERIFIED` · **ALREADY TRUE**

`idedat_staging.crosswalk` exists with a proper schema (`entity_type, source_key, target_id,
target_code, channel, run_id, created_at`, PK on `entity_type, source_key`) and holds **0 rows**.
`grep` finds no write path to it anywhere in the codebase.

**17,947 identity mappings** — 17,220 item products, 455 contacts, 197 GL pseudo-items and every
associated `financeAccountId` — exist only in `work/crosswalk_live.json` on one laptop, gitignored,
with four hand-made backups beside it (evidence it has been repaired by hand at least twice).

Losing it means every master is re-created and collides; adoption then depends on `product_index`
paging 17,886 rows through a rate-limited API; contacts are refused on GSTIN and can be reused only via
the *sibling* crosswalk entry that held their `addressId` (`:2419-2426`) — which would also be gone;
and `phase_cleanup` becomes destructive (H-6).

## H-8 — `work/reconcile.py` reports false-clean on the very thing it exists to check

**`work/reconcile.py:57-59, 110, 113, 99-102`** · `SCRIPT + ARTEFACT VERIFIED / VERIFIED` ·
**ALREADY HAPPENED — this is the report `run_all.sh --check` prints**

Four independent defects in the default reconciliation:

1. **Wrong population.** `:57` reads raw `idedat_staging.sage_ap_obl WHERE trx_type=12 AND
   srce_appl='AP'` with **no currency filter, no tax-group filter and no distribution-head filter** →
   `sage_ap_direct: 12,781` against `value_recon`'s 11,256. `in_sage_not_posted` reads **18,006** here
   and **16,488** there.
2. **`elif txb == sg: rcmok += 1` (`:113`) is a false-clean.** It counts *any* bill whose taxable
   equals Sage's gross as "RCM ok" — including a **forward-charge** bill that posted with its GST
   dropped entirely. **901 documents** sit in that bucket unexamined.
3. **No tolerance.** `:110` compares exactly, with no allowance for Sage's per-authority tax
   truncation → **375 "mismatches" of which ~362 are noise**, all flagged `<-- INVESTIGATE`.
4. **The SMEAssist side is built only from `posted.log`** (`:99-102`), so `in_smeassist_not_in_sage` is
   structurally **0** while `value_recon` finds 5.

It also uses `mysql --raw` (`:34`), which both siblings explicitly forbid because descriptions carry
newlines and tabs that split one row across several lines. It is currently lucky in its column
selection.

## H-9 — `work/find_sage.py` rewrites `.env` non-atomically, and now does so on every preflight

**`work/find_sage.py:226-235`; invoked twice by `run_all.sh:82-83` (uncommitted)**
`SCRIPT VERIFIED / VERIFIED` · **not observed; a `.env.bak-20260903-170301` exists on disk**

```python
txt = open(ENV_PATH).read()
new, n = re.subn(r"(?m)^SQL_HOST=.*$", "SQL_HOST=" + host, txt)
open(ENV_PATH, "w").write(new)          # truncate-then-write, no temp file, no backup
```

A crash, a full disk or a `SIGKILL` between truncate and write leaves `.env` empty or half-written,
**losing `SQL_PASSWORD` and `SME_TOKEN`**. `State.save:925` and both `repost_*` scripts use
tmp + `os.replace`; this one does not. The uncommitted `run_all.sh` change promotes this from an
occasional manual repair to **a write on every single run**, executed up to twice.

Also `:234-235`: if `.env` has no `SQL_HOST=` line, it logs "not modified" and still **exits 0**, after
which `run_all.sh:84` re-reads the old value while reporting relocation succeeded.

**Related (MEDIUM):** `is_sage:133-163` offers the real `SQL_USER`/`SQL_PASSWORD` to **every host
answering on the SQL port** across up to 4,096 addresses — and its own docstring notes there is a
second, foreign SQL Server on this network. The migration credentials are sprayed across the office
subnet twice per preflight.

## H-10 — The serial item-product path silently produces more unflagged `9999` placeholders

**`post_sage_bills.py:3489` vs `:3288`; `:3495-3499` vs `:3287-3296`**
`SCRIPT VERIFIED / VERIFIED` · **contributes to the 1,419 already in live data**

Two divergences between the serial `ensure_item_products:3414` and the parallel
`ensure_item_products_parallel:3303`, which build the *same* products:

```python
# parallel, via _item_payload:3276
_hsn, _hsn_src = resolve_item_hsn(it, by_item)          # sibling tier available
"hsnSource": _hsn_src, "hsnIsDefault": "true" if _hsn_src == "DEFAULT" else "false"

# serial, :3489
_hsn, _hsn_src = resolve_item_hsn(it)                   # by_item omitted -> sibling tier LOST
"metaData": {..., "hsnMissing": ..., "migrationSource": "IDEDAT"}   # no hsnSource, no hsnIsDefault
```

The serial path is taken whenever `not args.all_items and len(reps) <= 40` (`:3603-3607`).
`phase_goods_masters:3577-3582` builds `by_item` from every line in the goods book and it "resolves 135
items before the master lookup is needed" — the serial path forfeits all of them.

Worse, a `9999` product created serially carries **no `hsnIsDefault` flag**, so the entire premise of
`GOODS_HSN_DEFAULT` — "a visible placeholder can be found and corrected later" (`:236-239`) — does not
hold for those rows. `FINDINGS-EXPENSE-SAC.md:39-55` separately claims the server drops `metaData`
altogether, which would make **all** 1,419 unflagged; that claim is `DOCUMENTED` and was not verified
here.

`DATABASE VERIFIED`: **1,419** products carry `hsnCode='9999'`; only 8 are referenced by any bill line
(12 lines, ₹1,114,196.10); `billLineItem.hsn = '9999'` on **0** lines.

## H-11 — `POST /product/` silently discards `metaData`, so every product's provenance is lost

**`post_sage_bills.py:2085-2086`, `:3287-3296`, `:3495-3499`** · `DATABASE VERIFIED / VERIFIED` ·
**ALREADY HAPPENED — all 17,886 products**

The loader stamps `metaData` on every master it creates and relies on it in three separate places:
`{"sageAccount": acct, "migrationSource": "IDEDAT", "hsnIsDefault": "true"}` on GL pseudo-items
(`:2085`), and `{"sageItem", "sageItemFmt", "sageCategory", "sageUnit", "hsnMissing", "hsnSource",
"hsnIsDefault", "migrationSource"}` on item products (`:3287-3296`). The `9999` placeholder's entire
justification rests on it — `:236-239`: *"Flagged hsnIsDefault in metaData exactly like EXPENSE_SAC."*

**The product endpoint does not store it — the DTO field is named `meta`, not `metaData`**
(`ProductCreateUpdateDto.java:82`), and Jackson's `FAIL_ON_UNKNOWN_PROPERTIES` is at the Spring Boot
default of `false`, so the key is dropped without a murmur. `BACKEND + DATABASE VERIFIED`:

```sql
-- the column is `meta`, not `metaData`
SELECT COUNT(*) products, SUM(meta IS NULL OR meta='') empty, SUM(meta IS NOT NULL AND meta<>'') set_
  FROM product WHERE organisationId=<org>;
-- 17,886 | 17,875 empty | 11 set

SELECT COUNT(*) FROM product WHERE organisationId=<org> AND hsnCode='9999'
  AND meta IS NOT NULL AND meta<>'';
-- 0   (of 1,419)
```

Only **11 of 17,886** products carry any `meta` at all, and **not one of the 1,419 placeholder-HSN
products does.** For contrast, every other endpoint the loader uses *does* persist it:

| table | rows for this org | metadata populated |
|---|---:|---:|
| `bill.metadata` | 10,769 | **10,769** |
| `billLineItem.metaData` | 36,459 | **36,459** |
| `contact.metaData` | 537 | **537** |
| **`product.meta`** | **17,886** | **11** |

**Consequences.**
1. The 1,419 placeholder HSNs are **not flagged in the database**. They are findable only because
   `9999` is not a plausible HSN — which is why choosing a visible placeholder over a plausible one
   (`:236-239`) turned out to matter far more than the author knew.
2. `sageAccount`, `sageItem`, `sageItemFmt`, `sageCategory`, `sageUnit`, `hsnSource`, `hsnMissing` and
   `migrationSource` are all gone. **There is no way to tell from the database which products this
   migration created**, except by the `SAGE-` SKU prefix — which is exactly why
   `work/master_recon.py` has to reconstruct the mapping by re-reading Sage.
3. `work/build_item_categories.py:57-62` deliberately types 6 capital items (`1COMPU`, `1SOFTW`,
   `1FURNI`) as `STORES_AND_SPARES`/`SERVICE` rather than `ASSET`, calling it "a COMPROMISE, not a
   correct classification… flagged per product in metaData". **That flag does not exist either.**

`work/FINDINGS-EXPENSE-SAC.md:39-55` reported this on 2 Sep and recommended the only durable place
being `description` or a file on our side. The recommendation was not acted on and the code still
depends on the flag. `DOCUMENTED → now DATABASE VERIFIED`.

## H-12 — The vendor's stored address state comes from the PINCODE, and the ledger follows it

**`post_sage_bills.py:2385-2392` (payload), `:2141-2152` + `:2380` (the dead guard)**
`BACKEND + DATABASE VERIFIED / VERIFIED` · **ALREADY HAPPENED — 4 bills, ₹19,379.70**

`POST /contact/address/create` takes `OrgAddressMinUpsertDto`
(`yoda/commons/.../OrgAddressMinUpsertDto.java`), which **has no `city`, no `state` and no
`primaryAddress` field.** All three are dropped. `OrgAddressToAddressUpsertDtoConverter.java:32-73`
then derives them from the pincode: `geoService.getGeoDetails(pinCode, country)` →
`.withCity(...).withState(...)`.

The bill flag and the accounting then read different sources:

| consumer | reads |
|---|---|
| `bill.isInterState` | the **payload's** address state (GSTIN-derived) — `BillCreateConverter.java:38-40` |
| the **voucher's** CGST/SGST-vs-IGST split | the **stored** address, re-fetched by id (pincode-derived) — `EntityVoucherEntryCreateHelperService.java:3057-3065`, split at `:3078-3104` |

**The loader's guard against this is dead code.** `pincode_state:2141` calls
`GET /address/pincode/{pin}`. The real route is `GET /api/v1/pincode/{pincode}`
(`yoda/web/.../geo/PincodeController.java:25,34`). It always 404s, so `pincode_state` always returns
`None` and the hold at `:2255-2258` — whose comment reads *"posting would swap IGST for CGST+SGST"* —
**has never once fired.** The loader's later note (`:2237-2244`) spotted the 404 and concluded the
pincode "decides nothing here". It decides the stored state, and the stored state decides the tax head.

**Blast radius, measured** (`DATABASE VERIFIED`), comparing voucher legs against the flag:

```
isInterState = TRUE   5,961 bills : 5,926 IGST legs, 0 CGST+SGST      -> consistent
isInterState = FALSE  3,413 bills : 2,950 CGST+SGST, 129 with an IGST leg
   of those 129: 125 are the PASSTHRU_GL 0% ledger "IGST Input (Import) @ 0.00%"  -> by design
                   4 are a real split on "IGST Input @ 18.00 %"
```

Those 4 are **one vendor, GSTIN `29XXXPX1133X1ZV`** — state code 29, Karnataka, therefore intra-state —
whose Sage pincode is `110065`, so the address was filed in **DELHI** and **₹19,379.70 of GST was
booked as IGST instead of CGST + SGST**. The bill says `isInterState = false`; the voucher says
otherwise. A sweep of all 322 GST contacts finds **exactly one** such vendor, because
`STATE_HEAD_PINCODE` round-trips correctly (it is the head office *of the already-proven state*) and
`resolve_state` holds anything it cannot prove. The exposure is small today and grows with every
vendor whose Sage pincode belongs to a different state than its GSTIN.

## H-13 — All six backend tickets were reverted, and two of them cannot be restored

**`/home/namansharma/Desktop/PROJECTS/smeassist`, HEAD `2785b4c3a3`**
`BACKEND VERIFIED / VERIFIED` · **ALREADY TRUE**

HEAD is `2785b4c3a3 "#12425324 - Do not merge: revert the Sage migration backend changes"`, reverting
all 32 files. `sage-migration-tickets.md` and `sage-migration-backend-changes.patch` are **untracked**.
Every ticket symbol greps empty in live source.

**Ticket 2's absence is the live root cause of the burned SKUs.** `ProductServiceImpl.java:228` still
saves the product and *then* validates at `:231-235`, and `ProductRepository.java:73-74`'s SKU lookup
**omits `isDeleted = false`** — unlike every neighbouring query. A product that failed validation after
insertion therefore keeps its SKU forever and is unreachable through the `@Where(isDeleted=0)` API.
That is precisely "the row adoption cannot see" at `post_sage_bills.py:2100-2106`, and it is why the
2 genuine burns (`SAGE-4E2ME02-14`, `SAGE-4E5O024-01`) are permanent.

**Ticket 5's absence is a tax-return risk**: the org flag
`MIGRATION_REVERSE_INPUT_GST_ON_PURCHASE_DEBIT_NOTE` does not exist, so migrated purchase debit notes
take the standalone output-tax path, contradicting FY2026 returns already filed.

**Tickets 2 and 3 are not in the saved patch either** — it declares 20 modified + 6 new files against a
32-file revert and omits `ContactServiceImpl`, `ProductServiceImpl`, `ProductController`,
`EntityFeature`, `EntityName` and `VoucherCreationListener`. They exist only in commit `2f70c19da3`.

## H-14 — Five payload keys are silently dropped by the backend

**`post_sage_bills.py:2083`, `:2077`, `:2085`, `:2677-2679`, `:2385-2392`**
`BACKEND VERIFIED / VERIFIED` · **ALREADY HAPPENED**

Jackson `FAIL_ON_UNKNOWN_PROPERTIES` is at the Spring Boot default of `false` in both repos — no
`spring.jackson.*` config, no `ObjectMapper` bean, no converter override. Unrecognised keys vanish
without a 400.

| sent | endpoint | reason | consequence |
|---|---|---|---|
| `metaData` | `POST /product/` | field is **`meta`** | H-11 — all product provenance lost |
| `isManageInventory` | `POST /product/` | **no such field** on the DTO | inventory management not actually disabled on 17,886 products |
| `isBulkUpload` | `POST /product/` | Lombok emits `setBulkUpload`, so the JSON property is **`bulkUpload`** | `ProductServiceImpl.java:209-226` takes the `isFalse(isBulkUpload())` branch and runs the item-approval engine — a product can be saved `APPROVAL_PENDING`, not `ACTIVE` |
| `originCountry`, `destinationCountry` | `POST /bill/` | fields are **`originCountryDto`/`destinationCountryDto`** (`BillCreateDto.java:145,147`) | every international bill stores both as **NULL** — the country-of-origin work in §E.7 does not land |
| `city`, `state`, `primaryAddress` | `POST /contact/address/create` | not fields on the DTO | H-12 |

`itemDiscount` on a line is likewise not a field on `BillLineItemCreateUpdateDto` (it exists only on
the bulk-upload DTOs) — harmless, the loader sends 0.

**No bean validation runs on `POST /bill/` or `POST /contact/`**: neither controller carries `@Valid`
and neither service is `@Validated` with a `MethodValidationPostProcessor` present, so every `@NotNull`
/ `@NotBlank` / `@Email` / `@Pincode` on those DTOs is inert. Failures surface later as NPEs or
`CustomException`s, **all returned as HTTP 500** with an `errorMessage`
(`ExceptionHandlingController.java:65-133`) — which makes `Api.call:872-876`'s rule of retrying a 500
only when the message is empty load-bearing rather than merely tidy.

## H-15 — `readback_drift`'s taxable check compares the loader's number to itself

**`post_sage_bills.py:2933-2935`** · `BACKEND VERIFIED / VERIFIED` · **the check has never worked**

Bill-level `taxableAmount` is the **one** total the server does not recompute:
`BillCreateConverter.java:83` stores `source.getTaxableAmount()` verbatim and nothing compares it to
the lines. The payload's value is `float(shape["taxable_all"])` (`build_payload:2668`), so
`if got_txbl != want_txbl` compares that number to itself and can only fail on float round-tripping.
The comment above it — *"The server stores exactly SUM(line taxableAmount) and does not round it"* — is
a misreading of the evidence.

**Why this matters.** The server recomputes each *line's* taxable as `qty × unitPrice` at **6 dp**
(`BillLineItemConvertor.java:208-223`, `BigDecimalUtils` HALF_UP at 6), while
`assert_invariants:2726-2731` ties only `q2(unitPrice × qty)` at 2 dp — a divergence the loader
deliberately permits, since it stores full-precision unit costs. So `SUM(line.taxableAmount)` can
legitimately differ from the stored bill `taxableAmount`, and **nothing anywhere checks it.** The other
three readback checks (`gstAmount`, `billAmount`, the RCM identity) are genuine — those totals really
are server-recomputed.

---

# MEDIUM

## M-1 — `pincode_state` is *also* fail-open (superseded by H-12, which is the bigger half)
**`post_sage_bills.py:2141-2152`, used at `:2380`** · `SCRIPT VERIFIED / VERIFIED` · **never fires at all**
```python
st, body = api.get("/address/pincode/%s" % pincode)
d = api.data(body)          # any non-2xx (including a throttled 403) -> None
...
platform_state = None if international else pincode_state(api, pin)
if platform_state and platform_state != st_name:      # skipped entirely when None
```
The check whose comment reads "a disagreement is caught before it silently swaps IGST for CGST+SGST"
is skipped, with no log line, whenever the lookup fails. Under sustained rate limiting — 2,816 backoff
events in one run log — it would be absent anyway.

**But it never runs even on the happy path**: the route it calls does not exist (H-12), so
`api.data()` yields nothing on *every* call and `platform_state` is always `None`. Recorded separately
from H-12 because the fail-open shape would still be wrong once the route is corrected — a lookup that
did not complete must not be read as agreement, exactly as `LookupFailed:302` establishes elsewhere in
this same file.

## M-2 — `POST /taxation` and `PATCH /product/{id}/ACTIVE` results are discarded
**`post_sage_bills.py:2431` (`taxation`), `:2115`, `:3374`, `:3508` (`PATCH …/ACTIVE`)**
`SCRIPT VERIFIED / VERIFIED` · unmeasured
The taxation row's own comment says "Nothing else creates this row and its absence surfaces much later,
from the BILL module" — and the call's return value is thrown away. A failed taxation row records the
contact in the crosswalk as complete; the failure appears as an unexplained bill rejection later.
Likewise a failed activation leaves a product with a null `itemStatus`, invisible to every later
lookup — the exact failure mode `:2078-2081` documents for the `status` vs `itemStatus` field name.

## M-3 — `work/probe_stock_types_all.py` leaks live products; `work/revive_probe.py` mutates a real one
**`work/probe_stock_types_all.py:80`; `work/revive_probe.py:5-16`** · `SCRIPT VERIFIED / VERIFIED` ·
**ALREADY HAPPENED — `work/cleanup_probes.py` exists to clean it up**
`probe_stock_types_all.py:80` calls `m.item_ledger_for(api, pid)` — `mapping` became a required
parameter with no default (`post_sage_bills.py:1934-1941`), so it raises `TypeError` **after creating
9 live products and before its cleanup loop at `:89-91`.** `work/cleanup_probes.py` disables 14 leaked
ledgers and deletes 14 reference mappings, and its `KEEP` assertion guards `LEDGERS` but **not
`MAPPINGS`**.
`revive_probe.py:6-16` issues four mutation attempts (`PATCH /product/{id}/ACTIVE`, `PUT /product/`,
`PUT /product/{id}`, `POST /product/update`) against a **hardcoded real production product id**, with
`typeOfStock: "CHARGE"` and `hsnCode: 996719`, with no dry-run and no confirmation.

## M-4 — The `burned` list is inverted: 75 false positives, 2 true positives missing
**`post_sage_bills.py:2103-2105`, `:3396-3398`; `work/failure_report.py:107-108`**
`ARTEFACT VERIFIED / HIGH` · **ALREADY HAPPENED**
All **75** current `burned` entries also have a live `items` entry carrying the identical `skuCode`
(e.g. `ID41681A-TG01-412|NOS → SAGE-ID41681ATG01412-NOS`, productId `1545312590443347968`, with a
ledger). `failures-report.json` reports `burned_skus: 75` as a blocker; **none of them blocks
anything.** Meanwhile the **2 genuinely unrecoverable SKUs** — `SAGE-4E2ME02-14` and
`SAGE-4E5O024-01`, soft-deleted by `work/fix_misnamed_products.py` — are **absent from the list** and
still block the 17 documents `skipped.json` counts as "no product for GL account".

## M-5 — `extract.sql`'s `gl_accounts` block cannot serve the Sage-down fallback
**`extract.sql:275` vs `post_sage_bills.py:405-410`** · `DATABASE VERIFIED / VERIFIED` ·
latent (fires only when Sage is unreachable)
`extract.sql` filters `LEFT(RTRIM(ACCTID),2)='4E'` while `SQL_GL` filters `ACCTFMTTD` **and** `2A7T`.
The CSV therefore holds 1,029 rows against Sage's 1,039 and is **missing all 10 `2A7T` accounts**.
When Sage is down, `gl_groups:1064-1074` and `gl_names:1133-1138` fall back to that CSV and **cannot
resolve 4,479 `bills_lines` rows across 7 accounts** (`2A7TX01` 1,055, `2A7TX02` 1,054, `1L8TX14` 987,
`1L8TX15` 986, `2A7TX04` 171, `2A7TX03` 120, `1L8TX16` 106). `2A7T` is a tolerated head in the purity
filter, so those bills are in scope by design. The file's own header (`:261-262`) warns that
`ACCTFMTTD` is the join key and then filters on `ACCTID` anyway — the exact trap
`post_sage_bills.py:401-404` documents.

## M-6 — `extract.sql`'s validation target is a known-wrong number
**`extract.sql:34`** · `DATABASE VERIFIED / VERIFIED` · **misleads any re-run**
"Expected: 11,256 header rows / **46,385** line rows". 46,385 is the `wc -l` of the corrupt PSV,
inflated by exactly **1,206 tilde-less continuation fragments** from unstripped `TEXTDESC` newlines.
The truth is **45,179** — confirmed against live Sage today, and already recorded at
`MACHINE-CHANGES.md:190-196` ("Do not use the old row counts as validation targets"). Anyone validating
a re-extract against 46,385 will "find" 1,206 phantom missing rows.

## M-7 — `bills_lines.csv` ships 6,528 indistinguishable duplicate rows
**`extract.sql:119-122`** · `DATABASE VERIFIED / VERIFIED` · latent
The block orders by `CNTLINE` but never emits it. **6,528 rows across 2,645 groups are fully duplicate**
on `(vendor, invoice, gl, amount, description)`. The file's own comment warns that keying on
`(vendor, invoice, line)` loses rows — then ships a file with no line key at all. Anything that dedupes
it destroys real money. (The loader is unaffected: it takes lines from MySQL staging, which has
`cntline`.)

## M-8 — `notes_header` and `notes_lines` do not reconcile to each other
**`extract.sql:212`** · `DATABASE VERIFIED / VERIFIED` · **ALREADY IN THE EXTRACT**
`notes_lines` adds a `LEFT(RTRIM(d.IDGLACCT),2)='4E'` filter that `notes_header` does not, dropping
3 lines worth **₹37,918.00** of `2A7T`. `sum(notes_header.total) − sum(notes_lines.amount) =
₹40,422.03`, and nothing in the tree checks that they should agree — `control_counts` covers
`ap_direct_bills` only.

## M-9 — Bill-line HSN is sent unnormalised
**`post_sage_bills.py:2564`** · `DATABASE VERIFIED / VERIFIED` · latent
```python
"hsn": (s(it["hsn"]) or None) if it and it.get("hsn") else None
```
The product's `hsnCode` goes through `normalise_hsn:660` (truncate to 8, drop to a valid 4/6/8 level);
the **line's** `hsn` does not. Sage holds 677 goods lines (570 items) with a trailing dot, a decimal
tail or a 10-digit ITC-HS code, plus 17 codes at an invalid length across 42 items.
`DATABASE VERIFIED`: all 199 ACTIVE lines that currently carry an HSN are 4 or 8 digits, so it has not
fired — but the goods population has barely started posting.

## M-10 — `work/repost_stranded_parts.py` drops the tag even when the delete fails — **assumption holds**
**`work/repost_stranded_parts.py:190-197` (uncommitted)** · `BACKEND VERIFIED / VERIFIED` · **safe**
The uncommitted diff correctly stops `DELETE` running after a failed `REVOKE`, but simultaneously moves
`done.add(tag)` outside the success branch. On the revoke-ok / delete-failed path the tag is dropped
from `posted.log` on the reasoning that "a REVOKED bill books nothing, so `goods-post` will recreate it
whole". That holds **only if** the server's bill-number uniqueness check ignores REVOKED bills.

**It does.** `BillServiceImpl.java:611-633` restricts the existence check to
`billStatuses = [ACTIVE, APPROVAL_PENDING, BLOCKED]`; `REVOKED` is absent. A revoked bill therefore
does not block a repost under the same number, and the reasoning in the comment is correct.
**Recorded as verified rather than as a risk.**

Two pre-existing weaknesses the diff does not address remain MEDIUM: the fixed backup name
`posted.log.before-repost` (a second `--apply` overwrites the first backup), and `posted.log` being
rewritten **once at the end** (`:204-210`), so a crash after revoking N bills strands all N as
"posted" while they no longer exist.

## M-11 — `work/try_one_contact.py` truncates the hold list five other scripts read
**`work/try_one_contact.py:25` → `post_sage_bills.py:2445-2447`** · `SCRIPT VERIFIED / VERIFIED` ·
**ALREADY HAPPENED — `.b4-*` and `.before-intl-*` copies of the file exist**
`ensure_contacts` rewrites `work/contacts_held.json` with **only the vendors passed to it**. Running the
one-vendor helper for a vendor that gets held replaces the full 185-vendor hold list consumed by
`failure_report.py:101`, `probe_missing_state.py:49`, `probe_state_groups.py:55`,
`probe_state_rcm.py:21` and `probe_state_worth_it.py:26`.

## M-12 — `work/fix_misnamed_products.py` truncates the 4.1 MB crosswalk and ignores delete failures
**`work/fix_misnamed_products.py:22-30`** · `SCRIPT VERIFIED / VERIFIED` · **has run** (its 2 targets are
the 2 genuine burns)
`json.dump(xw, open(m.CROSSWALK, "w"), …)` is truncate-then-write: an exception mid-dump destroys all
17,947 mappings. And the crosswalk keys are dropped **regardless of whether the `DELETE` succeeded**,
so a failed delete leaves the product live, the SKU burned and no crosswalk entry — `masters` then
retries and hits `"Sku Code already exists"` forever. Deleting a product also leaves its ledger and
reference mapping behind (`cleanup_probes.py:3-8` documents this), producing exactly the
`ledger_orphan` rows of C-2.

## M-13 — The vendor's state depends on which phase ran first
**`post_sage_bills.py:2183-2184` vs `:3101-3105` (`SQL_STG_VENDORS`)**
`SCRIPT + DATABASE VERIFIED / HIGH` · **one live instance**
`ensure_contacts` short-circuits on `if state.xw["contacts"].get(code): continue`, so the **first**
phase to reach a vendor decides its state, registration type and address — permanently. The AP path
feeds it from live `APVEN` with `street1..street4`, `city`, `phone1`, `email1`; the goods path feeds it
from `idedat_staging.sage_vendor` with **only `street1`** and no email, from a mirror missing 469 of the
window's vendors. `resolve_state`'s corroboration loop reads
`("city","street4","street3","street2","street1")` (`:637`), so the goods path resolves strictly fewer
states. Nothing re-validates a contact already in the crosswalk.
`master-mismatches.json` reports one live consequence: **`OTHI022`** carries Sage GSTIN
`33XXXPX0291X1ZY` (Tamil Nadu) against a stored contact registered `29XXXPX0420X1Z9` (Karnataka) — a
GSTIN-reuse match onto a **different legal entity**. That vendor has no posted bill yet, so no money has
moved; it is a live trap.

## M-14 — `LEGAL_SLABS` contains single-authority rates
**`post_sage_bills.py:294-295`** · `SCRIPT VERIFIED / MEDIUM` · not observed
`LEGAL_SLABS` includes **6** and **7.5**, which are CGST-only halves of the 12% and 15% rates, not
combined rates. `line_rate:1347` sums the two authorities, so a document where Sage booked only one
would present a 6% total and pass the slab test at **half** the true rate. It is caught in practice by
the `stated_tax` vs `AMTTAXHC` gate at `:1459`, which *skips* the document — safe, but silent, and the
document then joins the invisible population of C-3.

---

# LOW

| # | issue | file:line | evidence | happened? |
|---|---|---|---|---|
| L-1 | `phase_legs` never shows the SMEAssist side. `rec = state.posted.get(tag,"")` is the whole log line, so `rec[0].isdigit()` tests the vendor code's first character and `bid` is always `None`. | `post_sage_bills.py:3800-3801` | `SCRIPT VERIFIED / VERIFIED` | yes — the diagnostic has never worked |
| L-2 | One forward-charge bill is still ₹1.00 below Sage — `JOBW258|108`, ₹323,826.00 stored vs ₹323,827.00. The pre-fix `hasRoundOff` defect. | `:2695-2696` (now fixed) | `DATABASE VERIFIED` | **yes, still live** |
| L-3 | 7 `UNVERIFIED` tags in `posted.log` name bills that are now VERIFIED with legs. The log was never updated, so the next `post` run re-issues `verify` on seven already-verified bills. | `:3000-3013` | `DATABASE VERIFIED` | yes |
| L-4 | `work/failure_report.py:30`'s `bucket()` tests `"state" in w` **before** `"slab"`/`"rate"`, so "**state**d per-line tax…" and "Sage **state**s none" both match. All 17 rows labelled `vendor_state_unresolved` are actually classify() holds; **not one is a state problem.** | `work/failure_report.py:26-36` | `SCRIPT + ARTEFACT VERIFIED` | yes |
| L-5 | `work/po_validate.py` and `work/po_holds.py` are dead — they call `m.load_po_book()`, `m.classify_po()`, `m.build_po_payload()`, `m.po_pilot_pick()`, none of which exist. | `po_validate.py:8,11,21,42,60`; `po_holds.py:4,7` | `SCRIPT VERIFIED` | n/a |
| L-6 | `epoch_ms` stores UTC midnight. Correct in IST (renders 05:30 the same day) and `DATABASE VERIFIED` exact on all 9,248 posted bills — but any consumer rendering in a timezone west of UTC shows the previous day on every bill. | `:781-785` | `DATABASE VERIFIED / INFERRED` | no |
| L-7 | `SQL_STAGING_LINES:369` has no `ORDER BY`, so `lineItemDtoList` ordering is non-deterministic between runs. Traceability survives via `metaData.sageLine`. | `:369` vs `:358` | `SCRIPT VERIFIED` | probably |
| L-8 | Multi-part consolidation sums `gross`/`header_tax` across parts (`:1419-1420`) but takes dates, tax group, `CNTBTCH` and `CNTITEM` from `headers[0]` only (`:1393`), and stamps only the **base** invoice in `metaData` (`:2590`, `:2698`). The other parts' batch/item numbers are unrecoverable — which is what breaks the 4.2 acceptance query at `:3754-3763`. | `:1393`, `:2590` | `SCRIPT VERIFIED` | yes (no harm measured) |

---

# What is verified *correct* — the negative results matter too

These were tested hard and found sound. `DATABASE VERIFIED / VERIFIED` unless noted.

| area | result |
|---|---|
| **GST rate on forward charge** | stored `gstPercentage` equals Sage's stated `RATETAX1+RATETAX2` on **all 23,322** forward-charge lines. 0 off-slab rates on any ACTIVE bill; all 767 off-slab lines are on REVOKED bills |
| **Place of supply — the FLAG** | `bill.isInterState` agrees with the Sage-GSTIN-derived state on **9,330 of 9,330** bills; registration type agrees on 9,330/9,330. *The ledger is a separate question — see H-12, where 4 bills book IGST against an intra-state GSTIN* |
| **Quantity × unit price** | `abs(qty × unitPrice − taxableAmount) > 1` on **0** of 36,459 lines. ₹0.00 discrepancy. `work/fix_qty_unitprice.py`'s repair landed cleanly |
| **Dates and FY** | stored `billDate` matches Sage `DATEINVC` on **9,248 of 9,248**; series prefix matches the bill's own FY on 9,248/9,248 |
| **RCM accounting** | voucher legs prove the vendor is credited the **taxable only** and the self-assessed tax goes to CGST/SGST Payable RCM with matching input debits. The +₹1,532,517.26 header gap against Sage is the intended gross-up, not drift |
| **RCM tax variance** | 636 of 902 documents differ from Sage's `1L8TX` figure; net −₹184.74, absolute ₹421.76, max ±₹1.95 — Sage rounds RCM tax to the rupee and no legal slab satisfies both |
| **Bill type mapping** | re-derived over the real `GLAMF`: the only window account that falls through is `4E1M016`, already settled. 0 window accounts absent from `GLAMF` |
| **Duplicates** | 0 duplicate `(contactId, billNumber)` in any status; 0 duplicate tags in `posted.log`; 0 duplicate SKUs among 17,886 products; 0 `base_invoice` collisions in Sage |
| **Line ledgers (defect 4.6)** | 0 ACTIVE bill lines with a NULL `financeAccountId` |
| **`extract.sql` freshness** | every population matches live Sage today to the row and to the paise — 11,256 / ₹719,631,371.43 / ₹49,763,350.83 / 412 vendors |
| **`extract.sql` read-only claim** | true — the only non-`SELECT` statements are 7 × `SET NOCOUNT ON` |
| **Staging line coverage** | 0 of the 11,256 in-book bills are missing their `sage_ap_dist` lines |
| **`RATETAX3/4/5`** | non-zero on **0** window lines, so reading only slots 1–2 is safe for this window |
| **Readback fix** | route corrected to `GET /bill/detail/{id}`; only 10 bills were posted inside the broken window and all 10 are clean under `value_recon`'s 18 checks |

---

# Recommended order of action

1. **Do not run `./run_all.sh` until C-1 is resolved.** The next `goods-post` posts 1,468 foreign-currency
   documents understated by ₹381.8 M.
2. **Delete or gate `work/cleanup_pilot.py`** (C-4). One invocation destroys 9,376 ACTIVE bills.
3. **Plan the C-2 repair**: 16,829 bill lines / ₹87.3 M must be revoked and reposted onto the re-headed
   ledgers. No existing script does this.
4. **Decide what happens to the 1,525 excluded documents / ₹1,044.3 M** (C-3), and make the exclusion
   *visible* — emit the four exclusion buckets into `failures-report.json` rather than filtering them
   out in SQL.
5. Fix H-1 (`preexisting` → verify), H-2 (rename `SAGE27`; reserve rather than read the counter),
   H-3 (guard `zero`-line products in `eligible`/`eligible_goods`; add `ROUNDOFF_GL` to
   `phase_goods_masters`), H-4 (back-port the revoke gate).
6. Write the crosswalk to `idedat_staging.crosswalk` (H-7) and make `phase_cleanup` refuse to run
   against an empty one (H-6).
7. Bring the goods path up to the AP path's standard: readback, URP hold, RCM detection (H-5).
8. **Point `pincode_state` at the real route** (`GET /api/v1/pincode/{pincode}`) so the IGST/CGST guard
   actually runs, and re-file the one vendor whose address landed in the wrong state (H-12).
9. **Decide what to do about the reverted backend tickets** (H-13). Tickets 2 and 3 are not recoverable
   from the saved patch and exist only in commit `2f70c19da3`; ticket 5's absence is a tax-return
   question, not an engineering one.
10. **Rename the dropped payload keys** (H-14): `metaData` → `meta` on products, `isBulkUpload` →
   `bulkUpload`, `originCountry` → `originCountryDto`. Drop `isManageInventory`, which does not exist.
11. Correct `README.md:7-9`, `:31-35`, `:71-72`, `:80`; `run_all.sh:94-95`; `extract.sql:4-7`, `:34`;
   and the stale `post_sage_bills.py:93` comment.
