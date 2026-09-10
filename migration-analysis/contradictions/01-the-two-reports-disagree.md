# Contradiction #1 — the project's own two reports disagree, and the gap is fully explained

**Status: RESOLVED by measurement.** Both reports are internally consistent; they count
different things, and neither says so. The reconciliation below closes the gap to the
document.

**Severity: HIGH** — not because a number is wrong, but because the file `run_all.sh`
presents as the record of outstanding work is silently incomplete by 5,302 documents.

---

## 1. The disagreement

`run_all.sh` finishes by running both reconcilers and printing:

> `DONE. Anything still outstanding is listed in work/failures-report.json`

But the two artefacts it just wrote do not agree on how much is outstanding:

| Source | Field | Value |
|---|---|---|
| `work/reconcile-report.json` | `in_sage_not_posted` | **18,006** |
| `work/failures-report.json` | `ap_direct_not_posted` + `goods_not_posted` | **11,179** |
| | **unexplained** | **6,827 Sage documents** |

Both files were generated in the same run (`2026-09-05T11:25:31`). DATABASE/SCRIPT
VERIFIED from the artefacts on disk.

---

## 2. Root cause — the failure report's definition of "failure"

`work/failure_report.py:64-72` (AP-direct) and `:79-88` (goods) share this shape:

```python
for key, bill in book.items():
    tag = "%s|%s" % key
    if tag in posted:
        continue
    sh, why = P.classify(bill)
    if not why and key[0] not in contacts:
        why = "no contact built for vendor"
    if not why:
        continue          # <-- not posted, no reason, and no record
    ...
```

A document reaches that last `continue` when it **shapes cleanly, has a vendor contact,
and simply has not been posted yet** — an interrupted run, a `--limit`, a throttled
batch, a crash. It is absent from SMEAssist and absent from the failure report.

The report's docstring is accurate about what it does — *"documents the shaper
**refuses**"* — but that is not the same as *everything not posted*, which is what the
orchestrator's closing line claims for it.

**SCRIPT VERIFIED. Confidence VERIFIED.**

---

## 3. Measurement — I reproduced the report's own population

Read-only sweep over `P.load_book()` and `P.load_goods_book()`, applying exactly the
report's logic and counting the branch it discards:

### AP-direct — 11,256 documents in the book
| outcome | documents | value |
|---|---:|---:|
| posted | 9,250 | — |
| reported as a failure | 1,962 | ₹8.51 Cr |
| **silently dropped** | **44** | **₹14.07 Cr** |

### Goods (PO-matched) — 14,603 documents in the book
| outcome | documents | value |
|---|---:|---:|
| posted | **128** | — |
| reported as a failure | 9,217 | ₹131.42 Cr |
| **silently dropped** | **5,258** | **₹14.44 Cr** |

**Total silently dropped: 5,302 documents, ₹28.51 crore.**

> The goods stream deserves separate emphasis: **128 of 14,603 documents are posted —
> 0.9%.** The AP-direct stream is at 82.2%. Read as one number ("9,378 posted") the
> migration looks two-thirds done; read as two streams, one is nearly complete and the
> other has barely started.

---

## 4. The gap closes exactly

| component | documents |
|---|---:|
| reported as failures (`failures-report.json`) | 11,179 |
| silently dropped by the failure report (§3) | 5,302 |
| never extracted at all — `extract.sql` filters (12,781 base − 11,256 extracted) | 1,525 |
| **total** | **18,006** |
| `reconcile-report.json → in_sage_not_posted` | **18,006** |
| **difference** | **0** |

**DATABASE VERIFIED + SCRIPT VERIFIED. Confidence VERIFIED.**

Both reports are correct. The failure report answers *"what did the shaper refuse?"*;
the reconciler answers *"what is not in SMEAssist?"*. Nothing in the tooling says the
two questions differ, and the orchestrator's final line conflates them.

---

## 5. Why this matters more than the arithmetic

Three distinct populations are being reported as one, and they need different actions:

| population | count | value | what it needs |
|---|---:|---:|---|
| **Refused** — a real blocker, with a reason | 11,179 | ₹139.92 Cr | fix the blocker (91% is one cause: `no_vendor_contact`) |
| **Not yet attempted** — loadable today | 5,302 | ₹28.51 Cr | *just run it* — no decision required |
| **Never extracted** — filtered out in SQL | 1,525 | ~₹104 Cr | **a business decision**, see `sage/` funnel analysis |

The middle row is the one the tooling hides, and it is the cheapest to clear. The bottom
row is the most consequential and the least visible — it is a scope exclusion encoded in
a `WHERE` clause.

---

## 6. Related observations from the same artefacts

- `documents_posted` = 9,378 (failure report) vs `smeassist_bills_active` = 9,376
  (reconciler). **A 2-document difference.** Small, but it means `posted.log` and the
  target database disagree about what exists. Worth chasing rather than rounding away —
  `posted.log` is the resume ledger, so a document it wrongly believes posted is one that
  will never be retried.
- Of 9,371 documents compared on value, **375 (4.00%) mismatch** on amount, beyond the
  901 explained by the RCM taxable-basis rule.
- Healthy signals in the same report, worth recording as evidence the design works:
  `unbalanced_vouchers = 0` and `bill_lines_null_ledger = 0`. The latter is the
  CRITICAL silent-data-loss defect (#1) from the handover's error register — it is
  currently clean.

---

## 7. Recommendation (NOT IMPLEMENTED — reported only)

1. Have `failure_report.py` emit a third bucket — *not attempted* — so its total equals
   the reconciler's `in_sage_not_posted`. Make the two reports reconcile to zero by
   construction, and fail loudly when they do not.
2. Have `extract.sql`'s exclusions surface as a named, counted category with its value,
   rather than being invisible outside the SQL comments.
3. Change the orchestrator's closing line to state the split, or stop claiming the
   failure report is complete.
