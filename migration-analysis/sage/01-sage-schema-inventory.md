# Sage 300 (`IDEDAT`) — Schema Inventory and Evidence Log

**Agent 1 — Sage 300 Database Analyst.** Read-only audit. Every statement issued against
`IDEDAT` in producing this document was a `SELECT`, `SHOW` or a `sys.*` catalogue read.
No migration script was executed.

- **Server**: `Microsoft SQL Server 2025 (RTM) - 17.0.1000.7 (X64)`
- **Database**: `IDEDAT`, login `linux_reader`
- **Tables**: 1,110 (575 non-empty) · **Views**: 18 · **Schemas**: 1 (`dbo`)
- **Date of audit**: 2026-09-05

---

## 1. How I established meaning

The mandate was to prove table meanings rather than expand abbreviations. Four
independent evidence sources were available, in descending order of strength.

### 1.1 Sage's own data dictionary IS present in this database

This is the single most important methodological finding: **`IDEDAT` carries Sage 300's
own metadata**, so most names in scope can be sourced to Sage rather than inferred.

| Table | Rows | What it gives us |
|---|---|---|
| `AUVIEW` | 63 | Maps `VWDLLNAME` (physical table name) → `VWDESC` (Sage's own English description) |
| `AUFLDS` | 1,057 | Maps `VIEWID` + `FLDNAME` → `FLDDESC` (Sage's own field descriptions) |
| `GLSRCE` | 64 | Maps `SRCELEDGER` + `SRCETYPE` → `SRCEDESC` — Sage's own module-and-transaction-type dictionary |
| `CSAPP` | 36 | The installed-application registry: `SELECTOR` is the module code, `PGMVER` the version |
| `DATADICT` | 1,089 | One `image` blob per table; its ASCII strings are that table's field-name list |
| `CSOPTFD` | 13,301 | Optional-field value/description master |

```sql
-- Sage's own table catalogue
SELECT VIEWID, RTRIM(VWDLLNAME) t, RTRIM(VWDESC) d FROM AUVIEW ORDER BY VIEWID;
-- Sage's own field catalogue
SELECT v.VIEWID, RTRIM(v.VWDLLNAME) tbl, f.FIELDIDX, RTRIM(f.FLDNAME), RTRIM(f.FLDDESC)
  FROM AUFLDS f JOIN AUVIEW v ON v.VIEWID = f.VIEWID ORDER BY v.VWDLLNAME, f.FIELDIDX;
-- Sage's own source-ledger dictionary
SELECT RTRIM(SRCELEDGER), RTRIM(SRCETYPE), RTRIM(SRCEDESC) FROM GLSRCE ORDER BY 1,2;
```

**Coverage limits, stated plainly.** `AUVIEW` documents only the 63 *audited* views —
it covers `APIBC/APIBH/APIBD/APIBS`, `APBTA/APTCR/APTCN/APTCP`, the whole `PO*` document
family, `GLJEH/GLJED/GLBCTL`, and `BKENTH/BKENTD/BKTRANR`. It does **not** cover
`APOBL`, `APOBS`, `APOBP`, `APOBLJ`, `APVEN`, `GLAMF`, `GLPOST`, `GLPJD`, `TXAUD*`,
`BKTRANH/BKTRAND` or any `IC*` table. Those were proved by columns + rows + join
behaviour, and are labelled `DATABASE VERIFIED` rather than `DOCUMENTED`.

`AUFLDS` uses Sage *view* field names, which sometimes differ from the physical column
name (the `APIBH` view calls the due date `DATEDUE` and the type `IDTRX`/`TEXTTRX`, and
the physical `APIBH` table agrees; but `APOBL` stores the same concepts as `DATEINVCDU`
and `IDTRXTYPE`). Field descriptions therefore transfer at concept level, not
name-for-name. Where I transferred a description across tables I say so.

`DATADICT` gives the **field list** but not descriptions — the blob is Sage's binary
dictionary and only the field names survive as ASCII:

```sql
SELECT TABLENAME, DATALENGTH(TABLEDATA), TABLEDATA FROM DATADICT WHERE RTRIM(TABLENAME)='APOBL';
-- 3,086 bytes; ASCII strings are IDVEND, IDINVC, AUDTDATE, ..., AMT1099REM (150 strings)
```

### 1.2 The other three evidence sources

2. **Column shape + real rows.** Reading 2–3 masked rows and the column list settles most
   questions (e.g. `APVEN` carries names, addresses, terms codes, currency, balances and
   ageing counters — it is unambiguously a vendor master).
3. **Join-match rates.** Because this database declares **zero foreign keys**, every
   relationship claim in section 4 is backed by a measured match rate, with the query.
4. **Cross-check against the project's own artefacts.** `extract.sql` states expected
   row counts; I re-ran its logic and reproduced them exactly (section 3.6), which
   validates both my reading and its filters.

### 1.3 Conventions that hold across the whole schema

| Convention | Proof |
|---|---|
| Fixed-width `CHAR` keys, right-padded — **always `RTRIM()`** | `APOBL.IDVEND char(12)`, `IDINVC char(22)`; 778,031 of 787,465 `IDINVC` values match `LIKE '% '` purely from padding |
| Dates are `decimal(9,0)` in `YYYYMMDD` | `SELECT DATEINVC, CONVERT(date, CAST(DATEINVC AS char(8)), 112) FROM APOBL` → `20260302` → `2026-03-02` |
| Times are `decimal(9,0)` in `HHMMSSCC` (centiseconds) | `AUDTTIME 12113964` = 12:11:39.64; `AUDTTIME 6381806` = 06:38:18.06 |
| `SWxxx` columns are boolean flags | `SWPAID`, `SWACTV`, `SWHOLD`, `SWJOB`, `SWRTG` — all `smallint` 0/1 |
| `AUDTDATE/AUDTTIME/AUDTUSER/AUDTORG` on nearly every table | audit stamp; `AUDTORG` is always `IDEDAT` |
| `xxxHC` = home currency (INR), `xxxTC` = transaction currency, `xxxRC` = tax-reporting currency | `AMTINVCHC` vs `AMTINVCTC` diverge only on non-INR documents |
| `O`-suffixed twin tables are **optional-field** child tables | `ICITEMO` PK `(ITEMNO, OPTFIELD)`; `APIBDO`, `APOBLJO`, `GLPOSTO`, `APPJDO` follow the same shape |

---

## 2. Module map, with evidence

### 2.1 The module codes are Sage 300 application selectors — proven three ways

**(a) `CSAPP` is the installed-application registry.** `SELECTOR` is the module code:

```sql
SELECT RTRIM(SELECTOR) sel, RTRIM(SEQUENCE) seq, RTRIM(PGMID) pgm, RTRIM(PGMVER) ver
  FROM CSAPP ORDER BY SELECTOR, SEQUENCE;
```

Installed selectors: `AP, AR, AS, AU, BK, BX, CS, DT, GE, GL, GP, IC, IE, JS, MM, OE, OL, PO, RP, TX, XK, XR, XX, ZC`.
Core Sage modules are at version `69A`; the add-ons (`DT`, `IE`, `JS`, `MM`, `OL`, `XR`, `XX`, `BX`, `XK`) at `62A`
and `RP` at `54A` — i.e. the `62A`/`54A` families are **third-party or older-generation add-ons**, not core Sage.

**(b) `GLSRCE` is Sage's own module dictionary**, and it names the modules in English:

```sql
SELECT RTRIM(SRCELEDGER) l, RTRIM(SRCETYPE) t, RTRIM(SRCEDESC) d FROM GLSRCE ORDER BY 1,2;
```

| Ledger | Sample `SRCEDESC` values (verbatim from the database) |
|---|---|
| `AP` | `A/P Invoice`, `A/P Credit Note`, `A/P Debit Note`, `A/P Check`, `A/P Prepayment`, `A/P Adjustment`, `A/P Discount`, `A/P Interest`, `A/P Payment Reversal`, `A/P Revaluation`, `A/P Rounding`, `A/P Consolidation` |
| `AR` | `A/R Invoice`, `A/R Payment Received`, `A/R Refund`, `A/R Write-Off`, `A/R Unapplied Cash`, … |
| `GL` | `G/L Journal Entry`, `G/L Closing Entry`, `G/L Revaluation Transactions`, `G/L Reallocation Transactions` |
| `IC` | `I/C Receipts`, `I/C Shipments`, `I/C Transfers`, `I/C Adjustments`, `I/C Assemblies`, `I/C Disassemblies` |
| `PO` | `P/O Receipts`, `P/O Invoices`, `P/O Returns`, `P/O Credit Notes`, `P/O Debit Notes`, `P/O Adjustments` |
| `OE` | `O/E Invoices`, `O/E Shipments`, `O/E Credit Notes`, `O/E Debit Notes` |
| `BK` | `Bank Entries`, `Bank Transfers`, `Bank Rec. Discrepancies`, `Bank Consolidated Entry` |
| `ZC` | `G/L Consolidation` |

That single table settles `AP = Accounts Payable`, `AR = Accounts Receivable`,
`GL = General Ledger`, `IC = Inventory Control`, `PO = Purchase Orders`,
`OE = Order Entry` and `BK = Bank Services` without any inference on my part.

**(c) The same codes appear as live foreign values.** `GLPOST.SRCELEDGER` for FY2026:

```sql
SELECT RTRIM(SRCELEDGER) srcl, RTRIM(SRCETYPE) srct, COUNT(*) n
  FROM GLPOST WHERE FISCALYR='2026' GROUP BY SRCELEDGER, SRCETYPE ORDER BY n DESC;
```
→ `IC/TF 1,003,022` · `IC/AS 465,235` · `AP/IN 362,096` · `IC/AD 282,468` · `IC/IN 199,638` ·
`PO/RC 187,874` · `AP/PY 110,337` · `AR/IN 109,755` · `OE/SH 61,392` · `AP/PP 32,738` ·
`AP/CR 19,480` · `PO/IN 13,628` · `AR/PP 11,575` · `AR/PY 8,627` · `AP/GL 8,560` · (23 more)

And `CSFSCST` (fiscal-period status) carries one row per module per year for exactly
`AP, AR, BK, GL, IC, OE, PO, ZC` — the modules that own a period-close.

### 2.2 `AP = Accounts Payable`, shown rather than asserted

`APVEN` (4,752 rows) is a vendor master. Column list (133 columns) contains, among others:
`VENDORID`, `VENDNAME`, `SHORTNAME`, `LEGALNAME`, `BRN`, `TEXTSTRE1..4`, `NAMECITY`,
`CODESTTE`, `CODEPSTL`, `CODECTRY`, `NAMECTAC`, `TEXTPHON1/2`, `EMAIL1/2`, `WEBSITE`,
`TERMSCODE`, `CURNCODE`, `IDACCTSET`, `BANKID`, `CODETAXGRP`, `TAXNBR`, `AMTBALDUEH`,
`CNTOPENINV`, `AVGDAYSPAY`, `DATELASTPA`, `SWACTV`, `SWHOLD`, `SUBJTOWTHH`.

That is a payables vendor record: identity, address, contact, **payment terms**, currency,
**control account set**, **bank**, **tax group and tax number**, and **outstanding balance
and ageing counters**. A sales/customer master would not carry `AMTBALDUEH` + `TERMSCODE`
+ `SUBJTOWTHH` (withholding). Live totals: 4,752 vendors, 2,062 active
(`SUM(CASE WHEN SWACTV=1 …)`), currencies `INR 4,028 / USD 641 / EUR 37 / GBP 16 /
CNY 10 / HKD 8 / RMB 5 / YEN 3` (+3 more).

Confirming from the other end: `APOBL` — the obligation ledger — carries `SWPAID`,
`AMTDUEHC`, `DATEPAID`, `CNTTOTPAYM`, `AMTREMIT`, `IDBANK`, `IDRMIT` ("Check Number" per
`AUFLDS`). Open balance right now:

```sql
SELECT COUNT(*) open_docs, CAST(SUM(AMTDUEHC) AS decimal(18,2)) open_balance_inr,
       MIN(DATEINVC) oldest, MAX(DATEINVC) newest
  FROM APOBL WHERE SWPAID = 0;
```
→ **6,989 open documents, ₹209,543,264.52 outstanding**, oldest 2016-03-31, newest 2026-07-17.
A live payables ledger with a real trade-creditor balance. `AP = Accounts Payable`: **VERIFIED**.

### 2.3 Full prefix map

Non-empty tables and rows by prefix (`sys.partitions`, `index_id IN (0,1)`):

| Prefix | Module | Tables (non-empty) | Rows | Evidence | In scope? |
|---|---|---|---|---|---|
| `GL` | General Ledger | 26 | 124,871,712 | `GLSRCE`, `CSAPP`, `AUVIEW` | **yes** |
| `IC` | Inventory Control | 62 | 88,830,416 | `GLSRCE`, `CSAPP` | **yes** |
| `PO` | Purchase Orders | 80 | 45,008,565 | `GLSRCE`, `CSAPP`, `AUVIEW` (14 PO views) | **yes** |
| `AU` | Sage 300 Audit / change logging | 10 | 26,831,554 | `AUVIEW` self-documents `AUVIEW`, `AUFLDS`, `AUMODULE`, `AUPURGE` | no — see §2.4 |
| `AP` | Accounts Payable | 53 | 19,369,806 | `GLSRCE`, `AUVIEW`, `APVEN` rows | **yes** |
| `OE` | Order Entry (sales) | 37 | 8,253,760 | `GLSRCE` `O/E Invoices` etc. | no |
| `IE` | Import/Export add-on (v62A) | 80 | 7,655,020 | `CSAPP` sel `IE`; tables `IEEXPINV`, `IEEXPLIC`, `IEPKLST*`, `IEBEENTD` | no |
| `AR` | Accounts Receivable | 54 | 7,004,604 | `GLSRCE` `A/R Invoice` etc., `AUVIEW` 9 AR views | no |
| `OL` | Costing/BOQ add-on (v62A) | 49 | 4,850,333 | `CSAPP` sel `OL`; tables `OLOLBOQD`, `OLCSTIND`, `OLDTLORH` | no |
| `TX` | Tax Services | 8 | 2,593,640 | `CSAPP` sel `TX`; `TXAUTH` holds CGST/SGST/IGST | **yes** |
| `BK` | Bank Services | 21 | 1,108,122 | `GLSRCE` `Bank Entries`, `AUVIEW` `BKENTH/BKENTD` | **yes** |
| `DT` | TDS (withholding-tax) add-on (v62A) | 18 | 458,441 | `CSAPP` sel `DT`, registered as an AP extension (`CSAPP` `AP`/`01` → `PGMID DT`); table `DTTDS` | partial — flagged |
| `XR` | add-on (v62A) | 13 | 439,883 | `CSAPP` sel `XR` | no |
| `RP` | Reporting (v54A) | 12 | 251,539 | `CSAPP` sel `RP` | no |
| `XX` | add-on (v62A) | 4 | 150,428 | `CSAPP` sel `XX` | no |
| `JS` | Job/Shop add-on (v62A) | 18 | 130,717 | `CSAPP` sel `JS` | no |
| `ZX` | — | 1 | 109,983 | not in `CSAPP` | no |
| `CS` | Common Services | 18 | 20,235 | `CSAPP` sel `CS`; `CSCOM` is the company profile | **yes** |
| `AS` | Administrative Services | 3 | 1,710 | `CSAPP` sel `AS` | no |
| `ZZ`,`DA`,`US`,`MM`,`GE`,`ZC` | misc / consolidation | 7 | ~2,500 | `CSAPP` | no |

### 2.4 Large tables deliberately excluded, and why

The mandate requires these be listed explicitly.

| Table / family | Rows | Why excluded |
|---|---|---|
| `GLJEC` | 25,787,355 | Journal-entry **comment/optional** child of `GLJED`; no financial content the migration needs |
| `GLPJC` | 22,818,637 | Posting-journal comment child of `GLPJD`; same reason |
| `ICIVAL` | 16,553,404 | IC **valuation/costing** history — inventory costing is not in the AP/PO migration scope |
| `ICHIST` | 15,209,431 | IC transaction **history**; superseded for our purposes by `PORCPL`/`POINVL` |
| `POPOALO` | 11,590,158 | **Optional-field values** on PO day-end audit lines (PK `DAYENDSEQ, PORAHSEQ, PORALSEQ, OPTFIELD`); documented in §3.4 but not dossiered per-column |
| `AUEXPT`, `AUDATA` | 11,052,560 / 8,909,908 | Sage's own **audit-log** payload (who changed which field when). Large, and irrelevant to what the migration moves. Worth knowing it exists if anyone needs to prove *when* a value changed. |
| `ICTREDO`, `ICTRNDP` | 7,238,818 / 7,160,967 | IC transaction detail/optional-field tables, costing scope |
| `AR*` (54 tables, 7.0M rows) | | Receivables — the migration moves **payables** only |
| `OE*` (37 tables, 8.3M rows) | | Sales order entry — out of scope |
| `IE*` (80 tables, 7.7M rows) | | Import/Export add-on (packing lists, export licences, shipping bills). **Flagged**: an exporter's AP data may have dependencies here that I have not traced. |
| `OL*`, `JS*`, `XR*`, `XX*`, `RP*`, `ZX*` | ~5.7M rows | Third-party add-ons at v62A/54A, no `GLSRCE` entry, no AP linkage found |
| `POHSTH`/`POHSTL` (490k / 1.64M) | | PO **history** (purged/archived orders); the live document tables cover the window |
| `*O`-suffixed optional-field twins (`APIBDO` 1.04M, `APOBLJO` 1.04M, `APPJDO` 2.35M, `ICITEMO` — dossiered, `GLPOSTO`, `GLPJDO`) | | Key/value optional-field children. `ICITEMO` **is** dossiered because it holds HSN codes. |

**`DT` (TDS) is a partial exclusion I want to flag.** `CSAPP` registers `DT` as an
extension *of AP* (`SELECTOR='AP', SEQUENCE='01', PGMID='DT'`), and `APIBC`/`APBTA` both
carry a `TDSPOSTED` column. `DTTDS` (131,247 rows), `DTTCR` (143,935) and `DTIBH`
(114,843) therefore hold Indian withholding-tax data that is *attached to* AP documents.
I did not trace it. See §8.

---

## 3. Table dossiers

### 3.0 The critical distinction: batch/entry vs posted ledger

This is the question the mandate says would migrate the wrong population if reversed.
**Answer: `APIBC`/`APIBH`/`APIBD`/`APIBS` are the invoice-BATCH (entry) side.
`APOBL`/`APOBS`/`APOBP`/`APOBLJ` are the POSTED open-payables ledger.** Six lines of proof:

**Proof 1 — Sage says so.** `AUVIEW` labels `APIBC` = `Invoice Batches`, `APIBH` =
`Invoices`, `APIBD` = `Invoice Details`, `APIBS` = `Invoice Payment Schedules`. All four
sit under view IDs `AP0020`–`AP0023`, a batch-entry family. `APOBL` is **not in `AUVIEW`
at all** — Sage does not audit it, because it is a derived posted ledger, not an entry point.

**Proof 2 — the primary keys.** `APIBH` PK is `(CNTBTCH, CNTITEM)` — batch number + entry
number within the batch. `APOBL` PK is `(IDVEND, IDINVC)` — vendor + document number, one
row per *document*, the shape of a ledger. `APIBD` PK is `(CNTBTCH, CNTITEM, CNTLINE)`.

**Proof 3 — the batch-status column exists only on the batch side.**
```sql
SELECT BTCHSTTS, COUNT(*) n, MIN(DATEBTCH) mind, MAX(DATEBTCH) maxd FROM APIBC GROUP BY BTCHSTTS;
```
→ `BTCHSTTS 1: 9 batches` (2026-06-23…2026-07-17, i.e. **open, unposted**) ·
`BTCHSTTS 3: 17,130` (2016-03-31…2026-09-30) · `BTCHSTTS 4: 1,190`.
Only 9 of 18,329 batches are in status 1. `APOBL` has no batch-status column at all.

**Proof 4 — the posted-lifecycle columns exist only on the ledger side.** `APOBL` carries
`POSTSEQNCE` (the posting sequence stamped at post time), `SWPAID`, `AMTDUEHC`, `DATEPAID`,
`CNTTOTPAYM`, `AMTREMIT`, `DATELSTACT`, `IDBANK`, `IDRMIT`. `APIBH` carries instead
`INVCSTTS`, `ERRBATCH`, `ERRENTRY` — work-in-progress error tracking. (`INVCSTTS` is 0 on
all 675,194 rows, so this company has no entries stuck mid-edit.)

**Proof 5 — one document traced through both.**
```sql
SELECT TOP 3 RTRIM(h.IDVEND) vend, RTRIM(h.IDINVC) invc, h.CNTBTCH, h.CNTITEM,
       h.DATEINVC, h.AMTGROSTOT apibh_gross, o.CNTBTCH o_bt, o.CNTITEM o_it,
       o.AMTINVCHC apobl_gross, o.AMTDUEHC apobl_due, o.SWPAID, o.POSTSEQNCE
  FROM APIBH h JOIN APOBL o ON o.IDVEND = h.IDVEND AND o.IDINVC = h.IDINVC
 WHERE h.DATEINVC BETWEEN 20260101 AND 20260430 AND h.SRCEAPPL='AP' AND h.IDTRX = 12
 ORDER BY h.CNTBTCH, h.CNTITEM;
```
→ vendor `OTHX003`, document `BAJAJ FINA`: `APIBH` batch **17103** entry **1**, gross
₹3,758,448.00 — and `APOBL` for the same `(IDVEND, IDINVC)` carries `CNTBTCH = 17103`,
`CNTITEM = 1` **as a back-reference**, the same ₹3,758,448.00, plus `POSTSEQNCE = 15646`,
`SWPAID = 1`, `AMTDUEHC = 0`. The batch row is the source; the ledger row is the result,
and it points back at its origin. Same for `OTHX003 / LINKED IN` (batch 17120 entry 1,
₹67,525.32, `POSTSEQNCE 15663`).

**Proof 6 — the populations differ in the direction the model predicts.** `APOBL` holds
things `APIBH` never can: transaction types **50** and **51** exist only in `APOBL`
(61,565 and 46,668 rows), only with `TYPEBTCH='PY'` — i.e. they originate in a *payment*
batch (`APBTA`/`APTCR`), not an invoice batch, so they have no `APIBH` row by construction.
`APIBH` has only types 12/22/32/40.

```sql
SELECT IDTRXTYPE, RTRIM(TYPEBTCH) tb, COUNT(*) n FROM APOBL GROUP BY IDTRXTYPE, TYPEBTCH ORDER BY 1,2;
```
| `IDTRXTYPE` | `TYPEBTCH` | Rows | Meaning (from `GLSRCE` + document numbers + sign) |
|---|---|---|---|
| 12 | `IN` | 620,409 | Invoice |
| 12 | `PY` | 8,751 | Invoice **raised from a payment batch** (misc. payment) |
| 22 | `IN` | 5,955 | Debit note (positive) |
| 32 | `IN` | 44,116 | Credit note (negative) |
| 40 | `IN` | 1 | Interest (`GLSRCE AP/IT = A/P Interest`) |
| 50 | `PY` | 61,565 | Prepayment — document numbers are `PP0nnnnn`; `GLSRCE AP/PP = A/P Prepayment` |
| 51 | `PY` | 46,668 | Payment applied — `GLSRCE AP/PY = A/P Check` |

**Retention caveat, and why it matters to this migration.** `APIBH` retains **posted**
batch entries: 675,194 rows across batches that are overwhelmingly status 3/4 (posted).
This company does not purge posted AP batches. That is the *only* reason
`extract.sql`'s design works — it takes headers from `APOBL` (posted ledger) and lines
from `APIBD` (batch detail) joined on `(CNTBTCH, CNTITEM)`. **If posted batches were ever
purged, `APOBL` would survive and `APIBD` would vanish, and the extract would silently
return zero lines for every bill.** `APOBLJ` (§3.1) is the purge-proof alternative.

---

### 3.1 AP — Accounts Payable

```
Table:                       APVEN
Verified full name:          A/P Vendor Master
Module:                      AP (Accounts Payable)
Purpose:                     One row per trade creditor: identity, address, contact, payment
                             terms, currency, control-account set, bank, tax group/number,
                             plus running balance and ageing statistics.
Important columns:           VENDORID char(12)  PK, e.g. 'ACCI008','OTHX003','FABI623'
                             VENDNAME char(60)  trading name
                             LEGALNAME char(?)  legal name
                             SHORTNAME char(10) sort/search name (KEY_1)
                             IDGRP char(6)      vendor group (KEY_2)
                             BRN                business registration number - HOLDS THE GSTIN
                                                (see Agent 2: TAXNBR and IDTAXREGI1..5 are
                                                 empty on all 4,752 vendors)
                             TEXTSTRE1..4, NAMECITY, CODESTTE, CODEPSTL, CODECTRY  address
                             CODESTTE char(30)  FREE TEXT, not a state code - see DQ-9
                             TERMSCODE char(6)  e.g. '30DAYS'
                             CURNCODE char(3)   INR 4,028 / USD 641 / EUR 37 / GBP 16 /
                                                CNY 10 / HKD 8 / RMB 5 / YEN 3 (+3 more)
                             IDACCTSET char(6)  AP control account set
                             CODETAXGRP char(12) links to TXGRP.GROUPID
                             SWACTV smallint    1=active (2,062 of 4,752)
                             SWHOLD smallint    on hold
                             SUBJTOWTHH         subject to withholding - 0 for every vendor
                             AMTBALDUEH         outstanding balance, home currency
                             CNTOPENINV, AVGDAYSPAY, DATELASTPA  ageing statistics
Primary key:                 APVEN_KEY_0 (VENDORID)          [sys.indexes, is_primary_key=1]
Foreign keys:                NONE. sys.foreign_keys returns 0 rows for the whole database.
Implicit relationships:      APOBL.IDVEND -> APVEN.VENDORID.  100% match:
                               SELECT COUNT(*) docs FROM APOBL o
                                 LEFT JOIN APVEN v ON RTRIM(v.VENDORID)=RTRIM(o.IDVEND)
                                WHERE v.VENDORID IS NULL;   -> 0   (all 787,465 rows)
                             NOTE the column names differ: APVEN calls it VENDORID,
                             every AP document table calls it IDVEND. POPORH1/PORCPH1/
                             POINVH1 call it VDCODE (also 100%: 357 window vendors resolve).
Row count:                   4,752
Example records:             VENDORID 'ACCI008' | 'MAINETTI INDIA PVT…' | INR | active
                             VENDORID 'OTHX003' | generic "others" vendor used for 12,781-doc
                                                   window bills incl. 'BAJAJ FINA','LINKED IN'
                             VENDORID 'FABI623' | fabric supplier, INR, on window PO invoices
Evidence:                    DATABASE VERIFIED (not in AUVIEW; proved from columns + rows +
                             100% join match from APOBL and POINVH1)
Confidence:                  VERIFIED
```

```
Table:                       APIBC
Verified full name:          Invoice Batches                        <- verbatim AUVIEW.VWDESC, VIEWID AP0020
Module:                      AP
Purpose:                     Batch control header for AP invoice entry. One row per batch of
                             invoices keyed in (or generated by PO day-end). Carries the
                             batch status that says whether the batch has been posted.
Important columns:           CNTBTCH decimal(9,0)  PK - batch number
                             BTCHSTTS smallint     "Batch Status" (AUFLDS). 1=open (9 rows),
                                                   3=posted (17,130), 4=posted+? (1,190)
                             BTCHTYPE smallint     "Batch Type" (AUFLDS). 1=entered (12,095),
                                                   2 (6), 3 (437 AP + 1,161 PO), 5 (4,630 PO)
                             SRCEAPPL char(2)      "Source Application": AP 12,538 / PO 5,791
                             CNTINVCENT            "Number of Entries"
                             AMTENTR decimal(19,3) "Batch Total"
                             DATEBTCH, BTCHDESC, POSTSEQNBR, NBRERRORS, POSTDATE
                             TDSPOSTED smallint    <- the DT (TDS) add-on hooks in here
Primary key:                 APIBC_KEY_0 (CNTBTCH)
Foreign keys:                NONE
Implicit relationships:      APIBH.CNTBTCH -> APIBC.CNTBTCH.  100% (0 orphan APIBH rows:
                               SELECT COUNT(*) FROM APIBH h LEFT JOIN APIBC c
                                 ON c.CNTBTCH=h.CNTBTCH WHERE c.CNTBTCH IS NULL;  -> 0)
Row count:                   18,329
Evidence:                    DOCUMENTED (AUVIEW/AUFLDS) + DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       APIBH
Verified full name:          Invoices                               <- verbatim AUVIEW.VWDESC, VIEWID AP0021
                             i.e. the invoice ENTRY record inside a batch, NOT the ledger.
Module:                      AP
Purpose:                     One row per invoice/credit-note/debit-note entry within an
                             invoice batch. This is the work-in-progress / as-keyed side.
                             Posted entries are retained here (not purged) in this company.
Important columns:           CNTBTCH, CNTITEM        PK (batch, entry-within-batch)
                             IDVEND char(12)         "Vendor Number"
                             IDINVC char(22)         "Document Number"
                             IDTRX / TEXTTRX smallint 12/1=Invoice(624,670), 22/2=Debit note
                                                     (6,002), 32/3=Credit note(44,521), 40/4(1)
                             INVCSTTS smallint       entry status - 0 on ALL 675,194 rows
                             ERRBATCH, ERRENTRY int  "Error Batch"/"Error Entry" (AUFLDS)
                             DATEINVC, DATEDUE, DATEBUS ("Posting Date"), FISCYR, FISCPER
                             CODECURN, EXCHRATEHC, RATETYPE
                             CODETAXGRP, CODETAX1..5, TAXCLASS1..5, AMTTAX1..5
                             AMTINVCTOT   "Document Total Before Tax"
                             AMTTAXTOT    "Tax Total"
                             AMTGROSTOT   "Document Total Including Tax"
                             AMTDUEHC     "Func. Amount Due"
                             SRCEAPPL char(2)  AP 242,908 / PO 432,286
                             SWHOLD "On Hold", SWRTG "Has Retainage", SWJOB "Job Related"
                             206 columns in total
Primary key:                 APIBH_KEY_0 (CNTBTCH, CNTITEM)
                             + KEY_1 (ORIGCOMP, IDVEND, IDINVC) - note the document number is
                               only a SECONDARY key here, and is not unique
Foreign keys:                NONE
Implicit relationships:      -> APIBC on CNTBTCH (100%)
                             -> APIBD on (CNTBTCH, CNTITEM) (15 orphan APIBD rows, see DQ-2)
                             -> APIBS on (CNTBTCH, CNTITEM)
                             -> APOBL on (IDVEND, IDINVC): of 32,115 window APIBH rows only
                               38 have no APOBL counterpart.
Row count:                   675,194
Example records:             (17103, 1) OTHX003 / 'BAJAJ FINA' / 20260101 / gross 3,758,448.000
                             (17120, 1) OTHX003 / 'LINKED IN'  / 20260101 / gross    67,525.320
                             (17120, 2) OTHX003 / 'ROADMAP EURO60*2' / 20260101 /   12,385.330
Evidence:                    DOCUMENTED (AUVIEW/AUFLDS) + DATABASE VERIFIED (traced document)
Confidence:                  VERIFIED
```

```
Table:                       APIBD
Verified full name:          Invoice Details                        <- verbatim AUVIEW.VWDESC, VIEWID AP0022
Module:                      AP
Purpose:                     GL distribution lines of an invoice-batch entry. This is the
                             ONLY place a line description lives.
Important columns:           CNTBTCH, CNTITEM, CNTLINE   PK
                             IDGLACCT char(45)  "G/L Account" - joins GLAMF.ACCTFMTTD (§4)
                             AMTDIST decimal(19,3)  "Distributed Amount" - PRE-TAX
                             AMTDISTNET             "Distributed Amount Before Taxes"
                             TEXTDESC char(60)      line description  <-- NOT PRESENT IN APOBLJ
                             RATETAX1..5 decimal(15,5)  the STATED per-line tax rate
                             TAXCLASS1..5, AMTTAX1..5, AMTTAXREC1..5, AMTTAXEXP1..5
                             IDITEM char(16), UNITMEAS, QTYINVC, AMTCOST  (empty on AP-direct)
                             IDDIST "Distribution Code", IDACCTTAX
                             170 columns in total
                             NOTE: APIBD carries NO IDVEND and NO IDINVC. Its only link to a
                             document is (CNTBTCH, CNTITEM), and the same invoice number
                             recurs across batches - so any staging table keyed on
                             (vendor, invoice, line) silently loses rows. extract.sql already
                             annotates this and it is correct.
Primary key:                 APIBD_KEY_0 (CNTBTCH, CNTITEM, CNTLINE)   [only index on the table]
Foreign keys:                NONE
Implicit relationships:      -> APIBH on (CNTBTCH, CNTITEM). 15 orphan rows out of 1,270,496.
                             IDGLACCT -> GLAMF.ACCTFMTTD: 22,287/22,287 = 100% on window
                             batches; -> GLAMF.ACCTID only 19,732/22,287 = 88.5%. See §4.
Row count:                   1,270,496
Example records:             (17120, 1, 20) acct '4E5O030' amt 67,525.32  item '' qty 0
Evidence:                    DOCUMENTED (AUVIEW/AUFLDS) + DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       APIBS
Verified full name:          Invoice Payment Schedules              <- verbatim AUVIEW.VWDESC, VIEWID AP0023
Module:                      AP
Purpose:                     Instalment schedule for a batch invoice entry (one row per
                             instalment). For single-payment terms there is exactly one row.
Important columns:           CNTBTCH, CNTITEM, CNTPAYM  PK
                             DATEDUE "Due Date", AMTDUE "Amount Due",
                             DATEDISC "Discount Date", AMTDISC "Discount Amount"  (all AUFLDS)
Primary key:                 APIBS_KEY_0 (CNTBTCH, CNTITEM, CNTPAYM)
Foreign keys:                NONE
Row count:                   675,321   (675,194 APIBH entries -> 127 have >1 instalment)
Example records:             (17103, 1, 1) due 20260101 amount 3,758,448.00 disc 0
Evidence:                    DOCUMENTED (AUVIEW/AUFLDS) + DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       APOBL
Verified full name:          A/P Obligation Ledger - the POSTED open-payables document ledger
                             ("OBL" = OBLigation). NOT in AUVIEW; proved in §3.0.
Module:                      AP
Purpose:                     One row per posted AP document (invoice, note, prepayment,
                             payment), for the life of the document. Retains fully-paid
                             documents (SWPAID=1) rather than purging them, so it is a
                             complete posted history, not just an open-items list.
Important columns:           IDVEND char(12), IDINVC char(22)   PK
                             CNTBTCH, CNTITEM       back-reference to the originating batch
                             TYPEBTCH char(2)       'IN' = came from an invoice batch,
                                                    'PY' = came from a payment batch
                             IDTRXTYPE smallint     12 Invoice / 22 Debit note / 32 Credit
                                                    note / 40 Interest / 50 Prepayment /
                                                    51 Payment   (table in §3.0)
                             SRCEAPPL char(2)       'AP' 358,925 / 'PO' 428,540
                             POSTSEQNCE decimal(9,0) posting sequence - proof of posting
                             SWPAID smallint        0 = open (6,989), 1 = settled (780,476)
                             AMTINVCHC / AMTINVCTC  document total, home / transaction ccy
                             AMTDUEHC   "Func. Amount Due"  - the live balance
                             AMTTAXHC               header tax. ZERO on RCM bills (Agent 2)
                             AMTTXBLHC / AMTNONTXHC taxable / non-taxable base
                             DATEINVC, DATEINVCDU (due), DATEBUS "Posting Date", DATEPAID
                             FISCYR char(4), FISCPER char(2)
                             CODECURN char(3), EXCHRATEHC decimal(15,7), IDRATETYPE
                             CODETAXGRP char(12), CODETAX1..5, AMTBASE1..5HC, AMTTAX1..5HC
                             IDBANK, IDRMIT ("Check Number"), LONGSERIAL
                             IDORDERNBR, IDPONBR    order / PO number as free text
                             149 columns in total
Primary key:                 APOBL_KEY_0 (IDVEND, IDINVC)
                             + KEY_1 UNIQUE (SWPAID, IDORDERNBR, IDVEND, IDINVC)
                             + KEY_3 (DATEINVCDU, IDVEND, IDINVC), KEY_5 (IDVEND, DATEINVC)
Foreign keys:                NONE
Implicit relationships:      IDVEND -> APVEN.VENDORID           100% (0 orphans of 787,465)
                             (CNTBTCH,CNTITEM) -> APIBH          13,466 of 19,548 window
                                                                 AP-source rows; the 6,082
                                                                 misses are all TYPEBTCH='PY'
                                                                 plus the 1,079 in DQ-1
                             (IDVEND,IDINVC) -> APOBLJ           100% of window type-12 docs
                             (IDVEND,IDINVC) -> APOBS            0 orphan APOBS rows
                             (IDVEND,IDINVC) -> APOBP            payments applied
Row count:                   787,465   (open: 6,989 = ₹209,543,264.52)
Example records:             OTHX003 / 'BAJAJ FINA' / type 12 / TYPEBTCH IN / 20260101 /
                               gross 3,758,448.00 / due 0.00 / SWPAID 1 / POSTSEQNCE 15646
                             OTHX003 / 'LINKED IN'  / type 12 / TYPEBTCH IN / 20260101 /
                               gross    67,525.32 / due 0.00 / SWPAID 1 / POSTSEQNCE 15663
                             FABU255 / 'PP059387'   / type 50 / TYPEBTCH PY / 20260101 /
                               -126,175.11 / bank SBIEFC          (a prepayment)
Evidence:                    DATABASE VERIFIED (six independent proofs, §3.0)
Confidence:                  VERIFIED
```

```
Table:                       APOBS
Verified full name:          A/P Obligation Payment Schedule (posted instalments)
Module:                      AP
Purpose:                     Posted-side twin of APIBS: the instalment schedule of a posted
                             document, carrying per-instalment settlement state.
Important columns:           IDVEND, IDINVC, CNTPAYM   PK
                             DATEDUE, AMTDUEHC/AMTDUETC, AMTDISCHC, AMTPYMRMHC/AMTPYMRMTC
                             SWPAID smallint, DAYSTOPAY smallint, DATEACTV
                             IDORDRNBR, IDPONBR, IDPREPAID, IDTRXTYPE, RTGAPPLYTO
                             31 columns
Primary key:                 APOBS_KEY_0 (IDVEND, IDINVC, CNTPAYM)  + 11 secondary indexes
Foreign keys:                NONE
Implicit relationships:      (IDVEND,IDINVC) -> APOBL: 0 orphan rows of 787,468
Row count:                   787,468   (vs APOBL 787,465 -> 3 documents have 2 instalments)
Example records:             OTHX003 / 'LINKED IN' / 1 / due 20260101 / 67,525.32 / SWPAID 1
Evidence:                    DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       APOBP
Verified full name:          A/P Obligation Payments — payment applications against posted docs
Module:                      AP
Purpose:                     One row per payment (or adjustment) APPLIED to a posted
                             document. This is how a document gets from SWPAID=0 to 1.
Important columns:           IDVEND, IDINVC, CNTPAYMNBR, IDRMIT, DATEBUS, TRANSTYPE, CNTSEQNCE  PK
                             AMTPAYMHC/AMTPAYMTC  "Total Cust. Amount Applied"
                             IDBANK "Bank Code", IDRMIT "Check Number", DATERMIT "Payment Date"
                             TRXTYPE smallint     mirrors APOBL.IDTRXTYPE (50/51 seen)
                             CNTBTCH, CNTITEM     the originating payment batch
                             CODECURN, RATEEXCHHC, FISCYR, FISCPER, LONGSERIAL, IDPREPAID
                             34 columns
Primary key:                 APOBP_KEY_0 (IDVEND, IDINVC, CNTPAYMNBR, IDRMIT, DATEBUS,
                                          TRANSTYPE, CNTSEQNCE)
Foreign keys:                NONE
Implicit relationships:      (IDVEND,IDINVC) -> APOBL
                             (IDBANK, CNTBTCH, CNTITEM) -> APBTA/APTCR (KEY_1)
                             IDBANK -> BKACCT.BANK
Row count:                   1,539,678
Example records:             OTHX003 / 'LINKED IN' / 1 / rmit '000000000000043423' /
                               DATEBUS 20251212 / TRXTYPE 51 / -67,525.32 / bank HDFCCC /
                               batch 33422
Evidence:                    DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       APOBLJ
Verified full name:          A/P Obligation Ledger Journal — the POSTED-side distribution lines
Module:                      AP
Purpose:                     ***THE MOST IMPORTANT TABLE THIS AUDIT FOUND THAT THE MIGRATION
                             DOES NOT USE.*** One row per GL distribution line of a POSTED
                             document, keyed directly on (vendor, document, line).
Important columns:           IDVEND, IDINVC, CNTLINE   PK  <-- keyed on the DOCUMENT, unlike APIBD
                             IDGLACCT char(45)     "G/L Account"
                             AMTINVCHC / AMTINVCTC line amount (GROSS - see below)
                             AMTDUEHC              line balance
                             TXRATE1..5 decimal(15,5)  the STATED per-line tax rate
                             TAXCLASS1..5, TXBSERT1..5TC, TXAMTRT1..5HC, TXALLRTHC, TXEXPRTHC
                             IDITEM, UNITMEAS, QTYINVC, AMTCOST, BILLRATE
                             IDDIST "Distribution Code", CONTRACT/PROJECT/CATEGORY/RESOURCE
                             76 columns
                             *** THERE IS NO LINE-DESCRIPTION COLUMN. *** APIBD.TEXTDESC has
                             no counterpart here. That is the one thing APIBD gives that
                             APOBLJ does not.
Primary key:                 APOBLJ_KEY_0 (IDVEND, IDINVC, CNTLINE)
Foreign keys:                NONE
Implicit relationships:      (IDVEND,IDINVC) -> APOBL: 0 orphans of 1,305,953.
                             COVERAGE vs APIBD on the migration window (see DQ-1):
                               12,781 window AP-direct type-12 documents
                               12,781 (100.0%) have APOBLJ lines
                               11,702 ( 91.6%) have APIBD lines
                             AMOUNT SEMANTICS DIFFER - this matters:
                               SUM(APOBLJ.AMTINVCHC) = APOBL.AMTINVCHC (GROSS) in 12,781/12,781
                               SUM(APIBD.AMTDIST)    = APOBL.AMTINVCHC - AMTTAXHC (PRE-TAX)
                                                       in 11,659 of 11,702
Row count:                   1,305,953
Example records:             OTHX003 / 'LINKED IN' / line 1 / acct '4E5O030' / 67,525.32 /
                               item '' / qty 0 / TXRATE1 0 / TXRATE2 0
Evidence:                    DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       APPJH / APPJD
Verified full name:          A/P Posting Journal Header / Detail
Module:                      AP
Purpose:                     The audit trail of each AP posting run: for each posted batch
                             entry, the GL legs it produced. APPJD is the closest thing in AP
                             to a "what did this document do to the GL" record.
Important columns:           APPJH PK (TYPEBTCH, POSTSEQNCE, CNTBTCH, CNTITEM)
                             APPJD PK (TYPEBTCH, POSTSEQNCE, CNTBTCH, CNTITEM, CNTSEQENCE)
                             APPJD: IDVEND, IDINVC, IDACCT, ACCTTYPE, SRCETYPE, FISCYR,
                                    FISCPER, AMTEXTNDHC/TC, BASETAXHC, AMTTAXHC, GLREF,
                                    GLDESC, IDDIST, IDBANK, IDRMIT
                             APPJD: GLBATCH char(6), GLENTRY char(5)  <-- SEE DQ-6, ALWAYS EMPTY
Primary key:                 as above (sys.indexes)
Foreign keys:                NONE
Implicit relationships:      (TYPEBTCH, POSTSEQNCE, CNTBTCH, CNTITEM) joins APPJH to APPJD.
                             POSTSEQNCE matches APOBL.POSTSEQNCE for the same document -
                             verified: APPJH POSTSEQNCE=15646 -> (IN, 17103, 1, OTHX003,
                             'BAJAJ FINA', FY2026 P10), which is exactly the APOBL row.
                             NOTE POSTSEQNCE is NOT unique on its own - the same value 15646
                             also returns a 2022 PY-batch row (FABU155/PP036012). It must
                             always be qualified by TYPEBTCH.
Row count:                   APPJH 986,489 / APPJD 4,236,655
Example records:             (IN, 15646, 17103, 1, seq 1) OTHX003 'BAJAJ FINA' acct 1L7OV09
                               ACCTTYPE 1  -3,758,448.00     <- the AP control account credit
                             (IN, 15646, 17103, 1, seq 2) acct 1L3L013 ACCTTYPE 2 +2,777,778.00
                             (IN, 15646, 17103, 1, seq 3) acct 4E6F056 ACCTTYPE 2 +1,089,634.00
Evidence:                    DATABASE VERIFIED
Confidence:                  HIGH
```

**Other AP tables proved or disproved by name (mandate asked for each):**

| Name | Verdict |
|---|---|
| `APVENA` | **DOES NOT EXIST.** No table of that name in `sys.tables`. The vendor-adjacent tables that do exist are `APVCM` (2,670 — vendor comments), `APVGR` (52 — vendor groups), `APVGS` (3,027), `APVSM` (62,601 — vendor statistics), `APSLVEN` (1). |
| `APTCR` | **`Payments/Adjustments`** — verbatim `AUVIEW.VWDESC`, VIEWID AP0031. PK `(BTCHTYPE, CNTBTCH, CNTENTR)`. 312,560 rows. The payment-batch *entry* header (one per cheque/remittance): `IDRMIT`, `IDVEND`, `DATERMIT`, `AMTRMIT`, `IDBANK`, `CODECURN`, `PAYMSTTS`. 166 columns. |
| `APTCP` | **`Applied Payments`** — verbatim `AUVIEW.VWDESC`, VIEWID AP0033. PK `(BATCHTYPE, CNTBTCH, CNTRMIT, CNTLINE)`, with `KEY_1 (IDVEND, IDINVC, CNTPAYM)` and a UNIQUE `KEY_2`. 716,389 rows. The application of a payment entry to a specific invoice — `IDVEND`, `IDINVC`, `CNTPAYM`, `AMTPAYM`, `AMTERNDISC`. |
| `APTCN` | **`Miscellaneous Payments`** — verbatim `AUVIEW.VWDESC`, VIEWID AP0032. PK `(BATCHTYPE, CNTBTCH, CNTRMIT, CNTLINE)`. 220,502 rows. Despite the AUVIEW label this is the **distribution-line** table for a misc payment: it carries `IDDISTCODE`, `IDACCT`, `GLREF`, `GLDESC`, `TXBSE1..5TC`, `RATETAX1..5`, `AMTDISTHC`. It is where a payment that hits a GL account directly (no invoice) records that account. |
| `APBTA` | **`Payment and Adjustment Batches`** — verbatim `AUVIEW.VWDESC`, VIEWID AP0030. PK `(PAYMTYPE, CNTBTCH)`, UNIQUE `KEY_2 (PAYMTYPE, BATCHSTAT, CNTBTCH)`. 40,463 rows. The payment-batch control record; the `PY` counterpart of `APIBC`. |
| `APTRK` | **NOT what the name suggests.** Not a generic "tracking" table. PK `(IDBANK, CNTBTCH, CNTENTRY, CNTLINE, CNTSEQ)` with `KEY_1 (IDVEND, IDINVC, CNTPAYM, IDRMIT, LONGSERIAL, TRXTYPE, CNTSEQOBP)`. 220,170 rows. It is the **cheque/remittance register keyed by bank** — it links a bank + payment batch + line to the document paid. `IDVEND` is blank on the rows sampled; `IDINVC` holds a payment-batch document number (`PY000001`). Confidence: MEDIUM. |
| `APIBT` (2,753), `APAGED` (25,575), `APDPO` (3,835), `APOBLO`/`APIBDO`/`APOBLJO`/`APTCRO`/`APPJHO`/`APPJDO` | Optional-field twins and ageing/statistics caches. Not dossiered. |

### 3.2 PO — Purchase Orders

`AUVIEW` documents this whole family verbatim, so the names below are **DOCUMENTED**, not
inferred. Note that Sage's *view* name drops the `1`/`2` suffix: view `PO0620` is
`POPORH` = `Purchase Orders`, and the physical storage is split into `POPORH1` + `POPORH2`.

| Table | `AUVIEW.VWDESC` (verbatim) | Rows | PK |
|---|---|---|---|
| `POPORH1` | `Purchase Orders` (PO0620) | 235,337 | `PORHSEQ` · UNIQUE `PONUMBER` · UNIQUE `(VDCODE, PONUMBER)` |
| `POPORH2` | (same view, second physical table) | 235,337 | `PORHSEQ` |
| `POPORL` | `Purchase Order Lines` (PO0630) | 779,031 | `(PORHSEQ, PORLREV)` · UNIQUE `(PORHSEQ, PORLSEQ)` |
| `PORCPH1` | `Receipts` (PO0700) | 446,748 | `RCPHSEQ` · UNIQUE `RCPNUMBER` · UNIQUE `(PONUMBER, RCPNUMBER)` |
| `PORCPH2` | (same view) | 446,748 | `RCPHSEQ` |
| `PORCPL` | `Receipt Lines` (PO0710) | 815,146 | `(RCPHSEQ, RCPLREV)` · UNIQUE `(PORHSEQ, PORLSEQ, RCPHSEQ, RCPLSEQ)` |
| `POINVH1` | `Invoices` (PO0420) | 420,152 | `INVHSEQ` · `INVNUMBER` · `RCPNUMBER` · UNIQUE `(VDCODE, INVHSEQ)` |
| `POINVH2` | (same view) | 420,152 | `INVHSEQ` |
| `POINVL` | `Invoice Lines` (PO0430) | 810,360 | `(INVHSEQ, INVLREV)` · UNIQUE `(INVHSEQ, INVLSEQ)` |
| `PORETH1/2`, `PORETL` | `Returns` / `Return Lines` (PO0731/PO0735) | 3,278 / 3,695 | |
| `POCRNH1/2`, `POCRNL` | `Credit/Debit Notes` / `…Lines` (PO0311/PO0315) | 8,484 / 12,603 | |
| `PORQNH1/2`, `PORQNL` | `Requisitions` / `…Lines` (PO0760/PO0770) | 149,771 / 1,387,256 | |
| `POINVS`, `PORCPS`, `POCRNS` | `Invoice/Receipt/CR-DR Additional Costs` | 111,078 / — / 1,273 | |
| `POINVC`, `POPORC`, `PORCPC` | `…Comments` | 6,911 / 5,796 / 46,738 | |

```
Table:                       POINVH1  (+ POINVH2)
Verified full name:          P/O Invoices                           <- verbatim AUVIEW.VWDESC, VIEWID PO0420
Module:                      PO (Purchase Orders)
Purpose:                     Vendor invoice matched against a PO receipt. THIS IS THE SOURCE
                             OF THE MIGRATION'S "GOODS" POPULATION - the staging mirror's
                             sage_bill_hdr is keyed on INVHSEQ (see §7).
Important columns:           INVHSEQ decimal(19,0)  PK, a monotonically-issued sequence key
                             INVNUMBER char(22)     vendor's invoice number
                             VDCODE char(12)        vendor - joins APVEN.VENDORID
                             VDNAME/VDADDRESS1..4/VDCITY/VDSTATE/VDZIP/VDCOUNTRY
                                                    vendor snapshot COPIED ONTO the document
                             DATE decimal(9,0)      invoice date
                             PORHSEQ, PONUMBER      the purchase order
                             RCPHSEQ, RCPNUMBER, RCPDATE   the receipt
                             RETHSEQ, RETNUMBER, RETDATE   the return, if any
                             CURRENCY char(3), RATE decimal(15,7), RATETYPE, RATEDATE
                             TAXGROUP char(12), TAXAUTH1..5
                             EXTENDED "Extended Cost", DOCTOTAL, AMOUNT
                             FISCYEAR char(4), FISCPERIOD smallint
                             ISCOMPLETE, POSTDATE, TERMSCODE, LINES, TAXLINES
                             210 columns; POINVH2 carries bill-to/ship-to address blocks and
                             the CAX* (additional-cost tax) amounts.
Primary key:                 POINVH1_KEY_0 (INVHSEQ);  POINVH2_KEY_0 (INVHSEQ)
Foreign keys:                NONE
Implicit relationships:      INVHSEQ -> POINVH2 (1:1), -> POINVL (0 headerless/lineless: 0 of
                               18,047 window headers lack lines)
                             VDCODE -> APVEN.VENDORID  (357 distinct in the window, all resolve)
                             PORHSEQ -> POPORH1.PORHSEQ ; RCPHSEQ -> PORCPH1.RCPHSEQ
                             (the full drill-down chain incl. DRILLDWNLK and POSTEDTOIC is
                              already proven in business-flows/01-sage-actual-flow.md)
Row count:                   420,152   (window Jan-Apr 2026: 18,047)
Example records:             INVHSEQ 214593177 / FABI623 / 'D012515924*1' / 20260102 /
                               PO 'IDPO2517990' / INR / 25,883.55
                             INVHSEQ 214599149 / FABC001 / 'ID25501208*1' / 20260103 /
                               PO 'IDPO2519954' / CNY /  9,185.24
Evidence:                    DOCUMENTED (AUVIEW) + DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       POINVL
Verified full name:          P/O Invoice Lines                      <- verbatim AUVIEW.VWDESC, VIEWID PO0430
Module:                      PO
Purpose:                     The item lines of a PO-matched vendor invoice. Carries quantity,
                             unit cost, tax base, and - critically - the STATED tax rate.
Important columns:           INVHSEQ, INVLREV  PK ; INVLSEQ the stable line sequence
                             ITEMNO char(?)   *** holds the FORMATTED item number - joins
                                              ICITEM.FMTITEMNO, NOT ICITEM.ITEMNO. See §4. ***
                             ITEMDESC, VENDITEMNO, MANITEMNO, LOCATION
                             RQRECEIVED / SQRECEIVED / OQRECEIVED  qty in receipt/stock/order UOM
                             ORDERUNIT, ORDERCONV, RCPUNIT, RCPCONV
                             UNITCOST, EXTENDED, FCEXTENDED, DISCPCT, DISCOUNT
                             TAXRATE1..5 decimal    <- the stated rate, per authority slot
                             TAXBASE1..5, TAXAMOUNT1..5, TAXCLASS1..5, TAXINCLUD1..5
                             TXRECVAMT1..5 (recoverable) / TXEXPSAMT1..5 (expensed)
                             GLACEXPENS, GLNONSTKCR   the GL accounts
                             RCPHSEQ, RCPLSEQ, PORHSEQ, PORLSEQ, PONUMBER, RCPNUMBER
                             POSTEDTOIC, COMPLETION, FULLYINV
                             196 columns
Primary key:                 POINVL_KEY_0 (INVHSEQ, INVLREV) ; UNIQUE KEY_1 (INVHSEQ, INVLSEQ)
Foreign keys:                NONE
Implicit relationships:      INVHSEQ -> POINVH1     100%
                             ITEMNO  -> ICITEM.FMTITEMNO   17,129/17,129 = 100% (window)
                             ITEMNO  -> ICITEM.ITEMNO         195/17,129 =   1.1% (window)
                             (RCPHSEQ, RCPLSEQ) -> PORCPL
                             (PORHSEQ, PORLSEQ) -> POPORL
Row count:                   810,360   (window: 32,586 lines)
Evidence:                    DOCUMENTED (AUVIEW) + DATABASE VERIFIED
Confidence:                  VERIFIED
```

**`POPOALO` and `POPORAL` — proved, and they are NOT what the mandate's name list implies.**
There is no table called `POPOALO`-as-allocation. Both are **PO day-end audit tables**:

- `POPORAL` (7,082,723 rows), PK `(DAYENDSEQ, PORAHSEQ, PORALSEQ)`, with a parent
  `POPORAH` (1,362,776 rows, PK `(DAYENDSEQ, PORAHSEQ)`, UNIQUE on `PONUMBER` and on
  `VENDOR`). Its columns are a **full snapshot of a PO line at day-end**: `TRANSTYPE`,
  `ITEMNO`, `LOCATION`, `ITEMDESC`, `OQORDERED/OQRECEIVED/OQCANCELED/OQOUTSTAND`,
  `UNITCOST`, `FCEXTENDED`, `TAXBASE1..5`, `TAXAMOUNT1..5`, `GLITEM`. Sample row:
  `DAYENDSEQ 1, PORAHSEQ 1, PORALSEQ 2509, ITEMNO 'OTH-STN-0003', LOCATION 'CENSTR',
  50 NOS @ 48.33 = 2,416.50, TAXAMOUNT2 132.91, GLITEM '2A8ST09'`.
- `POPOALO` (11,590,158 rows), PK `(DAYENDSEQ, PORAHSEQ, PORALSEQ, OPTFIELD)` — the
  **optional-field values on those audit lines**. It is the largest PO table purely
  because it is a key/value expansion of the largest PO audit table.
- `PORCPAH/PORCPAL`, `POINVAH/POINVAL`, `PORETAH/PORETAL`, `POCRNAH/POCRNAL` follow the
  identical `…AH`/`…AL` audit pattern for receipts, invoices, returns and notes.

Evidence: DATABASE VERIFIED. Confidence: HIGH.

### 3.3 IC — Inventory Control

```
Table:                       ICITEM
Verified full name:          I/C Item Master
Module:                      IC (Inventory Control - GLSRCE 'I/C Receipts', 'I/C Shipments', …)
Purpose:                     One row per stock-keeping item.
Important columns:           ITEMNO char(?)     PK - the UNFORMATTED item number, e.g. 'ID44874ATH01'
                             FMTITEMNO          the FORMATTED item number, e.g. 'ID44874A-TH01'
                                                *** this is the key PO/OE documents carry ***
                             DESC char(?)       description
                             CATEGORY char(6)   4ACCES 547,317 / 4PACK 276,846 / 5WPSEW 58,724
                                                / 5WPCUT 47,930 / 4FASHL 44,908 / 4FABRI 43,247
                                                / 4FAINT 37,622 / 4LABEL 36,481 / 7IKEA 29,688
                                                / 4THRED 23,772 / 6FGOOD 11,222 / 4CARTN 8,962 …
                             STOCKUNIT          NOS / MTRS / PCS / ROLLS / CONES / …
                             STOCKITEM smallint 1 on ALL 1,196,108 rows
                             INACTIVE smallint  1 on 333,583 rows
                             SEGMENT1..10       item-number segments
                             TARIFFCODE         present but EMPTY on the rows sampled - the
                                                HSN code lives in ICITEMO, not here
                             CNTLACCT, DEFPRICLST, ALTSET, ITEMBRKID, SERIALNO, LOTITEM
                             69 columns
Primary key:                 ICITEM_KEY_0 (ITEMNO)
                             + UNIQUE KEY_3 (FMTITEMNO)  <- so FMTITEMNO is a true alternate key
                             + UNIQUE KEY_1 (CATEGORY, ITEMNO), KEY_4 (DESC, ITEMNO)
Foreign keys:                NONE
Implicit relationships:      ICITEM.ITEMNO    -> ICITEMO / ICILOC / ICUNIT / ICPRIC (all keyed
                                                 on the UNFORMATTED ITEMNO)
                             ICITEM.FMTITEMNO <- POPORL.ITEMNO / POINVL.ITEMNO / PORCPL.ITEMNO
                             *** THE JOIN KEY CHANGES BETWEEN THE TWO SIDES. See §4/DQ-4. ***
Row count:                   1,196,108   (and COUNT(DISTINCT RTRIM(ITEMNO)) = 1,196,108, so
                             these really are 1.2 million distinct items, not duplication.
                             This is a garment exporter that mints a new SKU per style /
                             buyer / order; the staging mirror holds only 16,815 = 1.4%.)
Example records:             'CHGSBANK'      / fmt 'CHGSBANK-'      / 'BANK CHARGES' / 3OTCHG / NOS
                             'ID44874ATH01'  / fmt 'ID44874A-TH01'  / 'TEX27 3PLY RECYLED…' / 4THRED / MTRS
                             'ID44440XLB02'  / fmt 'ID44440X-LB02'  / 'LABEL 1 -PDM#059200' / 4LABEL / NOS
Evidence:                    DATABASE VERIFIED
Confidence:                  VERIFIED
```

```
Table:                       ICITEMO
Verified full name:          I/C Item Optional Fields
Module:                      IC
Purpose:                     Key/value optional fields per item. *** THIS IS WHERE THE HSN
                             CODE LIVES *** - the migration reads it, and run_all.sh's
                             "9999 placeholder" warning is about this table being unreachable.
Important columns:           ITEMNO, OPTFIELD  PK    (ITEMNO is the UNFORMATTED form)
                             VALUE char(60)          the optional-field value
                             TYPE, LENGTH, DECIMALS, ALLOWNULL, VALIDATE, SWSET
                             13 columns
Primary key:                 ICITEMO_KEY_0 (ITEMNO, OPTFIELD) ; UNIQUE KEY_1 (OPTFIELD, ITEMNO)
Foreign keys:                NONE
Implicit relationships:      ITEMNO -> ICITEM.ITEMNO
                             OPTFIELD -> CSOPTFD.OPTFIELD (the optional-field master)
Row count:                   984,055 total.  By OPTFIELD:
                               HSNCODE 971,675 · FABCC 3,095 · FABCODE 3,095 · FABCOM 3,095
                               · FABGSM 3,095      (query below)
                               SELECT RTRIM(OPTFIELD) f, COUNT(*) n FROM ICITEMO
                                 GROUP BY OPTFIELD ORDER BY n DESC;
Example records:             ('ID44874ATH01','HSNCODE') -> '5402'   (well-formed)
                             ('CHGDYE','HSNCODE')       -> ' 5407610000'  (10 digits + leading space)
                             ('ID13216XMNL1','HSNCODE') -> '5807120'      (7 digits - invalid)
Evidence:                    DATABASE VERIFIED
Confidence:                  VERIFIED
```

**HSN coverage — the number the migration actually needs.** Of the 17,129 distinct items
on window PO invoices, **15,115 (88.2%) have a non-blank HSN and 2,014 (11.8%) do not**:

```sql
WITH l AS (SELECT DISTINCT RTRIM(l.ITEMNO) i FROM POINVL l
             JOIN POINVH1 h ON h.INVHSEQ=l.INVHSEQ
            WHERE h.DATE BETWEEN 20260101 AND 20260430),
     j AS (SELECT l.i, t.ITEMNO ic FROM l JOIN ICITEM t ON RTRIM(t.FMTITEMNO)=l.i),
     f AS (SELECT j.i, (SELECT TOP 1 RTRIM(o.VALUE) FROM ICITEMO o
                          WHERE o.ITEMNO=j.ic AND RTRIM(o.OPTFIELD)='HSNCODE') hsn FROM j)
SELECT COUNT(*) items, SUM(CASE WHEN hsn IS NULL THEN 1 ELSE 0 END) no_hsn_row,
       SUM(CASE WHEN hsn='' THEN 1 ELSE 0 END) blank_hsn,
       SUM(CASE WHEN hsn IS NOT NULL AND hsn<>'' THEN 1 ELSE 0 END) has_hsn FROM f;
-- items 17,129 | no_hsn_row 0 | blank_hsn 2,014 | has_hsn 15,115
```
Every window item has an `ICITEMO` **row**; 2,014 of them have a **blank value**. Across
the whole item master the picture is far worse — see DQ-7.

**`ICITEM1` and `ICITEM2` are stale copies of the item master sitting in the live company
database.** Their PK index names give it away — `PK__ICITEM_c__7A8D6CD09C02BE34` and
`PK__ICITEM_c__7A8D6CD0E6DEC0EE`, i.e. auto-named PKs on tables created as `ICITEM_copy1`
/ `ICITEM_copy2`; their secondary indexes are literally named `KEY_1_copy4`,
`KEY_2_copy1`, `KEY_3_copy2`. Measured:

```sql
SELECT 'ICITEM' t, COUNT(*) n, MIN(AUDTDATE) mn, MAX(AUDTDATE) mx FROM ICITEM
UNION ALL SELECT 'ICITEM1', COUNT(*), MIN(AUDTDATE), MAX(AUDTDATE) FROM ICITEM1
UNION ALL SELECT 'ICITEM2', COUNT(*), MIN(AUDTDATE), MAX(AUDTDATE) FROM ICITEM2;
SELECT (SELECT COUNT(*) FROM ICITEM1 a LEFT JOIN ICITEM b ON b.ITEMNO=a.ITEMNO WHERE b.ITEMNO IS NULL) in1_not_in_main,
       (SELECT COUNT(*) FROM ICITEM  a LEFT JOIN ICITEM1 b ON b.ITEMNO=a.ITEMNO WHERE b.ITEMNO IS NULL) in_main_not_in_1,
       (SELECT COUNT(*) FROM ICITEM2 a LEFT JOIN ICITEM b ON b.ITEMNO=a.ITEMNO WHERE b.ITEMNO IS NULL) in2_not_in_main;
```

| Table | Rows | `AUDTDATE` range | Items absent from `ICITEM` |
|---|---|---|---|
| `ICITEM` | 1,196,108 | 20160312 – **20260717** | — |
| `ICITEM1` | 663,858 | 20160312 – **20240517** | 0 |
| `ICITEM2` | 691,769 | 20160312 – **20240620** | **996** |

`ICITEM1` is a strict subset frozen at May 2024; `ICITEM2` is frozen at June 2024 and
contains 996 items that have since been **deleted** from the live master. `ICITEM` has
532,250 items neither copy has. **Migration risk**: any code that globs `ICITEM*`, or
picks a table by fuzzy name match, silently reads two-year-old data. Report only — no fix.

**Other IC tables:**

| Table | Verified name | Rows | PK | Notes |
|---|---|---|---|---|
| `ICILOC` | I/C Item Location (per-item, per-warehouse stock) | 2,117,805 | `(ITEMNO, LOCATION)` | `QTYONHAND`, `QTYONORDER`, `QTYCOMMIT`, `TOTALCOST`, `STDCOST`, `LASTCOST`, `LEADTIME`. **750 distinct locations**; largest are `GIT` 532,105 (goods in transit), `CENSTR` 317,188 (Accessories Stores — name confirmed from `POPORH2.STDESC`), `IDTNWH` 162,062, `IDU7` 133,714, `IDU10` 109,829, `NJU1P1` 101,247. |
| `ICUNIT` | I/C Item Units of Measure | 1,396,595 | `(ITEMNO, UNIT)` | 7 columns: `CONVERSION` is the factor to stocking unit. Units: `NOS` 528,433 · `MTRS` 317,106 · `PCS` 240,899 · `ROLLS` 97,494 · `CONES` 79,383 · `BOX` 42,177 · `YRDS` 39,686 · `GROSS` 23,212 · `BOX10K` 12,716 · `KGS` 4,865 · … (15 distinct). |
| `ICPRIC` | I/C Item Price List | 1,095,156 | `(CURRENCY, ITEMNO, PRICELIST)` | Exactly **one** price list, `IDEPL`, in 7 currencies: INR 1,055,647 · USD 38,867 · EUR 249 · RMB 247 · CNY 127 · GBP 17 · HKD 2. |
| `ICIVAL`, `ICHIST`, `ICTREDO`, `ICTRNDP` | valuation / history / transaction detail | 16.5M / 15.2M / 7.2M / 7.2M | | **Excluded** — see §2.4. |

Evidence for all four: DATABASE VERIFIED. Confidence: HIGH (`ICILOC`/`ICUNIT`/`ICPRIC`
proved from columns + PK + value distributions; not in `AUVIEW`).

### 3.4 GL — General Ledger

```
Table:                       GLAMF
Verified full name:          G/L Account Master File - the chart of accounts
Module:                      GL
Purpose:                     One row per GL account.
Important columns:           ACCTID char(?)     PK - the UNFORMATTED account code, e.g.
                                                '1L3L01001' (segments concatenated)
                             ACCTFMTTD          the FORMATTED account code, e.g. '1L3L010-01'
                                                *** this is the form AP/PO documents carry ***
                             ACCTDESC           description, e.g. 'RBL Bank GECL loan Account'
                             ACCTTYPE char(1)   B = Balance sheet (537), I = Income statement
                                                (1,072), R = Retained earnings (1)
                             ACCTGRPCOD         account group, e.g. '1L1C'
                             ACTIVESW smallint  active flag
                             ACSEGVAL01..10     the individual segment values
                             ACCTSEGVAL         the natural (first) segment
                             ABRKID char(?)     account structure: 'HO' (single-segment) or
                                                'UNIT' (two-segment)
                             SRCELDGID, CTRLACCTSW, MCSW, DEFCURNCOD, QTYSW, UOM
                             42 columns
Primary key:                 GLAMF_KEY_0 (ACCTID) ; KEY_12 (ACCTFMTTD) - non-unique index
Foreign keys:                NONE
Implicit relationships:      *** APIBD.IDGLACCT / APOBLJ.IDGLACCT join ACCTFMTTD, NOT ACCTID. ***
Row count:                   1,610
Evidence:                    DATABASE VERIFIED
Confidence:                  VERIFIED
```

**Account code structure — `extract.sql`'s description is right in substance, wrong in
detail, and the "strip the suffix" step is safe here but for a reason worth writing down.**

`extract.sql` says account IDs look like `4E3EB01-IDEPL-1` with `-IDEPL-1` a
"manufacturing-unit suffix", and that `ACCTFMTTD` is the formatted code that joins to AP
lines while `ACCTID` is unformatted. Measured:

```sql
SELECT SUM(CASE WHEN CHARINDEX('-',RTRIM(ACCTID))>0 THEN 1 ELSE 0 END) with_dash,
       COUNT(*) total FROM GLAMF;                    -- with_dash 0 | total 1,610
SELECT TOP 5 RTRIM(ACCTID) id, RTRIM(ACCTFMTTD) fmt, LEFT(RTRIM(ACCTDESC),28) dsc,
       RTRIM(ACSEGVAL01) s1, RTRIM(ACSEGVAL02) s2, RTRIM(ABRKID) brk
  FROM GLAMF WHERE CHARINDEX('-',RTRIM(ACCTFMTTD))>0;
```
| `ACCTID` | `ACCTFMTTD` | `ACSEGVAL01` | `ACSEGVAL02` | `ABRKID` | `ACCTDESC` |
|---|---|---|---|---|---|
| `1L3L01001` | `1L3L010-01` | `1L3L010` | `01` | `UNIT` | RBL Bank GECL loan Account |
| `1L3L01002` | `1L3L010-02` | `1L3L010` | `02` | `UNIT` | SBI GECL 1 Term Loan … |
| `1L3L01003` | `1L3L010-03` | `1L3L010` | `03` | `UNIT` | Axis Bank GECL Term Loan … |

- **Confirmed**: `ACCTID` is unformatted (0 of 1,610 contain a dash), `ACCTFMTTD` is
  formatted, and AP lines carry the **formatted** form.
- **Corrected**: the second segment is a **2-digit unit number**, not `IDEPL-1`.
  Distinct suffixes: `-14` (118 accounts), `-51` (85), `-12` (70), `-01` (57), `-03` (54),
  `-04` (54), `-91` (53), `-05` (52), `-10` (51), `-07` (50), `-06` (49), `-92` (49),
  `-08` (48), `-11` (48), `-13` (48), `-15` (37), + 6 more = **22 unit codes**.
- **Join proof** on 22,287 AP distribution lines from window batches 17000–17400:
```sql
WITH d AS (SELECT RTRIM(IDGLACCT) a FROM APIBD WHERE CNTBTCH BETWEEN 17000 AND 17300),
     f AS (SELECT d.a,
            CASE WHEN EXISTS(SELECT 1 FROM GLAMF g WHERE RTRIM(g.ACCTID)=d.a)    THEN 1 ELSE 0 END mid,
            CASE WHEN EXISTS(SELECT 1 FROM GLAMF g WHERE RTRIM(g.ACCTFMTTD)=d.a) THEN 1 ELSE 0 END mfm
          FROM d)
SELECT COUNT(*) lines, SUM(mid) match_ACCTID, SUM(mfm) match_ACCTFMTTD FROM f;
-- lines 22,287 | match_ACCTID 19,732 (88.5%) | match_ACCTFMTTD 22,287 (100.0%)
```
  2,555 of those 22,287 lines (11.5%) carry a dashed account.
- **The suffix-strip is safe *on this data*, but it is a collapse.** `extract.sql` reduces
  `4E3EB01-14` to `4E3EB01`. All 159 distinct stripped natural accounts in the window
  resolve against `GLAMF.ACCTID`:
```sql
WITH d AS (SELECT DISTINCT LEFT(RTRIM(IDGLACCT),
             CASE WHEN CHARINDEX('-',RTRIM(IDGLACCT))>0 THEN CHARINDEX('-',RTRIM(IDGLACCT))-1
                  ELSE LEN(RTRIM(IDGLACCT)) END) a
             FROM APIBD WHERE CNTBTCH BETWEEN 17000 AND 17300),
     f AS (SELECT d.a, CASE WHEN EXISTS(SELECT 1 FROM GLAMF g WHERE RTRIM(g.ACCTID)=d.a) THEN 1 ELSE 0 END m FROM d)
SELECT COUNT(*) distinct_natural_accts, SUM(m) found_in_GLAMF_ACCTID FROM f;
-- distinct_natural_accts 159 | found_in_GLAMF_ACCTID 159
```
  But by design it **merges up to 22 unit-level accounts into one product**, so
  unit-level spend attribution is not recoverable downstream. That is a deliberate
  modelling choice, not a defect — flagging it so it is a known one.

**Account prefix map** (`SELECT LEFT(RTRIM(ACCTID),2) pfx, COUNT(*) n, MIN(LEFT(RTRIM(ACCTDESC),30)), MAX(...) FROM GLAMF GROUP BY LEFT(RTRIM(ACCTID),2)`):

| Prefix | Accounts | Meaning (from `ACCTDESC` + `ACCTTYPE`) | Sample descriptions |
|---|---|---|---|
| `1L` | 182 | Capital & Liabilities (`ACCTTYPE B`) | `Naseer Humayun Capital Account`, `A TREDS ACCOUNT`, `YES BANK-EPC A/C`, `RBL Bank GECL loan Account` |
| `2A` | 356 | Assets (`ACCTTYPE B`) | `Accessories Stock`, `Yes Bank Term Deposit`; `2A7TX01/02/03` are the SGST/CGST/IGST **recoverable** accounts per `TXAUTH.ACCTRECOV` |
| `3I` | 42 | Income (`ACCTTYPE I`) | `ABRY Benefit`, `Sales/Purchase- Local Intra co` |
| `4E` | 1,029 | Expenses (`ACCTTYPE I`) | `Bank Charges - Canara Bank`, `Xerox Machine Hire Charges, ID…` |
| `31` | **1** | **junk** — blank description, see DQ-8 | `''` |

`ACCTTYPE` counts corroborate: `I` = 1,072 ≈ `4E` 1,029 + `3I` 42 = 1,071; `B` = 537 ≈
`1L` 182 + `2A` 356 = 538; `R` = 1 (`Reserves & Surplus (Retained…`). The one-account
discrepancy is the blank `31` row.

**`GLPOST` vs `GLPJD` — they are the same posted data in two clusterings, not two populations.**

| | `GLPOST` | `GLPJD` |
|---|---|---|
| Rows | 22,885,750 | 22,885,750 (**identical**) |
| `FISCALYR` range | 2016 – 2027 | 2016 – 2027 |
| PK | `(ACCTID, FISCALYR, FISCALPERD, SRCECURN, SRCELEDGER, SRCETYPE, POSTINGSEQ, CNTDETAIL)` | `(POSTINGSEQ, BATCHNBR, ENTRYNBR, TRANSNBR)` |
| Columns | 42 | 42 |
| Rows for `POSTINGSEQ = 30529` | 1,190 | 1,190 |

Every `GLPJD` row sampled for FY2026 P10 resolves into `GLPOST` on
`(POSTINGSEQ, BATCHNBR, ENTRYNBR, TRANSNBR, ACCTID)`:
```sql
SELECT TOP 5 j.POSTINGSEQ, j.BATCHNBR, j.ENTRYNBR, j.TRANSNBR, RTRIM(j.ACCTID) acct, j.TRANSAMT,
       CASE WHEN g.ACCTID IS NULL THEN 'ABSENT from GLPOST' ELSE 'present' END inpost
  FROM GLPJD j LEFT JOIN GLPOST g
    ON g.POSTINGSEQ=j.POSTINGSEQ AND g.BATCHNBR=j.BATCHNBR
   AND g.ENTRYNBR=j.ENTRYNBR AND g.TRANSNBR=j.TRANSNBR AND g.ACCTID=j.ACCTID
 WHERE j.FISCALYR='2026' AND j.FISCALPERD=10;   -- all 'present'
```
`GLPOST` is clustered for **account-balance** queries; `GLPJD` for **posting-journal
report** order and additionally carries `CODESTATUS`, `SWREVERSE`, `ORIGCOMP`, `DATEENTRY`.
Either is a valid read of the posted GL. **`GLPJH` does not exist** — the header analogue
is `GLPJC` (22,818,637, the comment/description child), and the *batch* header is `GLBCTL`
(43,843). Confidence: HIGH (proved on FY2026 P10 sample + identical totals; I did not
scan all 22.9M rows).

**`GLJEH`/`GLJED` are the journal-ENTRY (batch) side.** `AUVIEW`: `GLJEH` = `Journal
Headers` (GL0006), `GLJED` = `Journal Details` (GL0010), `GLBCTL` = `Batches` (GL0008),
`GLJPST` = `Post Journal` (GL0030). `GLJEH` PK `(BATCHID, BTCHENTRY)`; `GLJED` PK
`(BATCHNBR, JOURNALID, TRANSNBR)`. `GLJEH` carries `SWEDIT`, `ERRBATCH`, `ERRENTRY`,
`JRNLDR`/`JRNLCR` (entry debit/credit totals) — the batch-entry shape. Sample FY2026:
`BATCHID 043790 / entry 00001 / GL / JE / P01 / 'BTA Entries - (NJK to IDEPl)' /
DR 3,878,170,102.000 = CR 3,878,170,102.000`.

So the GL mirrors AP exactly: **`GLJEH`/`GLJED` = batch/entry; `GLPOST`/`GLPJD` = posted ledger.**

**`GLAFS` — NOT COMPLETED.** `GLAFS` exists with **19,332 rows** and is almost certainly
the fiscal-set (per-account, per-year, per-period balance) table, which would be the right
source for opening balances and a trial balance. **I did not profile its columns, prove
its key, or reconcile it against `GLPOST`.** See §8.

### 3.5 TX — Tax Services

```
Table:                       TXAUDH / TXAUDD
Verified full name:          Tax Audit Header / Tax Audit Detail (the tax tracking ledger)
Module:                      TX (CSAPP SELECTOR 'TX', v69A)
Purpose:                     One header row per (document, tax authority) and one detail row
                             per (header, item class). This is the ledger the GST returns are
                             built from.
Important columns:           TXAUDH: SEQUENCE PK ; AUTHORITY char(12) ; TYPE smallint
                                     (1 = sales/AR, 2 = purchases/AP - matches TXGRP.TTYPE) ;
                                     FISCYEAR, FISCPERIOD ; CUSTVEND (the vendor/customer code) ;
                                     DOCDATE, POSTDATE, SRCEAPP (AP/AR), DOCTYPE (IN/CR/PY),
                                     DOCNUMBER, SRCEDOCNUM ; CUSTVENDNM ; ISFOREIGN, HCURN,
                                     SCURN, RATE ; SINVAMT/HINVAMT ; RECOVERABL, RATERECOV,
                                     SRECOVRAMT/HRECOVRAMT ; BUYERCLASS
                             TXAUDD: (SEQUENCE, ITEMCLASS) PK ; TAXRATE decimal ;
                                     SBASEAMT/HBASEAMT ; SCURNTAX/HCURNTAX ;
                                     SWHAMT/HWHAMT (withholding) ; SRCAMT/HRCAMT and
                                     SRCBASEAMT/HRCBASEAMT (REVERSE CHARGE amounts/base)
Primary key:                 TXAUDH_KEY_0 (SEQUENCE) ; UNIQUE KEY_1 (AUTHORITY, TYPE, FISCYEAR,
                               FISCPERIOD, CUSTVEND, DOCDATE, SRCEAPP, DOCTYPE, DOCNUMBER, SEQUENCE)
                             TXAUDD_KEY_0 (SEQUENCE, ITEMCLASS)
Foreign keys:                NONE
Implicit relationships:      TXAUDD.SEQUENCE -> TXAUDH.SEQUENCE
                             TXAUDH.AUTHORITY -> TXAUTH.AUTHORITY
                             TXAUDH.(CUSTVEND, DOCNUMBER) -> APOBL.(IDVEND, IDINVC) for SRCEAPP='AP'
                             (not measured as a match rate - see §8)
Row count:                   TXAUDH 1,216,805 / TXAUDD 1,376,133
Evidence:                    DATABASE VERIFIED
Confidence:                  HIGH
```

FY2026 distribution (`SELECT RTRIM(AUTHORITY), TYPE, RTRIM(SRCEAPP), DOCTYPE, COUNT(*) FROM TXAUDH WHERE FISCYEAR='2026' GROUP BY …`):
`IGST/2/AP/IN 42,558` · `CGST/2/AP/IN 32,966` · `SGST/2/AP/IN 32,966` · `NOTAXUSD/1/AR/IN 12,896` ·
`NOTAXUSD/2/AP/IN 3,445` · `SGST/2/AP/CR 2,780` · `CGST/2/AP/CR 2,780` · `TAXEXEMPT/2/AP/IN 2,632` ·
`IGST/2/AP/CR 2,511` · `CGST/2/AP/PY 751` · `SGST/2/AP/PY 751` · … (55 more).
GST authorities are directly identifiable; `TYPE=2` is the purchase side.

**The authoritative tax-rate tables — this matters for the project's "never divide to
infer a rate" rule.** Three masters, all proven:

- **`TXAUTH`** (24 rows) — tax authority master. `AUTHORITY`, `DESC`, `SCURN`, `TAXTYPE`,
  `RECOVERABL`, `RATERECOV`, `ACCTRECOV`. Real rows:
  `CGST | 'CGST' | INR | recoverable | ACCTRECOV '2A7TX02'` ·
  `SGST | 'SGST' | INR | recoverable | '2A7TX01'` ·
  `IGST | 'IGST' | INR | recoverable | '2A7TX03'` ·
  `ST | 'Service Tax' | recoverable | '2A7SL06'` · `VAT | 'VAT Recoverable' | '2A7SL08'` ·
  `TCS | 'TCS' | recoverable | '2A7SL15'` · `NRVAT | 'VAT Not Recoverable' | not recoverable` ·
  `NRST | 'Not Recoverable Service Tax'` · `CST`, `ED` (Excise Duty), `ENTRYTAX`,
  `TAXEXEMPT`, `URDLOCAL`, `URDINTER`, and nine `NOTAX<CCY>` import-exemption authorities
  (`NOTAXUSD`, `NOTAXEUR`, `NOTAXGBP`, `NOTAXCNY`, `NOTAXRMB`, `NOTAXYEN`, `NOTAXHKD`,
  `NOTAXAED`, `NOTAXCHF`, `NOTAXSEK`).
  **This confirms the pre-GST legacy groups `VAT`/`NRVAT`/`NRST`/`NRVATST` that
  `extract.sql` excludes are exactly that — pre-GST authorities.**
- **`TXRATE`** (58 rows) — **the authoritative rate table.** PK-shaped
  `(AUTHORITY, TTYPE, BUYERCLASS)` with `ITEMRATE1..ITEMRATE10`, indexed by tax class:

  | Authority | `TTYPE` | `BUYERCLASS` | rate1 | rate2 | rate3 | rate4 | rate5 |
  |---|---|---|---|---|---|---|---|
  | `CGST` | 2 (purchases) | 2 | 0.00 | **2.50** | **6.00** | **9.00** | **14.00** |
  | `SGST` | 2 | 2 | 0.00 | **2.50** | **6.00** | **9.00** | **14.00** |
  | `IGST` | 2 | 2 | 0.00 | **5.00** | **12.00** | **18.00** | **28.00** |
  | `TCS` | 2 | 2 | 0.00 | 0.075 | 0 | 0 | 0 |
  | `VAT` | 2 | 2 | 0.00 | 5.50 | 14.50 | 5.00 | 1.05 |
  | `NRVAT` | 2 | 2 | 0.00 | 5.50 | 14.50 | 3.50 | 4.00 |
  | `ST` / `NRST` | 2 | 2 | 0.00 | 14.50 | 15.00 | 0 | 0 |

  These are the clean legal GST slabs (2.5/6/9/14 for CGST+SGST = 5/12/18/28 combined,
  and 5/12/18/28 for IGST). **A line's stated rate = `TXRATE.ITEMRATE<TAXCLASSn>` for
  the authority in `CODETAX<n>`** — or, equivalently and more directly, the per-line
  columns `APIBD.RATETAX1..5`, `APOBLJ.TXRATE1..5`, `POINVL.TAXRATE1..5`.
- **`TXGRP`** (71 rows) — tax groups. PK-shaped `(GROUPID, TTYPE)`, with `AUTHORITY1..5`.
  Purchase-side (`TTYPE=2`) groups that matter: `LOCAL` = `'SGST + CGST'` (authorities
  SGST, CGST) · `INTERSTATE` = `'INTERSTATE'` (IGST) · `IMPORT` = `'IGST ON IMPORTS'` (IGST) ·
  `NOTAX` = `'Tax Exempted'` (TAXEXEMPT) · the nine `NOTAX<CCY>` groups · plus the pre-GST
  `VAT`, `NRVAT`, `NRST`, `NRVATST`, `ST`, `CST`, `ED*`, `ENTRYTAX`, `URDLOCAL`, `URDINTER`.
  `INTERSTATE1` exists with a **blank description and no authorities at all** (DQ-8).

  `APOBL.CODETAXGRP` for the window: `INTERSTATE 17,028` · `LOCAL 12,313` · **`'' (blank) 5,003`** ·
  `NOTAXUSD 1,897` · `NOTAX 1,426` · `NRST 110` · `NOTAXRMB 94` · `NRVATST 74` ·
  `NOTAXCNY 64` · `VAT 14` · `NRVAT 6` · `NOTAXEURO 2` · `NOTAXGBP 2`.

Also present: `TXCLASS` (199 — authority × class-type × class descriptions),
`TXSTATEH`/`TXSTATED` (125 / 225 — versioned tax-group state snapshots, which is what
`APIBH.TAXVERSION` / `APOBL.TAXVERSION` point at).

### 3.6 BK — Bank Services

```
Table:                       BKTRANH / BKTRAND
Verified full name:          Bank Transaction Header / Detail
Module:                      BK (Bank Services)
Purpose:                     One header per bank transaction (cheque run, deposit, transfer)
                             and one detail per line, with reconciliation state.
Important columns:           BKTRANH: (BANK, SERIAL) PK ; TRANSNUM ; SRCEAPP ; TRANSTYPE ;
                                      TRANSDATE, FSCYEAR, FSCPERIOD ; TOTAMOUNT, TOTBALAMT,
                                      TOTCLEARED ; RECSTATUS, RECONCILED-family ; COMPLETED ;
                                      PAYORNAME, VENDORNAME ; POSTDATE
                             BKTRAND: (BANK, SERIAL, LINE) PK ; IDREMIT, DATEREMIT ;
                                      PAYORID, VENDORNAME ; SRCEAMOUNT, FUNCAMOUNT, RATE ;
                                      DISTCODE, GLACCOUNT ; BTCHNBR, ENTRYNBR, POSTSEQ ;
                                      RECSTATUS, RECCLEARED, POSTYEAR, POSTPERIOD
Primary key:                 BKTRANH_KEY_0 (BANK, SERIAL) ; BKTRAND_KEY_0 (BANK, SERIAL, LINE)
Foreign keys:                NONE
Implicit relationships:      BANK -> BKACCT.BANK
                             (BANK, IDREMIT) <- APOBL.(IDBANK, IDRMIT) and APOBP.(IDBANK, IDRMIT)
                               - the route from an AP payment to the bank. NOT measured as a
                               match rate; see §8.
                             BKTRAND.GLACCOUNT -> GLAMF
Row count:                   BKTRANH 289,802 / BKTRAND 292,218
Evidence:                    DATABASE VERIFIED
Confidence:                  HIGH
```

`BKTRANH` by source and type — **AP is by far the dominant producer of bank activity**,
which is what you expect of a payables-heavy manufacturer:
```sql
SELECT RTRIM(SRCEAPP) app, TRANSTYPE, COUNT(*) n FROM BKTRANH GROUP BY SRCEAPP, TRANSTYPE ORDER BY n DESC;
-- AP/1 240,574 | AR/2 16,496 | BK/1 16,429 | BK/2 15,915 | AR/1 388
```
`BKACCT` (74 rows) is the bank master: `BANK` code, `NAME`, `IDACCT` (the GL account),
`MULTICUR`, `INACTIVE`. Samples: `CASH → 'Cash In Hand' → 2A5CA01`;
`AXISEPC → 'AXIS BANK EPC ACCOUNT' → 1L5B010`; `HDFCCC → 'HDFC Credit Card 5861' → 2A6BA34`;
`ATREDS → 'A.TReDS Ltd.,' → 1L5B023`. Note the split: current/credit-card accounts map to
`2A6BA*` (asset) and borrowing facilities (EPC, PCFC, CC, OD, term loans) to `1L5B*`/`1L3L*`
(liability) — consistent with the `1L`/`2A` prefix map above.
Also non-empty: `BKJTRANH`/`BKJTRAND` (202,266 / 204,625 — the posted-journal twins),
`BKJZGL` (48,973), `BKJTFRD`/`BKJTFR` (transfers), `BKENTH`/`BKENTD` (888 / 914 —
`Bank Entries Header`/`Bank Entries` per `AUVIEW` BK0450/BK0460), `BKCUR`, `BKTNUM`.

---

## 4. Relationship map — implicit joins with measured match rates

**There are ZERO declared foreign keys in this database.** Every relationship below is a
naming convention that the application maintains, not a constraint the database enforces.
Each row therefore carries a measured match rate and the query that produced it.

| From | To | Join predicate | Match rate | Note |
|---|---|---|---|---|
| `APOBL.IDVEND` | `APVEN.VENDORID` | `RTRIM()` both sides | **787,465 / 787,465 = 100%** | column names differ |
| `POINVH1.VDCODE` | `APVEN.VENDORID` | `RTRIM()` | 357 / 357 window vendors | third name for the same thing |
| `APIBH.(CNTBTCH,CNTITEM)` | `APIBC.CNTBTCH` | exact | **675,194 / 675,194 = 100%** (0 orphans) | |
| `APIBD.(CNTBTCH,CNTITEM)` | `APIBH` | exact | 1,270,481 / 1,270,496 (**15 orphans**, DQ-2) | |
| `APOBL.(CNTBTCH,CNTITEM)` | `APIBH.(CNTBTCH,CNTITEM,IDVEND,IDINVC)` | exact | **13,466 / 19,548 = 68.9%** (window, `SRCEAPPL='AP'`) | the 6,082 misses are `TYPEBTCH='PY'` documents that never had an invoice batch, plus DQ-1 |
| `APOBLJ.(IDVEND,IDINVC)` | `APOBL` | `RTRIM()` | **1,305,953 / 1,305,953 = 100%** (0 orphans) | |
| `APOBS.(IDVEND,IDINVC)` | `APOBL` | `RTRIM()` | **787,468 / 787,468 = 100%** (0 orphans) | |
| `APOBP.(IDVEND,IDINVC)` | `APOBL` | `RTRIM()` | not measured (§8) | |
| **`APIBD.IDGLACCT`** | **`GLAMF.ACCTFMTTD`** | `RTRIM()` | **22,287 / 22,287 = 100%** | **vs `ACCTID` only 19,732 = 88.5%** |
| **`POINVL.ITEMNO`** | **`ICITEM.FMTITEMNO`** | `RTRIM()` | **17,129 / 17,129 = 100%** | **vs `ICITEM.ITEMNO` only 195 = 1.1%** |
| **`POPORL.ITEMNO`** | **`ICITEM.FMTITEMNO`** | `RTRIM()` | **17,365 / 17,365 = 100%** | **vs `ICITEM.ITEMNO` only 223 = 1.3%** |
| `ICITEMO.ITEMNO` | `ICITEM.ITEMNO` | `RTRIM()` | keyed on the **unformatted** form | the key *flips* between the PO side and the IC child tables |
| `ICILOC.ITEMNO` | `ICITEM.ITEMNO` | exact | 2,117,747 / 2,117,805 (**58 orphans**) | |
| `ICUNIT.ITEMNO` | `ICITEM.ITEMNO` | exact | 1,396,568 / 1,396,595 (**27 orphans**) | |
| `POINVL.INVHSEQ` | `POINVH1.INVHSEQ` | exact | 32,586 / 32,586 window lines; 0 lineless headers | |
| `APPJD.(TYPEBTCH,POSTSEQNCE,CNTBTCH,CNTITEM)` | `APPJH` | exact | structural (shared PK prefix) | |
| `APOBL.POSTSEQNCE` | `APPJH.POSTSEQNCE` | **must also match `TYPEBTCH`** | verified on 15646 | `POSTSEQNCE` alone is **not unique** — 15646 returns both an `IN` FY2026 row and a `PY` FY2022 row |
| `TXAUDD.SEQUENCE` | `TXAUDH.SEQUENCE` | exact | structural | |
| `TXAUDH.AUTHORITY` | `TXAUTH.AUTHORITY` | `RTRIM()` | structural | |
| `APOBL.CODETAXGRP` | `TXGRP.GROUPID` (`TTYPE=2`) | `RTRIM()` | all non-blank window values resolve | 5,003 window rows are blank (DQ-8) |
| `BKACCT.BANK` | `APOBL.IDBANK` / `APOBP.IDBANK` / `BKTRANH.BANK` | `RTRIM()` | not measured (§8) | |
| `GLPJD.(POSTINGSEQ,BATCHNBR,ENTRYNBR,TRANSNBR,ACCTID)` | `GLPOST` | exact | all sampled FY2026 P10 rows present | same data, two clusterings |

### 4.1 The two key-form traps, stated as plainly as I can

Both are cases where **the same logical identifier is stored in two different textual
forms in different tables**, and picking the wrong one produces a near-total join failure
that looks like missing data rather than a bug.

1. **GL account**: documents carry the **formatted** code (`4E3EB01-14`),
   `GLAMF` PK is the **unformatted** code (`4E3EB0114`). Join `ACCTFMTTD`.
   Wrong choice loses 11.5% of lines.
2. **Item number**: PO/OE documents carry the **formatted** code (`ID44874A-TH01`),
   `ICITEM` PK and every IC child table (`ICITEMO`, `ICILOC`, `ICUNIT`, `ICPRIC`) use the
   **unformatted** code (`ID44874ATH01`). Join `POINVL.ITEMNO → ICITEM.FMTITEMNO`, then
   hop to `ICITEM.ITEMNO` before touching `ICITEMO`.
   Wrong choice loses **98.9%** of items — and would look exactly like "the item master is
   missing almost everything", which is a conclusion someone could plausibly draw and act on.

I did not find evidence that the migration gets either of these wrong; I am recording them
because they are the two places it is easiest to get wrong.

### 4.2 How an AP document reaches the General Ledger — partially proven, one gap

The chain `APOBL → APPJH/APPJD → GL` is real but **there is no key-based join from AP to
the GL in this database**. Two negative results:

```sql
SELECT SUM(CASE WHEN RTRIM(GLBATCH) IN ('','000000') THEN 1 ELSE 0 END) unpopulated, COUNT(*) n
  FROM APPJD WHERE FISCYR='2026';
-- unpopulated 539,153 | n 539,153     -> APPJD.GLBATCH/GLENTRY are EMPTY on every FY2026 row

SELECT RTRIM(SRCELEDGER) l, SUM(CASE WHEN RTRIM(CUSTVEND)<>'' THEN 1 ELSE 0 END) with_vend, COUNT(*) n
  FROM GLJEH WHERE FSCSYR='2026' GROUP BY SRCELEDGER;
-- AP: with_vend 0 of 155,049 ; PO: 0 of 60,929 ; IC: 0 of 261,927 ; AR: 0 of 19,465 ; …
--                              -> GLJEH.CUSTVEND / DOCNUMBER are blank on ALL FY2026 entries
```

What *does* work is a **description-text match**. The traced document's AP control leg
(`APPJD` seq 1: account `1L7OV09`, −3,758,448.00) is findable in `GLPOST`:

```sql
SELECT TOP 5 RTRIM(g.ACCTID) acct, g.POSTINGSEQ, g.BATCHNBR, g.ENTRYNBR, g.TRANSAMT,
       LEFT(RTRIM(g.JNLDTLDESC),30) dsc, LEFT(RTRIM(g.JNLDTLREF),22) ref
  FROM GLPOST g
 WHERE g.FISCALYR='2026' AND g.FISCALPERD=10
   AND RTRIM(g.SRCELEDGER)='AP' AND RTRIM(g.SRCETYPE)='IN'
   AND g.TRANSAMT = -3758448.000;
-- acct '1L7OV09' | POSTINGSEQ 30819 | BATCHNBR '040749' | ENTRYNBR '00027'
-- | TRANSAMT -3,758,448.000 | JNLDTLDESC 'Invoice-BAJAJ FINA' | JNLDTLREF 'Reimb IDEPL HO'
```

`GLPOST.JNLDTLDESC` = `'Invoice-' + <AP document number>`, and `SRCELEDGER='AP'`,
`SRCETYPE='IN'` (which `GLSRCE` calls `A/P Invoice`). Note also that AP's `POSTSEQNCE`
(15646) and GL's `POSTINGSEQ` (30819) are **different sequences** — they are not the same
counter and must not be joined.

**Conclusion: the AP↔GL tie in `IDEDAT` is a truncated description string, not a key.**
`JNLDTLDESC` is `char(?)` and the document number is truncated at the field width
(`'BAJAJ FINA'` is itself already the full `APOBL.IDINVC`, but longer document numbers
will be cut). Any reconciliation that needs to walk AP→GL per document should be treated
as unreliable and reported as such rather than relied on. **Match rate NOT measured** — see §8.

---

## 5. Structural facts

### 5.1 Declared constraints — the headline

```sql
SELECT COUNT(*) FROM sys.foreign_keys;      -- 0
SELECT type, COUNT(*) FROM sys.key_constraints GROUP BY type;   -- PK 1,106 | UQ 1
SELECT COUNT(*) FROM sys.check_constraints;    -- 0
SELECT COUNT(*) FROM sys.default_constraints;  -- 0
SELECT COUNT(*) FROM sys.extended_properties;  -- 11
```

| Constraint kind | Count |
|---|---|
| Tables | 1,110 (575 non-empty) |
| **Declared foreign keys** | **0** |
| Primary keys | 1,106 (so 4 tables have none) |
| Unique constraints | 1 |
| Check constraints | 0 |
| Default constraints | 0 |
| Views | 18 |

**Migration risk, stated plainly:** every relationship in this database is *convention*,
maintained by the Sage application layer, not *constraint* enforced by the engine. The
database will not stop a bad write, and — more relevant to a read-only migration — it
gives you no schema-level statement of what joins to what. That is why every relationship
in §4 carries a measured match rate rather than a diagram. It also means the two key-form
traps in §4.1 are invisible: nothing in the catalogue says `APIBD.IDGLACCT` targets
`ACCTFMTTD` rather than the primary key.

The 18 views are user-authored reporting helpers, not Sage objects:
`VW_BKCAS`, `VW_OCACCSET_VALUE`, `VW_OCADJ_VALUE`, `VW_OCITEM`, `VW_OCITEM_ADJ`,
`VW_OCITEM_ADJ_copy1`, `VW_OCITEM_COST`, `VW_OCLIST`, `VW_OCPOMIR_VALUE`, `VW_OCRMCOST`,
`VW_OLCOST`, `vw_po_sum`, `vw_PurchaseRegister_Detail`, `vw_PurchaseRegister_Summary`,
`vw_SalesRegister`, `vw_SalesRegister_Summary`, `vwAPTransaction`, `vwARLedger`.
(`VW_OCITEM_ADJ_copy1` is another `_copy1` artefact, like `ICITEM1`/`ICITEM2`.)
I did **not** read their definitions — see §8.

### 5.2 Primary keys for the priority tables

All from `sys.indexes` + `sys.index_columns`, never guessed:

| Table | Primary key | Notable secondary keys |
|---|---|---|
| `APVEN` | `VENDORID` | `SHORTNAME`; `IDGRP` |
| `APIBC` | `CNTBTCH` | UNIQUE `(BTCHSTTS, CNTBTCH)` |
| `APIBH` | `(CNTBTCH, CNTITEM)` | `(ORIGCOMP, IDVEND, IDINVC)`; `(ORIGCOMP, IDVEND, DATEINVC)` |
| `APIBD` | `(CNTBTCH, CNTITEM, CNTLINE)` | none — this is the only index on the table |
| `APIBS` | `(CNTBTCH, CNTITEM, CNTPAYM)` | |
| `APOBL` | `(IDVEND, IDINVC)` | UNIQUE `(SWPAID, IDORDERNBR, IDVEND, IDINVC)`; `(SWPAID, IDPONBR, …)`; `(DATEINVCDU, …)`; `(IDVEND, DATEINVC)`; `(SWRTGOUT, …)` |
| `APOBS` | `(IDVEND, IDINVC, CNTPAYM)` | 11 more |
| `APOBP` | `(IDVEND, IDINVC, CNTPAYMNBR, IDRMIT, DATEBUS, TRANSTYPE, CNTSEQNCE)` | `(IDBANK, CNTBTCH, CNTITEM, …)` |
| `APOBLJ` | `(IDVEND, IDINVC, CNTLINE)` | none |
| `APPJH` | `(TYPEBTCH, POSTSEQNCE, CNTBTCH, CNTITEM)` | |
| `APPJD` | `(TYPEBTCH, POSTSEQNCE, CNTBTCH, CNTITEM, CNTSEQENCE)` | `(TYPEBTCH, POSTSEQNCE, FISCYR, FISCPER, IDACCT, CODECURN, SRCETYPE, IDINVC)` |
| `APBTA` | `(PAYMTYPE, CNTBTCH)` | UNIQUE `(PAYMTYPE, BATCHSTAT, CNTBTCH)` |
| `APTCR` | `(BTCHTYPE, CNTBTCH, CNTENTR)` | |
| `APTCP` | `(BATCHTYPE, CNTBTCH, CNTRMIT, CNTLINE)` | `(IDVEND, IDINVC, CNTPAYM)`; UNIQUE `(BATCHTYPE, CNTBTCH, CNTRMIT, IDINVC, CNTPAYM)` |
| `APTCN` | `(BATCHTYPE, CNTBTCH, CNTRMIT, CNTLINE)` | |
| `APTRK` | `(IDBANK, CNTBTCH, CNTENTRY, CNTLINE, CNTSEQ)` | `(IDVEND, IDINVC, CNTPAYM, IDRMIT, LONGSERIAL, TRXTYPE, CNTSEQOBP)` |
| `POPORH1` / `POPORH2` | `PORHSEQ` | UNIQUE `PONUMBER`; UNIQUE `(VDCODE, PORHSEQ)`; UNIQUE `(VDCODE, PONUMBER)` |
| `POPORL` | `(PORHSEQ, PORLREV)` | UNIQUE `(PORHSEQ, PORLSEQ)`; UNIQUE `(ITEMNO, EXPARRIVAL, PORHSEQ, PORLSEQ)` |
| `PORCPH1` / `PORCPH2` | `RCPHSEQ` | UNIQUE `RCPNUMBER`; UNIQUE `(PONUMBER, RCPNUMBER)`; `PORHSEQ` |
| `PORCPL` | `(RCPHSEQ, RCPLREV)` | UNIQUE `(RCPHSEQ, RCPLSEQ)`; UNIQUE `(PORHSEQ, PORLSEQ, RCPHSEQ, RCPLSEQ)` |
| `POINVH1` / `POINVH2` | `INVHSEQ` | `INVNUMBER`; `RCPNUMBER`; `RETNUMBER`; `RCPHSEQ`; UNIQUE `(VDCODE, INVHSEQ)` |
| `POINVL` | `(INVHSEQ, INVLREV)` | UNIQUE `(INVHSEQ, INVLSEQ)`; `RCPLSEQ` |
| `POPORAH` / `POPORAL` | `(DAYENDSEQ, PORAHSEQ)` / `(DAYENDSEQ, PORAHSEQ, PORALSEQ)` | |
| `POPOALO` | `(DAYENDSEQ, PORAHSEQ, PORALSEQ, OPTFIELD)` | UNIQUE `(OPTFIELD, DAYENDSEQ, PORAHSEQ, PORALSEQ)` |
| `ICITEM` | `ITEMNO` | **UNIQUE `FMTITEMNO`**; UNIQUE `(CATEGORY, ITEMNO)`; UNIQUE `(DESC, ITEMNO)` |
| `ICITEM1` / `ICITEM2` | `ITEMNO` (auto-named `PK__ICITEM_c__…`) | `…_copy1` / `…_copy2` index names |
| `ICITEMO` | `(ITEMNO, OPTFIELD)` | UNIQUE `(OPTFIELD, ITEMNO)` |
| `ICILOC` | `(ITEMNO, LOCATION)` | UNIQUE `(LOCATION, ITEMNO)` |
| `ICUNIT` | `(ITEMNO, UNIT)` | UNIQUE `(UNIT, ITEMNO)` |
| `ICPRIC` | `(CURRENCY, ITEMNO, PRICELIST)` | |
| `GLAMF` | `ACCTID` | `ACCTFMTTD` (KEY_12, **non-unique**); 10 segment indexes |
| `GLJEH` | `(BATCHID, BTCHENTRY)` | |
| `GLJED` | `(BATCHNBR, JOURNALID, TRANSNBR)` | |
| `GLPOST` | `(ACCTID, FISCALYR, FISCALPERD, SRCECURN, SRCELEDGER, SRCETYPE, POSTINGSEQ, CNTDETAIL)` | `(JRNLDATE, POSTINGSEQ, BATCHNBR, ENTRYNBR, TRANSNBR)` |
| `GLPJD` | `(POSTINGSEQ, BATCHNBR, ENTRYNBR, TRANSNBR)` | `ACCTID` |
| `TXAUDH` | `SEQUENCE` | UNIQUE `(AUTHORITY, TYPE, FISCYEAR, FISCPERIOD, CUSTVEND, DOCDATE, SRCEAPP, DOCTYPE, DOCNUMBER, SEQUENCE)` |
| `TXAUDD` | `(SEQUENCE, ITEMCLASS)` | |
| `BKTRANH` | `(BANK, SERIAL)` | UNIQUE `(BANK, FSCYEAR, FSCPERIOD, SERIAL)`; UNIQUE `(BANK, TRANSNUM, SERIAL)` |
| `BKTRAND` | `(BANK, SERIAL, LINE)` | 7 more, incl. `(BANK, SRCEAPP, PAYORID, IDREMIT, SERIAL, LINE)` |

### 5.3 Date and time encoding — proven

Dates are **`decimal(9,0)` integers in `YYYYMMDD`**, not a date type:
```sql
SELECT TOP 3 DATEINVC, CONVERT(date, CAST(DATEINVC AS char(8)), 112) AS as_date,
       DATEINVCDU, CONVERT(date, CAST(DATEINVCDU AS char(8)),112) AS due_as_date, AUDTDATE, AUDTTIME
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430 AND DATEINVCDU>0;
-- 20260302 -> 2026-03-02 ; 20260423 -> 2026-04-23 ; 20260102 -> 2026-01-02, due 20260201 -> 2026-02-01
```
**Conversion (T-SQL)**: `CONVERT(date, CAST(<col> AS char(8)), 112)`.
**Conversion (Python)**: `datetime.strptime(str(int(v)), "%Y%m%d").date()`.
**Sentinel**: `0` means "no date" (`DATEDISC = 0` on documents with no discount). Guard for it.

Range sanity across all 787,465 `APOBL` rows: min `20150409`, max `20260930`; **0 rows**
outside `19900101…20301231`; **0 rows** null or zero on `DATEINVC`. Dates are clean.

Times are `decimal(9,0)` in **`HHMMSSCC`** (hours, minutes, seconds, centiseconds), stored
without leading zeros: `AUDTTIME 12113964` = 12:11:39.64; `AUDTTIME 6381806` = 06:38:18.06.

### 5.4 Fiscal calendar — and the window straddles a year end

`CSCOM.PERDFSC = 12`, `QTR4PERD = 4`. `CSFSC` holds one row per fiscal year with
`BGNDATE1..13` / `ENDDATE1..13`:
```sql
SELECT FSCYEAR, PERIODS, ACTIVE, BGNDATE1, ENDDATE12, STATUSADJ, STATUSCLS FROM CSFSC ORDER BY FSCYEAR;
```
**The fiscal year runs April–March and is labelled by its ENDING year** (consistent with
Agent 2's finding). Years present: 2016 … 2027, all `ACTIVE = 1`.

| `FSCYEAR` | `BGNDATE1` | `ENDDATE12` |
|---|---|---|
| `2026` | 2025-04-01 | 2026-03-31 |
| `2027` | 2026-04-01 | 2027-03-31 |

Period 13 is the adjustment period (`BGNDATE13`/`ENDDATE13` are pinned at `20160301`/`20160331`
in every year, which looks like a data-entry artefact but is harmless — period 13 is
addressed by number, not date).

**Where the Jan–Apr 2026 migration window falls — this is the important bit:**

| Calendar month | Fiscal year | Period | `APOBL` docs |
|---|---|---|---|
| **Jan 2026** | **2026** | **10** | 10,499 (+5 posted to P11) |
| **Feb 2026** | **2026** | **11** | 8,121 (+1 to P10, +7 to P12) |
| **Mar 2026** | **2026** | **12** | 11,478 (+2 to FY2027 P03) |
| **Apr 2026** | **2027** | **01** | 7,919 (+1 to FY2027 P02) |
| | | **Total** | **38,033** |

```sql
SELECT DATEINVC/100 AS yyyymmdd_over_100, RTRIM(FISCYR) fy, RTRIM(FISCPER) fp, COUNT(*) n
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430
 GROUP BY DATEINVC/100, FISCYR, FISCPER ORDER BY 1,2,3;
```
**The window crosses the fiscal year boundary at 1 April 2026.** Anything that groups,
reconciles or reports by fiscal year will split this population 3:1 across FY2026 and
FY2027. `extract.sql` selects `f.FISCYR` and annotates it "reference only, never filtered
on", which is the right call — but any downstream consumer that *does* group by fiscal
year needs to know the window is not one year.

**16 of the 38,033 window documents are posted to a period that does not match their
invoice date** (back-dated or forward-dated posting): 5 dated in January posted to P11,
1 dated in February posted to P10, 7 dated in February posted to P12, 2 dated in March
posted to **FY2027 P03**, and 1 dated 2026-04-27 posted to FY2027 P02. Small, but it means
`DATEINVC` and `(FISCYR, FISCPER)` are not interchangeable as a period filter. See DQ-10.

`CSFSCST` carries per-module period-open status (`STATUS1..13`) for `AP, AR, BK, GL, IC,
OE, PO, ZC`. As of the audit, FY2027 has periods 1–3 **closed** and 4–13 open for every
module — i.e. April–June 2026 are already locked. FY2026 GL shows `STATUS1=1`,
`STATUS12=1`, `STATUS13=1`.

### 5.5 Multi-currency — the company genuinely uses it

`CSCOM`: `HOMECUR = 'INR'`, `REPORTCUR = 'INR'`, **`MULTICURSW = 1`**, `RATETYPE = 'AV'`,
`EUROCURSW = 0`, `WARNDAYS = 30`.

Where currency and rate live:

| Level | Currency column | Rate column | Amount pair |
|---|---|---|---|
| AP document header | `APOBL.CODECURN` / `APIBH.CODECURN` | `EXCHRATEHC` `decimal(15,7)`, `IDRATETYPE`, `RATEDATE`, `RATEOP`, `SWRATEOVRD` | `AMTINVCTC` (transaction) / `AMTINVCHC` (home) |
| AP tax-reporting overlay | `CODECURNRC` | `RATERC`, `RATETYPERC`, `RATEDATERC` | `TXAMT1..5RC`, `TXTOTRC` |
| AP posting journal | `APPJD.CODECURN` | `RATEEXCHHC` | `AMTEXTNDHC` / `AMTEXTNDTC` |
| AP payment | `APOBP.CODECURN` | `RATEEXCHHC` | `AMTPAYMHC` / `AMTPAYMTC` |
| PO document | `POINVH1.CURRENCY` | `RATE`, `SPREAD`, `RATETYPE`, `RATEDATE`, `RATEOPER`, `RATEOVER` | `EXTENDED` / `FCEXTENDED` |
| GL posted line | `GLPOST.SRCECURN`, `SCURNCODE`, `HCURNCODE` | `CONVRATE`, `RATESPREAD`, `RATETYPE`, `RATEDATE` | `SCURNAMT` (source) / `TRANSAMT` (home) / `RPTAMT` |
| Rate tables | `CSCRD` (1,289 rows: `HOMECUR, RATETYPE, SOURCECUR, RATEDATE → RATE, SPREAD`), `CSCRH` (6 rate headers), `CSCRT` (4 rate types) | | |

**Yes, this company really is multi-currency.** `APOBL` by currency:
```sql
SELECT RTRIM(CODECURN) cur, COUNT(*) n, CAST(SUM(AMTINVCHC) AS decimal(20,0)) hc,
       CAST(SUM(AMTINVCTC) AS decimal(20,0)) tc FROM APOBL GROUP BY CODECURN ORDER BY n DESC;
```
| Currency | All-time docs | Jan–Apr 2026 window |
|---|---|---|
| INR | 754,948 | 35,661 |
| USD | 28,892 | 2,162 |
| RMB | 1,651 | 119 |
| CNY | 1,389 | 82 |
| EUR | 508 | 5 |
| GBP | 48 | 3 |
| YEN | 19 | — |
| SEK | 3 | — |
| HKD | 3 | — |
| CHF | 2 | — |
| AED | 2 | 1 |
| **Total non-INR** | **32,517 (4.1%)** | **2,372 (6.2%)** |

`APVEN` by currency: INR 4,028 / USD 641 / EUR 37 / GBP 16 / CNY 10 / HKD 8 / RMB 5 / YEN 3 (+3).
`ICPRIC` carries prices in 7 currencies. `TXAUTH` has a dedicated `NOTAX<CCY>`
import-exemption authority for nine currencies.

**`RMB` and `CNY` are two codes for the same currency** and both are live (1,651 + 1,389
documents; 119 + 82 in the window), with separate `TXAUTH` entries (`NOTAXRMB`,
`NOTAXCNY`), separate `TXGRP`s and separate `ICPRIC` price rows. See DQ-8.

`extract.sql` restricts to `RTRIM(CODECURN)='INR'`; on the raw type-12 AP-direct window
that excludes **43** documents (the file says 39, which is the count after the other
filters interact — both are right, they are measuring different points in the pipeline).

### 5.6 Which company does `IDEDAT` hold?

**One company, and the database *is* the company.** `CSCOM` has exactly **one row**:

```sql
SELECT ORGID, CONAME, CITY, STATE, POSTAL, COUNTRY, PERDFSC, HOMECUR, MULTICURSW,
       RATETYPE, REPORTCUR, TAXNBR, LEGALNAME, BRN FROM CSCOM;
```

| Field | Value |
|---|---|
| `ORGID` | `IDEDAT` |
| `CONAME` | **INDIAN DESIGNS EXPORTS PVT LTD** |
| `CITY` / `STATE` / `POSTAL` / `COUNTRY` | Bangalore / Karnataka / 560045 / India |
| `TAXNBR` (GSTIN) | `29XXXCX0000X0ZX` *(masked; state code `29` = Karnataka and 6th char `C` = Company preserved)* |
| `LEGALNAME` | holds the **PAN**, `XXXCX0000X` *(masked; 4th char `C` = Company preserved)* |
| `BRN` | the **CIN**, `U01810KA1995PTC018845` — sector 01810, state KA, incorporated 1995, PTC = private limited |
| `ADDR01` | `LUTNo.: AD290426001291E, Date:01/04/2026` — an **LUT (Letter of Undertaking)** number, i.e. a registered zero-rated exporter |
| `ADDR02` | `IEC No: 0795014422` — Importer-Exporter Code |
| `ADDR03` | `TIN : 29850379251` — the legacy pre-GST TIN, still parked in an address line |
| `ADDR04` | `29` — the state code, also parked in an address line |
| `HOMECUR` / `REPORTCUR` / `MULTICURSW` / `RATETYPE` | INR / INR / 1 / `AV` |

Corroboration that there is only one org: `SELECT DISTINCT AUDTORG FROM APOBL` returns
the single value `IDEDAT`, and every `CSAPP` row has `AUDTORG = 'IDEDAT'`. There is **no
multi-company structure inside this database** — in Sage 300 each company is its own
database, and `IDEDAT` is one of them. (A `ZC` consolidation module is installed and
`GLSRCE` has a `ZC/CO = G/L Consolidation` entry, so this company may be consolidated
*into* something elsewhere, but that target is not in this database.)

Note the `-IDEPL-` suffix `extract.sql` mentions and the `IDEPL` price list name: `IDEPL`
appears to be an internal abbreviation for the same entity. The GL `ABRKID = 'UNIT'`
accounts and the 22 two-digit account suffixes are its **manufacturing units**, and
`ICILOC` has 750 stock locations. This is one legal entity operating many plants, not many
companies.

---

## 6. Data-quality findings

Report only — nothing here was fixed, and nothing should be fixed in Sage. Scope is
AP / PO / IC / GL / TX / BK. Every finding carries a count and an example.

### DQ-1 — 1,079 window bills are dropped for "no distribution lines", but they DO have lines (in `APOBLJ`)

**Severity: HIGH — this is the most consequential finding in the audit.**

`extract.sql` filters out AP-direct bills that have no `APIBD` rows, annotating
*"a bill with no distribution lines cannot be shaped; bill create rejects an empty line
list. 1,071 bills fail this."* Both the mechanism and the count are reproduced exactly:

```sql
WITH b AS (SELECT * FROM APOBL WHERE IDTRXTYPE=12 AND SRCEAPPL='AP'
                                 AND DATEINVC BETWEEN 20260101 AND 20260430),
     f AS (SELECT b.*,
       CASE WHEN EXISTS(SELECT 1 FROM APIBD d WHERE d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM)
            THEN 1 ELSE 0 END has_apibd,
       CASE WHEN EXISTS(SELECT 1 FROM APIBD d WHERE d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM
              AND LEFT(RTRIM(d.IDGLACCT),2)<>'4E' AND LEFT(RTRIM(d.IDGLACCT),4)<>'2A7T'
              AND LEFT(RTRIM(d.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))
            THEN 1 ELSE 0 END impure FROM b)
SELECT COUNT(*) raw, SUM(1-has_apibd) fails_apibd_exists,
  SUM(CASE WHEN RTRIM(CODECURN)<>'INR' THEN 1 ELSE 0 END) non_inr,
  SUM(CASE WHEN RTRIM(CODETAXGRP) IN ('VAT','NRVAT','NRST','NRVATST') THEN 1 ELSE 0 END) legacy_taxgrp,
  SUM(CASE WHEN has_apibd=1 AND RTRIM(CODECURN)='INR'
            AND RTRIM(CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')
            AND impure=0 THEN 1 ELSE 0 END) final_headers
FROM f;
-- raw 12,781 | fails_apibd_exists 1,079 | non_inr 43 | legacy_taxgrp 126 | final_headers 11,256
```
`final_headers = 11,256` matches `extract.sql`'s stated expectation **exactly**, which
validates my reading of its logic. After the currency/tax-group filters the no-`APIBD`
count is **1,071** — also exactly as the file states.

**But the stated reason is wrong. Every one of those documents has distribution lines —
in `APOBLJ`, the posted-side line table (§3.1):**

```sql
SELECT COUNT(*) apobl_docs,
       SUM(CASE WHEN j.c>0 THEN 1 ELSE 0 END) with_apoblj_lines,
       SUM(CASE WHEN d.c>0 THEN 1 ELSE 0 END) with_apibd_lines
  FROM APOBL o
  OUTER APPLY (SELECT COUNT(*) c FROM APOBLJ x WHERE x.IDVEND=o.IDVEND AND x.IDINVC=o.IDINVC) j
  OUTER APPLY (SELECT COUNT(*) c FROM APIBD  y WHERE y.CNTBTCH=o.CNTBTCH AND y.CNTITEM=o.CNTITEM) d
 WHERE o.IDTRXTYPE=12 AND o.SRCEAPPL='AP' AND o.DATEINVC BETWEEN 20260101 AND 20260430;
-- apobl_docs 12,781 | with_apoblj_lines 12,781 (100.0%) | with_apibd_lines 11,702 (91.6%)
```
```sql
WITH miss AS (SELECT o.* FROM APOBL o
   WHERE o.IDTRXTYPE=12 AND o.SRCEAPPL='AP' AND o.DATEINVC BETWEEN 20260101 AND 20260430
     AND NOT EXISTS(SELECT 1 FROM APIBD d WHERE d.CNTBTCH=o.CNTBTCH AND d.CNTITEM=o.CNTITEM))
SELECT COUNT(*) docs, SUM(CASE WHEN j.c>0 THEN 1 ELSE 0 END) with_apoblj,
       SUM(j.c) total_apoblj_lines, CAST(SUM(m.AMTINVCHC) AS decimal(18,0)) gross_at_risk
  FROM miss m OUTER APPLY (SELECT COUNT(*) c FROM APOBLJ x WHERE x.IDVEND=m.IDVEND AND x.IDINVC=m.IDINVC) j;
-- docs 1,079 | with_apoblj 1,079 (100%) | total_apoblj_lines 1,209 | gross_at_risk 295,107,560
```

**Why they have no `APIBD` row:** all 1,079 are `TYPEBTCH = 'PY'` — they were created by a
*payment* batch, not an invoice batch, so `APIBC/APIBH/APIBD` never held them. That is a
structural property, not missing data.

**How much is actually recoverable.** Applying `extract.sql`'s own purity filter
(`4E*` / `2A7T*` / `1L8TX14|15|16`) to the `APOBLJ` lines instead of the `APIBD` lines:

```sql
WITH miss AS (SELECT o.IDVEND,o.IDINVC,o.AMTINVCHC FROM APOBL o
   WHERE o.IDTRXTYPE=12 AND o.SRCEAPPL='AP' AND o.DATEINVC BETWEEN 20260101 AND 20260430
     AND RTRIM(o.CODECURN)='INR' AND RTRIM(o.CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')
     AND NOT EXISTS(SELECT 1 FROM APIBD d WHERE d.CNTBTCH=o.CNTBTCH AND d.CNTITEM=o.CNTITEM)),
  flagged AS (SELECT m.*, CASE WHEN EXISTS(SELECT 1 FROM APOBLJ j
        WHERE j.IDVEND=m.IDVEND AND j.IDINVC=m.IDINVC
          AND LEFT(RTRIM(j.IDGLACCT),2)<>'4E' AND LEFT(RTRIM(j.IDGLACCT),4)<>'2A7T'
          AND LEFT(RTRIM(j.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))
     THEN 0 ELSE 1 END AS pure FROM miss m)
SELECT COUNT(*) docs, SUM(pure) would_pass_purity,
       CAST(SUM(CASE WHEN pure=1 THEN AMTINVCHC ELSE 0 END) AS decimal(18,0)) value_recoverable
  FROM flagged;
-- docs 1,071 | would_pass_purity 591 | value_recoverable 22,918,849
```

**591 documents worth ₹22,918,849 (~₹2.29 crore) are excluded for a reason that does not
hold**, and are recoverable by sourcing lines from `APOBLJ`. The remaining 480 legitimately
fail the purity filter (they post to `1L9OL*`, `2A7S*`, `2A6B*`, `1L9E*` etc. — balance-sheet
heads, i.e. genuinely "journals wearing an invoice's clothes"). GL-prefix profile of the
1,079: `1L9O` 412 lines / 357 docs · `4E5O` 341 / 312 · `4E3E` 228 / 166 · `4E2M` 127 / 112 ·
`2A7S` 45 / 44 · `1L9E` 20 / 17 · `4E1M` 11 / 10 · `1L8T` 7 / 5 · `4E6F` 5 / 4 · `2A6B` 4 / 4 ·
`2A7T` 4 / 2 · `2A1F` 3 / 3 · `2A3L` 1 / 1 · `4E4S` 1 / 1.

**Two caveats before anyone acts on this**, because switching line source is not free:

1. **`APOBLJ` has no line description.** `APIBD.TEXTDESC` — which `extract.sql` maps to the
   bill line's `description` — has no counterpart in `APOBLJ`. Recovered bills would have
   no line narrative.
2. **The amount semantics differ.** `APIBD.AMTDIST` is **pre-tax**; `APOBLJ.AMTINVCHC` is
   **gross**. Measured on the 12,781 window documents:

   | Line source | Sums to header **gross** | Sums to header **gross − tax** | No lines |
   |---|---|---|---|
   | `APOBLJ.AMTINVCHC` | **12,781 / 12,781** | 3,023 | 0 |
   | `APIBD.AMTDIST` | 1,903 | **11,659 / 11,702** | 1,079 |

   A naive swap would inflate every line by its tax. This needs a deliberate transform,
   not a substitution. **Reporting only — Agent 5 owns whether and how to change the script.**

### DQ-2 — 15 orphan `APIBD` rows

```sql
SELECT COUNT(*) FROM APIBD d LEFT JOIN APIBH h
  ON h.CNTBTCH=d.CNTBTCH AND h.CNTITEM=d.CNTITEM WHERE h.CNTBTCH IS NULL;   -- 15
```
15 distribution lines out of 1,270,496 (0.001%) whose batch entry no longer exists. Because
`extract.sql` drives from `APOBL` and joins *to* `APIBD`, these are invisible to it — they
cannot pull a phantom line into a bill. Low impact; recorded for completeness.
By contrast `APIBH → APIBC` and `APOBLJ`/`APOBS → APOBL` all have **zero** orphans.

### DQ-3 — `ICITEM1` / `ICITEM2`: two-year-old copies of the item master in the live database

**Severity: MEDIUM (latent).** Detailed in §3.3. `ICITEM1` (663,858 rows, frozen
2024-05-17) and `ICITEM2` (691,769 rows, frozen 2024-06-20) sit alongside the live
`ICITEM` (1,196,108 rows, current to 2026-07-17). `ICITEM2` contains **996 items that no
longer exist** in the live master. Auto-named PKs (`PK__ICITEM_c__…`) and `_copy1`/`_copy2`
index names confirm they were created by a copy operation, not by Sage. Nothing in the
migration currently reads them, but the names sort adjacent to `ICITEM` and any
prefix-glob or fuzzy table pick would silently take 2024 data. The same pattern appears in
the view list (`VW_OCITEM_ADJ_copy1`).

### DQ-4 — 98.9% of PO item references "fail" against `ICITEM` if you join the wrong column

**Severity: HIGH (as a trap, not as a defect in the data).** Detailed in §4.1.

```sql
WITH l AS (SELECT DISTINCT RTRIM(l.ITEMNO) i FROM POINVL l JOIN POINVH1 h ON h.INVHSEQ=l.INVHSEQ
            WHERE h.DATE BETWEEN 20260101 AND 20260430),
     f AS (SELECT l.i,
        CASE WHEN EXISTS(SELECT 1 FROM ICITEM t WHERE RTRIM(t.ITEMNO)=l.i)    THEN 1 ELSE 0 END m_itemno,
        CASE WHEN EXISTS(SELECT 1 FROM ICITEM t WHERE RTRIM(t.FMTITEMNO)=l.i) THEN 1 ELSE 0 END m_fmt FROM l)
SELECT COUNT(*) distinct_po_items, SUM(m_itemno) match_ICITEM_ITEMNO, SUM(m_fmt) match_ICITEM_FMTITEMNO FROM f;
-- distinct_po_items 17,129 | match_ICITEM_ITEMNO 195 | match_ICITEM_FMTITEMNO 17,129
```
The *data* is complete — 100% of PO invoice items resolve. But joined on `ICITEM.ITEMNO`
the same query reports 16,934 "missing items", which would read as a catastrophic
item-master gap. Same for `POPORL`: 223/17,365 vs 17,365/17,365. Example:
`POINVL.ITEMNO 'ID41376A-LB10'` → `ICITEM.FMTITEMNO 'ID41376A-LB10'`,
`ICITEM.ITEMNO 'ID41376ALB10'`, `DESC 'WASH CARE LABEL HMINC98477 EN/…'`.

### DQ-5 — `TXAUDD.TAXRATE` is polluted with back-computed rates; it is not a rate source

**Severity: MEDIUM.** The project's stated rule is *"Read the tax rate Sage states; never
divide to infer one."* `TXAUDD.TAXRATE` looks like the place to read it, and is not:

```sql
SELECT d.TAXRATE, COUNT(*) n FROM TXAUDD d JOIN TXAUDH h ON h.SEQUENCE=d.SEQUENCE
 WHERE h.FISCYEAR='2026' GROUP BY d.TAXRATE ORDER BY n DESC;
```
| `TAXRATE` | Rows |
|---|---|
| 0.00000 | 45,910 |
| 9.00000 | 26,429 |
| 18.00000 | 16,195 |
| 5.00000 | 14,407 |
| 2.50000 | 13,372 |
| 6.00000 | 7,682 |
| 12.00000 | 2,430 |
| **5.00001** | **704** |
| **2.50001** | **609** |
| **4.99999** | **451** |
| **2.49999** | **374** |
| **9.00001** | **358** |
| **2.50002** | **350** |
| **9.00002** | **342** |
| **5.00002** | **336** |
| **2.50003** | **286** |
| … | **2,872 further distinct rate values** |

Clean slabs dominate, but there is a long tail of **2,872 additional distinct rate values**
that are clearly rounding artefacts of `tax ÷ base` rather than stated rates. Any code
that reads `TXAUDD.TAXRATE` and validates it against the legal GST slabs will reject
thousands of otherwise-valid rows.

**The authoritative sources are `TXRATE` (master, §3.5) and the per-line stated-rate
columns** `APIBD.RATETAX1..5`, `APOBLJ.TXRATE1..5`, `POINVL.TAXRATE1..5`,
`POPORL.TAXRATE1..5`, `PORCPL.TAXRATE1..5`.

### DQ-6 — There is no key-based join from AP to the General Ledger

**Severity: MEDIUM (limits verification, not the migration itself).** Detailed in §4.2.
`APPJD.GLBATCH` and `APPJD.GLENTRY` are empty on **539,153 of 539,153** FY2026 rows, and
`GLJEH.CUSTVEND` / `GLJEH.DOCNUMBER` are blank on **all 155,049** FY2026 AP-sourced
entries (and on all AR/PO/IC/OE/BK entries too). The only tie is the text in
`GLPOST.JNLDTLDESC` (`'Invoice-' + document number`, truncated to the column width).
Anyone building a GL-level reconciliation should know this before promising one.

### DQ-7 — 78% of the item master has no usable HSN code

**Severity: HIGH for the goods population.**
```sql
SELECT SUM(CASE WHEN RTRIM(VALUE)='' THEN 1 ELSE 0 END) blank,
       SUM(CASE WHEN LEN(RTRIM(VALUE)) NOT IN (4,6,8) AND RTRIM(VALUE)<>'' THEN 1 ELSE 0 END) bad_length,
       SUM(CASE WHEN RTRIM(VALUE) LIKE '%[^0-9]%' AND RTRIM(VALUE)<>'' THEN 1 ELSE 0 END) non_numeric,
       COUNT(*) total
  FROM ICITEMO WHERE RTRIM(OPTFIELD)='HSNCODE';
-- blank 712,905 | bad_length 4,494 | non_numeric 1,250 | total 971,675
```
| Measure | Count |
|---|---|
| Items in `ICITEM` | 1,196,108 |
| Items with an `HSNCODE` row in `ICITEMO` | 971,675 (81.2%) |
| …of which the value is **blank** | **712,905** |
| Items with **no `HSNCODE` row at all** | **224,433** |
| **Items with a non-blank HSN** | **258,770 (21.6%)** |
| Non-blank but **not 4/6/8 digits** (invalid HSN length) | 4,494 |
| Non-blank but **containing a non-digit** | 1,250 |

Malformed examples: `CHGDYE → ' 5407610000'` (leading space + 10 digits),
`ID13216XMNL1 → '5807120'` (7 digits), `ID13274YTHR1001 → '555081000'` (9 digits),
and the `MAX()` of the whole column is the string **`'yx2016'`**.

For the **migration window specifically** the picture is much better — 15,115 of 17,129
window PO-invoice items (88.2%) have a usable HSN, 2,014 (11.8%) do not (§3.3). Those
2,014 are the population that gets the `9999` placeholder `run_all.sh` warns about.

### DQ-8 — Master-data hygiene: blanks, duplicates and a second code for the same currency

| # | Finding | Count | Example |
|---|---|---|---|
| a | `APOBL` window documents with a **blank `CODETAXGRP`** | **5,003** of 38,033 (13.2%) | `SELECT SUM(CASE WHEN RTRIM(CODETAXGRP)='' THEN 1 ELSE 0 END) FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430` |
| b | **`RMB` and `CNY` are both live codes for the Chinese yuan** | 1,651 + 1,389 docs (119 + 82 in window) | separate `TXAUTH` (`NOTAXRMB`, `NOTAXCNY`), separate `TXGRP`, separate `ICPRIC` rows. Two exchange-rate series for one currency. |
| c | `TXGRP` entry `INTERSTATE1` with **blank description and no authorities** | 1 | alongside the real `INTERSTATE` |
| d | `GLAMF` account with prefix `31`, **blank description** | 1 of 1,610 | breaks the otherwise-clean `1L`/`2A`/`3I`/`4E` prefix map |
| e | `APVEN.BRN` **blank** (no GSTIN — Agent 2 established `BRN` is where the GSTIN lives) | **1,256** of 4,752 (26.4%) | |
| f | `APVEN.BRN` present but **not 15 characters** (not GSTIN-shaped) | **819** of 4,752 | free text `'TIN - …'` (17–18 chars), and 14-char values like `'06AABC…'` |
| g | `APVEN.BRN` GSTIN-shaped (15 chars) | 2,677 (56.3%) | |
| h | `ICITEM` rows with a **blank `DESC`** | 9 of 1,196,108 | |
| i | `ICILOC` rows whose item is absent from `ICITEM` | 58 of 2,117,805 | |
| j | `ICUNIT` rows whose item is absent from `ICITEM` | 27 of 1,396,595 | |
| k | `ICUNIT.CONVERSION` implausible maxima | `CONES` max **5,000,000,000**; `PKT` max 50,000; `ROLLS` max 25,000 | a 5-billion-unit conversion factor cannot be right |
| l | `APVEN.CODESTTE` is **free text, not a state code** | 2,035 of 4,752 blank; `'KARNATAKA'` 1,723 vs `'Tamil Nadu'` 294 vs `'TAMILNADU'` 72 — same state, three spellings | unusable as-is for GST place-of-supply; `extract.sql` already annotates this |
| m | `APVEN` vendors **inactive** but still transacting in the window | **28** of the 669 AP-direct window vendors (`SWACTV=0`); 0 on hold | |

### DQ-9 — Invoice-number whitespace: real, small, and `extract.sql` handles it correctly

`extract.sql` warns *"RTRIM only. Never LTRIM: Sage holds ' WPL/25-26/07516' and
'WPL/25-26/07516' as two separate obligations."* Measured on the window:

```sql
SELECT SUM(CASE WHEN IDINVC<>LTRIM(IDINVC) THEN 1 ELSE 0 END) leading_ws
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430;                    -- 8
SELECT COUNT(*) collisions FROM (SELECT RTRIM(IDVEND) v, LTRIM(RTRIM(IDINVC)) i, COUNT(*) c
  FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430
 GROUP BY RTRIM(IDVEND), LTRIM(RTRIM(IDINVC)) HAVING COUNT(*)>1) x;           -- 2
```
- **8** window documents carry a leading space.
- **2** `(vendor, LTRIM(invoice))` groups collapse two distinct Sage documents into one —
  so `LTRIM` would lose 2 documents. The warning is justified; the "154 documents" in the
  comment counts trailing `CHAR` padding as well, which is not meaningful (778,031 of
  787,465 rows have it, purely from fixed-width storage).
- **0** window documents contain CR, LF or TAB in `IDINVC`.
- **0 case-collisions** across the entire `APOBL` table:
  `SELECT COUNT(*) FROM (SELECT RTRIM(IDVEND) v, UPPER(RTRIM(IDINVC)) i, COUNT(*) c FROM APOBL
   GROUP BY RTRIM(IDVEND), UPPER(RTRIM(IDINVC)) HAVING COUNT(*)>1) x` → **0**. This matters
  because the MySQL mirror stores document numbers under a **case-insensitive collation**
  (`utf8mb4_unicode_ci`, §7) — that would have been a silent row-loss channel, and happens
  not to be one on this data.

### DQ-10 — Everything I checked that came back clean

Recording the negatives so nobody re-runs them:

| Check | Result |
|---|---|
| `APOBL.IDVEND` with no `APVEN` row (all 787,465 rows) | **0** |
| `APIBH` with no `APIBC` batch | **0** |
| `APOBLJ` with no `APOBL` header | **0** |
| `APOBS` with no `APOBL` header | **0** |
| `APOBL` window rows with blank `IDVEND` or blank `IDINVC` | **0** / **0** |
| `APOBL` `DATEINVC` null or zero | **0** |
| `APOBL` `DATEINVC` outside 1990–2030 | **0** (min 20150409, max 20260930) |
| `APIBD` window lines with blank `IDGLACCT` | **0** of 33,719 |
| `APIBD` window accounts absent from `GLAMF` (either form) | **0** of 313 distinct |
| `APOBL` window type-12 documents with a **negative** amount | **0** of 30,827 |
| `APOBL` window type-12 documents with a **zero** amount | **3** of 30,827 |
| `POINVH1` window headers with no `POINVL` lines | **0** of 18,047 |
| `POINVL` window lines with negative qty or negative/zero unit cost | 0 neg qty, 0 neg cost, 0 zero cost; **11 zero-qty** of 32,586 |
| `APOBL` window documents posted to a period inconsistent with `DATEINVC` | **16** of 38,033 (§5.4) |
| Implausible exchange rates (`>200` or `<0.01`) across all `APOBL` | **1** — vendor `ACCU026`, doc `PP031976`, USD, `EXCHRATEHC = 730375.0000000`, dated **2021-01-29**, TC −63.80 → HC **−46,597,925.00**. A ₹4.66 crore error from a fat-fingered rate. **Outside the migration window** (2021), so it does not affect this load, but it is a real defect in Sage's history and someone should know. |

---

## 7. Mirror (`idedat_staging`) vs live Sage

### 7.1 What the mirror actually is

The staging mirror on the devbox holds 21 tables. The important structural finding:

**`sage_bill_hdr` is NOT the AP-direct population — it is the PO-matched (goods)
population, sourced from `POINVH1`.** Its primary key is `invhseq`, which is
`POINVH1.INVHSEQ`:

```sql
-- MySQL
SHOW CREATE TABLE sage_bill_hdr;
--   `invhseq` varchar(24) NOT NULL, `porhseq` varchar(24), `po_number` varchar(64), …
--   PRIMARY KEY (`invhseq`), KEY ix_bill_logical (vendor_code, inv_number_base)
```

Anyone reading the name "bill header" and assuming it holds the 11,256 AP-direct bills
will be wrong by construction. The two populations are disjoint (`SRCEAPPL='AP'` vs `'PO'`).

### 7.2 `sage_bill_hdr` vs live `POINVH1` — exact agreement

| Measure | Live `POINVH1` (window) | Mirror `sage_bill_hdr` |
|---|---|---|
| Rows | **18,047** | **18,047** (17,914 labelled `PO` + 132 `NULL` + 1 `AP`) |
| Distinct vendors | **357** | **357** |
| Date range | 20260101 – 20260430 | 20260101 – 20260430 |
| INR | **16,049** | **16,049** |
| USD | **1,841** | **1,841** |
| RMB | **92** | **92** |
| CNY | **65** | **65** |

```sql
-- live
SELECT RTRIM(CURRENCY) cur, COUNT(*) n FROM POINVH1
 WHERE DATE BETWEEN 20260101 AND 20260430 GROUP BY CURRENCY ORDER BY n DESC;
SELECT COUNT(*) n, COUNT(DISTINCT RTRIM(VDCODE)) vends FROM POINVH1 WHERE DATE BETWEEN 20260101 AND 20260430;
```
**No divergence.** The `srce_appl` column has 133 mislabelled rows (132 `NULL`, 1 `AP`)
but that is a labelling artefact in the mirror's own column, not a row-count difference.
Note MySQL's `information_schema.tables.table_rows` reports 17,701 for this table — that
is InnoDB's **estimate**, not a count. The real `COUNT(*)` is 18,047. Any check that reads
`table_rows` will report a phantom 346-row shortfall.

### 7.3 `sage_ap_obl` vs live `APOBL`

Mirror `sage_ap_obl` holds **53,296 rows** spanning 20160331–20260930 across 1,161
vendors — i.e. it is a *scoped subset* of live `APOBL` (787,465 rows), not a full copy.
By type: `12: 44,666` · `22: 252` · `32: 3,412` · `50: 2,293` · `51: 2,673` — the same five
transaction types live Sage has (§3.0), which is a good sign the extraction understood them.

Agent 4 has already reconciled the **window** slice and reports it matches live exactly
(12,781 documents / ₹1,763,950,260.97). My independent count of the same population —
`IDTRXTYPE=12 AND SRCEAPPL='AP' AND DATEINVC BETWEEN 20260101 AND 20260430` — is
**12,781 documents**, which corroborates it. I did not re-derive the value total.

**One structural risk in the mirror worth flagging**, independent of current row counts:
`sage_ap_obl.inv_number_raw` and `sage_bill_hdr.inv_number_raw` are
`varchar … COLLATE utf8mb4_unicode_ci` — a **case-insensitive, accent-insensitive**
collation. Sage's document numbers are case-*sensitive*. As measured in DQ-9 there are
**0** case-collisions in live `APOBL` today, so nothing is being lost — but the mirror's
key would silently merge two Sage documents the day one appears. Also note
`sage_ap_obl`'s PK is `(vendor_code, inv_number_raw, trx_type)` whereas Sage's `APOBL` PK
is `(IDVEND, IDINVC)` alone; the extra `trx_type` column is harmless but implies the
mirror's author was unsure whether a document number could carry two types. It cannot.

### 7.4 The "469 missing vendors" claim — challenged

`run_all.sh` line 95 warns that if Sage is unreachable, *"vendors come from the staging
mirror, which is missing 469 of the vendors live APVEN has."*

**I confirm the coordinator's conclusion: `idedat_staging.sage_vendor` (357 rows) is
set-identical to the PO/goods vendor population, and the "469" figure is not reproducible
against any baseline I can construct.**

Independent corroboration of the set-identity, from the live side:
```sql
SELECT COUNT(DISTINCT RTRIM(IDVEND)) FROM APOBL
 WHERE DATEINVC BETWEEN 20260101 AND 20260430 AND SRCEAPPL='PO';    -- 357
SELECT COUNT(DISTINCT RTRIM(VDCODE)) FROM POINVH1
 WHERE DATE BETWEEN 20260101 AND 20260430;                          -- 357
```
and from the mirror side, 0 of the 357 vendors on `sage_bill_hdr` are absent from
`sage_vendor`. So `sage_vendor` = exactly the vendors on the PO-matched window population.

Baselines I measured, none of which yields 469 against 357:

| Baseline | Vendors | Shortfall vs 357 |
|---|---|---|
| Live `APVEN`, all | 4,752 | 4,395 |
| Live `APVEN`, active (`SWACTV=1`) | 2,062 | 1,705 |
| `APOBL` window, any source, any currency | 937 | 580 |
| `APOBL` window, `SRCEAPPL='AP'`, any type/currency | 903 | 546 |
| `APOBL` window, types 12/22/32, any source, any currency | 883 | 526 |
| `APOBL` window, any source, INR | 836 | 479 |
| `APOBL` window, types 12/22/32, any source, INR | 793 | 436 |
| **`extract.sql`'s own `vendors` query** (12/22/32, `SRCEAPPL='AP'`, INR, window) | **669** | **312** |
| `APOBL` window, type 12 only, `SRCEAPPL='AP'` | 557 | 200 |
| Vendors on AP-direct window docs **not** among the 357 PO vendors | 580 | — |

The nearest miss is `836 − 357 = 479`, ten away from 469 and on a baseline
(*all* window vendors, INR, any source) that nobody would naturally choose.
**The warning's direction is right and its magnitude is wrong.** The number that matters
operationally is the one from `extract.sql`'s own vendor query: **the mirror is missing
312 of the 669 vendors the AP-direct window population needs** — and running masters off
the mirror would create the wrong contact set for 47% of them. The warning should say
that. (The coordinator reports 377 of 412 AP-direct vendors absent; that is a narrower
AP-direct baseline than my 669 — the two are consistent in direction, and the difference
is which document types and currencies are counted. Either way the shortfall is large and
the "469" is not the right number.)

### 7.5 Mirror coverage of the item master

`sage_item` holds **16,815** items against live `ICITEM`'s **1,196,108** — **1.4%**. That
is not a defect: the mirror is scoped to the items the window's PO invoices touch (17,129
distinct, §3.3), and 16,815 is the right order of magnitude for that. But it means the
mirror **cannot** answer any question about an item outside the window, and
`run_all.sh`'s warning that HSN codes are unavailable when Sage is unreachable is
well-founded — `sage_item` is a window-scoped snapshot, not an item master.
