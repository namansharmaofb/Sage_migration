# Documentation analysis — what the documents claim, and whether it holds

*Lead-agent pass. Every claim below is labelled with its evidence class and, where I
checked it myself, the query that settles it.*

---

## 1. There is no Sage 300 vendor documentation in this environment

A full sweep of `~/Desktop` found 40 PDFs. **39 are MSME/Udyam certificates** belonging
to an unrelated project. The 40th is
`~/Desktop/Admin pdf/SME Assist __ Finance Account - Organisation Finance Accounts.pdf`
— a print of **SMEAssist's** chart-of-accounts screen. I scanned it for embedded URIs:
**none**. So the brief's instruction to *follow hyperlinks in the PDFs* has no target.

**Consequence, as I first stated it:** every statement about what a Sage table means must
be proven from the data or from an internal document, with no manual to appeal to.

> ### CORRECTION — I was wrong, and the correction improves the audit
>
> That conclusion was right about *files on disk* and wrong about the **database**. The
> Sage schema analyst subsequently found that **`IDEDAT` carries Sage 300's own metadata**:
>
> | Table | Rows | What it gives |
> |---|---:|---|
> | `AUVIEW` | 63 | physical table name → **Sage's own English description** |
> | `AUFLDS` | 1,057 | view + field name → **Sage's own field descriptions** |
> | `GLSRCE` | 64 | source ledger + type → module/transaction-type dictionary |
> | `CSAPP` | 36 | installed-application registry (module code → version) |
> | `DATADICT` | 1,089 | one blob per table; its ASCII strings are that table's field list |
> | `CSOPTFD` | 13,301 | optional-field value/description master |
>
> So there **is** authoritative Sage documentation available — it is inside the database
> rather than in a PDF. My sweep looked for documentation as *files*, found none, and
> generalised too far. The right statement is: **no Sage vendor manual exists on this
> machine, but Sage's own data dictionary is queryable in `IDEDAT` and is the strongest
> evidence source for table and field meaning.**
>
> This materially strengthens the audit — several table meanings that would otherwise rest
> on inference can be sourced to Sage itself. It also makes the brief's rule *never expand
> an abbreviation from intuition* not merely prudent but unnecessary: there is something
> better to consult. Anyone extending this work should query `AUVIEW` and `AUFLDS` **first**.
>
> It is also a small lesson in the failure mode this whole audit is looking for: a search
> that came back empty was reported as an absence, when it had only searched one place.
> That is exactly the mistake the archived prompt warns about with `APVEN.BRN`.

---

## 2. The documents that do exist, ranked by usefulness

| # | Document | Size | What it is | Trust |
|---|---|---|---|---|
| 1 | `ref/SAGE-TO-SMEASSIST-HANDOVER.txt` | 3,392 lines | **The most important document in the project.** Complete handover written 2026-09-01 from the Windows box that hosts Sage. Contains the extraction SQL verbatim, the column mapping, the posting step, a 41-entry error register, the state of play and the open decisions. | HIGH — self-describing methodology, and its checkable claims held (§3) |
| 2 | `extract.sql` | 425 lines | The live extraction SQL, densely annotated with *why* each predicate exists | HIGH — it is executable and I ran its logic |
| 3 | `~/Desktop/PROJECTS/smeassist/sage-migration-tickets.md` | 14 KB | The backend changes this migration needed, in plain language, with module/class names | HIGH for intent; the code is reverted (§5) |
| 4 | `~/Desktop/smeassist-bulk-templates/README.md` | 4.4 KB | The bulk-sheet upload chain, header sources named per file | HIGH, but describes a **different mechanism** |
| 5 | `work/*.md` (9 files, ~2,000 lines) | — | Field findings: burned SKUs, broken readback, expense SAC, item master, fix proposals, run book | MEDIUM — contemporaneous, some superseded |
| 6 | `~/Desktop/indiandesign-archive/built-html/` (3 files) | 311 KB | Prior analyses: financial migration analysis, field mapping reference, Sage↔SMEAssist comparison | MEDIUM — **scoped to FY2026-27, not this window** (§4) |
| 7 | `Admin pdf/*.pdf` + 2 `.xlsx` | — | SMEAssist chart-of-accounts screen; finance-account and ledger-creation upload sheets | HIGH for target structure |
| 8 | `README.md`, `MACHINE-CHANGES.md`, `PROMPT-*.md` | — | This repo's own orientation and infrastructure diary | MEDIUM |

---

## 3. I spot-checked the handover's load-bearing claims. They held.

The handover is an internal document, so its claims are **DOCUMENTED** only. I promoted
four of them to **DATABASE VERIFIED** by querying live Sage.

### 3.1 Vendor GSTIN lives in a non-obvious column — CONFIRMED

The handover says an earlier pass wrongly reported *"vendor GSTIN is not in the
database"* because it checked the standard columns. The GSTIN is in **`APVEN.BRN`**.

```sql
SELECT COUNT(*) AS vendors,
       SUM(CASE WHEN LEN(RTRIM(BRN))=15 THEN 1 ELSE 0 END)       AS brn_len15,
       SUM(CASE WHEN LEN(RTRIM(TAXNBR))>0 THEN 1 ELSE 0 END)     AS taxnbr_nonblank,
       SUM(CASE WHEN LEN(RTRIM(IDTAXREGI1))>0 THEN 1 ELSE 0 END) AS idtaxregi1_nonblank
FROM APVEN;
```
→ `vendors 4752 · brn_len15 2677 · taxnbr_nonblank 0 · idtaxregi1_nonblank 0`

**DATABASE VERIFIED.** The standard tax-registration columns are 100% empty across all
4,752 vendors; `BRN` carries a 15-character value for 2,677 of them. Confidence
**VERIFIED**. This also means **2,075 vendors (43.7%) have no GSTIN-shaped `BRN`** — the
unregistered/foreign population, which is exactly the group the backend tickets say
needed special identity handling.

### 3.2 Reverse charge is booked to three named accounts — CONFIRMED

The handover: *"Sage books reverse charge EXPLICITLY to 1L8TX14 (SGST Payable-RCM),
1L8TX15 (CGST) and 1L8TX16 (IGST). Hitting one in IDGLACCT is definitive and is the ONLY
correct RCM test."*

```sql
SELECT RTRIM(ACCTID) acct, RTRIM(ACCTDESC) descr FROM GLAMF
 WHERE RTRIM(ACCTID) IN ('1L8TX14','1L8TX15','1L8TX16');
```
→ `1L8TX14 = 'SGST Payable - RCM' · 1L8TX15 = 'CGST Payable -RCM' · 1L8TX16 = 'IGST Payable - RCM'`

And their usage in the AP distribution table:
`1L8TX14: 17,204 lines / −8,202,092.59 · 1L8TX15: 17,216 / −8,297,350.92 · 1L8TX16: 3,403 / −471,546.87`

**DATABASE VERIFIED.** Confidence **VERIFIED**. This incidentally proves two other things
the audit needs: **`GLAMF` is Sage's chart of accounts** (`ACCTID` → `ACCTDESC`), and
**`APIBD` is the AP distribution table** carrying `IDGLACCT` + `AMTDISTHC`. Note the RCM
amounts are **negative** — consistent with the handover's warning that on a reverse-charge
bill the header tax field is zero and the tax exists only as negative distribution lines.

### 3.3 The AP-direct window is 11,256 documents — CONFIRMED, and I found what it costs

The handover and `extract.sql` both state the AP-direct extract yields 11,256 header rows.
It does. But the *funnel that gets there* is the finding (§6).

### 3.4 The staging mirror is faithful for this population — CONFIRMED

`idedat_staging.sage_ap_obl` vs live `APOBL`, `IDTRXTYPE=12 AND SRCEAPPL='AP'`,
Jan–Apr 2026:

| | live Sage | staging mirror |
|---|---|---|
| documents | 12,781 | 12,781 |
| gross (`AMTINVCHC`) | 1,763,950,260.97 | 1,763,950,260.97 |

**DATABASE VERIFIED, exact agreement to the paisa.** Confidence **VERIFIED**.

This matters because the README warns the mirror is stale for *vendors* (*"missing 469 of
the vendors live APVEN has"*). That warning is **specific to the vendor table** and does
not generalise: for AP obligations in the migration window the mirror is trustworthy.
Two different tables, two different answers — worth stating plainly so nobody applies
the vendor warning to the document data or vice versa.

---

## 4. The prior HTML analyses are scoped to a *different window*

`~/Desktop/indiandesign-archive/built-html/sage_smeassist_mapping.html` (179 KB) is
described by its own build prompt as:

> *"Scope: **FY 2026-27**, document date `>= 20260401`. This is a restore, data as of
> 2026-07-17."*

The current migration's window is **1 Jan – 30 Apr 2026**. Those overlap in April only.
In the Indian fiscal calendar, Jan–Mar 2026 is FY2025-26 and April 2026 is FY2026-27 —
so the migration window **straddles a financial-year boundary**, and the prior mapping
document covers only one month of it.

**This is a contradiction to hold on to**, not a mistake: the earlier reference is not
wrong, it is answering a different question. Any figure quoted from those HTML pages
must be re-derived before it is used for the Jan–Apr population. The FY straddle is also
why the loader needs two counter series (`SAGE` for FY2025-26, `SAGE27` for FY2026-27) —
DOCUMENTED in the handover, and a fact the reconciliation has to respect.

Those pages also record `IDEDAT` as having **1,105 tables**; it now has **1,110**.
DATABASE VERIFIED. The Sage database has changed since the analysis was written.

---

## 5. The backend documentation describes work that is currently reverted

`~/Desktop/PROJECTS/smeassist/sage-migration-tickets.md` describes six commits under
placeholder ticket `#12425324`, every one marked *"Do not merge"*, covering bulk-upload
sheet changes, a new payment-allocation sheet, a product-create ordering fix, and more.

The repo's own git HEAD is `2785b4c3a3 — "#12425324 - Do not merge: revert the Sage
migration backend changes"`, and the tickets file says so explicitly: *"Those changes
were rolled back on the branch and saved as `sage-migration-backend-changes.patch`."*

The document also carries its own warning about re-applying that patch:

> *"It contains an older version of one small piece — how the sheet reads the RCM column.
> In the old version, a typo in that column was ignored silently, which quietly books a
> bill under reverse charge. The current code rejects the typo instead."*

**A silent RCM default is the same failure mode as error #2 in the handover's register**
(*"isRcmEnabled hardcoded null; the platform reads null as reverse charge applies, so the
vendor is credited NET of GST"* — marked CRITICAL). Two independent documents describe
the same class of defect. That correspondence raises my confidence that **null/absent RCM
handling is a genuine systemic hazard in this platform**, not a one-off. Confidence
**HIGH**; the current code path is under verification by the backend and financial agents.

---

## 6. What the extraction SQL discards — my own finding

`extract.sql` is honest about its filters, but no document states their **combined cost**.
I measured it (`APOBL`, live Sage):

| stage | documents | gross `AMTINVCHC` (₹) |
|---|---:|---:|
| 0. base — `IDTRXTYPE=12`, `SRCEAPPL='AP'`, Jan–Apr 2026 | 12,781 | 1,763,950,260.97 |
| 1. + has `APIBD` distribution lines | 11,702 | 1,468,842,701.21 |
| 2. + `CODECURN = 'INR'` | 11,659 | 1,214,597,827.13 |
| 3. + tax group not pre-GST legacy | 11,541 | 1,208,928,547.13 |
| 4. + **the purity filter** → **final extract** | **11,256** | **719,631,371.43** |

**The extraction keeps 88.1% of the documents but only 40.8% of the value.**
₹104.43 crore of AP-direct documents in the window never enter the pipeline.

The purity filter — *"reject any document whose legs touch anything that is not 4E*
(expense), 2A7T* (tolerated balance-sheet head) or the 1L8TX14/15/16 RCM payable
accounts"* — removes only **285 documents** but **₹48.93 crore**. What it removes:

| account | docs | amount HC (₹) | account name |
|---|---:|---:|---|
| `1L9O` | 19 | 287,774,142.35 | Difference Adjustment Control A/C |
| `1L9E` | 59 | 54,594,152.00 | Bonus Payable |
| `2A1F` | 114 | 27,724,833.00 | Accumulated Depreciation on ROU |
| `2A7S` | 33 | 13,976,376.00 | Advance against DDBK Receivable |
| `1L8T` | 44 | 8,612,488.79 | CGST Payable *(the non-RCM one)* |
| `1L3L` | 4 | 11,111,112.00 | Axis Bank GECL Term Loan |
| `2A3L` | 5 | 3,255,323.00 | Axis Bank Term Deposit Account |
| *(6 more)* | ~15 | ~1.7 m | provisions, clearing, cash |

The SQL's own comment calls these *"journals wearing an invoice's clothes"*, and on the
evidence that reading is defensible — a depreciation entry or a bonus provision is not a
purchase bill. **The design decision looks right. The concern is different:** this is a
₹104 crore scope exclusion encoded in a `WHERE` clause rather than recorded as an
accepted business decision, and one of the excluded groups (`1L8T`, plain CGST Payable,
44 documents) is a *tax* account, which deserves a second look rather than automatic
exclusion. **SCRIPT VERIFIED + DATABASE VERIFIED. Confidence HIGH.** Reported, not fixed.

---

## 7. Documents that describe things which are out of scope — and say so

The handover's §6.2 is unusually candid, and these are load-bearing for the final report:

| Area | Status per the handover | Consequence if true |
|---|---|---|
| **Journal vouchers** | *"NONE. Never extracted for load, never mapped, never posted."* | Manual GL journals — the largest FY2026 block by value at ₹8,375 Cr on 7,881 legs — have no migration path |
| **Payments / prepayments / settlements** | *"OUT OF SCOPE, closed 27 Aug 2026, and they do not run."* | Every migrated bill shows unpaid; **cheque payment mode does not survive** (14,851 cheque payments, 55% of the total) |
| **Opening balances** | *"Not loaded."* Data extracted (`GLAFS`, 3,222 rows); trial balance ties — 249 accounts, ₹769.05 Cr, difference 0.0000 | The target's payables balance cannot be correct without them |
| **Chart of accounts** | *"Not loaded."* 1,610 Sage accounts collapse to 386 natural accounts | The JV path resolves ledgers by account code and cannot run without it |
| **AR / customers** | *"Not in scope at all."* 431 customers, 189K invoices exist in Sage | — |

And the double-count hazard, which the handover calls *"the largest correctness risk in
the whole migration"*: `GLPOST` already contains the AP/AR/IC subledger postings, while a
migrated bill generates its own voucher on verify. *"Per source module, GL history OR the
real entity, never both."*

**These are documented claims about scope, not verified facts.** Agent 6 is testing each
against the databases; §6.3 of the handover is where the numbers to test against live.

---

## 8. The one thing every document agrees on, and it is easy to miss

The handover states it in its own §6.0:

> *"Nothing has been posted to staging or production, and the target org on the devbox is
> **Wonderblues**, not Indian Designs."*

I confirmed the current `.env` still points at that same org: `SME_ORG_ID`
`1029113552088076445`, namespace `wonderblues` — **the identical org id hardcoded in the
August bulk-upload handoff prompt.** DATABASE/CONFIG VERIFIED.

**So everything measured in this audit is a rehearsal against a stand-in organisation.**
That is the correct way to run a migration, and it means the numbers here describe
*method fidelity*, not a delivered migration. It also means the org's real GSTIN and PAN
are still placeholders — the handover lists *"THE TARGET ORG'S REAL GSTIN AND PAN, once
there is an Indian Designs org"* as an outstanding blocker, and warns that `companyGst`
*"must be the TARGET org's, never Sage's"*. Any figure in this report should be read with
that framing.
