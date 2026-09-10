# 01 — Financial Validation: Sage 300 (IDEDAT) → SMEAssist

**Agent 6 — Financial Validation.** Read-only audit. Every statement issued to either database
was a `SELECT` / `SHOW` / `DESCRIBE`. No project script was executed; every number below comes
from a query written for this report and reproduced with it.

- **Working window:** AP documents with Sage `APOBL.DATEINVC` between `20260101` and `20260430`.
- **Currency:** every figure is **Indian Rupees (INR)** and is a **home-currency (HC)** amount
  unless the line says otherwise. Sage stores AP money as `decimal(19,3)` — there is no implied
  scale; `AMTINVCHC = 4720.000` means ₹4,720.00. SMEAssist stores `decimal(19,6)`.
- **Target org:** `SME_ORG_ID = 1029113552088076445` (`.env`). Every SMEAssist query is filtered
  on `organisationId`.
- **Sage source database:** `IDEDAT` on MS SQL Server (`.env: SQL_HOST/SQL_DATABASE`).
- **Snapshot taken:** 5 Sep 2026. Sage is a live production database; balance columns such as
  `APOBL.AMTDUEHC` reflect *today*, not the window.
- **GSTINs are masked** in the form `29XXXP…`: state code and the 6th character (the entity-type
  character) are kept, everything else is replaced.

Evidence labels used throughout: **DATABASE VERIFIED** (I ran the query), **BACKEND VERIFIED**
(read in the SMEAssist Java source), **SCRIPT VERIFIED** (read in `post_sage_bills.py` /
`work/*.py`), **DOCUMENTED** (asserted in a repo `.md` or a code comment, not independently
checked), **INFERRED**.

---

## 0. The headline, before the detail

| Question | Answer | Confidence |
|---|---|---|
| Do the migrated documents carry the right values? | **Yes, to the paise.** 6,668 of 9,371 match Sage exactly; 2,693 more differ by ≤ ₹0.01; only **3 documents** differ by ≥ ₹1. | VERIFIED |
| Is the migration complete? | **No.** 9,376 of 27,409 in-window Sage invoices reached SMEAssist — **34.2 % by count, 13.5 % by value**. ₹3,207,903,604.89 of Sage AP invoice value never crossed. | VERIFIED |
| Do the payables tie? | **No.** SMEAssist's creditor balance is ₹497,239,932.81. Sage's creditor control heads read ₹535,122,264.66 Cr at 1 Jan 2026 and ₹629,943,197.98 Cr at 30 Apr 2026 (₹429,484,090.46 net of prepayments). The target's figure is none of these, and is not a movement either. | VERIFIED |
| Opening balances? | **Not migrated. Zero rows.** | VERIFIED |
| Payments / allocations? | **Not migrated. Zero rows.** Every migrated bill is 100 % unpaid; 97.8 % of the source documents are marked fully paid in Sage. | VERIFIED |
| Credit notes / debit notes / prepayments? | **Not migrated.** 2,066 credit notes, 137 debit notes and 1,729 prepayments in the window; the target holds 96 note records, all `REVOKED` and soft-deleted. | VERIFIED |

---

## 1. The accounting model on both sides, proven

### 1.1 Sage 300 — AP document → distribution → journal → posted GL

**Tables and keys** (DATABASE VERIFIED, VERIFIED):

| Layer | Table | Key | What it holds |
|---|---|---|---|
| Invoice entry, header | `APIBH` | `CNTBTCH`, `CNTITEM` | the batch entry as keyed |
| Invoice entry, distributions | `APIBD` | `CNTBTCH`, `CNTITEM`, `CNTLINE` | one row per expense/asset line: `IDGLACCT`, `AMTDIST`, `RATETAX1..5`, `AMTTAX1..5` |
| Posted obligation | `APOBL` | `IDVEND`, `IDINVC` | the open item: `AMTINVCHC`, `AMTTAXHC`, `AMTDUEHC`, `CODETAXGRP`, `FISCYR/FISCPER`, back-reference `CNTBTCH/CNTITEM` and `POSTSEQNCE` |
| Payment schedule | `APOBS`, `APOBP` | `IDVEND`, `IDINVC`, `CNTPAYM(NBR)` | instalments; `APOBP` holds each payment/credit **application** |
| AP posting journal | `APPJH` / `APPJD` | `POSTSEQNCE`, `CNTBTCH`, `CNTITEM`, `CNTSEQENCE` | the double entry AP generated: `IDACCT`, `ACCTTYPE`, `AMTEXTNDHC`, `AMTTAXHC` |
| Posted GL | `GLPOST` | `POSTINGSEQ`, `BATCHNBR`, `ENTRYNBR`, `CNTDETAIL` | the GL detail as posted: `ACCTID`, `TRANSAMT`, `SRCELEDGER='AP'`, `SRCETYPE='IN'` |
| GL period balances | `GLAFS` | `ACCTID`, `FSCSYR`, `CURNTYPE='F'` | `OPENBAL` + `NETPERD1..13` |

`GLJEH`/`GLJED` are the **GL batch-entry** tables (journals awaiting/at posting). For AP-sourced
invoices the posted record lives in `GLPOST`; a probe for `GLJEH WHERE SRCELEDGER='AP' AND
DOCNUMBER='0056'` returned no rows while `GLPOST` returned the entry. **DATABASE VERIFIED.**

**Worked proof — vendor `SELD407`, invoice `0056`, 1 Jan 2026, ₹35,400.00**

```sql
-- header
SELECT RTRIM(IDVEND), RTRIM(IDINVC), CNTBTCH, CNTITEM, POSTSEQNCE, AMTINVCHC, AMTTAXHC,
       RTRIM(CODETAXGRP), FISCYR, FISCPER
  FROM APOBL WHERE IDTRXTYPE=12 AND SRCEAPPL='AP'
   AND DATEINVC BETWEEN 20260101 AND 20260430 AND AMTTAXHC > 1000;
-- → SELD407 | 0056 | 17226 | 3 | 15771 | 35400.000 | 5400.000 | LOCAL | 2026 | 10
```
```sql
-- distribution
SELECT CNTLINE, RTRIM(IDGLACCT), RTRIM(TEXTDESC), AMTDIST, RATETAX1, RATETAX2,
       AMTTAX1, AMTTAX2, AMTTOTTAX
  FROM APIBD WHERE CNTBTCH=17226 AND CNTITEM=3;
-- → 20 | 4E5O014 | Professional service for Dec-25 | 30000.000 | 9.00000 | 9.00000
--        | 2700.000 | 2700.000 | 5400.000
```
```sql
-- AP posting journal
SELECT CNTSEQENCE, RTRIM(IDACCT), ACCTTYPE, AMTEXTNDHC, AMTTAXHC
  FROM APPJD WHERE POSTSEQNCE=15771 AND CNTBTCH=17226 AND CNTITEM=3;
-- 1 | 1L7SV01 | 1 | -35400.000 | 0.000     <- payables control (ACCTTYPE 1)
-- 2 | 4E5O014 | 2 | +30000.000 | 0.000     <- expense           (ACCTTYPE 2)
-- 3 | 2A7TX01 | 6 |      0.000 | 2700.000  <- SGST recoverable  (ACCTTYPE 6)
-- 4 | 2A7TX02 | 6 |      0.000 | 2700.000  <- CGST recoverable
```
```sql
-- posted GL, the same entry
SELECT RTRIM(ACCTID), CNTDETAIL, TRANSAMT FROM GLPOST
 WHERE POSTINGSEQ=30996 AND BATCHNBR='041169' AND ENTRYNBR='00022' ORDER BY CNTDETAIL;
-- 1L7SV01 100 -35400.000
-- 4E5O014 101 +30000.000
-- 2A7TX01 102  +2700.000
-- 2A7TX02 103  +2700.000
-- SUM(TRANSAMT) = 0.000        <- debits equal credits
```
**Debits = credits, proven on real numbers. DATABASE VERIFIED / VERIFIED.**

**Which accounts a purchase hits** (DATABASE VERIFIED):

| Role | Sage heads | Evidence |
|---|---|---|
| Payables control | `1L6T*` Trade Payables (Fabric / Accessories / Packing / A-P Clearing), `1L7CG*` capital goods, `1L7JV*` job work, `1L7MV*` maintenance, `1L7OV*` other expense, `1L7SV*` selling & distribution, `1L9OL*` group companies | `SELECT RTRIM(IDACCT), COUNT(*) FROM APPJD WHERE ACCTTYPE=1 AND FISCYR='2026' AND FISCPER IN ('10','11','12') …` → **252 distinct control accounts** in the window |
| Input tax (recoverable) | `2A7TX01` SGST Recoverable, `2A7TX02` CGST Recoverable, `2A7TX03` IGST Recoverable, `2A7TX04` IGST Recoverable on Imports | `GLAMF WHERE LEFT(ACCTFMTTD,4)='2A7T'` |
| Reverse-charge output tax | `1L8TX14` SGST Payable-RCM, `1L8TX15` CGST Payable-RCM, `1L8TX16` IGST Payable-RCM | `GLAMF` |
| Expense / P&L | `4E1M` (COGS + processing), `4E2M` manufacturing, `4E3E` employee, `4E4S` selling & distribution, `4E5O` other, `4E6F` finance | `GLAMF.ACCTGRPCOD` |
| Inventory / assets | `2A1F*` fixed assets; PO-matched goods clear through `1L6T…07/13` "A/P Clearing" | distribution census below |

There is **no single AP control account.** Sage runs a many-headed payables control keyed on the
vendor's account set (`APOBL.IDACCTSET`). This matters in §1.3.

**Distribution census, all in-window type-12 documents** (`APOBL ⨝ APIBD`, 67,054 rows):

| GL prefix | lines | amount INR | meaning |
|---|---:|---:|---|
| `4E4S` | 32,109 | 102,003,644.15 | selling & distribution expense |
| `1L6T` | 16,543 | 1,359,989,346.72 | **A/P clearing — the PO-matched leg** |
| `4E2M` | 8,141 | 457,542,659.36 | manufacturing expense |
| `2A7T` | 2,810 | −7,539,456.70 | input tax recoverable |
| `4E5O` | 1,607 | 89,947,486.88 | other expense |
| `1L7M` | 1,265 | 19,542,162.93 | maintenance vendors |
| `1L8TX14/15/16` | 2,168 | −1,999,198.20 (net) | reverse-charge payable |
| `4E1M` | 994 | 15,672,906.63 | COGS / processing |
| `4E3E` | 558 | 79,888,476.00 | employee cost |
| `2A1F` | 121 | 28,186,103.00 | fixed assets |
| `1L9E`,`1L9O`,`1L3L`,`2A3L`,`2A5C`,`2A7S`,`1L8TX04/11/12/13` | 250 | — | payroll, group co., loans, prof. tax, forward GST payable |

Query: `SELECT LEFT(RTRIM(d.IDGLACCT),4), COUNT(*), SUM(d.AMTDIST) FROM APOBL b JOIN APIBD d ON
d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM WHERE b.DATEINVC BETWEEN 20260101 AND 20260430 AND
b.IDTRXTYPE=12 GROUP BY 1`. **DATABASE VERIFIED.**

**Fiscal calendar** (`CSFSC`, DATABASE VERIFIED / VERIFIED):

```sql
SELECT FSCYEAR, PERIODS, BGNDATE1, ENDDATE1, BGNDATE10, ENDDATE10, BGNDATE12, ENDDATE12
  FROM CSFSC WHERE FSCYEAR IN ('2025','2026','2027');
-- 2026 | 12 | 20250401 | 20250430 | 20260101 | 20260131 | 20260301 | 20260331
-- 2027 | 12 | 20260401 | 20260430 | 20270101 | 20270131 | 20270301 | 20270331
```
The fiscal year runs **April → March, 12 periods**. The Jan–Apr 2026 window therefore straddles
**two fiscal years**:

| Calendar month | Fiscal year | Period | Documents in `APOBL` |
|---|---|---|---:|
| Jan 2026 | 2026 | 10 | 10,500 |
| Feb 2026 | 2026 | 11 | 8,126 |
| Mar 2026 | 2026 | 12 | 11,485 |
| Apr 2026 | **2027** | 01 | 7,919 |
| (3 stragglers posted to 2027 P02/P03) | 2027 | 02–03 | 3 |

Query: `SELECT FISCYR, FISCPER, MIN(DATEINVC), MAX(DATEINVC), COUNT(*) FROM APOBL WHERE DATEINVC
BETWEEN 20260101 AND 20260430 GROUP BY FISCYR, FISCPER`. **DATABASE VERIFIED.**
The loader's own series map (`SERIES_BY_FY = {"2025-2026": "SAGE", "2026-2027": "SAGE27"}`,
`post_sage_bills.py:85`) reflects the same split. **SCRIPT VERIFIED.**

### 1.2 SMEAssist — what a bill produces

**Tables** (DATABASE VERIFIED): `bill` (header — `billAmount`, `taxableAmount`, `gstAmount`,
`roundOffAmount`, `isInterState`, `billType`, `billStatus`, `contactId`), `billLineItem`
(`taxableAmount`, `gstPercentage`, `isRcmEnabled`, `financeAccountId`, `hsn`, `skuCode`),
`voucherEntry` (the ledger legs — `financeAccountId`, `transactionType` DEBIT/CREDIT, `amount`,
`remainingAmount`, `referenceType`, `referenceId`, `partyType`, `voucherId`), `financeAccount`
(the chart of accounts as a tree: `path`, `parentFinanceId`, `leaf`, `netBalance`, `partyType`).

**Worked proof — the same document, bill id `1544945856318308352`:**

```sql
SELECT id,billNumber,billType,billAmount,taxableAmount,gstAmount,roundOffAmount,isInterState
  FROM bill WHERE id='1544945856318308352';
-- 0056 | PURCHASE | 35400.000000 | 30000.000000 | 5400.000000 | 0.00 | 0 (intra-state)

SELECT ve.transactionType, ve.amount, fa.name
  FROM voucherEntry ve JOIN financeAccount fa ON fa.id=ve.financeAccountId
 WHERE ve.referenceId='1544945856318308352';
-- CREDIT 35400.000000  Gst_29XXXP…_M N GENERAL TRADING_KEL743   (partyType VENDOR)
-- DEBIT  30000.000000  Professional Charges_SAGE-4E5O014 Expense
-- DEBIT   2700.000000  CGST Input @ 9.00 %
-- DEBIT   2700.000000  SGST Input @ 9.00 %
```

Leg-for-leg the same entry Sage posted. **DATABASE VERIFIED / VERIFIED.**

**Does the target ledger balance?** Yes, org-wide:

```sql
SELECT SUBSTRING_INDEX(fa.path,' > ',1) root,
       SUM(CASE WHEN ve.transactionType='DEBIT'  THEN ve.amount ELSE 0 END) dr,
       SUM(CASE WHEN ve.transactionType='CREDIT' THEN ve.amount ELSE 0 END) cr
  FROM voucherEntry ve JOIN financeAccount fa ON fa.id=ve.financeAccountId
 WHERE ve.organisationId='1029113552088076445' AND ve.isDeleted=0 GROUP BY 1;
```

| Root group | leaf legs | Debit INR | Credit INR | Net credit INR |
|---|---:|---:|---:|---:|
| Current Liabilities | 23,265 | 35,753,523.99 | 498,772,450.16 | **463,018,926.17** |
| Purchase Accounts | 199 | 3,215,975.13 | 0.00 | −3,215,975.13 |
| Indirect Expenses | 9,919 | 71,154,880.54 | 1.45 | −71,154,879.09 |
| Direct Expenses | 19,364 | 388,648,071.95 | 0.00 | −388,648,071.95 |
| **Total** | | **498,772,451.61** | **498,772,451.61** | **0.00** |

Zero unbalanced vouchers. `financeAccount` root `FAROOT` carries `netBalance = −0.000001`.
**DATABASE VERIFIED / VERIFIED.**

The migrated trial balance decomposes as: Creditors ₹497,239,932.81 Cr + GST Output (RCM)
₹1,532,517.35 Cr − GST Input ₹35,753,523.99 Dr − Direct Expenses ₹388,648,071.95 Dr −
Indirect Expenses ₹71,154,879.09 Dr − Purchases ₹3,215,975.13 Dr + Misc ₹0.20 = 0.

### 1.3 Where the two models do **not** correspond

| # | Concept | Sage | SMEAssist | Consequence | Label |
|---|---|---|---|---|---|
| 1 | **Payables control** | 252 control accounts by account set (fabric / accessories / packing / capital / job-work / maintenance / other / S&D / group co.), each a GL head | one leaf ledger **per party**, all under `Current Liabilities > Creditor > Vendor` | Sage's payables *analysis by category* has no counterpart. You cannot produce Sage's "Fabric Vendors – Interstate" balance from SMEAssist. | DATABASE VERIFIED |
| 2 | **Input-tax account granularity** | one account per tax type (`2A7TX01/02/03/04`) regardless of rate | one ledger **per rate** (`CGST Input @ 9.00 %`, `IGST Input @ 18.00 %`, …) — 594 `GST` reference mappings | many-to-many; a Sage 2A7TX balance maps to N SMEAssist ledgers | DATABASE VERIFIED |
| 3 | **Expense-account identity** | one GL account = one balance | the *same* Sage account can become **two** ledgers, "…Expense" (Direct) and "…Indirect Expense" | **48 Sage accounts split across two P&L groups, ₹139,577,534.48** — see §6.5 | DATABASE VERIFIED |
| 4 | **Reverse charge** | vendor payable excludes the tax; `2A7TX0x` Dr and `1L8TX1x` Cr are a distribution pair; `APOBL.AMTTAXHC = 0` | `bill.gstAmount` carries the self-assessed tax and `bill.billAmount = taxable + RCM tax`, but the **vendor is credited taxable only** | `billAmount` is *not* the payable on 901 bills; any report that sums `billAmount` as "amount owed" overstates it by ₹1,532,517.35 | DATABASE VERIFIED |
| 5 | **Payments / applications** | `APOBP` (1.54 M rows), `APOBS`, `APPYM`, doc types 50/51 | **no concept present in the data** — `voucherEntry.referenceType` is `BILL` and nothing else | every migrated bill is unpaid; §5.2 | DATABASE VERIFIED |
| 6 | **Credit / debit notes** | `IDTRXTYPE` 32 / 22 in `APOBL`, with tax | `creditDebitNote` exists but holds **0 live rows** for the org | Sage's ₹398 M of in-window credit notes have no target representation | DATABASE VERIFIED |
| 7 | **Prepayments / advances** | doc type 50, dedicated GL heads (`1L6TF05`, `1L7JV05` …), ₹1.55 bn in the window | no advance/prepayment record | vendor balances cannot be net of advances | DATABASE VERIFIED |
| 8 | **Opening balance** | `GLAFS.OPENBAL` + period nets per account | `accountingLedger` table exists with `openingBalance`, **0 rows for this org** | the target's ledger starts at zero on 1 Jan 2026 | DATABASE VERIFIED |
| 9 | **Multi-currency** | `CODECURN` + `AMTINVCTC` (source) vs `AMTINVCHC` (home); 2,372 non-INR docs in the window | `bill.currency`/`conversionRate` exist; **every migrated bill is INR at rate 1.0** | 815 non-INR type-12 documents worth ₹757,366,728.49 excluded outright | DATABASE VERIFIED |
| 10 | **Fiscal calendar** | Apr–Mar, 12 periods, `FISCYR/FISCPER` stamped on every document | no fiscal-period stamp on `bill`/`voucherEntry`; only `billDate`/`voucherDate` (epoch ms) | period-close and period-locking cannot be reproduced | DATABASE VERIFIED |
| 11 | **Vendor identity** | one sub-ledger per `IDVEND` | one ledger per contact; **7 contacts absorb 2–14 Sage vendor codes each** (₹23,814,466.84) | Sage per-vendor balances are not recoverable for those; it also silently dropped 2 documents (§6.7) | DATABASE VERIFIED |
| 12 | **Withholding (TDS/TCS)** | `APOBL.OAMTWHT1..5TC`, and TDS credit notes (`32`) such as `TDS 0.1% ON RS 578448` | `bill.txsType/txsAmount/txsPercentage` all NULL on every migrated bill | TDS deducted at source is not carried; the payable is gross | DATABASE VERIFIED |

---

## 2. Value reconciliation, both directions

### 2.1 Method

Sage side: `APOBL` for the window, consolidated on **(vendor, base invoice)** — the trailing
`*<digits>` receipt suffix stripped, exactly as `post_sage_bills.py:base_invoice` does, because
that is the key the loader posts under. SMEAssist side: the `bill` table for the org, joined to a
Sage document through `work/posted.log` (`vendor|invoice || billId`) — never through `billNumber`,
which recurs across vendors. Reproduced from:

```sql
-- Sage
SELECT RTRIM(IDVEND), RTRIM(IDINVC), RTRIM(SRCEAPPL), IDTRXTYPE, DATEINVC, RTRIM(CODECURN),
       RTRIM(CODETAXGRP), AMTINVCHC, AMTTAXHC, AMTDUEHC, SWPAID, AMTTAX1HC, AMTTAX2HC,
       RTRIM(CODETAX1), RTRIM(CODETAX2), CNTBTCH, CNTITEM, POSTSEQNCE
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430;            -- 38,033 rows
-- SMEAssist
SELECT id,billNumber,billType,billStatus,isDeleted,billDate,billAmount,taxableAmount,gstAmount,
       roundOffAmount,isInterState,contactId,contactName,currency
  FROM bill WHERE organisationId='1029113552088076445';               -- 10,769 rows
```

### 2.2 The Sage universe in the window

```sql
SELECT IDTRXTYPE, RTRIM(SRCEAPPL), COUNT(*), SUM(AMTINVCHC), SUM(AMTTAXHC)
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430 GROUP BY 1,2;
```

| `IDTRXTYPE` | Meaning (`TXTTRXTYPE`) | Source | Rows | Gross INR | Tax INR |
|---|---|---|---:|---:|---:|
| 12 | Invoice (1) | PO | 18,046 | 1,942,654,924.63 | 78,593,161.95 |
| 12 | Invoice (1) | AP | 12,781 | 1,763,950,260.97 | 54,266,283.66 |
| 51 | Payment (11) | AP | 3,274 | −1,860,075,231.25 | 0.00 |
| 32 | Credit note (3) | AP+PO | 2,066 | −407,960,126.59 | −7,676,351.58 |
| 50 | Prepayment (10) | AP | 1,729 | −1,548,255,387.03 | 0.00 |
| 22 | Debit note (2) | AP+PO | 137 | 67,596,606.48 | 134,085.22 |
| | | | **38,033** | | |

Consolidated on the `*N` suffix, the **type-12 invoice population is 27,409 documents**
(12,781 AP-direct + 14,628 PO-matched), **₹3,706,605,185.60 gross, ₹132,859,445.61 tax**.
**DATABASE VERIFIED / VERIFIED.**

### 2.3 Counts — both directions

| | Documents | Sage gross INR | Sage tax INR |
|---|---:|---:|---:|
| Sage type-12 invoices in window | **27,409** | 3,706,605,185.60 | 132,859,445.61 |
| … reached SMEAssist | **9,376** (34.21 %) | 498,701,580.71 (13.45 %) | 33,599,718.20 |
| … **in Sage, missing in SMEAssist** | **18,033** (65.79 %) | **3,207,903,604.89** | 99,259,727.41 |
| **in SMEAssist, not in Sage** | **0** | — | — |

Of the 9,376: 9,371 carry a `billId` in `posted.log`; 5 more are recorded as `preexisting`
(the loader found the bill already present and did not re-post) and were traced back to their
Sage documents by hand. There are **no duplicate postings** — `SELECT contactId, billNumber,
COUNT(*) … HAVING COUNT(*)>1` over ACTIVE bills returns 0 groups, and no `billId` in
`posted.log` is claimed by two Sage documents. **DATABASE VERIFIED / VERIFIED.**

A further **1,393 bills are `REVOKED`** (all `isDeleted=1`) from an earlier abandoned run,
₹38,150,130.24. Their 13,069 voucher entries are also `isDeleted=1`, so they do **not**
contaminate the ledger — checked explicitly. **DATABASE VERIFIED.**

**Why 18,033 documents are missing** (classified with the loader's own filter, `SQL_HEADERS`
at `post_sage_bills.py:333`):

| Reason | Docs | Sage gross INR |
|---|---:|---:|
| **PO-matched**: distribution head outside `4E` / `2A7T` / `1L8TX14-16` (the A/P-clearing leg `1L6T…`) | 13,729 | 1,434,581,190.20 |
| **PO-matched**: non-INR currency | 772 | 503,121,854.41 |
| **AP-direct**: in the loader's scope, attempted, never posted | 2,012 | 225,991,779.95 |
| **AP-direct**: no `APIBD` distribution row at all | 1,071 | 289,097,477.04 |
| **AP-direct**: distribution head outside the allowed set | 285 | 489,297,175.70 |
| **AP-direct**: `VAT` / `NRVAT` / `NRST` tax group | 126 | 11,679,362.72 |
| **AP-direct**: non-INR currency | 43 | 254,244,874.08 |
| **Total** | **18,033** | **3,207,903,604.89** |

The 2,012 AP-direct documents that were *in scope and simply did not post* carry
**₹225,991,779.95 gross and ₹16,495,072.77 of input tax**. The project's own
`work/failures-report.json` records 1,962 of these with reasons — `no_vendor_contact` 1,938,
`vendor_state_unresolved` 17, `unmapped_balance_sheet_leg` 6, `other` 1 — and 9,217 goods
failures. **DATABASE VERIFIED (my classification) / DOCUMENTED (their reasons).**

**The PO-matched (goods) population is effectively not migrated at all: 127 of 14,628
documents, 0.87 %, ₹4,951,880.02 of ₹1,942,654,924.63.**

### 2.4 Amounts on the 9,371 `billId`-joined documents

| Measure | Sage INR | SMEAssist INR | Δ INR | Δ % |
|---|---:|---:|---:|---:|
| Document value (`AMTINVCHC` vs `billAmount`) | 498,591,471.50 | 498,666,028.78 | **+74,557.28** | +0.0150 % |
| Tax (`AMTTAXHC` vs `gstAmount`) | 33,593,935.24 | 35,057,024.58 | **+1,463,089.34** | +4.3552 % |
| Taxable (gross − tax vs `taxableAmount`) | 464,997,536.26 | 463,609,004.00 | **−1,388,532.26** | −0.2986 % |
| **AP liability created** (`AMTINVCHC` vs vendor-ledger credit) | 498,591,471.50 | 497,133,511.43 | **−1,457,960.07** | −0.2924 % |

The +₹1,463,089.34 tax variance is **not** an error: it is the reverse-charge gross-up. Sage
records `AMTTAXHC = 0` on an RCM document and books the tax through the distribution instead;
SMEAssist puts the self-assessed ₹1,532,517.35 into `gstAmount`. Netting it out leaves the
forward-charge tax variance at **−₹69,428.01** (§3.1).

### 2.5 Per-document distribution — the decisive test

Raw `billAmount − AMTINVCHC`:

| bucket | documents |
|---|---:|
| exact 0.00 | 5,767 |
| ≤ ₹0.01 | 2,693 |
| ≤ ₹0.50 | 7 |
| ≤ ₹1.00 | 1 |
| ≤ ₹100 | 14 |
| ≤ ₹10,000 | 856 |
| > ₹10,000 | 33 |

Of the 33 above ₹10,000, **31 are reverse-charge documents** (expected gross-up). Removing the
RCM self-assessed tax from `billAmount` gives the honest picture:

| bucket (RCM-adjusted) | documents |
|---|---:|
| **exact 0.00** | **6,668** |
| ≤ ₹0.01 | 2,693 |
| ≤ ₹1.00 | 8 |
| ≤ ₹100 | 0 |
| ≤ ₹10,000 | 0 |
| **> ₹10,000** | **2** |

**Is this a systematic bug or noise?** Both, and they separate cleanly:

1. **Noise (2,693 documents, net −₹1.38).** All ≤ ₹0.01, split 1,290 positive / 1,403 negative,
   positives +₹3.85 and negatives −₹5.23. This is `decimal(19,6)` storage of a unit price times
   quantity; **2,731 ACTIVE bills store a `billAmount` that is not a whole paise**, while
   `taxableAmount` is always whole paise. Not accumulating in one direction. Immaterial in value,
   but it means `billAmount` is never a payable figure to the paise on 29 % of bills.
2. **A systematic bug, but with a population of exactly 2** — split-document truncation:

| Document | Sage parts | Sage gross INR | SMEAssist INR | Lost INR |
|---|---|---:|---:|---:|
| `FABI470 \| 1173/2025-26` | `*1` 805,684.01 + `*2` 10,183.95 | 815,867.96 | 10,183.95 | **−805,684.01** |
| `FABI470 \| 1177/2025-26` | `*1` 652,273.65 + `*2` 327,021.66 | 979,295.31 | 327,021.66 | **−652,273.65** |
| `JOBW258 \| 108` | single | 323,827.00 | 323,826.00 | −1.00 |
| | | | | **−1,457,958.66** |

`base_invoice()` consolidates `1173/2025-26*1` and `*2` into one key, but only one part was
posted. Verified in Sage: both parts exist under `CNTBTCH 17228`, both distribute to `1L6TF13`
(A/P Clearing – Fabric), both `SWPAID=1`. **These two documents are the entire taxable variance
(−₹1,388,531.10 of −₹1,388,532.26) and the entire forward-tax variance
(−₹69,426.56 of −₹69,428.01).** DATABASE VERIFIED / VERIFIED.

**Verdict: the values that crossed are right. Nothing systematic is wrong with the amounts.
What is wrong is how few of them crossed.**

### 2.6 By month

| Month | Sage docs | Sage gross INR | Sage tax INR | SME docs | SME billAmount INR | SME gstAmount INR | SME vendor credit INR | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-01 | 7,889 | 1,102,073,079.67 | 37,864,975.27 | 2,693 | 137,340,257.77 | 10,391,652.89 | 136,905,289.74 | 34.1 % |
| 2026-02 | 6,138 | 668,327,183.09 | 28,850,642.88 | 2,222 | 120,195,771.10 | 8,173,184.13 | 119,726,526.43 | 36.2 % |
| 2026-03 | 7,863 | 1,237,739,593.62 | 33,152,701.58 | 3,098 | 139,773,101.96 | 9,944,635.95 | 139,362,594.77 | 39.4 % |
| 2026-04 | 5,519 | 698,465,329.22 | 32,991,125.88 | 1,358 | 101,356,897.95 | 6,547,551.61 | 101,139,100.49 | 24.6 % |
| **Total** | **27,409** | **3,706,605,185.60** | **132,859,445.61** | **9,371** | **498,666,028.78** | **35,057,024.58** | **497,133,511.43** | **34.2 %** |

`billDate` matches Sage `DATEINVC` on **all 9,371** documents (0 exceptions), and `voucherDate`
equals `billDate` on all of them. Dates are the one thing that is completely right.
**DATABASE VERIFIED / VERIFIED.**

### 2.7 By source and by bill type

| Sage source | Sage docs | Sage gross INR | Migrated | Coverage | Sage gross migrated INR |
|---|---:|---:|---:|---:|---:|
| AP-direct (`SRCEAPPL='AP'`) | 12,781 | 1,763,950,260.97 | 9,244 | 72.3 % | 493,639,591.48 |
| PO-matched (`SRCEAPPL='PO'`) | 14,628 | 1,942,654,924.63 | 127 | **0.87 %** | 4,951,880.02 |

| SMEAssist `billType` | Bills | Sage gross INR | SME billAmount INR | Δ INR | Sage tax INR | SME gst INR | Δ INR |
|---|---:|---:|---:|---:|---:|---:|---:|
| `PURCHASE` | 6,220 | 275,009,737.26 | 274,441,162.42 | −568,574.84 | 20,595,707.75 | 21,415,664.97 | +819,957.22 |
| `IN_DIRECT_EXPENSE` | 2,626 | 77,438,776.84 | 78,004,114.90 | +565,338.06 | 5,918,730.34 | 6,484,068.40 | +565,338.06 |
| `DIRECT_EXPENSE` | 525 | 146,142,957.40 | 146,220,751.46 | +77,794.06 | 7,079,497.15 | 7,157,291.21 | +77,794.06 |

(These three rows cover the 9,371 `billId`-joined documents; the five `preexisting` bills, all
`PURCHASE`, bring the live `PURCHASE` count to 6,225.)

For the two expense types the document-value delta **is exactly** the tax delta — i.e. taxable
value ties to the rupee and the whole difference is the RCM gross-up. For `PURCHASE` the extra
−₹1,388,532 of taxable is the two truncated `FABI470` documents. **DATABASE VERIFIED / VERIFIED.**

### 2.8 Expense-side coverage

```sql
-- Sage 4E distribution lines on in-window type-12 documents
SELECT LEFT(RTRIM(d.IDGLACCT),2), SUM(d.AMTDIST) FROM APOBL b JOIN APIBD d
  ON d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM
 WHERE b.DATEINVC BETWEEN 20260101 AND 20260430 AND b.IDTRXTYPE=12 AND d.IDGLACCT LIKE '4E%';
```
Total ₹760,527,671.42; on migrated documents ₹459,680,034.42 — **60.4 % of the AP-side expense
charge**. The gap is mostly the 1,071 AP-direct documents with no `APIBD` row (₹289 M) and the
285 with a balance-sheet leg (₹489 M). **DATABASE VERIFIED.**

---

## 3. Tax reconciliation

### 3.1 CGST / SGST / IGST, forward charge

Sage carries the authority split on the header: `CODETAX1/2` with `AMTTAX1HC/AMTTAX2HC`.
`LOCAL` documents book **SGST + CGST**, `INTERSTATE` documents book **IGST**:

```sql
SELECT RTRIM(CODETAX1), RTRIM(CODETAX2), RTRIM(CODETAXGRP), COUNT(*),
       SUM(AMTTAX1HC), SUM(AMTTAX2HC)
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430 AND IDTRXTYPE=12 GROUP BY 1,2,3;
-- IGST |      | INTERSTATE | 16,008 | 93,466,010.64 |          0.00
-- SGST | CGST | LOCAL      | 11,295 | 19,672,456.87 | 19,672,683.38
-- NRST |      | NRST       |     65 |     48,294.72 |          0.00
-- (NOTAX*, TAXEXEMPT, VAT, NRVAT groups: zero tax)
```

SMEAssist has **no** CGST/SGST/IGST columns on `bill` — only `gstAmount` and `isInterState`.
The split exists only in the ledger, as rate-labelled finance accounts. I classified every
voucher leg on the 9,371 joined bills by parsing the ledger name
(`^(CGST|SGST|IGST)\s+(Input|Payable RCM)(\s*\(Import\))?\s*@\s*([0-9.]+)\s*%`):

| Leg kind | INR |
|---|---:|
| `CGST Input @ x %` | 6,313,324.33 |
| `SGST Input @ x %` | 6,313,324.33 |
| `IGST Input @ x %` | 22,430,375.92 |
| `IGST Input (Import) @ 0.00 %` (customs pass-through, carries no tax) | 691,279.00 |
| `CGST Payable RCM @ x %` | 672,945.23 |
| `SGST Payable RCM @ x %` | 672,945.23 |
| `IGST Payable RCM @ x %` | 186,626.90 |

Netting the reverse-charge input out of the input legs gives the **forward-charge** comparison:

| Head | Sage INR | SMEAssist INR | Δ INR |
|---|---:|---:|---:|
| CGST | 5,649,748.05 | 5,640,379.10 | −9,368.95 |
| SGST | 5,649,748.05 | 5,640,379.10 | −9,368.95 |
| IGST | 22,294,439.14 | 22,243,749.02 | −50,690.12 |
| **Total** | **33,593,935.24** | **33,524,507.22** | **−69,428.02** |

**₹69,426.56 of that ₹69,428.02 is the two truncated `FABI470` documents** (§2.5); the residual
₹1.46 is paise rounding across 2,697 documents. Per-document forward-tax deltas: 5,764 exactly
zero, 2,697 within ₹0.01, 7 within ₹1, 14 within ₹100, and the rest are the RCM population.
**DATABASE VERIFIED / VERIFIED.**

Sage's ₹48,294.72 of `NRST` authority tax (65 documents) is in the excluded tax groups and
reached SMEAssist as **zero**. **DATABASE VERIFIED.**

### 3.2 Place of supply — is the intra/inter split legally right?

This is the test that a netting reconciliation cannot see. For every migrated document I
compared Sage's `CODETAXGRP` against the *actual* SMEAssist tax legs (pass-through 0 % legs
excluded, since they carry no tax):

| Sage | SMEAssist | Documents |
|---|---|---:|
| INTERSTATE | IGST only | 5,921 |
| LOCAL | CGST+SGST only | 2,921 |
| NOTAX / other | no tax leg | 259 |
| LOCAL | no tax leg | 202 |
| INTERSTATE | no tax leg | 37 |
| NOTAX / other | CGST+SGST | 24 |
| **LOCAL** | **IGST — WRONG** | **4** |
| **INTERSTATE** | **CGST+SGST — WRONG** | **3** |

**7 documents carry the wrong GST head, ₹20,022.90 of tax.** Small in value, but each of these
is a wrong line in GSTR-3B and a credit taken from the wrong pool:

| Document | Sage | SMEAssist | Tax INR |
|---|---|---|---:|
| `SELD078 \| KXM/BR0639/25-26` | LOCAL | IGST | 15,426.00 |
| `SELD078 \| KXM/BR0534/25-26` | LOCAL | IGST | 1,944.00 |
| `SELD078 \| KXM/BR0596/25-26` | LOCAL | IGST | 1,057.50 |
| `SELD078 \| KXM/BR0532/25-26` | LOCAL | IGST | 952.20 |
| `OTHI121 \| IE29/25-26/02389` | INTERSTATE | CGST+SGST | 246.60 |
| `OTHI121 \| IE29/25-26/02390` | INTERSTATE | CGST+SGST | 246.60 |
| `OTHL292 \| E1502` | INTERSTATE | CGST+SGST | 150.00 |

**The split is not netting to the right total while being wrong on every bill** — I tested for
exactly that and it is not what is happening. 8,842 of 8,849 taxed documents agree with Sage's
place of supply. The 7 are individual contact-state defects, not a systematic inversion.
**DATABASE VERIFIED / VERIFIED.**

Three further documents (`SELD292 | C3912`, `C4256`, `C252620688`) carry ₹15,588.00 of
SMEAssist input tax where Sage records none — these are the customs/courier pass-through cases
booked to an IGST-input head. Sage books the same amounts to `2A7TX04` "IGST Recoverable on
Imports", so the *head* agrees; what differs is that Sage does not count them in `AMTTAXHC`.
**DATABASE VERIFIED / HIGH.**

### 3.3 Reverse charge (RCM)

**The true RCM population in Sage** is documents where an RCM payable head is **credited**:

```sql
SELECT RTRIM(b.IDVEND), RTRIM(b.IDINVC), RTRIM(d.IDGLACCT), d.AMTDIST
  FROM APOBL b JOIN APIBD d ON d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM
 WHERE b.DATEINVC BETWEEN 20260101 AND 20260430 AND b.IDTRXTYPE=12
   AND LEFT(RTRIM(d.IDGLACCT),7) IN ('1L8TX14','1L8TX15','1L8TX16');
```
2,168 lines over **1,140 documents**. 13 of those lines are *debits* — the five monthly RCM
settlement documents booked through vendor `OTHX003` ("SGST PAYABLE RCM JAN-26" etc.), which are
liability clearances, not purchases. Excluding them:

| | Documents | Self-assessed GST INR |
|---|---:|---:|
| **True RCM population in Sage, Jan–Apr 2026** | **1,135** | **1,944,885.80** (SGST 785,497.00 / CGST 786,796.00 / IGST 372,592.80) |
| Migrated | 901 | 1,532,702.00 |
| **Not migrated** | **234** | **412,183.80** |

**What SMEAssist recorded on the 901 that crossed:** ₹1,532,517.35 against Sage's ₹1,532,702.00 —
**Δ −₹184.65, −0.0120 %.** Per document: 234 exact, 369 within ₹0.50, 281 within ₹1.00, 17 within
₹5.00; net −₹184.65 made of +₹118.55 and −₹303.20. Scattered, not clustered — this is rounding,
not a bug. **RCM intra/inter split disagreements: 0.**

The ledger treatment is **correct**, and better than Sage's:

```
FABL164 | AL-2025-0028   (Sage: gross 17,740.00, AMTTAXHC 0.00, 1L8TX14 −444, 1L8TX15 −444)
  DEBIT  17,740.00  Carriage Inward, IDEPL-1_SAGE-4E2ME13-01 Expense
  DEBIT     443.50  CGST Input @ 2.50 %
  DEBIT     443.50  SGST Input @ 2.50 %
  CREDIT 17,740.00  Gst_29XXXP…_APEXTRANSIT LOGISTICS_HN87SY   <- vendor gets taxable only
  CREDIT    443.50  CGST Payable RCM @ 2.50 %
  CREDIT    443.50  SGST Payable RCM @ 2.50 %
```
The vendor is credited ₹17,740.00, not ₹18,627.00. Sage booked ₹444 + ₹444 = ₹888 (whole rupees
per authority); SMEAssist booked ₹443.50 + ₹443.50 = ₹887.00 at the legal 5 % slab. **The
platform is arithmetically right and Sage is the one rounding**, but the two GST registers will
not agree to the rupee. **DATABASE VERIFIED / VERIFIED.**

**The exposure is completeness, not accuracy: ₹412,183.80 of self-assessed GST liability on 234
documents is simply absent from the target's GST Output account** — which currently reads
₹1,532,517.35 where Sage's window liability is ₹1,944,885.80, a **21.2 % understatement of the
reverse-charge liability**.

`work/rcm_variance.json` (the project's own artefact, 245 entries) shows the same shape: deltas
of −1.00, +0.78, +0.08, −1.00 … i.e. per-document rounding. **DOCUMENTED, corroborated.**

**Reverse-charge heads the loader does not handle at all** (`RCM_GL` covers only
`1L8TX14/15/16`, `post_sage_bills.py:94`):

| Head | Name | Lines | Amount INR |
|---|---|---:|---:|
| `1L8TX04` | Professional Tax | 21 | 717,800.00 |
| `1L8TX11` | SGST Payable (forward) | 4 | 1,890,800.00 |
| `1L8TX12` | CGST Payable (forward) | 4 | 1,890,800.00 |
| `1L8TX13` | IGST Payable (forward) | 4 | 6,433,947.00 |

These are the monthly statutory settlement documents. They are excluded by the header filter
(balance-sheet leg) and never posted — correct for a *bill* migration, but it means
**₹10,933,347.00 of tax-settlement activity in the window has no target record at all**.
**DATABASE VERIFIED / HIGH.**

### 3.4 Rounding drift

| Measure | Value |
|---|---|
| Documents with 0 < \|Δ\| ≤ ₹0.01 on `billAmount` | 2,693 |
| Net drift | **−₹1.375099** |
| Positive drift (1,290 docs) | +₹3.853891 |
| Negative drift (1,403 docs) | −₹5.228990 |
| Documents with ₹0.01 < \|Δ\| ≤ ₹100 | 22, net +₹1,121.96 (RCM gross-up) |
| Sage round-off head `4E1M016` in window | 574 lines, ₹61.04 (₹44.25 on migrated documents) |
| SMEAssist `bill.roundOffAmount` on migrated bills | ₹0.20 |

**Rounding is not accumulating in one direction** — the split is 1,290 up / 1,403 down and the
net is under ₹1.40 across the whole population. The Sage round-off account is carried as a 0 %
expense **line** (`Round Off Value on Purchases_SAGE-4E1M016 Expense`, 399 legs) rather than in
`roundOffAmount`; the accounting result is the same head, the presentation differs.
**DATABASE VERIFIED / VERIFIED.**

---

## 4. Rate snapping — is `snap_to_slab` doing harm or good?

`post_sage_bills.py` holds two rules: **read the rate Sage states, never divide to infer one**
(README "defect 4.1", implemented in `line_rate()` at line 1347), and **snap a derived rate to a
legal slab or refuse** (`snap_to_slab()` at line 1381, `LEGAL_SLABS` at line 294). I tested both
against the data.

### 4.1 Does Sage ever state a rate that is not a legal slab?

```sql
-- 46,156 AP-direct distribution lines in the window
--   RATETAX1, RATETAX2 rounded to 2 dp each, summed
```

| | Lines |
|---|---:|
| Lines stating a rate | 29,917 |
| … whose stated rate **is** a legal slab | **29,917 (100 %)** |
| … whose stated rate is **not** a legal slab | **0** |
| Lines stating **no** rate at all (`RATETAX1 = RATETAX2 = 0`) | 16,239 |

**Sage never states an illegal rate on an AP-direct line in this window.** The loader's refusal
to divide is therefore not defensive theatre — it is load-bearing. Had it divided, the derived
rate would have been off-slab on **5,507 lines**:

| Derived (tax ÷ base) rate | Lines | Taxable INR |
|---|---:|---:|
| 17.99 | 1,603 | 108,132.33 |
| 18.01 | 1,594 | 109,551.51 |
| 18.02 | 401 | 13,860.84 |
| 17.98 | 391 | 13,968.87 |
| … 60 more values (17.92 – 18.06, 4.99, 5.01, …) | 1,518 | 30,338.12 |
| **Total** | **5,507** | **275,851.67** |

The whole population sits on ₹275,851.67 of taxable value — these are tiny bases where Sage's
per-authority truncation moves the second decimal of the implied rate. **DATABASE VERIFIED /
VERIFIED. Defect 4.1's fix is correct and materially matters on 5,507 lines.**

### 4.2 What did the loader post?

```sql
SELECT gstPercentage, COUNT(*), SUM(taxableAmount) FROM billLineItem li
  JOIN bill b ON b.id=li.billId
 WHERE li.organisationId='1029113552088076445' AND b.billStatus='ACTIVE' AND b.isDeleted=0
   AND li.isDeleted=0 GROUP BY 1;
```

| `gstPercentage` | Lines | Taxable INR | Legal slab? |
|---|---:|---:|---|
| 18.00 | 20,227 | 110,142,104.99 | yes |
| 5.00 | 6,921 | 304,628,913.65 | yes |
| 0.00 | 2,441 | 48,837,985.36 | yes |

**Zero posted lines carry an illegal rate on the live population** (29,589 lines checked). The
live GST ledger set is 24 accounts, all at legal half-slabs (`@ 2.50 %`, `@ 9.00 %`) or full slabs
(`@ 5.00 %`, `@ 18.00 %`). **DATABASE VERIFIED / VERIFIED.**

### 4.3 The earlier run *did* post illegal rates — and the evidence is still there

**60 soft-deleted finance accounts** survive in the org's chart at rates that are not legal
slabs: `CGST Input @ 2.46 / 2.48 / 2.49 / 2.51 / 2.52 / 2.53 / 2.66 / 6.71 / 9.01 %`,
`IGST Input @ 4.76 / 4.86 / 4.92 / 4.95 / 4.99 / 5.01 / 5.02 / 5.45 %`, and the matching
`Payable RCM` heads, plus `CGST Input @ 0.32 / 0.35 / 1.25 / 1.34 / 2.29 %`. Every one is
`isDeleted=1` with `netBalance = 0.00`, and they are attached only to the 1,393 `REVOKED` bills
whose voucher entries are also soft-deleted. **They do not affect the live trial balance**, but
they are the fingerprint of the pre-fix behaviour and they remain visible in the chart of
accounts. **DATABASE VERIFIED / VERIFIED.**

### 4.4 Snapping on the reverse-charge path — how far did it move the rate?

RCM is the only path where Sage states no rate and the loader must derive one. Derived rate =
self-assessed GST ÷ `AMTINVCHC` (which *is* the taxable base on an RCM document), snapped to the
nearest legal slab:

| Snap distance | Documents | Taxable INR |
|---|---:|---:|
| exact (0 pp) | 266 | 5,425,281.24 |
| ≤ 0.01 pp | 568 | 18,373,789.33 |
| ≤ 0.10 pp | 67 | 357,194.09 |
| ≤ 0.50 pp | 0 | 0.00 |
| > 0.50 pp | 0 | 0.00 |

**No RCM document was moved more than 0.10 percentage points.** The resulting tax variance is
−₹184.65 across ₹1,532,702.00 (§3.3) and it scatters both ways. **The snapping is defensible and
the "right answer" is the slab, not Sage's whole-rupee-per-authority figure.**
**DATABASE VERIFIED / VERIFIED.**

### 4.5 Bills where the migrated tax differs from Sage's stated tax

| Cause | Documents | Value at stake INR |
|---|---:|---:|
| Split-document truncation (`FABI470 ×2`) — tax lost with the missing part | 2 | **69,426.56** |
| RCM slab snapping (per-document rounding, both directions) | 667 | net **−184.65** |
| Paise rounding on forward charge | 2,697 | net **−1.46** |
| Place-of-supply misclassification (right total, wrong head) | 7 | **20,022.90** |
| Pass-through import IGST counted as tax by SMEAssist, not by Sage's `AMTTAXHC` | 128 legs / 125 bills | **691,279.00** |

Everything else ties. **DATABASE VERIFIED / VERIFIED.**

---

## 5. Opening balances, payments, notes — what is and is not migrated

### 5.1 Opening balances: **not loaded. Zero rows.**

```sql
SELECT COUNT(*) FROM accountingLedger WHERE organisationId='1029113552088076445';   -- 0
SELECT referenceType, COUNT(*) FROM voucherEntry
 WHERE organisationId='1029113552088076445' AND isDeleted=0 GROUP BY 1;             -- BILL 52,747 (only)
```

Every live ledger entry in the target org originates from a migrated **bill**. There is no
opening-balance voucher, no journal, no `accountingLedger` row. **DATABASE VERIFIED / VERIFIED.**

**The gap, quantified.** Sage's creditor control heads (`1L6T*` Trade Payables plus `1L7C/J/M/O/S`
vendor heads — 65 accounts) from `GLAFS`, `FSCSYR='2026'`, `CURNTYPE='F'`, `OPENBAL + NETPERD1..9`
= balance at **31 Dec 2025**, the instant before the window opens:

| At 31 Dec 2025 | Accounts | INR |
|---|---:|---:|
| Credit balances (amounts owed) | 27 | **−535,122,264.66** |
| Debit balances (prepayments / A-P clearing) | 23 | +220,879,093.81 |
| **Net creditor position** | | **−314,243,170.85** |

The five largest: `1L6TF02` Fabric Vendors–Interstate ₹303,960,454.02; `1L6TA02` Accessories–
Interstate ₹42,407,108.34; `1L7JV04` Job Work–Foreign ₹38,015,411.31; `1L6TF03` Fabric–Imports
₹28,523,477.21; `1L6TA01` Accessories–Local ₹23,481,981.58. **DATABASE VERIFIED / VERIFIED.**

**Consequence, stated plainly:** the target's payables ledger begins at **zero** on 1 Jan 2026.
₹535.1 M of gross creditor balances (₹314.2 M net of advances) that existed on that date are
simply not there. No vendor balance in SMEAssist can equal the vendor's balance in Sage, and no
ageing report in SMEAssist can be correct, because every invoice older than 1 Jan 2026 is absent.

### 5.2 Payments and payment allocation: **not migrated. Zero live rows.**

| Check | Result |
|---|---|
| `voucherEntry` legs where `remainingAmount <> amount` | **0** |
| Vendor legs: `SUM(remainingAmount)` vs `SUM(amount)` | ₹497,239,932.81 vs ₹497,239,932.81 — **identical** |
| `voucherEntryMapping` (the credit↔debit allocation table) for the org | 5 rows, **all `isDeleted=1`**, all attached to `referenceType='NOTE'` entries from the 31 Aug 2026 smoke test |
| `paymentRequest`, `paymentRequestAdvanceBillMapping` | **0 rows** |

**Every one of the 9,376 migrated bills is 100 % unpaid.** The claim in the backend tickets that
a payment-allocation sheet was newly built is not contradicted — the *table* exists
(`voucherEntryMapping`: `creditVoucherEntryId`, `debitVoucherEntryId`, `mappedAmount`) — but
**no allocation data reached this org.** **DATABASE VERIFIED / VERIFIED.**

**What Sage says about the same documents:**

```sql
SELECT SWPAID, COUNT(*), SUM(AMTINVCHC), SUM(AMTDUEHC) FROM APOBL
 WHERE IDTRXTYPE=12 AND DATEINVC BETWEEN 20260101 AND 20260430 GROUP BY SWPAID;
-- SWPAID=1 : 30,138 docs | 3,210,883,096.18 gross |         0.00 still due
-- SWPAID=0 :    689 docs |   495,722,089.42 gross | 429,116,850.35 still due
```

**97.8 % of the source documents are marked fully paid in Sage.** SMEAssist shows 100 % of the
migrated subset as outstanding. The claim "every migrated bill shows as unpaid" is **verified,
and it is worth ₹497,239,932.81 of fictitious outstanding payables.**

Payment activity that occurred inside the window and was not carried: **80,388 applications
against 39,707 documents, net ₹141,610,118.14** (`SELECT COUNT(*), COUNT(DISTINCT vendor||invoice),
SUM(AMTPAYMHC) FROM APOBP WHERE DATEBUS BETWEEN 20260101 AND 20260430`). **DATABASE VERIFIED.**

### 5.3 Credit notes, debit notes, prepayments, adjustments

| Sage document type | In window | Gross INR | Tax INR | In SMEAssist |
|---|---:|---:|---:|---|
| 32 — Credit note | 2,066 (INR 2,057) | −407,960,126.59 (INR −398,419,621.73) | −7,676,351.58 | **none** |
| 22 — Debit note | 137 (INR 128) | +67,596,606.48 (INR +20,500,152.83) | +134,085.22 | **none** |
| 50 — Prepayment | 1,729 | −1,548,255,387.03 | 0.00 | **none** |
| 51 — Payment | 3,274 | −1,860,075,231.25 | 0.00 | **none** |

```sql
SELECT noteType, noteStatus, isDeleted+0, COUNT(*), SUM(totalAmount) FROM creditDebitNote
 WHERE organisationId='1029113552088076445' GROUP BY 1,2,3;
-- CREDIT_NOTE | REVOKED | 1 | 46 | 9,357,923.58
-- DEBIT_NOTE  | REVOKED | 1 | 50 | 3,390,121.73
```

All 96 note records were created on 31 Aug 2026, `REVOKED` and soft-deleted — a smoke test.
**The target holds zero live credit notes, debit notes, prepayments, payments or adjustments.**
**DATABASE VERIFIED / VERIFIED.**

Sage's in-window credit notes include TDS deductions booked as credit notes
(`TDS 0.1% ON RS 578448`, etc.). SMEAssist's `bill.txsType / txsAmount / txsPercentage` are
**NULL or zero on all 9,376 migrated bills**, as are `discountAmount`, `extraCharges` and
`cessPercentage`; `currency='INR'` and `conversionRate=1` on all of them. **DATABASE VERIFIED.**

### 5.4 Where that leaves the payables balance

| Figure | INR |
|---|---:|
| SMEAssist `Current Liabilities > Creditor > Vendor` (302 party ledgers) | **497,239,932.81** |
| Sage creditor control heads, credit side, **at 1 Jan 2026** | 535,122,264.66 |
| Sage creditor control heads, credit side, **at 30 Apr 2026** | 629,943,197.98 |
| Sage creditor control heads, **net of prepayments, at 30 Apr 2026** | 429,484,090.46 |
| Sage AP invoice value in the window (type 12) | 3,706,605,185.60 |

**The migrated payables cannot be tied to Sage's payables.** ₹497,239,932.81 is not the opening
balance, not the closing balance, and not the period movement. It is the arithmetic sum of the
34 % of invoices that happened to load, with no opening balance under it and no payment,
credit note or advance applied against it. **DATABASE VERIFIED / VERIFIED.**

---

## 5A. What the backend actually does — corroboration from source

All **BACKEND VERIFIED**, from `/home/namansharma/Desktop/PROJECTS/smeassist`:

| Claim | Source |
|---|---|
| Bill legs are built by `EntityVoucherEntryCreateHelperService.getVoucherBreakageForBill(BillDto)`, fired from `BillServiceImpl.publishVoucherCreateRevokeEvent` → `VoucherCreationListener.billVoucherCreate` | `core/.../ledger/helper/EntityVoucherEntryCreateHelperService.java:2815‑3182`; `core/.../bill/service/Impl/BillServiceImpl.java:1381‑1417` |
| **The intra/inter split is *not* read from `bill.isInterState`.** It is recomputed at leg-build time: `isIgst = companyBillingAddressDto.getState() != contactBillingAddressDto.getState()` | `EntityVoucherEntryCreateHelperService.java:3057‑3065`; `commons/.../invoice/dto/BillDto.java:245‑247` |
| GST ledger names come from the **submitted `gstPercentage`** (`gstPercentage/2` for CGST/SGST), rounded to 2 dp — never back-computed from tax ÷ taxable. Pattern `"%s Input @ %s %%"`, `"%s Payable RCM @ %s %%"` | `ledger/commons/.../FinanceAccountUtils.java:172‑195`; `FinanceAccountConstant.java:12‑31` |
| RCM: the vendor leg is `billAmount − totalRCMApplied − TDS − GST-TDS`, so the party is credited taxable only; the RCM GST is credited to `… Payable RCM @ x %` and debited to `… Input @ x %` | `EntityVoucherEntryCreateHelperService.java:3116‑3166` |
| **There is no server-side GST-slab validation.** `BillLineItemCreateUpdateDto.gstPercentage` is an unannotated `BigDecimal`; the only slab list in the codebase is an Excel dropdown on the item-master template. Any rate — 2.51 %, 19 %, negative — is accepted and mints a matching ledger. | `commons/.../BillLineItemCreateUpdateDto.java:58`; `core/.../ItemMasterBulkServiceImpl.java:424‑432` |
| `billAmount` is **silently recomputed and overwritten** server-side (`setBillAmount(totalPrice)`, `setRoundOffAmount(...)`) — a mismatch is logged, never rejected. Only `billAmount < 0` throws. | `BillServiceImpl.java:2124‑2132, 2187‑2215` |
| Payment allocation *is* `voucherEntryMapping` (`creditVoucherEntryId`, `debitVoucherEntryId`, `mappedAmount`); `remainingAmount` starts equal to `amount` and only drops when a mapping is written | `VoucherEntryHelperServiceImpl.java:265‑271`; `VoucherEntryMappingServiceImpl.java:113‑136` |
| The recent bulk work is `PaymentRequestBulkUploadService` — **bulk creation of standalone payment requests with `entityId = null` (unallocated)**, not a bill-allocation sheet. No `*PaymentAllocation*` sheet class exists. | `core/.../service/impl/PaymentRequestBulkUploadService.java:343` |
| **Opening balances are supported**: `FinanceAccountServiceImpl.createFinanceAccount` posts a `VoucherType.VIRTUAL_VOUCHER` numbered `OP_BL_…` against a contra ledger named "Opening Balance"; loadable via the org-onboarding Excel (`FinanceAccountUploadSheetDto.openingBalance`) or `FinanceAccountClosingBalanceBulkUpdateServiceImpl` | `ledger/core/.../FinanceAccountServiceImpl.java:162‑217`; `VoucherEntryHelperServiceImpl.java:125‑176`; `FinanceAccountConstant.java:107` |
| `ReferenceType` has 44 members (BILL, INVOICE, PAYMENT, NOTE, JOURNAL, DEPRECIATION, …); `VoucherType` has 41 | `commons/.../ledger/enums/ReferenceType.java:14‑58`; `commons/.../enums/VoucherType.java:14‑89` |

Checked against the data:

```sql
SELECT voucherType, COUNT(*) FROM voucherEntry
 WHERE organisationId='1029113552088076445' AND isDeleted=0 GROUP BY 1;
-- PURCHASE 33,834 | INDIRECT_EXPENSE 16,398 | EXPENSE 2,515      (3 of 41 types)
SELECT COUNT(*) FROM voucherEntry WHERE organisationId='1029113552088076445'
   AND voucherNumber LIKE 'OP\_BL\_%';                            -- 0
SELECT id FROM financeAccount WHERE organisationId='1029113552088076445'
   AND name='Opening Balance';                                    -- no rows
```

**The platform has an opening-balance mechanism and the migration did not use it.** That is a
choice that was made, not a capability that was missing. **DATABASE + BACKEND VERIFIED /
VERIFIED.**

Two consequences of the backend behaviour worth flagging:

- Because the split is recomputed from the **contact's billing-address state**, the 7 wrong
  place-of-supply bills in §3.2 are a **contact-master defect**, not a payload defect: the loader
  can send whatever it likes for `isInterState` and the ledger will ignore it.
- Because `billAmount` is **overwritten** by `Σ(unitPrice × quantity) + tax + round-off` computed
  at `decimal(19,6)`, the 2,731 fractional-paise `billAmount` values in §2.5 are produced by the
  **server**, not by the loader. `assert_invariants()` (`post_sage_bills.py:2718`) checks the
  payload before it is sent; it cannot see the value the server stores. That is exactly the class
  of drift `readback_drift()` (`post_sage_bills.py:2893`) exists to catch.

---

## 6. The sceptic's list — what would fail an audit

Ordered by the size of the hole.

### 6.1 The migration is 34 % complete and nobody can close a period on it
**Claim:** 18,033 of 27,409 Sage AP invoices in the window — **₹3,207,903,604.89 gross,
₹99,259,727.41 of input tax** — are not in SMEAssist. The PO-matched (goods) population is
**0.87 % migrated**: 127 of 14,628 documents.
**Evidence:** §2.3, §2.7. `SELECT` over `APOBL` type-12 in-window consolidated on the `*N` suffix,
joined to `bill` via `work/posted.log`.
**Why it fails:** an auditor cannot vouch a population that is missing two-thirds of itself, and
₹99.3 M of input tax credit that Sage claimed has no support in the target.
**To fix:** load the remaining 18,033 documents, or restate the scope of the migration as "a
partial extract, not a ledger".

### 6.2 The migrated payables cannot be tied to Sage's payables
**Claim, in those words: the migrated payables cannot be tied to Sage's payables.**
**Evidence:** SMEAssist creditors = **₹497,239,932.81**. Sage's creditor control heads read
**₹535,122,264.66 Cr at 1 Jan 2026**, **₹629,943,197.98 Cr at 30 Apr 2026** (₹429,484,090.46 net
of prepayments). Not one of those equals the target figure, and the target figure is not a
movement either (the window's invoice value is ₹3,706,605,185.60). §5.4.
**Why it fails:** there is no arithmetic that turns Sage's payables into SMEAssist's. Three
independent reasons compound: no opening balance, 66 % of invoices missing, no payments applied.
**To fix:** all three of §6.1, §6.3 and §6.4 together. Any one alone leaves the balance wrong.

### 6.3 No opening balances — the target ledger begins at zero
**Claim:** zero opening-balance vouchers, zero `accountingLedger` rows, no "Opening Balance"
finance account.
**Evidence:** §5.1, §5A. `voucherEntry.referenceType` is `BILL` on all 52,747 live legs; only 3
of 41 `voucherType` values are in use; `voucherNumber LIKE 'OP_BL_%'` returns 0.
**Value at stake:** **₹535,122,264.66** of creditor balances at the window start
(₹314,243,170.85 net of ₹220,879,093.81 of advances and A/P clearing).
**Why it fails:** every vendor balance, every ageing bucket and every creditors figure in the
target is wrong by the vendor's pre-2026 position.
**To fix:** the platform already supports it — load per-ledger opening balances through the
onboarding sheet (`FinanceAccountUploadSheetDto.openingBalance`) or the closing-balance bulk
update, dated 31 Dec 2025.

### 6.4 Every migrated bill is unpaid; 97.8 % of them are paid in Sage
**Claim:** 0 payments, 0 live allocations, `remainingAmount == amount` on every one of the
52,747 legs. Sage marks **30,138 of 30,827** in-window invoices `SWPAID=1`.
**Evidence:** §5.2.
**Value at stake:** **₹497,239,932.81** shown as outstanding that is, in Sage, almost entirely
settled; plus ₹141,610,118.14 of in-window payment applications (80,388 rows in `APOBP`) with no
target record.
**Why it fails:** the creditors listing is fiction, the ageing is fiction, and anyone paying from
it would pay twice.
**To fix:** migrate `APOBP` applications as `voucherEntryMapping` rows against the bill legs —
which requires the payment vouchers to exist first, which requires §6.1.

### 6.5 48 Sage expense accounts are split across two P&L groups
**Claim:** the same Sage GL account becomes **two** SMEAssist ledgers — `…_SAGE-4E4SD01 Expense`
(under *Direct Expenses*) **and** `…_SAGE-4E4SD01 Indirect Expense` (under *Indirect Expenses*) —
because the item ledger is minted from the **document's** `billType`, which is decided by a
majority vote over that document's expense lines (`bill_type_of`, `post_sage_bills.py:1079`).
**Evidence (DATABASE VERIFIED):** 48 of 313 Sage accounts affected, **₹139,577,534.48**.

| Sage account | as "Expense" (Direct) INR | as "Indirect Expense" INR |
|---|---:|---:|
| `4E4SD04` | 21,651,747.35 (145 legs) | 11,032,282.18 (48 legs) |
| `4E4SD01` Custom Clearance & Forwarding | 17,702,070.92 (11,742 legs) | 10,382,912.20 (5,840 legs) |
| `4E5O014` Professional Charges | 16,549,052.00 (64 legs) | 2,042,164.00 (38 legs) |
| `4E4SD03` Carriage Outwards | 9,626,522.05 (3,338 legs) | 3,935,615.80 (1,527 legs) |
| `4E2ME14` Freight Charges – Imports | 5,427,925.07 (503 legs) | 3,914,032.06 (505 legs) |
| `4E2ME13` Carriage Inward | 431,484.67 (74 legs) | 2,416,348.66 (494 legs) |
| … 42 more | | |

**Why it fails:** no Sage expense account reconciles to a single SMEAssist ledger, and the
Direct/Indirect split of the P&L — which drives gross margin — is decided by which invoice a
line happened to travel on. The loader's own docstring asserts the opposite ("the minority lines
keep their own head"); the data contradicts it.
**To fix:** derive the item ledger from the **line's** GL account group, not the document's
`billType`. The mapping already exists per account (`BILLTYPE_BY_GROUP`, `post_sage_bills.py:135`).

### 6.6 Split documents lose a part, silently
**Claim:** `base_invoice()` consolidates Sage's `*1`/`*2` receipt parts into one key, but only one
part was posted on 2 documents.
**Evidence:** §2.5. `FABI470|1173/2025-26` lost ₹805,684.01; `FABI470|1177/2025-26` lost
₹652,273.65. Both parts exist in `APOBL` under `CNTBTCH 17228`.
**Value at stake:** **₹1,457,957.66** of payable and **₹69,426.56** of input tax.
**Why it fails:** it is a silent under-statement with no error, no log entry and no failure
report — and it is the *entire* per-document value variance of the migration.
**To fix:** assert `Σ(parts posted) == Σ(APOBL parts)` per consolidated key before accepting a
document as done. Two documents today; the mechanism scales with the goods population, which is
where all 3,418 multi-part documents live.

### 6.7 Two documents dropped by a vendor-code collision
**Claim:** Sage carries the same supplier under two vendor codes (`ACCL271` and `MNTL523`, both
GSTIN `29XXXP…`), with the same invoice numbers. The loader saw `ST2273/25-26` and
`ST2280/25-26` already posted under `ACCL271` and recorded `MNTL523`'s as **`preexisting`**.
**Evidence:** `work/posted.log` lines `MNTL523|ST2273/25-26||preexisting`; `APOBL` shows
`MNTL523 | ST2273/25-26 | 20260331 | 84,882.12` and `ACCL271 | ST2273/25-26 | 20260117 |
84,882.00` — different dates, different amounts, different documents.
**Value at stake:** **₹102,434.62** gross, ₹15,625.62 tax.
**Why it fails:** the duplicate test is `billNumber` within a merged contact. 7 SMEAssist
contacts absorb 2–14 Sage vendor codes each (₹23,814,466.84 of posted value), so the collision
surface is real. It also means Sage may itself hold a duplicated supplier worth investigating.
**To fix:** key the duplicate test on the Sage document identity carried in `metaData`, never on
`billNumber`.

### 6.8 Two bills exist with no accounting entries at all
**Claim:** `1544944925505781760` (`KA-B1-157425684`, ₹1,709.82) and `1544948068155162624`
(`103326913568`, ₹1,978.01) are `ACTIVE`, not deleted, and have **zero `voucherEntry` rows**.
**Evidence:** `SELECT COUNT(*) FROM bill b WHERE … AND NOT EXISTS (SELECT 1 FROM voucherEntry ve
WHERE ve.referenceId=b.id)` → 2. §2.3.
**Why it fails:** a document that appears in the purchase register and not in the ledger is an
audit finding on its own, whatever the amount. It also proves the create-then-post path is not
atomic.
**To fix:** re-post the voucher for those two, and add a post-run assertion that
`COUNT(bill) == COUNT(DISTINCT voucherEntry.referenceId)`.

### 6.9 GST is wrong on 7 bills and the platform will not stop it happening again
**Claim:** 4 `LOCAL` documents booked IGST and 3 `INTERSTATE` documents booked CGST+SGST,
**₹20,022.90** of tax on the wrong head. Separately, **the backend performs no GST-slab
validation at all** — `gstPercentage` is an unannotated `BigDecimal` and any value mints a
matching ledger.
**Evidence:** §3.2 (data), §5A (`BillLineItemCreateUpdateDto.java:58`). The proof that it can
happen: **60 soft-deleted finance accounts** at rates like `CGST Input @ 2.51 %`,
`IGST Input @ 4.76 %`, `CGST Input @ 6.71 %`, left over from the pre-fix run (§4.3).
**Why it fails:** a wrong head is a wrong GSTR-3B line and a credit taken from the wrong pool —
it nets to the right total and is still illegal on every one of those bills. The only thing
standing between the target and illegal rates is `snap_to_slab()` in a Python script.
**To fix:** correct the 4 contacts' billing-address state (the split is recomputed from
`contactBillingAddressDto.getState()`, so fixing the contact fixes the ledger), and raise the
missing server-side slab validation as a platform defect.

### 6.10 ₹412,183.80 of reverse-charge liability is missing
**Claim:** 234 of the 1,135 in-window RCM documents did not migrate.
**Evidence:** §3.3. Target GST Output reads ₹1,532,517.35 against Sage's ₹1,944,885.80 — a
**21.2 % understatement of the self-assessed GST liability**.
**Why it fails:** RCM liability is a statutory payment obligation; understating it understates
tax payable.
**To fix:** covered by §6.1.

### 6.11 Everything else Sage has, that the target does not
| Missing concept | In Sage, Jan–Apr 2026 | In target |
|---|---|---|
| Credit notes | 2,066 docs, ₹−407,960,126.59 (tax ₹−7,676,351.58) | 0 live |
| Debit notes | 137 docs, ₹67,596,606.48 | 0 live |
| Prepayments / advances | 1,729 docs, ₹−1,548,255,387.03 | 0 |
| Payments | 3,274 docs, ₹−1,860,075,231.25 | 0 |
| Non-INR documents | 815 type-12 docs, ₹757,366,728.49 | 0 (all excluded) |
| TDS / TCS withholding | booked as credit notes and `OAMTWHT*` | `txs*` NULL on all 9,376 bills |
| Tax settlement journals (`1L8TX11/12/13`, `1L8TX04`) | ₹10,933,347.00 | 0 |
| Payables analysis by category | 252 control accounts | one flat "Creditor > Vendor" group |

### 6.12 What is *right*, and should be said
Scepticism cuts both ways. On the 9,376 documents that did cross:

- **6,668 match Sage to the paise**; 2,693 more are within ₹0.01; only **3** differ by ≥ ₹1, and
  2 of those are one bug (§6.6).
- **Debits equal credits on every voucher** — org-wide DR ₹498,772,451.61 = CR ₹498,772,451.61,
  zero unbalanced vouchers, no bill line with a NULL ledger.
- **`billDate` matches Sage `DATEINVC` on 100 % of documents.**
- **Zero duplicate postings**, zero bills in SMEAssist that Sage does not have.
- **Rounding is not accumulating**: net −₹1.38 across 2,693 documents, 1,290 up / 1,403 down.
- **RCM is modelled correctly** — the vendor is credited taxable only — and to within −₹184.65
  on ₹1,532,702.00.
- **Place of supply agrees on 8,842 of 8,849 taxed documents.**
- **No posted line carries an illegal GST rate.**
- The **REVOKED** population and the illegal-rate ledgers from the earlier run are properly
  soft-deleted and do not touch the live trial balance.

Both of the "everything balances" claims were re-run for this report rather than taken from
`work/reconcile-report.json`:

```sql
SELECT COUNT(*) FROM (SELECT voucherId,
   SUM(CASE WHEN transactionType='DEBIT' THEN amount ELSE -amount END) r
   FROM voucherEntry WHERE organisationId='1029113552088076445' AND isDeleted=0
  GROUP BY voucherId HAVING ABS(r)>0.005) t;                          -- 0 unbalanced
SELECT COUNT(*) FROM billLineItem li JOIN bill b ON b.id=li.billId
 WHERE li.organisationId='1029113552088076445' AND li.isDeleted=0
   AND b.billStatus='ACTIVE' AND b.isDeleted=0 AND li.financeAccountId IS NULL;   -- 0
-- 9,374 vouchers, 52,747 legs
```
**DATABASE VERIFIED / VERIFIED.**

---

## 7. What I could NOT prove

1. **The AP sub-ledger opening balance as the AP module would report it.** I derived
   ₹535,122,264.66 Cr at 31 Dec 2025 from `GLAFS` over 65 GL heads (`1L6T*`, `1L7C/J/M/O/S*`).
   That is a **general-ledger** view, not an AP aged trial balance. I deliberately **excluded the
   `1L9O*` group**, which stands at **₹3,263,443,257.65 Cr** at the same date — but four of its
   accounts (`1L9OL04`, `1L9OL05`, `1L9OL06`, `1L9OL11` — Excel Apparels, Indian Designs Unit-7
   and Unit-9, OFB Tech) **do** act as AP control accounts in `APPJD` with `ACCTTYPE=1`. If those
   related-party balances belong in payables, the opening-balance gap is an order of magnitude
   larger than I have stated. **I could not determine the company's own definition of "trade
   payables" and did not guess.** Confidence: **MEDIUM** on the ₹535 M figure, **UNKNOWN** on
   whether it is the right boundary.
2. **Whether Sage's own AP is internally consistent.** `SUM(APOBL.AMTINVCHC)` across the whole
   table is −₹3,536,632,464.64 while `SUM(AMTDUEHC)` is ₹209,543,264.52; payment documents are
   themselves `APOBL` rows with negative gross, so the two are not a clean identity and I did not
   reconstruct the AP-to-GL tie-out. **UNKNOWN.**
3. **The goods (PO-matched) path against its real source.** I reconciled everything against
   `APOBL`. The goods loader reads `idedat_staging.sage_bill_hdr` / `sage_goods_line`, a MySQL
   mirror. I did **not** audit that mirror against Sage's PO and inventory tables, so I cannot say
   whether the 127 migrated goods bills carry the right quantities and unit costs — only that
   their document totals tie to `APOBL`. **UNKNOWN.**
4. **The 1,071 AP-direct documents (₹289,097,477.04) with no `APIBD` distribution row.** They
   exist in `APOBL` with a value; their distributions are not in `APIBD`. They may live in
   `APIBDA`/`APIBDO`, or be job-costed (`SWJOB`), or be summary-posted. I did not chase them.
   **UNKNOWN — and this is the single largest unexplained slice of the missing population.**
5. **Whether the migration was *intended* to carry only in-window transactions.** The absence of
   opening balances, payments and notes is consistent with a deliberately narrow scope. I have
   reported it as a defect because the target now presents a creditors balance that reads as
   complete. Whether that was the plan is a question for §8. **UNKNOWN.**
6. **Paid status as at 30 Apr 2026.** `APOBL.SWPAID` and `AMTDUEHC` are live columns read on
   5 Sep 2026, so "97.8 % paid" is *today's* state, not the window's. I reported the in-window
   payment applications (`APOBP.DATEBUS`, 80,388 rows, ₹141,610,118.14) separately and did not
   attempt to replay the sub-ledger to a date. **HIGH on the direction, MEDIUM on the number.**
7. **Whether the expense head each SKU maps to is correct in the client's chart-of-accounts
   sense.** I proved that 48 Sage accounts fragment across two P&L groups (§6.5); I could not
   prove which of the two is right for any given line without the client's intent.
8. **GST return impact.** Neither system holds filed GSTR data that I could reach, so I could not
   test whether either side agrees with what was actually filed. **UNKNOWN.**
9. **Sage FX policy for the 815 excluded non-INR documents** (₹757,366,728.49 at home-currency
   rates). I read `EXCHRATEHC` but did not test whether the home-currency amounts are the ones
   that should have migrated. **UNKNOWN.**
10. **Whether the 2,012 in-scope-but-unposted AP-direct documents were attempted.** I classified
    them by re-applying the loader's filter; the *reasons* (1,938 `no_vendor_contact`, etc.) come
    from `work/failures-report.json`, which I read but did not regenerate. **DOCUMENTED.**

---

## 8. Open questions for the human

1. **Is this a ledger or an extract?** If SMEAssist is meant to be the books, opening balances,
   payments, credit notes and the other 66 % of documents are mandatory and this migration is not
   ready. If it is a partial data load for analysis, say so in writing, because the target
   currently presents ₹497,239,932.81 of outstanding creditors that nobody owes.
2. **Does "payables" include the `1L9O*` related-party heads (₹3.26 bn at 31 Dec 2025)?** Four of
   them act as AP control accounts in Sage. The answer changes the opening-balance gap by an
   order of magnitude. (§7.1)
3. **What happened to the 1,071 AP-direct documents with no `APIBD` row, worth ₹289 M?** Where do
   their distributions live? (§7.4)
4. **Was the PO-matched population ever in scope?** 0.87 % of it migrated.
   `work/failures-report.json` reports 9,217 goods failures, mostly `no_vendor_contact`
   (8,271) — but that leaves roughly **5,260–5,280 goods documents that are neither posted nor
   reported as failures** (5,259 against the project's own staging universe of 14,603; 5,284
   against the 14,628 I measured in `APOBL`). The two universes also differ, which is itself
   worth resolving.
5. **Which of the two P&L heads is right for the 48 split accounts (₹139,577,534.48)?** Should
   `4E4SD01` Custom Clearance be Direct or Indirect? Sage's `GLAMF.ACCTGRPCOD` says `4E4S` →
   selling & distribution → Indirect for every one of its lines; the migration put 63 % of it in
   Direct. (§6.5)
6. **Are `ACCL271` and `MNTL523` the same supplier?** Same GSTIN, same invoice numbers, different
   dates and amounts (₹84,882.00 on 17 Jan vs ₹84,882.12 on 31 Mar). Either Sage has duplicate
   postings worth ₹102,434.62, or the migration dropped two real invoices. (§6.7)
7. **Should the 815 non-INR documents (₹757,366,728.49) have migrated at the Sage home-currency
   amount?** They are currently excluded outright.
8. **Should the four `SELD078` bills be IGST or CGST+SGST?** Sage says `LOCAL`; the SMEAssist
   contact's billing-address state says otherwise and the ledger follows the contact. Which
   master is right? (§3.2, §5A)
9. **Is the reverse-charge rounding difference acceptable to the tax team?** SMEAssist is
   arithmetically correct (₹443.50 at 2.5 %); Sage booked whole rupees (₹444). The GST registers
   will differ by ₹184.65 over the window on the migrated subset. Which figure was filed?
10. **Who owns the missing server-side GST-slab validation?** The platform will accept any
    `gstPercentage` and mint a ledger for it. Today only a Python script prevents that. (§5A, §6.9)

---

## Appendix — how to reproduce

Sage (read-only, `SELECT` only):
```bash
cd /home/namansharma/Desktop/sage-pull
.venv/bin/python -c 'import sys;sys.path.insert(0,"work");import sage;print(sage.q("<SQL>"))'
```
SMEAssist / staging (read-only):
```bash
DBH=$(sed -n 's/^SME_DB_HOST=//p' .env|head -1)
ssh root@$DBH 'mysql smeassist -t -e "<SQL>"'
```
Every SMEAssist query in this document is filtered on
`organisationId='1029113552088076445'`. Every Sage query is filtered on
`DATEINVC BETWEEN 20260101 AND 20260430` unless it is establishing a balance at a date, in which
case it reads `GLAFS` for `FSCSYR IN ('2026','2027')` with `CURNTYPE='F'`.

The join between the two systems is `work/posted.log` (`vendor|base-invoice || billId`), with the
Sage side consolidated on `base_invoice()` — trailing `*<digits>` stripped, `RTRIM` only, never
`LTRIM`.

No script in this repository was executed to produce any figure above.
