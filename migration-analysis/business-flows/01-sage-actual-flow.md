# Sage 300 (IDEDAT) — the actual business flow, proved against live data

**Agent 2 — Sage Business Flow Analyst.** Read-only audit. Every number below came from a
`SELECT` against the live Sage 300 SQL Server (`IDEDAT`, MS SQL Server 2025) unless the label
says otherwise. No script in this repository was executed. Nothing was written anywhere except
this file.

**Working window throughout:** `DATEINVC BETWEEN 20260101 AND 20260430` (Sage dates are
`decimal(9,0)` integers in `YYYYMMDD` form — there is no date type in the schema).

**Evidence labels**

| Label | Meaning |
|---|---|
| **DATABASE VERIFIED** | I ran the SQL shown against live Sage and the result is quoted |
| **DOCUMENTED** | Asserted by a project document; I did not independently re-prove it |
| **INFERRED** | Reasoned from proven facts, not directly measured |
| **CONTRADICTED** | A project document says X; live Sage says Y |

Confidence: **VERIFIED / HIGH / MEDIUM / LOW / UNKNOWN**.

---

## 1. The proven flow

```
                     ┌──────────────────────────────────────────────────────────┐
                     │  APVEN            vendor master (4,752 rows, 2,062 active)│
                     │  key: VENDORID  ──────────────────────────────────┐      │
                     └───────────────────────────────────────────────────┼──────┘
                                                                         │ IDVEND / VDCODE
   ══════════════ GOODS PATH  (SRCEAPPL='PO', 18,046 invoices) ══════════╪══════════════
                                                                         │
   PORQNH1 ──RQNHSEQ──► POPORH1 ──PORHSEQ──► PORCPH1 ──RCPHSEQ──► POINVH1 │
   PORQNL     RQNLSEQ    POPORL    PORLSEQ    PORCPL    RCPLSEQ   POINVL  │
   requisition          purchase order       RECEIPT/GRN         PO invoice
   6,644 in window      9,708 in window      19,327 in window    18,047 in window
                                                   │                  │
                                    ┌──────────────┘                  │ APIBH.DRILLAPP='PO'
                                    │ inventory moves HERE            │ APIBH.DRILLTYPE=5
                                    ▼ (PORCPL.POSTEDTOIC=1)           │ APIBH.DRILLDWNLK
                             ICHIST / ICIVAL                          │   = POINVH1.INVHSEQ
                             APP='PO' TRANSTYPE=1                     │
                             DRILLDWNLK = RCPHSEQ                     ▼
                                    │                          ┌──────────────┐
                                    │  Dr Stock 2A8ST*         │  APIBH       │ invoice batch header
                                    │  Cr GRNI  1L6T*          │  APIBD       │ distribution lines
                                    ▼                          └──────┬───────┘
                             GLPOST SRCELEDGER='PO'                   │ (CNTBTCH, CNTITEM)
                                    SRCETYPE='RC'                     ▼
                                                              ┌──────────────┐
   ══════ AP-DIRECT PATH (SRCEAPPL='AP', 12,781 invoices) ════►│   APOBL      │ the obligation
        no PO, no receipt, no item, no quantity               │ (IDVEND,IDINVC)│ 1 row / document
                                                              └───┬──────┬────┘
                                                                  │      │
                              APOBLJ  (obligation GL/job legs) ◄───┘      │
                              APOBP   (settlements) ◄────────────────────┤ (IDVEND, IDINVC)
                              APOBS   (obligation statistics)            │
                                                                         │ (TYPEBTCH,POSTSEQNCE,
                                                                         │  CNTBTCH,CNTITEM)
                                                                         ▼
                                                                 ┌───────────────┐
                                                                 │ APPJH / APPJD │ AP posting journal
                                                                 └───────┬───────┘
                                              APPJH.GLBATCH / GLENTRY    │  DRILLDWNLK =
                                                        │                │  55·10^16
                                                        ▼                ▼  + POSTSEQNCE·10^11
                                                 ┌───────────────────────────┐ + CNTBTCH·10^6
                                                 │ GLJEH / GLJED  (batch)    │ + CNTITEM
                                                 │ GLPOST         (posted)   │
                                                 └───────────────────────────┘
```

### Hop-by-hop evidence

#### Hop 0 — Vendor
`APVEN.VENDORID` (`char(12)`) → `APOBL.IDVEND`, `POINVH1.VDCODE`, `PORCPH1.VDCODE`,
`POPORH1.VDCODE`. **DATABASE VERIFIED / VERIFIED.**

```sql
SELECT (SELECT COUNT(*) FROM APVEN) apven_rows,
       (SELECT COUNT(*) FROM APVEN WHERE SWACTV=1) active,
       (SELECT COUNT(DISTINCT RTRIM(IDVEND)) FROM APOBL
         WHERE DATEINVC BETWEEN 20260101 AND 20260430) vendors_in_window;
-- 4752 | 2062 | 937
```

#### Hop 1 — Requisition → Purchase Order  *(this hop is missing from the hypothesis)*
`POPORL.RQNHSEQ` / `POPORL.RQNLSEQ` → `PORQNH1.RQNHSEQ` / `PORQNL`. **DATABASE VERIFIED / HIGH.**

```sql
SELECT COUNT(DISTINCT h.PORHSEQ) pos,
       COUNT(DISTINCT CASE WHEN l.RQNHSEQ<>0 THEN h.PORHSEQ END) pos_from_reqn,
       COUNT(*) po_lines,
       SUM(CASE WHEN l.RQNHSEQ<>0 THEN 1 ELSE 0 END) lines_from_reqn
FROM POPORH1 h JOIN POPORL l ON l.PORHSEQ=h.PORHSEQ
WHERE h.DATE BETWEEN 20260101 AND 20260430;
-- 9690 | 8549 | 31586 | 27690
```

**88.2 % of purchase orders in the window (8,549 of 9,690) originate in a requisition**, and
87.7 % of PO lines (27,690 of 31,586) carry a requisition line reference. The requisition layer
is real and is silently dropped by the textbook chain.

#### Hop 2 — Purchase Order → Receipt
`PORCPH1.PORHSEQ` → `POPORH1.PORHSEQ`; line level `PORCPL.PORLSEQ` → `POPORL.PORLSEQ`.
**DATABASE VERIFIED / VERIFIED.**

```sql
SELECT COUNT(*) n, SUM(CASE WHEN PORHSEQ<>0 THEN 1 ELSE 0 END) has_po,
       SUM(CASE WHEN ISINVOICED=1 THEN 1 ELSE 0 END) invoiced,
       SUM(CASE WHEN ISCOMPLETE=1 THEN 1 ELSE 0 END) complete
FROM PORCPH1 WHERE DATE BETWEEN 20260101 AND 20260430;
-- 19327 | 19322 | 18980 | 19149
```

#### Hop 3 — Receipt → **Inventory** (this is where stock moves, not at invoice)
**DATABASE VERIFIED / VERIFIED.** Three independent proofs:

```sql
-- (a) every receipt line in the window is flagged as posted to Inventory Control
SELECT l.POSTEDTOIC, l.STOCKITEM, COUNT(*) n
FROM PORCPL l JOIN PORCPH1 h ON h.RCPHSEQ=l.RCPHSEQ
WHERE h.DATE BETWEEN 20260101 AND 20260430
GROUP BY l.POSTEDTOIC, l.STOCKITEM;
-- POSTEDTOIC=1, STOCKITEM=1, 32971  (single row: no line is 0)

-- (b) no PO invoice line in the window is posted to IC
SELECT SUM(CASE WHEN l.POSTEDTOIC=1 THEN 1 ELSE 0 END) postedtoic, COUNT(*) n
FROM POINVL l JOIN POINVH1 h ON h.INVHSEQ=l.INVHSEQ
WHERE h.DATE BETWEEN 20260101 AND 20260430;
-- 0 | 32586

-- (c) the IC movement record is keyed to the RECEIPT number, not the invoice number
SELECT RTRIM(ITEMNO) ITEMNO, RTRIM(LOCATION) LOC, TRANSDATE, RTRIM(DOCNUM) DOCNUM,
       RTRIM(APP) APP, TRANSTYPE, QUANTITY, HOMEEXTCST, DRILSRCTY, DRILLDWNLK, RTRIM(DRILAPP) DRILAPP
FROM ICHIST WHERE RTRIM(DOCNUM)='IDRCP2546248' ORDER BY [LINENO];
-- ID40819XTG01 | CENSTR | 20260131 | IDRCP2546248 | PO | 1 | 14712 | 89743.200 | 3 | 217109442 | PO
-- ID40819XDR01 | CENSTR | 20260131 | IDRCP2546248 | PO | 1 | 42000 | 58800.000 | 3 | 217109442 | PO
-- ID40819XTG01 | CENSTR | 20260131 | IDRCP2546248 | PO | 1 | 23100 | 140910.000 | 3 | 217109442 | PO
SELECT COUNT(*) FROM ICHIST WHERE RTRIM(DOCNUM)='SP/25-26/9833';   -- 0  (the invoice number)
```

`ICHIST.DRILLDWNLK = 217109442 = PORCPH1.RCPHSEQ`. **The linking key from receipt to inventory
is `ICHIST.DRILLDWNLK = PORCPH1.RCPHSEQ` with `DRILAPP='PO'`, `DRILSRCTY=3`.**

**Important nuance — the invoice *does* touch inventory, but only for value.**
`ICHIST` rows with `APP='PO', TRANSTYPE=2` carry `QUANTITY = 0` and a non-zero `HOMEEXTCST`,
`DRILSRCTY=5`, and a `DOCNUM` that is a *vendor invoice number*:

```sql
SELECT TOP 6 RTRIM(APP) APP, TRANSTYPE, RTRIM(DOCNUM) DOCNUM, TRANSDATE, QUANTITY, HOMEEXTCST, DRILSRCTY
FROM ICHIST WHERE TRANSDATE BETWEEN 20260101 AND 20260430 AND RTRIM(APP)='PO' AND TRANSTYPE=2;
-- PO | 2 | #1/S25-26/217004 | 20260207 | 0.0000 |  18.240 | 5
-- PO | 2 | #1/S25-26/217004 | 20260207 | 0.0000 |  -1.010 | 5
-- ... (3,621 rows in window, net -34,376,392.67)
```

So: **quantity moves at receipt; cost can be revalued at invoice.**
`APP='PO', TRANSTYPE=3` (3,695 rows all-time, exactly `PORETL`'s row count) is a return
(`DOCNUM` = `IDRET25xxxxx`, negative quantity). **DATABASE VERIFIED / HIGH.**

#### Hop 4 — Receipt → PO Invoice
`POINVH1.RCPHSEQ` → `PORCPH1.RCPHSEQ`; line level `POINVL.RCPLSEQ` → `PORCPL.RCPLSEQ`.
**DATABASE VERIFIED / VERIFIED.**

```sql
SELECT COUNT(*) n,
  SUM(CASE WHEN PORHSEQ<>0 THEN 1 ELSE 0 END) has_po,
  SUM(CASE WHEN RCPHSEQ<>0 THEN 1 ELSE 0 END) has_rcp,
  SUM(CASE WHEN MULTIRCP<>0 THEN 1 ELSE 0 END) multircp
FROM POINVH1 WHERE DATE BETWEEN 20260101 AND 20260430;
-- 18047 | 18046 | 18047 | 281

SELECT COUNT(*) n, SUM(CASE WHEN l.PORLSEQ<>0 THEN 1 ELSE 0 END) has_poline,
       SUM(CASE WHEN l.RCPLSEQ<>0 THEN 1 ELSE 0 END) has_rcpline
FROM POINVL l JOIN POINVH1 h ON h.INVHSEQ=l.INVHSEQ
WHERE h.DATE BETWEEN 20260101 AND 20260430;
-- 32586 | 32586 | 32586
```

**Every single PO invoice in the window (18,047 of 18,047) references a receipt, and every
line (32,586 of 32,586) references both a PO line and a receipt line.** `POINVH1.FROMDOC = 3`
on all 18,047 — the invoice is always created *from* a receipt, never standalone.

Three-way match quality:

```sql
SELECT COUNT(*) lines,
  SUM(CASE WHEN ABS(l.RQRECEIVED - r.RQRECEIVED)<0.0001 THEN 1 ELSE 0 END) qty_equal,
  SUM(CASE WHEN ABS(l.UNITCOST   - r.UNITCOST)  <0.000001 THEN 1 ELSE 0 END) cost_equal
FROM POINVL l JOIN POINVH1 h ON h.INVHSEQ=l.INVHSEQ
JOIN PORCPL r ON r.RCPHSEQ=l.RCPHSEQ AND r.RCPLSEQ=l.RCPLSEQ
WHERE h.DATE BETWEEN 20260101 AND 20260430;
-- 32586 | 32585 | 32585
```

32,585 of 32,586 lines match the receipt on **both** quantity and unit cost exactly. One line
differs. **DATABASE VERIFIED / VERIFIED.**

#### Hop 5 — PO Invoice → AP obligation *(the load-bearing hop)*
**`APIBH.DRILLAPP='PO'` + `APIBH.DRILLTYPE` + `APIBH.DRILLDWNLK` is the authoritative link.**
**DATABASE VERIFIED / VERIFIED.**

```sql
SELECT b.DRILLTYPE, COUNT(*) n, COUNT(c.CRNHSEQ) in_POCRNH1,
       COUNT(r.RETHSEQ) in_PORETH1, COUNT(p.INVHSEQ) in_POINVH1
FROM APIBH b
LEFT JOIN POCRNH1 c ON c.CRNHSEQ=b.DRILLDWNLK
LEFT JOIN PORETH1 r ON r.RETHSEQ=b.DRILLDWNLK
LEFT JOIN POINVH1 p ON p.INVHSEQ=b.DRILLDWNLK
WHERE b.DATEINVC BETWEEN 20260101 AND 20260430 AND RTRIM(b.DRILLAPP)='PO'
GROUP BY b.DRILLTYPE;
-- 5 | 18127 | 0   | 0 | 18127     DRILLTYPE 5 -> POINVH1  (PO invoice)
-- 6 |   465 | 465 | 0 |     0     DRILLTYPE 6 -> POCRNH1  (PO credit note)
-- 7 |     6 |   6 | 0 |     0     DRILLTYPE 7 -> POCRNH1  (PO debit note)
```

100 % resolution on all three drill types. `APIBH.DRILLDWNLK` is **0 and `DRILLAPP` blank on
every AP-direct document** (13,517 rows in the window) — this is the cleanest available split
marker at batch level.

The natural-key alternative (`APOBL.IDVEND = POINVH1.VDCODE AND APOBL.IDINVC = POINVH1.INVNUMBER`)
resolves **17,915 of 18,047** headers, leaving 132 unresolved and fanning out slightly where
one vendor+invoice number exists twice. Use the drilldown, not the natural key.

`APIBD` → `APOBL` is `(CNTBTCH, CNTITEM)`. `APIBD` has **no `IDVEND` and no `IDINVC`** —
`(CNTBTCH, CNTITEM, CNTLINE)` is its only key. **DATABASE VERIFIED / VERIFIED.**

#### Hop 6 — AP obligation → payment / settlement
`APOBP` keyed `(IDVEND, IDINVC, CNTPAYMNBR, CNTSEQNCE, CNTBTCH, CNTITEM)`. **DATABASE VERIFIED / HIGH.**

```sql
SELECT TRANSTYPE, COUNT(*) n, CAST(SUM(AMTPAYMHC) AS decimal(20,2)) amt,
       SUM(CASE WHEN RTRIM(ISNULL(IDMEMOXREF,''))<>'' THEN 1 ELSE 0 END) with_memoxref
FROM APOBP WHERE DATEBUS BETWEEN 20260101 AND 20260430 GROUP BY TRANSTYPE ORDER BY n DESC;
-- 11 | 70954 | -1,409,409,884.40 | 70851     payment
-- 10 |  5899 |  1,541,146,547.68 |  5867     prepayment / apply-document
-- 16 |  2196 |      9,876,532.73 |  2196
--  8 |   591 |    -46,937,674.17 |   591  ┐ note cross-reference pair
--  9 |   591 |     46,937,674.17 |   591  ┘
-- 17 |   119 |             -0.59 |     0
--  6 |    14 |      5,361,210.84 |    14  ┐
--  7 |    14 |     -5,361,210.84 |    14  ┘
-- 14 |    10 |         -3,077.28 |    10
```

#### Hop 7 — AP obligation → GL journal → posted GL
**DATABASE VERIFIED / VERIFIED.** Two independent routes, both proved:

**(a) `APPJH.GLBATCH` / `APPJH.GLENTRY` → `GLPOST.BATCHNBR` / `GLPOST.ENTRYNBR` → `GLJEH.BATCHID` / `GLJEH.BTCHENTRY`.**
(`APPJD.GLBATCH` is blank — do not use the detail table for this; `APPJH` carries it.)

**(b) A packed drilldown key, which I reverse-engineered and then verified across the
whole window:**

```
GLPOST.DRILLDWNLK = 55·10^16 + APOBL.POSTSEQNCE·10^11 + APOBL.CNTBTCH·10^6 + APOBL.CNTITEM
                    (with GLPOST.DRILAPP = 'AP')
```

Worked example: `APOBL` batch 17492 item 10, `POSTSEQNCE` 16020 →
`551602017492000010`, which is exactly the `DRILLDWNLK` on the four `GLPOST` legs and on
`GLJEH`. Match rate across the window:

```sql
SELECT o.IDTRXTYPE, COUNT(*) docs, SUM(CASE WHEN g.n IS NOT NULL THEN 1 ELSE 0 END) matched
FROM APOBL o
OUTER APPLY (SELECT COUNT(*) n FROM GLPOST p
             WHERE p.DRILLDWNLK = (CAST(550000000000000000 AS decimal(19,0))
                   + o.POSTSEQNCE*CAST(100000000000 AS decimal(19,0))
                   + o.CNTBTCH*1000000 + o.CNTITEM)
               AND RTRIM(p.SRCELEDGER)='AP' HAVING COUNT(*)>0) g
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 GROUP BY o.IDTRXTYPE;
-- 12 | 30827 | 30824      (99.990 %)   138,786 GL legs
-- 22 |   137 |   137      (100 %)
-- 32 |  2066 |  2064      (99.90 %)
-- 50 |  1729 |  1729      (100 %)
-- 51 |  3274 |  3274      (100 %)
```

Same construction with `DRILAPP='PO'` for receipts, where `DRILLDWNLK` is simply the raw
`RCPHSEQ` (`DRILSRCTY=3`).

#### Hop 8 — GL account resolution *(a trap)*
**DATABASE VERIFIED / VERIFIED. The two GL-account columns use different formats and need
different joins.**

```sql
-- GLPOST.ACCTID is the UNFORMATTED code
SELECT COUNT(*) legs, COUNT(a1.ACCTID) match_on_ACCTID, COUNT(a2.ACCTID) match_on_ACCTFMTTD
FROM GLPOST g
LEFT JOIN GLAMF a1 ON RTRIM(a1.ACCTID)   = RTRIM(g.ACCTID)
LEFT JOIN GLAMF a2 ON RTRIM(a2.ACCTFMTTD)= RTRIM(g.ACCTID)
WHERE g.JRNLDATE BETWEEN 20260101 AND 20260430
  AND RTRIM(g.SRCELEDGER)='AP' AND RTRIM(g.SRCETYPE)='IN';
-- 132326 | 132326 (100 %) | 128882 (97.4 %)

-- APIBD.IDGLACCT is the FORMATTED code
SELECT COUNT(*) lines, COUNT(a1.ACCTID) match_on_ACCTID, COUNT(a2.ACCTID) match_on_ACCTFMTTD
FROM APIBD d JOIN APOBL o ON o.CNTBTCH=d.CNTBTCH AND o.CNTITEM=d.CNTITEM
LEFT JOIN GLAMF a1 ON RTRIM(a1.ACCTID)   = RTRIM(d.IDGLACCT)
LEFT JOIN GLAMF a2 ON RTRIM(a2.ACCTFMTTD)= RTRIM(d.IDGLACCT)
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.IDTRXTYPE=12;
-- 67054 | 63610 (94.9 %) | 67054 (100 %)
```

Example: `GLAMF` holds `ACCTID='4E5O02401'` / `ACCTFMTTD='4E5O024-01'` /
`ACCTDESC='Telephone And Mobile Expenses, IDEPL - 1'`. A migration that joins `GLPOST.ACCTID`
to `ACCTFMTTD` loses 2.6 % of legs; one that joins `APIBD.IDGLACCT` to `ACCTID` loses 5.1 % of
lines. Both failures are silent.

---

## 2. Where the real flow differs from the textbook hypothesis

The hypothesis was:
`Vendor → PO → PO Line → Receipt/GRN → Inventory → Invoice → AP → Payment → Journal → GL`

| # | Hypothesis says | Sage actually does | Evidence | Confidence |
|---|---|---|---|---|
| 1 | One chain | **Two disjoint chains.** 41.5 % of invoices (12,781 of 30,827) never touch the PO module at all — no PO, no receipt, no item, no quantity. | §3 split table | VERIFIED |
| 2 | (omitted) | **A requisition layer sits above the PO.** 88 % of POs in the window come from a `PORQNH1` requisition. | Hop 1 SQL | HIGH |
| 3 | Inventory moves at invoice | **Inventory quantity moves at RECEIPT.** `PORCPL.POSTEDTOIC=1` on 32,971/32,971 lines; `POINVL.POSTEDTOIC=0` on 32,586/32,586. The invoice posts a *value-only* IC adjustment (`ICHIST APP='PO' TRANSTYPE=2`, `QUANTITY=0`). | Hop 3 SQL | VERIFIED |
| 4 | The AP invoice debits expense | **On the PO path the AP invoice debits a CLEARING account, never an expense head.** 16,531 of 20,290 PO-sourced distribution lines hit `1L6T*` A/P Clearing (₹1,835,493,941.72). The expense/inventory debit already happened at receipt. | §4 | VERIFIED |
| 5 | Invoice → AP is 1:1 | **PO invoices are split per receipt.** 6,421 of 18,047 `POINVH1` headers carry a `*N` suffix on `INVNUMBER`; 18,047 headers collapse to 14,602 logical vendor bills. | §6 anomaly 12 | VERIFIED |
| 6 | Payment closes the loop | **Payments, prepayments and note applications all live in `APOBP`, and 4 of 9 `TRANSTYPE` codes are not payments at all.** Also `IDTRXTYPE` 50 (prepayment) and 51 (payment) are themselves rows in `APOBL`, so the "AP document" table is not an invoice register. | Hop 6 SQL, §5 | VERIFIED |
| 7 | AP → Journal → GL | Correct, but the join is not obvious: `APPJD.GLBATCH` is blank; the link is `APPJH.GLBATCH/GLENTRY` or the packed `DRILLDWNLK`. | Hop 7 | VERIFIED |
| 8 | (omitted) | **The window straddles two Sage fiscal years.** Sage FY runs April–March and is labelled by the *ending* calendar year: Jan-26 = FY2026 P10, Feb-26 = P11, Mar-26 = P12, **Apr-26 = FY2027 P01**. | §4.5 | VERIFIED |
| 9 | (omitted) | **1,079 rows that look like invoices are not invoices.** They have no `APIBH` header, no `APIBD` distribution, and were posted through a *payment* batch. ₹295,107,559.76. | §6 anomaly 1 | VERIFIED |

---

## 3. The AP-direct vs PO-matched split (the load-bearing ratio)

**The splitting column is `APOBL.SRCEAPPL` (`char(2)`): `'PO'` = PO-matched, `'AP'` = AP-direct.**
**DATABASE VERIFIED / VERIFIED.**

```sql
SELECT IDTRXTYPE, RTRIM(SRCEAPPL) AS srce, COUNT(*) AS n,
       CAST(SUM(AMTINVCHC) AS decimal(20,2)) AS sum_home_INR,
       COUNT(DISTINCT RTRIM(IDVEND)) AS vendors
FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430
GROUP BY IDTRXTYPE, RTRIM(SRCEAPPL) ORDER BY n DESC;
```

| IDTRXTYPE | meaning | SRCEAPPL | documents | value (INR, home) | vendors |
|---:|---|---|---:|---:|---:|
| 12 | invoice | **PO** | **18,046** | **1,942,654,924.63** | 357 |
| 12 | invoice | **AP** | **12,781** | **1,763,950,260.97** | 557 |
| 51 | payment | AP | 3,274 | −1,860,075,231.25 | 638 |
| 50 | prepayment | AP | 1,729 | −1,548,255,387.03 | 250 |
| 32 | credit note | AP | 1,633 | −333,034,800.90 | 413 |
| 32 | credit note | PO | 433 | −74,925,325.69 | 48 |
| 22 | debit note | AP | 131 | 67,225,961.28 | 56 |
| 22 | debit note | PO | 6 | 370,645.20 | 3 |
| | **total** | | **38,033** | | |

**Invoice split (IDTRXTYPE = 12): 18,046 PO-matched (58.54 %) vs 12,781 AP-direct (41.46 %) by
count; ₹1,942.65 Cr-scale vs ₹1,763.95 M by value → 52.41 % / 47.59 %.**

Three cross-checks that `SRCEAPPL` is the right column:

```sql
-- (a) PO number present on essentially every SRCEAPPL='PO' invoice
SELECT RTRIM(SRCEAPPL) srce, IDTRXTYPE, COUNT(*) n,
       SUM(CASE WHEN RTRIM(ISNULL(IDPONBR,''))<>'' THEN 1 ELSE 0 END) has_ponbr
FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430
GROUP BY RTRIM(SRCEAPPL), IDTRXTYPE;
-- PO | 12 | 18046 | 18045     (one exception - see anomaly 14)
-- AP | 12 | 12781 |   427     (a PO number typed into an AP-direct bill; not PO-matched)

-- (b) SRCEAPPL='PO' invoices always drill to POINVH1, SRCEAPPL='AP' never do
SELECT COUNT(*) n FROM APOBL o JOIN APIBH b ON b.CNTBTCH=o.CNTBTCH AND b.CNTITEM=o.CNTITEM
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.IDTRXTYPE=12 AND RTRIM(o.SRCEAPPL)='PO'
  AND NOT EXISTS (SELECT 1 FROM POINVH1 p WHERE p.INVHSEQ=b.DRILLDWNLK);
-- 0

-- (c) AP-direct invoices almost never appear in POINVH1 by natural key
SELECT COUNT(*) n, SUM(CASE WHEN p.INVHSEQ IS NOT NULL THEN 1 ELSE 0 END) matched
FROM APOBL o LEFT JOIN POINVH1 p
  ON RTRIM(p.VDCODE)=RTRIM(o.IDVEND) AND RTRIM(p.INVNUMBER)=RTRIM(o.IDINVC)
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.IDTRXTYPE=12 AND RTRIM(o.SRCEAPPL)='AP';
-- 12781 | 1     (a single accidental invoice-number collision)
```

**Caution:** 427 AP-direct invoices carry a non-blank `IDPONBR` and 8,836 carry a non-blank
`IDORDERNBR`. Neither field makes them PO-matched. Anyone splitting the population on
"has a PO number" instead of `SRCEAPPL` will misclassify 427 documents.

---

## 4. The accounting flow, proven

### 4.1 The two posting shapes

**PO-matched (`SRCEAPPL='PO'`) — a two-stage entry across two documents:**

```
Receipt   (GLPOST SRCELEDGER='PO', SRCETYPE='RC', DRILLDWNLK = RCPHSEQ)
    Dr  2A8ST*   Stock / Inventory
        Cr  1L6T*   A/P Clearing (GRNI)

Invoice   (GLPOST SRCELEDGER='AP', SRCETYPE='IN', DRILLDWNLK = packed AP key)
    Dr  1L6T*    A/P Clearing (clears the GRNI raised at receipt)
    Dr  2A7TX0*  GST Recoverable
        Cr  1L6T*/1L7*  the vendor's AP control account (from APOBL.IDACCTSET)
```

**AP-direct (`SRCEAPPL='AP'`) — a single entry:**

```
Invoice   (GLPOST SRCELEDGER='AP', SRCETYPE='IN')
    Dr  4E*      the real expense head
    Dr  2A7TX0*  GST Recoverable
        Cr  1L7*/1L6*  AP control account
```

### 4.2 Where the distribution lives

`APIBD` — one row per distribution line, key `(CNTBTCH, CNTITEM, CNTLINE)`, account in
`IDGLACCT` (**formatted**), pre-tax amount in `AMTDIST` (**document currency**) and
`AMTDISTHC` (**home currency, INR**).

A second table, `APOBLJ`, keyed `(IDVEND, IDINVC, CNTLINE)`, carries the same split but
**tax-inclusive** and in obligation terms (`AMTINVCHC`, `AMTDUEHC`). For trace T1:
`APIBD` = 270,551.60 + 58,800.00 (pre-tax); `APOBLJ` = 319,250.89 + 69,384.00 = 388,634.89
(the document gross). Both are needed; they are not duplicates.

### 4.3 Account family split on AP distributions (window, IDTRXTYPE=12)

```sql
SELECT RTRIM(o.SRCEAPPL) srce, LEFT(RTRIM(d.IDGLACCT),4) acct4, COUNT(*) lines,
       CAST(SUM(d.AMTDISTHC) AS decimal(20,2)) amt
FROM APIBD d JOIN APOBL o ON o.CNTBTCH=d.CNTBTCH AND o.CNTITEM=d.CNTITEM
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.IDTRXTYPE=12
GROUP BY RTRIM(o.SRCEAPPL), LEFT(RTRIM(d.IDGLACCT),4) ORDER BY lines DESC;
```

| SRCEAPPL | prefix | lines | INR (home) | what it is |
|---|---|---:|---:|---|
| AP | 4E4S | 31,866 | 101,847,097.49 | selling & distribution expense |
| **PO** | **1L6T** | **16,531** | **1,835,493,941.72** | **A/P Clearing (GRNI) — the PO path's only debit** |
| AP | 4E2M | 6,091 | 456,930,082.30 | manufacturing expense |
| AP | 2A7T | 2,517 | −7,565,163.70 | GST recoverable booked as a distribution |
| AP | 1L8T | 2,244 | 10,611,686.99 | RCM payable / TDS |
| PO | 4E2M | 2,050 | 4,639,963.87 | freight & handling on import POs |
| AP | 4E5O | 1,607 | 99,341,012.90 | other expense |
| PO | 1L7M | 1,263 | 19,539,002.93 | manufacturing vendors control |
| AP | 4E1M | 992 | 253,164,822.61 | material expense (incl. `4E1M016` Round Off) |
| AP | 4E3E | 551 | 79,885,686.00 | establishment expense |
| AP | 1L9O / 1L9E / 2A1F / 2A7S / 2A3L / 1L3L / 2A20 | 224 | ≈ 392 M | **balance-sheet heads on documents that are formally invoices** |

Rolled to two characters, AP-direct is 41,123 lines of `4E*` (₹1,006,638,839.91), 2,681 lines of
`2A*` (₹42,046,002.30) and 2,352 lines of `1L*` (₹365,939,870.06). **A quarter of AP-direct
value by that measure is not P&L expense at all.** 293 AP-direct documents touch an account
outside `4E*`/`2A7T*`/`1L8TX14-16`.

### 4.4 Where tax lands, and whether it is recoverable

**Input GST in this data is 100 % recoverable. DATABASE VERIFIED / VERIFIED.**

```sql
SELECT RTRIM(o.SRCEAPPL) srce,
 CAST(SUM(d.AMTTAXREC1+d.AMTTAXREC2+d.AMTTAXREC3+d.AMTTAXREC4+d.AMTTAXREC5) AS decimal(20,2)) recoverable,
 CAST(SUM(d.AMTTAXEXP1+d.AMTTAXEXP2+d.AMTTAXEXP3+d.AMTTAXEXP4+d.AMTTAXEXP5) AS decimal(20,2)) expensed,
 CAST(SUM(d.AMTTOTTAX) AS decimal(20,2)) total_tax
FROM APIBD d JOIN APOBL o ON o.CNTBTCH=d.CNTBTCH AND o.CNTITEM=d.CNTITEM
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.IDTRXTYPE=12
GROUP BY RTRIM(o.SRCEAPPL);
-- AP | 54,217,988.94 | 0.00 | 54,217,988.94
-- PO | 78,593,161.95 | 0.00 | 78,593,161.95
```

`AMTTAXEXP1..5` is **zero on every one of the 69,713 distribution lines in the window**. The
account the tax lands in comes from `APIBH.ACCTREC1..5` (recoverable) / `ACCTEXP1..5` (expensed);
`ACCTEXP*` is blank throughout.

Tax accounts actually hit in GL in the window:

```sql
SELECT RTRIM(g.ACCTID) acct, RTRIM(a.ACCTDESC) descr, COUNT(*) legs,
       CAST(SUM(g.TRANSAMT) AS decimal(20,2)) amt
FROM GLPOST g LEFT JOIN GLAMF a ON RTRIM(a.ACCTID)=RTRIM(g.ACCTID)
WHERE g.JRNLDATE BETWEEN 20260101 AND 20260430
  AND (RTRIM(g.ACCTID) LIKE '2A7T%' OR RTRIM(g.ACCTID) LIKE '1L8TX%')
GROUP BY RTRIM(g.ACCTID), RTRIM(a.ACCTDESC) ORDER BY legs DESC;
```

| account | description | legs | INR |
|---|---|---:|---:|
| 2A7TX03 | IGST Recoverable | 16,481 | 16,169,309.08 |
| 2A7TX01 | SGST Recoverable | 11,292 | 20,070,745.41 |
| 2A7TX02 | CGST Recoverable | 11,291 | 20,072,257.92 |
| 1L8TX14 | SGST Payable - RCM | 1,040 | 70,830.00 |
| 1L8TX15 | CGST Payable - RCM | 1,039 | 69,531.00 |
| 1L8TX13 | IGST Payable | 405 | 707,412.72 |
| 2A7TX04 | IGST Recoverable on Imports | 374 | 11,155,419.00 |
| 1L8TX12 / 1L8TX11 | CGST / SGST Payable | 220 / 220 | 730,247.40 each |
| 1L8TX16 | IGST Payable - RCM | 135 | −323,761.37 |
| 1L8TX04 | Professional Tax | 56 | −1,256,630.00 |
| 2A7TX07/08/09 | GST Recoverable – 2A Reco | 4/4/3 | −426,460 / −426,460 / 42,352 |

Intra-state vs inter-state is driven by `APOBL.CODETAXGRP` = `'LOCAL'` (→ CGST+SGST,
`APIBH.ACCTREC1=2A7TX01`, `ACCTREC2=2A7TX02`) vs `'INTERSTATE'` (→ IGST,
`APIBH.ACCTREC1=2A7TX03`). `'NOTAX'`, `'NOTAXUSD'` and legacy `'VAT'/'NRVAT'/'NRST'/'NRVATST'`
also occur.

**Reverse charge.** 1,140 AP-direct invoices in the window carry an RCM leg
(`IDGLACCT` beginning `1L8TX14/15/16`). `APOBL.AMTTAXHC` is **0** on these — the tax exists
only as a self-assessed pair of distribution lines, credit side negative:

```sql
SELECT LEFT(RTRIM(d.IDGLACCT),7) acct,
 SUM(CASE WHEN d.AMTDISTHC>0 THEN 1 ELSE 0 END) pos_lines,
 CAST(SUM(CASE WHEN d.AMTDISTHC>0 THEN d.AMTDISTHC ELSE 0 END) AS decimal(20,2)) pos_amt,
 SUM(CASE WHEN d.AMTDISTHC<0 THEN 1 ELSE 0 END) neg_lines,
 CAST(SUM(CASE WHEN d.AMTDISTHC<0 THEN d.AMTDISTHC ELSE 0 END) AS decimal(20,2)) neg_amt
FROM APIBD d JOIN APOBL o ON o.CNTBTCH=d.CNTBTCH AND o.CNTITEM=d.CNTITEM
WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.IDTRXTYPE=12
  AND LEFT(RTRIM(d.IDGLACCT),7) IN ('1L8TX14','1L8TX15','1L8TX16')
GROUP BY LEFT(RTRIM(d.IDGLACCT),7);
-- 1L8TX14 |  5 |   831,914.00 | 1018 | -786,796.00
-- 1L8TX15 |  4 |   830,615.00 | 1018 | -786,796.00
-- 1L8TX16 |  4 | 2,282,854.00 |  117 | -372,592.80
```

Trace T4 shows the shape exactly (see §5.4): `Dr 2A7TX01 +12,144 / Dr 2A7TX02 +12,144 /
Cr 1L8TX14 −12,144 / Cr 1L8TX15 −12,144` — net zero on the document, taxable base
₹485,742.06, implied rate 5.0002 % (GTA).

**Zero PO-matched documents carry an RCM leg** — the `EXISTS` test above returns nothing for
`SRCEAPPL='PO'`. **DATABASE VERIFIED / VERIFIED.**

### 4.5 Posting date vs document date vs fiscal period

**They routinely disagree.** `APOBL.DATEINVC` is the document date; `APOBL.DATEBUS` is the
posting (business) date; `FISCYR`/`FISCPER` follow the posting date.

```sql
SELECT o.IDTRXTYPE, COUNT(*) n, MIN(o.DATEBUS-o.DATEINVC) mindelta, MAX(o.DATEBUS-o.DATEINVC) maxdelta
FROM APOBL o WHERE o.DATEINVC BETWEEN 20260101 AND 20260430 AND o.DATEBUS <> o.DATEINVC
GROUP BY o.IDTRXTYPE;
-- 12 | 18005 | -89 | 310
-- 22 |     3 |   2 |  15
-- 32 |    41 |   1 |  29
```

**18,005 of 30,827 invoices (58 %) post on a different date from the document date**, spanning
−89 to +310 (raw `YYYYMMDD` integer difference, so read as "materially earlier / later").

**The fiscal calendar is April–March, labelled by the ending calendar year:**

```sql
SELECT CAST(DATEINVC/100 AS int) AS yyyymm, FISCYR, FISCPER, COUNT(*) n FROM APOBL
WHERE DATEINVC BETWEEN 20251001 AND 20260731
GROUP BY CAST(DATEINVC/100 AS int), FISCYR, FISCPER HAVING COUNT(*)>50 ORDER BY yyyymm;
-- 202510 | 2026 | 07 |  9001      202601 | 2026 | 10 | 10499
-- 202511 | 2026 | 08 |  8965      202602 | 2026 | 11 |  8121
-- 202512 | 2026 | 09 |  9861      202603 | 2026 | 12 | 11478
--                                 202604 | 2027 | 01 |  7919   <-- FY rolls inside the window
```

**The Jan–Apr 2026 migration window straddles two Sage fiscal years** (FY2026 P10–P12 and
FY2027 P01). 16 documents in the window have a `FISCYR`/`FISCPER` that does not correspond to
their own document date at all.

The `GLPOST` legs carry **both**: `DOCDATE` (document date) and `JRNLDATE` (posting date). For
trace T3, `DOCDATE=20260217` while `JRNLDATE=20260228`, `FISCALYR=2026`, `FISCALPERD=11`.

### 4.6 Debits equal credits

```sql
-- every posted GL entry in the window balances
SELECT COUNT(*) unbalanced_entries FROM (
 SELECT BATCHNBR, ENTRYNBR, SUM(TRANSAMT) s FROM GLPOST
 WHERE JRNLDATE BETWEEN 20260101 AND 20260430
 GROUP BY BATCHNBR, ENTRYNBR HAVING ABS(SUM(TRANSAMT))>0.005) x;
-- 0
```

Zero unbalanced entries out of ~700,000 legs. Worked numbers for two traced documents are in
§5.1 and §5.4.

---

## 5. Document traces

All eight are real documents in the migration window. Vendor codes and invoice numbers are
given because the traces are worthless without them; no GSTINs appear.

### 5.1 T1 — PO-matched goods bill, multi-line, taxed, paid  ·  `ACCI217 / SP/25-26/9833`

| Hop | Table | Record ID | Doc number | Date | Vendor | Item | Qty | UOM | Amount | Tax | Cur | Status | → next |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | POPORH1 | PORHSEQ 214879249 | IDPO2521766 | 20260106 | ACCI217 | — | — | — | — | — | INR | PORTYPE 1, open | PORHSEQ |
| 2 | POPORL | PORLSEQ 214879313/15/17 | IDPO2521766 | — | — | ID40819X-TG01, -DR01 | 18,900 / 23,100 / 42,000 | NOS, PCS | — | — | INR | — | PORLSEQ |
| 3 | PORCPH1 | RCPHSEQ 217109442 | IDRCP2546248 | 20260131 | ACCI217 | — | 79,812 | — | 289,453.20 | — | INR | ISINVOICED 1 | RCPHSEQ |
| 4 | PORCPL | RCPLSEQ 217109505/07/09 | IDRCP2546248 | 20260131 | — | ID40819X-TG01 ×2, -DR01 | 14,712 / 23,100 / 42,000 | NOS/NOS/PCS | 89,743.20 / 140,910.00 / 58,800.00 | — | INR | **POSTEDTOIC = 1** | RCPLSEQ |
| 5 | **ICHIST** | DAYENDSEQ 4398241 | IDRCP2546248 | 20260131 | — | ID40819XTG01, ID40819XDR01 | same | NOS/PCS | 89,743.20 / 140,910.00 / 58,800.00 | — | INR | APP='PO' TRANSTYPE=1 | DRILLDWNLK=217109442 |
| 6 | GLPOST | batch 041358 entry 00003 | — | 20260131 | — | — | — | — | Dr 2A8ST03 89,743.20 + 140,910.00; Dr 2A8ST02 58,800.00; Cr 1L6TP07 −230,653.20; Cr 1L6TA07 −58,800.00 | — | INR | FY2026 P10 | — |
| 7 | POINVH1 | INVHSEQ 218789443 | SP/25-26/9833 | 20260221 | ACCI217 | 5 lines | — | — | EXTENDED 329,351.60 / DOCTOTAL 388,634.89 | 59,283.29 IGST | INR | FROMDOC 3, MULTIRCP 1, RCPS 3 | INVHSEQ |
| 8 | POINVL | INVLSEQ 218790471…479 | SP/25-26/9833 | — | — | ID40819X-TG01/-DR01/-ST01 | 14,712 / 23,100 / 42,000 / 42,000 / 344 | NOS, PCS | 89,743.20 / 140,910.00 / 58,800.00 / 37,800.00 / 2,098.40 | 18 % each | INR | **POSTEDTOIC = 0** | RCPHSEQ+RCPLSEQ |
| 9 | APIBH | (17492, 10) | SP/25-26/9833 | 20260221 | ACCI217 | — | — | — | 329,351.60 | 59,283.29 | INR | **DRILLAPP='PO' DRILLTYPE=5 DRILLDWNLK=218789443** | (CNTBTCH,CNTITEM) |
| 10 | APIBD | (17492,10,20) & (…,40) | — | — | — | *(none)* | *(0)* | *(blank)* | 270,551.60 → 1L6TP07; 58,800.00 → 1L6TA07 | 48,699.29 + 10,584.00, **all recoverable** | INR | — | (CNTBTCH,CNTITEM) |
| 11 | APOBL | (17492, 10) | SP/25-26/9833 | 20260221 (due 20260323) | ACCI217 | — | — | — | 388,634.89 | 59,283.29 | INR | SWPAID 1, DATEPAID 20260520, POSTSEQNCE 16020 | POSTSEQNCE |
| 12 | APOBLJ | lines 1–2 | SP/25-26/9833 | — | — | — | — | — | 319,250.89 + 69,384.00 | incl. | INR | — | — |
| 13 | APOBP | (1,1,34962,65) | — | 20260520 | — | — | — | — | −388,634.89 | — | INR | TRANSTYPE 11, cheque `…554375` | — |
| 14 | APPJH | IN / 16020 / 17492 / 10 | Invoice-17492-10 | 20260221 | — | — | — | — | — | — | INR | **GLBATCH 041645 GLENTRY 00010** | GLBATCH/GLENTRY |
| 15 | GLJEH | 041645 / 00010 | Invoice-17492-10 | 20260221 | — | — | — | — | **JRNLDR 388,634.89 = JRNLCR 388,634.89** | — | INR | DRILLDWNLK 551602017492000010 | — |
| 16 | GLPOST | 041645 / 00010, 4 legs | — | 20260221 | — | — | — | — | see below | — | INR | FY2026 P11 | — |

**The posted GL entry (hop 16), debits = credits:**

| account | description | type | INR |
|---|---|---|---:|
| 1L7OV06 | (AP control, from `IDACCTSET='OTHINT'`) | B | −388,634.89 |
| 1L6TP07 | A/P Clearing – Packing Items | B | +270,551.60 |
| 1L6TA07 | A/P Clearing – Accessories | B | +58,800.00 |
| 2A7TX03 | IGST Recoverable | B | +59,283.29 |
| | **sum** | | **0.00** |

Notice the receipt (20260131, FY2026 **P10**) and the invoice (20260221, FY2026 **P11**) land in
different periods; `1L6TP07`/`1L6TA07` carries the difference. Notice also that the invoice
pulls lines from **three** receipts (`IDRCP2546248`, `IDRCP2546249`, `IDRCP2547108`) and two POs.

### 5.2 T2 — PO-matched, UNPAID  ·  `FABI027 / 252608144*1`

| Hop | Table | ID | Date | Amount | Tax | Status |
|---|---|---|---|---:|---:|---|
| APOBL | (17837, 41) | POSTSEQNCE 16355 | 20260414, due 20260514 | 1,742,554.49 | 82,978.79 IGST | **SWPAID 0, AMTDUEHC 1,742,554.49, DATEPAID 0**, FISCYR **2027** P01 |
| APIBH | (17837, 41) | DRILLDWNLK **223076373** | 20260414 | 1,659,575.70 | 82,978.79 | DRILLAPP PO / TYPE 5, ACCTREC1 2A7TX03 |
| APIBD | line 20 | 1L6TF13 A/P Clearing – Fabric | — | 1,659,575.70 | 82,978.79 recoverable @5 % | — |
| APOBP | — | — | — | — | — | **no rows — nothing has been applied** |
| APPJD | IN / 16355 | Cr 1L6TF02 −1,742,554.49 · Dr 1L6TF13 +1,659,575.70 · tax 2A7TX03 82,978.79 | 20260414 | | | sums to 0 |

The `*1` suffix on the invoice number is Sage's receipt-split marker, not part of the vendor's
invoice number. The AP control account here is `1L6TF02` (Fabric Vendors – Interstate), from
`IDACCTSET='FABINT'` — **the AP control account varies by vendor account set**, so it cannot be
hard-coded.

### 5.3 T3 — AP-direct service bill, 8 lines, forward-charge CGST+SGST, paid  ·  `SELD043 / IN1BLR261005890`

| Hop | Table | ID | Doc date | Post date | Amount | Tax | Status |
|---|---|---|---|---|---:|---:|---|
| APOBL | (17534, 19) | POSTSEQNCE 16065 | 20260217 | **20260228** | 458,879.63 | 69,998.58 (SGST 34,999.29 + CGST 34,999.29) | SWPAID 1, DATEPAID 20260415, LOCAL |
| APIBH | (17534, 19) | **DRILLAPP blank, DRILLDWNLK 0** | 20260217 | 20260228 | 388,881.05 | 69,998.58 | ACCTREC1 `2A7TX01`, ACCTREC2 `2A7TX02`, AMTEXPTAX 0 |
| APIBD | 8 lines 20…160 | all `4E4SD04` Freight Charges-Export | — | — | 24,552.93 / 20,757.86 / 21,965.38 / 9,430.16 / 5,807.60 / 23,172.90 / 199,873.48 / 83,320.74 | 9 %+9 % per line, `AMTTAXREC*` = full, `AMTTAXEXP*` = 0 | `IDITEM`, `QTYINVC`, `UNITMEAS` all **empty** |
| APOBP | 3 rows | 20260320 −458,879.63 → 20260325 +458,879.63 (**reversal**) → 20260415 −458,879.63 | — | — | — | — | TRANSTYPE 11, two cheques |
| GLPOST | 041710 / 00021, 11 legs | — | DOCDATE 20260217 | JRNLDATE 20260228 | see below | | FY2026 P11 |

**GL entry (11 legs), debits = credits:**
`Cr 1L7SV01 Selling & Distribution Vendors −458,879.63` ·
`Dr 4E4SD04 Freight Charges-Export ×8 = +388,881.05` ·
`Dr 2A7TX01 SGST Recoverable +34,999.29` · `Dr 2A7TX02 CGST Recoverable +34,999.29` → **sum 0.00**.

This document is the shape the AP-direct migration population takes: **no item, no quantity,
no UOM, no PO, no receipt** — a GL account, a narration and an amount.

Note also `APOBL.DESCINVC = 'From I/E Invoice Bill bat no- 59 Ent no- 154'` — an upstream
"I/E" feeder system outside Sage generated it. **INFERRED / MEDIUM** — I could not locate an
I/E module in the schema.

### 5.4 T4 — AP-direct with REVERSE CHARGE, 98 lines  ·  `SELD243 / JPS/2025-26/729`

| Hop | Table | ID | Date | Amount | Tax | Status |
|---|---|---|---|---:|---:|---|
| APOBL | (17810, 1) | POSTSEQNCE 16329 | doc 20260327, post 20260331 | 485,742.06 | **AMTTAXHC = 0.000** | SWPAID 1, DATEPAID 20260417, LOCAL, FY2026 P12 |
| APIBH | (17810, 1) | DRILLAPP blank | — | 485,742.06 | AMTTAXTOT 0, AMTRECTAX 0 | ACCTREC1 2A7TX01 / ACCTREC2 2A7TX02 |
| APIBD | 94 lines | `4E2ME13` Carriage Inward | — | 485,742.06 | 0 stated | forward tax absent |
| APIBD | lines 1900/1920 | `2A7TX01` +12,144.00 · `2A7TX02` +12,144.00 | — | — | — | the **input** leg |
| APIBD | lines 1940/1960 | `1L8TX14` −12,144.00 · `1L8TX15` −12,144.00 | — | — | — | the **self-assessed output** leg |
| GLPOST | 042758 / 00062, **99 legs** | DOCDATE 20260327, JRNLDATE 20260331 | | `Cr 1L7SV01 −485,742.06` + 94 × `Dr 4E2ME13` + the four tax legs | | **SUM(TRANSAMT) = 0.00** |

```sql
SELECT COUNT(*) legs, CAST(SUM(TRANSAMT) AS decimal(20,2)) total
FROM GLPOST WHERE DRILLDWNLK=551632917810000001;
-- 99 | 0.00
```

Implied RCM rate 24,288 / 485,742.06 = **5.0002 %**. The rate is **not stated anywhere in the
record** — `RATETAX1..5` is 0 on every line. Anyone migrating RCM has to derive the rate from
the amounts. **DATABASE VERIFIED / VERIFIED.**

### 5.5 T5 — Credit note (IDTRXTYPE 32), PO-sourced, taxed  ·  `FABI049 / DN-FD-1053`

| Hop | Table | ID | Date | Amount | Tax | Status |
|---|---|---|---|---:|---:|---|
| APOBL | (17748, 5) | POSTSEQNCE 16256 | 20260331 | **−3,692,045.36** | **−563,193.36** | SWPAID 0, AMTDUEHC −3,692,045.36, SRCEAPPL **PO** |
| APIBH | (17748, 5) | **DRILLTYPE 6 → POCRNH1.CRNHSEQ 222368972** | 20260331 | 3,128,852.00 | 563,193.36 | IDTRX 32 |
| APIBD | line 20 | `1L6TF13` A/P Clearing – Fabric | — | 3,128,852.00 (positive here) | 563,193.36 @18 % | — |
| APPJD | IN / 16256, SRCETYPE **CR** | — | 20260331 | — | — | — |
| GLPOST | 042286 / 00400, 3 legs | 20260331, FY2026 P12 | `Dr 1L6TF02 +3,692,045.36` · `Cr 1L6TF13 −3,128,852.00` · `Cr 2A7TX03 −563,193.36` | | **sum 0.00** |

**Sage reverses Input GST on the credit note** (`2A7TX03` goes negative) rather than raising
Output GST. That is the whole substance of the "credit-note GST fork" the project holds notes
for. **DATABASE VERIFIED / VERIFIED.** Note also the document number literally reads `DN-…`
while `IDTRXTYPE=32` is a **credit** note — the number is not the type.

### 5.6 T6 — Debit note (IDTRXTYPE 22), AP-direct, no tax  ·  `OTHL438 / CH:55900/14.01.26`

| Hop | Table | ID | Date | Amount | Status |
|---|---|---|---|---:|---|
| APOBL | (17549, 1) | POSTSEQNCE 16074 | doc 20260228 | +4,300,000.00 | SWPAID 1, **DATEPAID 20260114 — 45 days BEFORE the document date** |
| APIBD | line 20 | `4E5O036` Donation, "Twds donation paid to ID CARE TRUST" | — | 4,300,000.00 | zero tax |
| APOBP | (1,1,34458,8) | 20260301 | −4,300,000.00 | TRANSTYPE **10** (prepayment application), `IDMEMOXREF='PP059608'` |
| GLPOST | 041724 / 00001, SRCETYPE **DB** | 20260228, FY2026 P11 | `Cr 1L7OV01 −4,300,000` · `Dr 4E5O036 +4,300,000` | **sum 0.00** |

A debit note (22) increases the payable and is **positive** in Sage; a credit note (32) is
negative. Direction is carried by the type, not only by the sign.

### 5.7 T7 — Foreign-currency PO-matched import  ·  `ACCU187 / TIN1-2601080001`

| Hop | Table | ID | Date | Src amount | Rate | Home amount | Status |
|---|---|---|---|---:|---:|---:|---|
| APOBL | (17361, 21) | POSTSEQNCE 15899 | 20260131 | **USD 76,807.40** | **89.5000000** | INR 6,874,262.30 | SWPAID 1, DATEPAID 20260202, TAXGRP `NOTAXUSD` |
| APIBH | (17361, 21) | DRILLDWNLK 217326553 (POINVH1) | 20260131 | USD 76,807.40 | 89.50 | — | DRILLTYPE 5 |
| APIBD | lines 20, 40 | `1L6TA07` USD 71,051.07 → INR 6,359,070.77; `4E2ME17` USD 5,756.33 → INR 515,191.54 | | | | **`AMTDIST` is document currency; `AMTDISTHC` is INR** |
| GLPOST | 041440 / 00020, 4 legs | 20260131, FY2026 P10 | | | | see below |

**GL entry:** `Cr 1L6TA03 Accessories Vendors – Import −6,874,262.30` ·
`Dr 1L6TA07 A/P Clearing – Accessories +6,359,070.77` ·
`Dr 4E2ME17 Handling & Packing Charges Imports – PO +515,191.54` ·
`Cr 4E6F025 A/P Realised Exchange Gain/Loss −0.01` (SRCETYPE **RD**) → **sum 0.00**.

Two findings here:

1. **A single GL entry can mix `SRCETYPE` values** (`IN` legs plus an `RD` rounding leg).
   Grouping GL by source type breaks the entry.
2. **`GLPOST` does not retain the foreign amount.** `SCURNCODE='INR'`, `SCURNAMT` = the INR
   figure, `CONVRATE = 1.0000000` — even for this USD document. The USD amount and the 89.50
   rate live **only** in `APOBL`/`APIBD`/`POINVH1`. A GL-first migration silently loses
   currency. **DATABASE VERIFIED / VERIFIED.**

### 5.8 T8 — a document that looks like an invoice and is not  ·  `OTHX003 / '18 03 2026'`

| Table | Result |
|---|---|
| APOBL | `IDTRXTYPE=12`, `SRCEAPPL='AP'`, `CNTBTCH=34336`, `CNTITEM=3`, `DATEINVC=20260318`, `AMTINVCHC=10,000,000.00`, `CODETAXGRP='NOTAX'`, `DESCINVC='SBIOSB OFB TECH REPAYMENT LOAN MAIL:19.03.26'`, `CNTOBLJ=1` |
| APIBH | **no row** |
| APIBD | **no rows** |
| APPJH | `TYPEBTCH='PY'` — posted through a **payment** batch |
| GLPOST | hits `2A6BA01 State Bank Of India – A/c # 10605517870` and `1L9OL06 Indian Designs Exports Pvt ltd., Unit-9` |

There are **1,079** of these in the window, worth **₹295,107,559.76**. Three of them
(`'18 03 2026'`, `'18.03.2026'`, `'18032026'`) are the *same* ₹1 crore loan repayment keyed
three different ways for the same vendor. See §6.1.

---

## 6. The Jan–Apr 2026 population profile

### 6.1 Documents by type

`IDTRXTYPE` code → meaning, **proved** from `APPJH.TRXTYPE`, `APPJD.SRCETYPE` and `GLSRCE`:

| code | Sage meaning | proof |
|---:|---|---|
| 12 | Invoice | `APPJH.TYPEBTCH='IN'`, `TRXTYPE=12`, `APPJD.SRCETYPE='IN'` → `GLSRCE` "A/P Invoice" |
| 22 | Debit note | `TRXTYPE=22`, `SRCETYPE='DB'` → `GLSRCE` "A/P Debit Note"; amounts **positive** |
| 32 | Credit note | `TRXTYPE=32`, `SRCETYPE='CR'` → `GLSRCE` "A/P Credit Note"; amounts **negative** |
| 50 | Prepayment | `TYPEBTCH='PY'`, `TRXTYPE=57/58`, `SRCETYPE='PP'` → `GLSRCE` "A/P Prepayment" |
| 51 | Payment | `TYPEBTCH='PY'`, `TRXTYPE=51`, `SRCETYPE='PY'` → `GLSRCE` "A/P Check" |
| 40 | (1 row in the whole database, outside the window) | — |

Counts and values: see the table in §3. Totals in the window: **38,033 `APOBL` rows**, of which
**30,827 are invoices**, 2,203 are notes and 5,003 are payments/prepayments.

The complete `GLSRCE` dictionary (64 rows) is available and is the authority for every
`SRCELEDGER`/`SRCETYPE` pair; the ones that matter here are `AP-IN`, `AP-CR`, `AP-DB`, `AP-PY`,
`AP-PP`, `AP-RD` (rounding), `AP-RV` (payment reversal), `PO-RC` (receipts), `PO-IN`,
`PO-RT` (returns), `IC-RC`, `IC-AD`.

### 6.2 Value by type and currency

```sql
SELECT IDTRXTYPE, RTRIM(SRCEAPPL) srce, RTRIM(CODECURN) cur, COUNT(*) n,
  CAST(SUM(AMTINVCHC) AS decimal(20,2)) home_INR,
  CAST(SUM(AMTINVCTC) AS decimal(20,2)) source_currency,
  CAST(SUM(AMTTAXHC)  AS decimal(20,2)) tax_INR
FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430
GROUP BY IDTRXTYPE, RTRIM(SRCEAPPL), RTRIM(CODECURN);
```

| type | srce | cur | n | home INR | source ccy | tax INR |
|---:|---|---|---:|---:|---:|---:|
| 12 | AP | INR | 12,738 | 1,509,705,386.89 | 1,509,705,386.89 | 54,266,283.66 |
| 12 | AP | USD | 39 | 251,043,281.12 | 2,714,747.85 | 0.00 |
| 12 | AP | GBP | 2 | 1,916,143.20 | 15,744.00 | 0.00 |
| 12 | AP | EUR | 2 | 1,285,449.76 | 12,207.50 | 0.00 |
| 12 | PO | INR | 16,048 | 1,439,533,070.22 | 1,439,533,070.22 | 78,593,161.95 |
| 12 | PO | USD | 1,842 | 242,200,338.18 | 2,689,681.93 | 0.00 |
| 12 | PO | RMB | 92 | 138,912,052.52 | 10,886,465.28 | 0.00 |
| 12 | PO | CNY | 64 | 122,009,463.71 | 9,583,150.35 | 0.00 |
| 22 | AP | INR | 122 | 20,129,507.63 | — | 116,435.44 |
| 22 | AP | USD | 7 | 47,092,255.07 | 508,830.46 | 0.00 |
| 22 | AP | RMB | 2 | 4,198.58 | 314.50 | 0.00 |
| 22 | PO | INR | 6 | 370,645.20 | — | 17,649.78 |
| 32 | AP | INR | 1,629 | −323,603,480.66 | — | −2,138,811.46 |
| 32 | AP | USD | 4 | −9,431,320.24 | −101,911.88 | 0.00 |
| 32 | PO | INR | 428 | −74,816,141.07 | — | −5,537,540.12 |
| 32 | PO | USD | 5 | −109,184.62 | −1,187.60 | 0.00 |
| 50 | AP | INR/USD/RMB/CNY/EUR/GBP/AED | 1,729 | −1,548,255,387.03 | — | 0.00 |
| 51 | AP | INR | 3,274 | −1,860,075,231.25 | — | 0.00 |

### 6.3 The multi-currency picture

**Home currency is INR.** `EXCHRATEHC = 1.0000000` on every INR row (min = max = 1.0) and
`AMTINVCHC = AMTINVCTC` for INR. **DATABASE VERIFIED / VERIFIED.**

Non-home-currency invoices (IDTRXTYPE 12): **2,041 of 30,827 = 6.62 %** — USD 1,881,
RMB 92, CNY 64, GBP 2, EUR 2. Across all document types the window sees 7 currency codes
(INR 35,661, USD 2,162, RMB 119, CNY 82, EUR 5, GBP 3, AED 1) with rates from 12.60 to 123.295.

**`RMB` and `CNY` are two codes for the same currency** and their rate ranges overlap exactly
(12.60–13.835 both). 156 type-12 invoices are affected. Any per-currency report double-counts.

### 6.4 Distinct entities touched

| dimension | count | SQL basis |
|---|---:|---|
| vendors, types 12/22/32 | **883** | `COUNT(DISTINCT RTRIM(IDVEND))` |
| vendors, all types | 937 | same, no type filter |
| vendors, PO-matched invoices only | 357 | grouped by `SRCEAPPL` |
| vendors, AP-direct invoices only | 557 | grouped by `SRCEAPPL` |
| `APVEN` rows total / active | 4,752 / 2,062 | `SWACTV=1` |
| GL accounts on AP distributions (formatted) | **297** | `COUNT(DISTINCT RTRIM(IDGLACCT))` |
| …collapsed to the natural account (before the `-unit` suffix) | 130 | `LEFT(…, CHARINDEX('-',…)-1)` |
| GL accounts touched by `AP-IN` legs in `GLPOST` | 310 | |
| GL accounts touched by `PO-RC` legs | 19 | |
| distinct items on PO invoice lines | **17,129** | `COUNT(DISTINCT RTRIM(ITEMNO))` |
| distinct (item, order-unit) pairs | 17,364 | |
| inventory locations | 23 | |
| AP distribution lines (types 12/22/32) | 69,713 | |
| PO invoice lines | 32,586 | |
| PO additional-cost/service lines (`POINVS`) | 2,563 on 2,264 headers | |

### 6.5 Paid vs open vs partially paid

**How to tell:** `APOBL.SWPAID` (0/1) plus `APOBL.AMTDUEHC` (remaining balance) plus
`APOBL.DATEPAID`. `SWPAID=1` ⇔ `AMTDUEHC=0` in every one of the 30,827 invoices in the window.

```sql
SELECT CASE WHEN SWPAID=1 THEN 'fully paid'
            WHEN AMTDUEHC = AMTINVCHC THEN 'open, untouched'
            WHEN ABS(AMTDUEHC) < ABS(AMTINVCHC) THEN 'partially settled'
            ELSE 'other' END AS status,
       COUNT(*) n, CAST(SUM(AMTINVCHC) AS decimal(20,2)) invoiced,
       CAST(SUM(AMTDUEHC) AS decimal(20,2)) still_due
FROM APOBL WHERE DATEINVC BETWEEN 20260101 AND 20260430 AND IDTRXTYPE=12 GROUP BY …;
```

| status | docs | invoiced INR | still due INR |
|---|---:|---:|---:|
| fully paid (`SWPAID=1`) | **30,138** (97.8 %) | 3,210,883,096.18 | 0.00 |
| open, untouched | 483 | 415,318,514.53 | 415,318,514.53 |
| partially settled | 205 | 80,337,987.69 | 13,710,689.02 |
| **other (due > invoiced)** | **1** | 65,587.20 | **87,646.80** |

Same test on notes: 112 of 137 debit notes and 1,680 of 2,066 credit notes are settled;
all 3,274 payments and 1,514 of 1,729 prepayments show `SWPAID=1`.

### 6.6 PO-module document volumes in the window

| document | table | count |
|---|---|---:|
| Requisitions | `PORQNH1` | 6,644 |
| Purchase orders | `POPORH1` | 9,708 |
| **Receipts / GRN** | `PORCPH1` | **19,327** |
| PO invoices | `POINVH1` | 18,047 |
| PO returns | `PORETH1` | 191 |
| PO credit/debit notes | `POCRNH1` | 439 |

Note the shape: ~2 receipts per PO, and roughly 1 invoice per receipt. Also:

* **3,653 of 18,045** PO-matched invoices reference a PO raised **before** the window
  (earliest `20240709`).
* 58 reference a receipt dated before the window (earliest `20250623`).
* Timing: 11,410 invoices are dated the same day as their receipt, 6,596 after, **41 before**.

---

## 7. Flow anomalies (reported, not fixed)

| # | Anomaly | Count | Value (INR) | SQL / evidence | Confidence |
|---:|---|---:|---:|---|---|
| 1 | **Type-12 "invoices" with no `APIBD` distribution, no `APIBH` header, posted through a `PY` (payment) batch.** Loan repayments, bank transfers, IOU settlements. | **1,079** | 295,107,559.76 | `NOT EXISTS(APIBD)` + `LEFT JOIN APIBH` (0 hits) + `APPJH.TYPEBTCH='PY'` (1,079/1,079) | VERIFIED |
| 2 | `POINVH1` headers with **no** matching `APOBL` type-12 obligation (natural key) | 132 | 2,382,705.90 | `NOT EXISTS` on (VDCODE, INVNUMBER) | VERIFIED |
| 3 | Receipts never invoiced (`ISINVOICED=0`) | 347 | 3,697,970.27 | `PORCPH1 WHERE ISINVOICED=0` | VERIFIED |
| 4 | **AP documents with no GL posting** via the drilldown key | 3 invoices + 2 credit notes | — | 30,824/30,827 and 2,064/2,066 matched | VERIFIED |
| 5 | Zero-gross type-12 invoices (all `SRCEAPPL='PO'`) | 3 | 0.00 | `AMTINVCHC=0` | VERIFIED |
| 6 | **`DATEPAID` earlier than `DATEINVC`** | 3,467 inv + 81 DN + 11 CN | — | `SWPAID=1 AND DATEPAID>0 AND DATEPAID<DATEINVC` | VERIFIED |
| 7 | **Posting date ≠ document date** | 18,005 inv (58 %) | — | `DATEBUS<>DATEINVC`, delta −89…+310 | VERIFIED |
| 8 | Fiscal period does not match the document date's period | 16 | — | explicit month→FISCYR/FISCPER test | VERIFIED |
| 9 | AP-direct invoices touching a non-`4E*`/`2A7T*`/`1L8TX14-16` account (fixed assets, TDS, other liabilities, bank) | 293 | — | purity-filter `EXISTS` | VERIFIED |
| 10 | **Credit notes that are journals**: `IDGR004/007/010` "AP TO GL TRANSFER", "TRANSFERED TO GL ACCOU", "IDEPL 7 BALANCE TRANS" | ≥5 seen | −112,236,970.82 / −66,928,531 / −64,269,334 / −35,734,025 / −2,756,600 | top-5 by `ABS(AMTINVCHC)` | VERIFIED |
| 11 | `RMB` and `CNY` used as separate codes for the same currency | 156 invoices | — | §6.3 | VERIFIED |
| 12 | PO invoice headers split per receipt with a `*N` suffix | **6,421 of 18,047** headers → 14,602 logical bills | — | `CHARINDEX('*', INVNUMBER)` | VERIFIED |
| 13 | Invoice numbers with a **leading space** (Sage treats `' X'` and `'X'` as different obligations) | 8 in window, 386 all-time | — | `LEFT(IDINVC,1)=' '` | VERIFIED |
| 14 | `SRCEAPPL='PO'` invoice whose `IDPONBR` does not resolve to `POPORH1` | 1 | — | `LEFT JOIN POPORH1` 18,045/18,046 | VERIFIED |
| 15 | `AMTDUEHC` greater than `AMTINVCHC` | 1 | due 87,646.80 vs invoiced 65,587.20 | §6.5 "other" bucket | VERIFIED |
| 16 | `POPORH1.HASRQNDATA=0` on 8,953 POs of which **7,815 do have** a line with `RQNHSEQ<>0` | 7,815 | — | §Hop 1 vs `HASRQNDATA` cross-tab | VERIFIED |
| 17 | Unbalanced `GLJEH` entry `042090/00001` "TDS REVERSAL ENTRY" (`JRNLDR` 12,320 / `JRNLCR` 0) | 1 | 12,320.00 | present in `GLJEH`, **absent from `GLPOST`** — an unposted/error batch | VERIFIED |
| 18 | PO invoice line disagreeing with its receipt line on qty or unit cost | 1 of 32,586 | — | three-way-match query, §Hop 4 | VERIFIED |
| 19 | `POINVL.GLACEXPENS` and `GLNONSTKCR` blank on **all 32,586** lines — the inventory account is not on the document | 32,586 | — | `SUM(CASE WHEN RTRIM(...)<>'' …)` = 0 | VERIFIED |

**Things that are notably clean** (also worth knowing):

* **Zero** documents where `SUM(APIBD.AMTDISTHC) + AMTTAXHC ≠ AMTINVCHC` (tolerance 0.01),
  once the 1,079 no-distribution documents are excluded. Distributions tie to the paisa.
* **Zero** unbalanced posted GL entries in the window (~700,000 legs).
* **Zero** input tax expensed — 100 % recoverable.
* **Zero** duplicate `(IDVEND, IDINVC)` pairs among type-12 invoices in the window.

---

## 8. What I could NOT prove

| # | Claim | Status | Why |
|---:|---|---|---|
| 1 | The exact semantics of `GLPOST.DRILLDWNLK`'s `55` prefix, and whether the formula survives `POSTSEQNCE ≥ 100000` | **UNKNOWN** | The formula matched 99.99 % of the window (all `POSTSEQNCE` values there are 5 digits). The digit positions are inferred from arithmetic, not from Sage documentation. A 6-digit `POSTSEQNCE` may shift the packing. **Use `APPJH.GLBATCH`/`GLENTRY` as the primary link and the packed key only as a cross-check.** |
| 2 | Meaning of `APOBP.TRANSTYPE` 6, 7, 14, 16, 17 | **UNKNOWN** | 11 = payment and 10 = prepayment/apply are proved by trace; 8/9 are a matched cross-reference pair. The rest I only counted. |
| 3 | Meaning of `ICHIST` `IC` transaction types 6–15, 20 | **UNKNOWN** | Only `PO/1` (receipt), `PO/2` (invoice value adjustment) and `PO/3` (return) were proved. The `IC/12,13` pair (4,010,441 rows each, equal and opposite) and `IC/14,15` look like assembly/disassembly but I did not verify. |
| 4 | Whether `ICIVAL` (16.5 M rows) is required to reconstruct stock value, or `ICHIST` suffices | **UNKNOWN** | I proved the movement record in `ICHIST` only. `ICIVAL` carries costing buckets (`RECENTCOST`, `LASTCOST`, `STDCOST`) that I did not exercise. |
| 5 | Whether the 132 `POINVH1` headers with no `APOBL` obligation are unposted, reversed or genuinely orphaned | **UNKNOWN** | I counted them; I did not open one. |
| 6 | Where the `1L6T*` clearing balance is finally relieved for the 347 uninvoiced receipts | **UNKNOWN** | Not traced. |
| 7 | The identity of the "I/E Invoice Bill" feeder named in `APOBL.DESCINVC` on AP-direct documents | **INFERRED / LOW** | No `I/E` module exists in the schema. It is likely an external system or a Sage macro. |
| 8 | `APOBS` (787,468 rows) content and purpose | **UNKNOWN** | Row count is 3 higher than `APOBL` (787,465), suggesting a 1:1 statistics sibling, but I did not read it. |
| 9 | Whether the 41 invoices dated before their receipt are back-dated documents or data entry errors | **UNKNOWN** | Counted, not investigated. |
| 10 | Whether `SWPAID=1` can ever coexist with a non-zero `AMTDUEHC` outside the window | **UNKNOWN** | Inside the window the relationship is exact. |

**One project claim I could not reproduce (CONTRADICTED / HIGH):** the handover states
*"154 documents in this window carry leading/trailing whitespace"*. Live Sage shows **8** in the
window and **386** across all time (`LEFT(IDINVC,1)=' '`). Trailing whitespace is *undetectable*
in a `char(22)` column — it is indistinguishable from the column's own padding — so the "154"
figure cannot be reproduced by any SQL test I can construct. The underlying warning (never
`LTRIM` `IDINVC`) is still correct and still matters for those 8.

---

## 9. Open questions for the human

1. **The 1,079 payment-batch pseudo-invoices (₹295 M).** These are loan repayments and bank
   transfers that Sage records as `IDTRXTYPE=12` obligations with no distribution. They are
   currently excluded by the extract's "must have an `APIBD` row" filter, which is right — but
   are they supposed to arrive in SMEAssist at all, as anything? If they are simply dropped,
   ₹295 M of AP movement has no counterpart.

2. **The clearing-account problem.** On the PO path the AP invoice debits `1L6T*` A/P Clearing —
   a balance-sheet account — because the expense/inventory debit already happened at the
   receipt, which the migration does not carry. 16,531 distribution lines / ₹1,835,493,941.72
   in this window. Migrating the AP document alone books a balance-sheet head as if it were
   expense. Should the migration instead source the PO-matched expense from `POINVL` + the
   item's account set, or migrate receipts as a separate document class?

3. **The window straddles two fiscal years.** Apr-2026 is FY2027 P01 in Sage. Does the target
   system's period logic agree, and is the FY boundary inside the window deliberate?

4. **`RMB` vs `CNY`.** 156 invoices. Should they be folded to one currency before load, and at
   which rate — they have identical rate ranges but are separately coded.

5. **Foreign currency is lost in GL.** `GLPOST.SCURNCODE` is `'INR'` and `CONVRATE` is `1.0`
   even on USD documents. If reconciliation is GL-first, 2,041 foreign-currency invoices
   (₹656 M home value) will reconcile in INR only. Is that acceptable for customs/BOE?

6. **Receipt-split invoices.** 6,421 headers carry a `*N` suffix; 18,047 Sage headers are
   14,602 real vendor bills. Should SMEAssist receive 18,047 documents (matching Sage's
   obligations, and therefore its AP ageing) or 14,602 (matching the vendor's actual paper)?

7. **RCM rate is not stored.** `RATETAX1..5` is 0 on effectively every RCM line; the rate has to
   be derived from the `1L8TX` amount against the `4E*` base. Who signs off on the derived rate
   for the 1,140 affected documents, and what is the tolerance?

8. **293 AP-direct documents touch balance-sheet accounts** (fixed assets, TDS, bank, other
   liabilities). The extract's purity filter drops them. Confirm they should be dropped rather
   than routed somewhere.

9. **Credit notes carrying GST reverse Input GST in Sage** (`2A7TX03` negative). If the target
   posts a purchase debit note to Output GST instead, GSTR-3B blocks move even though cash is
   neutral. Has this been agreed with the tax team for the 428 PO-sourced and 1,629 AP-direct
   credit notes in the window?

10. **The requisition layer (88 % of POs).** It is currently invisible to the migration. Is that
    intentional, or is requisition-to-PO traceability a requirement in the target system?

11. **3,467 invoices show `DATEPAID` before `DATEINVC`.** Is that a legitimate business pattern
    (advance paid, invoice raised later, applied retrospectively) or a data-quality problem
    that will confuse any ageing report built from these dates?

---

*Prepared by Agent 2 (Sage Business Flow Analyst). All SQL in this document is read-only and was
executed against `IDEDAT` between the times of this session. No migration script was run; no
row was written to any database.*
