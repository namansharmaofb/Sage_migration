# The migration is not blocked by 10,209 documents. It is blocked by 289 vendor contacts.

**Class:** finding — root-cause consolidation · **Severity: CRITICAL to schedule, LOW to
correctness** · **Evidence:** DATABASE VERIFIED + SCRIPT VERIFIED · **Confidence: VERIFIED**

This is the most actionable result in the audit. The project's failure report lists
11,179 not-posted documents across five reason codes, which reads like a large and diffuse
problem. It is not. **One reason code accounts for 91% of it, and behind that code sit 289
vendors — and behind more than half of those sits a single missing data file.**

---

## 1. The failure distribution, as reported

From `work/failures-report.json` (generated 2026-09-05T11:25:31):

| stream | not posted | value blocked (₹) |
|---|---:|---:|
| AP-direct | 1,962 | 8.51 Cr |
| Goods (PO-matched) | 9,217 | 131.42 Cr |
| **total** | **11,179** | **139.92 Cr** |

By reason:

| reason | AP-direct | goods | total | share |
|---|---:|---:|---:|---:|
| **`no_vendor_contact`** | **1,938** | **8,271** | **10,209** | **91.3%** |
| `amount_does_not_tie` | — | 562 | 562 | 5.0% |
| `other` | 1 | 346 | 347 | 3.1% |
| `distribution_shape` | — | 36 | 36 | 0.3% |
| `vendor_state_unresolved` | 17 | 2 | 19 | 0.2% |
| `unmapped_balance_sheet_leg` | 6 | — | 6 | 0.1% |

---

## 2. How many vendors is that, actually?

Measured against live Sage and `work/crosswalk_live.json`:

| population | vendors | have a contact | **missing a contact** |
|---|---:|---:|---:|
| AP-direct (post-filter) | 412 | 319 | **93** |
| PO / goods | 357 | 161 | **196** |
| **union needed by the load** | **734** | 455 | **289** |

**289 vendor contacts stand between the project and 10,209 documents — a leverage ratio
of about 35 documents per vendor.**

The goods stream is the worse half: **196 of its 357 vendors (54.9%) have no contact**,
which is why only 128 of 14,603 goods documents have posted.

---

## 3. What is actually stopping those 289

185 of the 289 are explicitly held with a recorded reason
(`work/contacts_held.json`); the remaining ~104 have simply not been attempted.

| held reason | vendors |
|---|---:|
| **GSTIN 6th character `C` — needs a CIN that exists neither in Sage nor any other source** | **115** |
| **GSTIN 6th character `F` — needs an LLPIN, same** | **42** |
| billing address failed: Invalid Zipcode | 21 |
| country `'Island'` is not a country this platform names — needs a decision | 2 |
| shares a GSTIN with an existing contact that has no usable identity | 2 |
| create failed: Pan Number is not valid | 1 |
| no country in Sage, so the vendor cannot be filed abroad | 1 |
| create failed: Mobile Cannot be blank for System Login | 1 |
| **total held** | **185** |
| not held, not built | ~104 |

*(GSTINs masked to state code + 6th character, per the project's convention.)*

### 3.1 The 6th character is doing real work here

In a GSTIN the 6th character encodes entity type. `C` is a company and `F` is an LLP, and
for those SMEAssist requires the corresponding statutory registration number — a **CIN**
or **LLPIN**. The loader refuses to invent one, which is correct: the README's own rule is
*"Nothing is guessed… Cases with no honest answer are held for a decision and reported,
not filled in with a plausible value."*

**157 of 185 held vendors (85%) are held for exactly this one reason.** The handover
document independently records the same blocker — *"A CIN / LLPIN FILE for the 169 vendors
whose GSTIN 6th character is C or F. 125 of them unblock on this alone. The data does not
exist in Sage."* Two sources, one week apart, same conclusion. **Confidence VERIFIED.**

---

## 3b. The absence of CIN/LLPIN is now PROVEN, by the right method — and the scale is larger

An "absent from the database" claim is exactly the kind this project has been wrong about
before: an earlier pass declared *"vendor GSTIN is not in the database"* because it checked
`TAXNBR` and `IDTAXREGI1` and stopped. The GSTIN was in `APVEN.BRN` all along.

So I did not accept the CIN claim on column inspection. I ran the **value-shape scan** —
searching for the *shape of the value* across every character column, which is the method
that would have caught the earlier mistake:

```sql
-- run per character column of APVEN (48 of them), plus APVENO and APVENC
SELECT SUM(CASE WHEN RTRIM([<col>]) LIKE
       '[LU][0-9][0-9][0-9][0-9][0-9][A-Z][A-Z][0-9][0-9][0-9][0-9][A-Z][A-Z][A-Z][0-9][0-9][0-9][0-9][0-9][0-9]'
       THEN 1 ELSE 0 END) cin,                      -- CIN: 21 chars, LNNNNNAANNNNAAANNNNNN
       SUM(CASE WHEN RTRIM([<col>]) LIKE '[A-Z][A-Z][A-Z]-[0-9][0-9][0-9][0-9]'
       THEN 1 ELSE 0 END) llpin
FROM APVEN;
```

**Result across all 48 character columns of `APVEN`, plus `APVENO` and `APVENC`:
zero CIN-shaped values. Not one.**

The only LLPIN-shaped hits were 10 rows in `IDINVCHI` — the last-invoice-number column — and
they are invoice numbers, not LLPINs: `GST-2367`, `INV-1064`, `BGL-4929`, `JPR-2145`,
`MDS-0213`, `HFS-2529`, `BGL-4929`, `ERD-3470`, `BWD-3513`, `MTP-2078`.

**Verdict: the CIN/LLPIN genuinely is not in Sage.** Upgraded from *not found by column
inspection* (HIGH) to **DATABASE VERIFIED, confidence VERIFIED**.

### The scale is bigger than the held list implies

While scanning I counted how many vendors would ever need one:

```sql
SELECT SUBSTRING(RTRIM(BRN),6,1) ch, COUNT(*) n FROM APVEN
 WHERE LEN(RTRIM(BRN))=15 GROUP BY SUBSTRING(RTRIM(BRN),6,1) ORDER BY COUNT(*) DESC;
```

| GSTIN 6th char | entity type | vendors |
|---|---|---:|
| `P` | proprietorship | 1,040 |
| **`C`** | **company — needs a CIN** | **1,033** |
| **`F`** | **LLP / partnership — needs an LLPIN** | **483** |
| `A`,`B`,`H`,`L`,`D`,`E`,`G`,`T`,`K`,`J`,`M`,`S`,`Y` + 4 malformed | other | 104 |

**1,516 vendors in the full master carry a `C` or `F` GSTIN**, against the 157 currently
held. The 157 is only what the *current window's* blocked documents happen to need.

**Planning consequence:** if this migration widens beyond Jan–Apr 2026 — and it is explicitly
a pilot for a much larger scope — the CIN/LLPIN file is a **~1,500-row** data-sourcing task,
not a 157-row one. That is worth knowing before someone scopes it as an afternoon's work.
Sourcing it once, for the whole master, is almost certainly cheaper than three partial
passes.

---

## 4. The consequence, stated plainly

> **A single spreadsheet of CIN/LLPIN numbers for 157 vendors is the highest-leverage
> action available on this migration.** It is a data-sourcing task for a person with access
> to the MCA registry or the vendor master — not a code change, not a schema change, and
> not something any amount of further engineering will produce.

Nothing in the codebase can resolve it, because the data is genuinely not in Sage. I
confirmed the absence rather than taking it on trust: `APVEN.TAXNBR` and
`APVEN.IDTAXREGI1` are empty across **all 4,752 vendors**, and the GSTIN itself lives in
the non-standard `APVEN.BRN`. There is no CIN column.

---

## 5. Why the failure report makes this hard to see

The report is organised **by document**, so a single missing vendor appears as up to
several hundred separate failures. Ranked by document count, `no_vendor_contact` looks
like a 10,209-item problem. Ranked by *root cause* it is a 289-item problem, and by
*action* it is closer to a 4-item problem: source the CIN file, fix 21 pincodes, decide on
2 countries, and build the ~104 that were never attempted.

The project's own agent brief already prescribes the right instinct — *"Group by root
cause, not by check. One defect fires several checks at once."* The failure report does
not yet do that for vendors.

---

## 6. Second-order observation — the staging vendor mirror is scoped to goods only

While measuring the above I established something the README's warning states imprecisely.
It says the staging mirror is *"missing 469 of the vendors live APVEN has"*.

What is actually true:

| set | vendors |
|---|---:|
| `idedat_staging.sage_vendor` | 357 |
| PO/goods vendors in the window | 357 |
| **are they the same set?** | **yes — identical, zero missing** |
| AP-direct vendors also present in the mirror | 35 of 412 |
| **AP-direct vendors absent from the mirror** | **377 (91.5%)** |

**The mirror is not a degraded copy of `APVEN`. It is a complete copy of the goods vendor
population and nothing else.**

That matters because of the documented fallback behaviour: when the Sage laptop is
unreachable, the loader falls back to the staging mirror for vendors. In that state **the
AP-direct vendor master is 91.5% absent**, not merely stale — every AP-direct vendor
lookup that is not one of the 35 overlaps fails, and `no_vendor_contact` is the exact
symptom it would produce.

The "469" figure could not be reproduced against any baseline I tried (937 window vendors
− 357 = 580; 412 AP-direct − 35 = 377). **The claim needs its baseline stated**, and until
it is, the number should not be quoted. The structural fact — *the mirror covers goods
vendors only* — is the one to carry forward.

---

## 7. Proposed actions — REPORTED, NOT IMPLEMENTED

1. **Source the CIN/LLPIN file for 157 vendors.** Highest leverage available. Owner: a
   person, not the pipeline.
2. **Fix 21 vendor pincodes** in Sage or supply a correction list.
3. **Decide the 2 country cases** (`'Island'`) and the 2 shared-GSTIN identities.
4. **Build the ~104 vendors that were never attempted** — no decision needed, they are
   simply outstanding work.
5. **Re-rank the failure report by root cause**, so 289 vendors do not present as 10,209
   documents.
6. **Never let a run fall back to the staging vendor mirror for AP-direct.**
   `run_all.sh` already refuses to start when Sage is unreachable unless
   `--allow-stale-sage` is passed — verify that flag cannot silently reach the vendor path,
   given the mirror covers only 8.5% of AP-direct vendors.
