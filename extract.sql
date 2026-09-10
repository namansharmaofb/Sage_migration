-- ============================================================================
-- Sage 300 (IDEDAT) extraction queries for the SMEAssist migration.
--
-- This file runs FROM Linux. pull.py reads it, splits it on the @@name
-- markers, and sends each query to SQL Server over port 1433 as the read-only
-- login named in $SQL_USER.
-- Each @@name becomes output/<name>.csv.
--
-- PROVENANCE: the WHERE/CTE logic below is carried over VERBATIM from the
-- queries that produced the data actually posted to the devbox (handover
-- sections 2.a.1 and 2.c). Do not "tidy" the filters - every predicate is
-- deliberate and is annotated where it is not obvious.
--
-- WHAT CHANGED FROM THE WINDOWS VERSION, AND WHY:
--   The original queries concatenated every column into one string with a
--   '~' separator, because sqlcmd's -s option takes a single character and
--   bcp needed a multi-char terminator that no vendor field contained.
--   pymssql returns real typed columns and Python's csv module quotes
--   properly, so the concatenation, the '~'->'-' substitution and the
--   ','->' ' substitution are all gone. Field content is now HIGHER fidelity
--   than the proven run: commas and tildes in descriptions survive intact.
--   CR/LF/TAB stripping is KEPT - a newline inside a Sage description once
--   truncated a bill line and forced a revoke-and-repost.
--
-- WINDOW: 1 Jan 2026 - 30 Apr 2026, the window everything loaded on.
--         Sage dates are 8-digit yyyymmdd integers, not a date type.
-- ============================================================================


-- ============================================================================
-- AP-DIRECT (ADHOC) BILLS
-- APOBL + APIBD, SRCEAPPL='AP'. No item lines - Sage's AP module stores money,
-- not goods, so the only content is a GL account and an amount.
-- Expected: 11,256 header rows / 46,385 line rows.
-- ============================================================================

-- @@name: bills_header
SET NOCOUNT ON;
WITH b AS (
    SELECT * FROM APOBL
     WHERE IDTRXTYPE = 12          -- 12=invoice (NOT 1: an earlier script used 1 and matched zero rows)
       AND SRCEAPPL  = 'AP'        -- AP-direct only; 'PO' is the PO-matched population
       AND DATEINVC BETWEEN 20260101 AND 20260430
),
f AS (
    SELECT b.* FROM b
     WHERE EXISTS (SELECT 1 FROM APIBD d
                    WHERE d.CNTBTCH = b.CNTBTCH AND d.CNTITEM = b.CNTITEM)
       -- a bill with no distribution lines cannot be shaped; bill create
       -- rejects an empty line list. 1,071 bills fail this.
       AND RTRIM(b.CODECURN) = 'INR'                                    -- 39 excluded
       AND RTRIM(b.CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')  -- pre-GST legacy, 114 excluded
       -- THE PURITY FILTER: reject any document whose legs touch anything that
       -- is not 4E* (expense), 2A7T* (tolerated balance-sheet head) or the
       -- 1L8TX14/15/16 RCM payable accounts. Those are journals wearing an
       -- invoice's clothes - fixed assets, TDS deductions, other liabilities.
       AND NOT EXISTS (
           SELECT 1 FROM APIBD d
            WHERE d.CNTBTCH = b.CNTBTCH AND d.CNTITEM = b.CNTITEM
              AND LEFT(RTRIM(d.IDGLACCT),2) <> '4E'
              AND LEFT(RTRIM(d.IDGLACCT),4) <> '2A7T'
              AND LEFT(RTRIM(d.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))
)
SELECT
    RTRIM(f.IDVEND)                        AS vendor,
    RTRIM(f.IDINVC)                        AS invoice,
    -- RTRIM only. Never LTRIM: Sage holds ' WPL/25-26/07516' and
    -- 'WPL/25-26/07516' as two separate obligations with different balances.
    -- 154 documents in this window carry leading/trailing whitespace.
    f.DATEINVC                             AS bill_date,
    f.DATEINVCDU                           AS due_date,
    CAST(f.AMTINVCHC AS decimal(18,2))     AS gross,
    CAST(f.AMTTAXHC  AS decimal(18,2))     AS header_tax,
    -- ZERO on RCM bills. On a genuine reverse-charge bill the tax exists only
    -- as negative amounts on the 1L8TX14/15/16 lines. Never read RCM tax here.
    RTRIM(f.CODETAXGRP)                    AS tax_group,   -- LOCAL=intra-state, INTERSTATE
    f.FISCYR                               AS fisc_year    -- reference only, never filtered on
FROM f
ORDER BY f.DATEINVC, f.IDVEND;


-- @@name: bills_lines
SET NOCOUNT ON;
WITH b AS (
    SELECT * FROM APOBL
     WHERE IDTRXTYPE = 12
       AND SRCEAPPL  = 'AP'
       AND DATEINVC BETWEEN 20260101 AND 20260430
),
f AS (
    SELECT b.* FROM b
     WHERE EXISTS (SELECT 1 FROM APIBD d
                    WHERE d.CNTBTCH = b.CNTBTCH AND d.CNTITEM = b.CNTITEM)
       AND RTRIM(b.CODECURN) = 'INR'
       AND RTRIM(b.CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')
       AND NOT EXISTS (
           SELECT 1 FROM APIBD d
            WHERE d.CNTBTCH = b.CNTBTCH AND d.CNTITEM = b.CNTITEM
              AND LEFT(RTRIM(d.IDGLACCT),2) <> '4E'
              AND LEFT(RTRIM(d.IDGLACCT),4) <> '2A7T'
              AND LEFT(RTRIM(d.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))
)
SELECT
    RTRIM(f.IDVEND)  AS vendor,
    RTRIM(f.IDINVC)  AS invoice,
    -- Strip Sage's manufacturing-unit suffix: 4E3EB01-IDEPL-1 -> 4E3EB01.
    -- Products are keyed on the natural account.
    LEFT(RTRIM(d.IDGLACCT),
         CASE WHEN CHARINDEX('-', RTRIM(d.IDGLACCT)) > 0
              THEN CHARINDEX('-', RTRIM(d.IDGLACCT)) - 1
              ELSE LEN(RTRIM(d.IDGLACCT)) END)   AS gl_account,
    CAST(d.AMTDIST AS decimal(18,2))             AS amount,      -- PRE-TAX
    -- The only record of what was bought: IDITEM is empty on every AP-direct
    -- line (0 of 178,592 FY2026 lines populated).
    REPLACE(REPLACE(REPLACE(RTRIM(d.TEXTDESC), CHAR(13),' '), CHAR(10),' '), CHAR(9),' ')
                                                 AS description
FROM APIBD d
JOIN f ON d.CNTBTCH = f.CNTBTCH AND d.CNTITEM = f.CNTITEM
-- APIBD has NO IDVEND and NO IDINVC. Its key is (CNTBTCH,CNTITEM,CNTLINE) and
-- the same invoice number appears in more than one batch, so any staging table
-- keyed on (vendor,invoice,line) silently loses rows.
ORDER BY f.IDVEND, f.IDINVC, d.CNTLINE;


-- ============================================================================
-- CREDIT / DEBIT NOTES
-- Same tables as bills, differing only by IDTRXTYPE. 22=Sage debit note,
-- 32=Sage credit note.
--
-- The proven query carried "AND IDVEND IN (<list>)" sourced from a notevend.txt
-- that no longer exists. Per the handover that predicate is REMOVED here to
-- widen to the full population, so these counts will EXCEED the 96 notes that
-- were posted. That is expected, not a regression.
-- ============================================================================

-- @@name: notes_header
SET NOCOUNT ON;
WITH n AS (
    SELECT * FROM APOBL
     WHERE IDTRXTYPE IN (22,32)
       AND DATEINVC BETWEEN 20260101 AND 20260430
       AND SRCEAPPL = 'AP'
       AND RTRIM(CODECURN) = 'INR'
),
f AS (
    SELECT n.* FROM n
     -- at least one expense line ...
     WHERE (SELECT COUNT(*) FROM APIBD d
             WHERE d.CNTBTCH = n.CNTBTCH AND d.CNTITEM = n.CNTITEM
               AND LEFT(RTRIM(d.IDGLACCT),2) = '4E') > 0
       -- ... and nothing outside 4E*/2A7T*. Of 6,432 FY2026 "notes" only ~1,300
       -- are real purchase returns; ~3,600 are TDS memos, 29 intercompany
       -- transfers, 333 control/suspense, ~100 GST journals.
       AND (SELECT COUNT(*) FROM APIBD d
             WHERE d.CNTBTCH = n.CNTBTCH AND d.CNTITEM = n.CNTITEM
               AND LEFT(RTRIM(d.IDGLACCT),2) <> '4E'
               AND LEFT(RTRIM(d.IDGLACCT),4) <> '2A7T') = 0
       -- THE SCOPE GUARD. Admits Sage DN(22) - which become SMEAssist
       -- CREDIT_NOTE and are unaffected - plus zero-tax notes. Sage CN(32)
       -- carrying GST is deliberately held: an ADHOC purchase DEBIT_NOTE posts
       -- GST to Output GST instead of reversing Input GST, while Sage reversed
       -- Input GST on all 1,873 FY2026 credit-note GST legs and the returns
       -- were filed that way. Cash-neutral but moves GSTR-3B blocks.
       AND (n.IDTRXTYPE = 22 OR n.AMTTAXHC = 0)
)
SELECT
    RTRIM(f.IDVEND)                             AS vendor,
    REPLACE(REPLACE(RTRIM(f.IDINVC), CHAR(13),' '), CHAR(10),' ')  AS note_number,
    f.IDTRXTYPE                                 AS sage_type,   -- 22=DN, 32=CN
    f.DATEINVC                                  AS note_date,
    -- Sage stores notes as NEGATIVE. ABS() everywhere; the type carries the
    -- direction. Never send negatives to SMEAssist.
    CAST(ABS(f.AMTINVCHC) AS decimal(18,2))     AS total,
    CAST(ABS(f.AMTTAXHC)  AS decimal(18,2))     AS tax,
    RTRIM(f.CODETAXGRP)                         AS tax_group
FROM f
ORDER BY f.IDVEND, f.IDINVC;


-- @@name: notes_lines
SET NOCOUNT ON;
WITH n AS (
    SELECT * FROM APOBL
     WHERE IDTRXTYPE IN (22,32)
       AND DATEINVC BETWEEN 20260101 AND 20260430
       AND SRCEAPPL = 'AP'
       AND RTRIM(CODECURN) = 'INR'
),
f AS (
    SELECT n.* FROM n
     WHERE (SELECT COUNT(*) FROM APIBD d
             WHERE d.CNTBTCH = n.CNTBTCH AND d.CNTITEM = n.CNTITEM
               AND LEFT(RTRIM(d.IDGLACCT),2) = '4E') > 0
       AND (SELECT COUNT(*) FROM APIBD d
             WHERE d.CNTBTCH = n.CNTBTCH AND d.CNTITEM = n.CNTITEM
               AND LEFT(RTRIM(d.IDGLACCT),2) <> '4E'
               AND LEFT(RTRIM(d.IDGLACCT),4) <> '2A7T') = 0
       AND (n.IDTRXTYPE = 22 OR n.AMTTAXHC = 0)
)
SELECT
    RTRIM(f.IDVEND)  AS vendor,
    REPLACE(REPLACE(RTRIM(f.IDINVC), CHAR(13),' '), CHAR(10),' ')  AS note_number,
    LEFT(RTRIM(d.IDGLACCT),
         CASE WHEN CHARINDEX('-', RTRIM(d.IDGLACCT)) > 0
              THEN CHARINDEX('-', RTRIM(d.IDGLACCT)) - 1
              ELSE LEN(RTRIM(d.IDGLACCT)) END)  AS gl_account,
    CAST(ABS(d.AMTDIST) AS decimal(18,2))       AS amount,
    REPLACE(REPLACE(REPLACE(RTRIM(d.TEXTDESC), CHAR(13),' '), CHAR(10),' '), CHAR(9),' ')
                                                AS description
FROM APIBD d
JOIN f ON d.CNTBTCH = f.CNTBTCH AND d.CNTITEM = f.CNTITEM
WHERE LEFT(RTRIM(d.IDGLACCT),2) = '4E'
ORDER BY f.IDVEND, f.IDINVC, d.CNTLINE;


-- ============================================================================
-- VENDOR MASTER
-- Every vendor referenced by a bill or note in the window. Feeds contact
-- creation. Note the key column is APVEN.VENDORID - APOBL calls the same thing
-- IDVEND, and mixing them up joins nothing.
--
-- BRN is a generic "business registration number" column: it holds GSTINs, but
-- also pre-GST VAT TINs and free text like "TIN - 29480145830". It is emitted
-- RAW here; the GSTIN shape test belongs in the transform, never in SQL.
-- ============================================================================

-- @@name: vendors
SET NOCOUNT ON;
WITH docs AS (
    SELECT DISTINCT RTRIM(IDVEND) AS vendor
      FROM APOBL
     WHERE IDTRXTYPE IN (12,22,32)
       AND SRCEAPPL = 'AP'
       AND DATEINVC BETWEEN 20260101 AND 20260430
       AND RTRIM(CODECURN) = 'INR'
)
SELECT
    RTRIM(v.VENDORID)    AS vendor,
    REPLACE(REPLACE(RTRIM(v.VENDNAME), CHAR(13),' '), CHAR(10),' ')   AS name,
    REPLACE(REPLACE(RTRIM(v.LEGALNAME),CHAR(13),' '), CHAR(10),' ')   AS legal_name,
    RTRIM(v.BRN)         AS brn,
    REPLACE(REPLACE(RTRIM(v.TEXTSTRE1),CHAR(13),' '), CHAR(10),' ')   AS street1,
    REPLACE(REPLACE(RTRIM(v.TEXTSTRE2),CHAR(13),' '), CHAR(10),' ')   AS street2,
    REPLACE(REPLACE(RTRIM(v.TEXTSTRE3),CHAR(13),' '), CHAR(10),' ')   AS street3,
    REPLACE(REPLACE(RTRIM(v.TEXTSTRE4),CHAR(13),' '), CHAR(10),' ')   AS street4,
    RTRIM(v.NAMECITY)    AS city,
    RTRIM(v.CODESTTE)    AS state_text,   -- free text, unusable as-is
    RTRIM(v.CODEPSTL)    AS pincode,      -- often punctuated; normalise later
    RTRIM(v.CODECTRY)    AS country,
    RTRIM(v.NAMECTAC)    AS contact_person,
    v.SUBJTOWTHH         AS subject_to_withholding,  -- 0 for every Sage vendor
    v.SWACTV             AS is_active
FROM APVEN v
JOIN docs d ON d.vendor = RTRIM(v.VENDORID)
ORDER BY v.VENDORID;


-- ============================================================================
-- CHART OF ACCOUNTS (expense heads only)
-- Supplies the product NAME for each synthetic SAGE-<account> CHARGE product.
-- ACCTFMTTD is the formatted code that joins to APIBD.IDGLACCT; ACCTID is the
-- unformatted one.
-- ============================================================================

-- @@name: gl_accounts
SET NOCOUNT ON;
SELECT
    RTRIM(ACCTID)      AS acct_id,
    RTRIM(ACCTFMTTD)   AS acct_formatted,
    REPLACE(REPLACE(RTRIM(ACCTDESC), CHAR(13),' '), CHAR(10),' ')  AS description,
    ACCTTYPE           AS acct_type,
    RTRIM(ACCTGRPCOD)  AS acct_group,
    ACTIVESW           AS is_active
FROM GLAMF
WHERE LEFT(RTRIM(ACCTID),2) = '4E'
ORDER BY ACCTID;


-- ============================================================================
-- CONTROL COUNTS - run these to prove the extract matches the proven run.
-- ============================================================================

-- @@name: control_counts
SET NOCOUNT ON;
WITH b AS (
    SELECT * FROM APOBL
     WHERE IDTRXTYPE = 12 AND SRCEAPPL = 'AP'
       AND DATEINVC BETWEEN 20260101 AND 20260430
),
f AS (
    SELECT b.* FROM b
     WHERE EXISTS (SELECT 1 FROM APIBD d
                    WHERE d.CNTBTCH = b.CNTBTCH AND d.CNTITEM = b.CNTITEM)
       AND RTRIM(b.CODECURN) = 'INR'
       AND RTRIM(b.CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')
       AND NOT EXISTS (
           SELECT 1 FROM APIBD d
            WHERE d.CNTBTCH = b.CNTBTCH AND d.CNTITEM = b.CNTITEM
              AND LEFT(RTRIM(d.IDGLACCT),2) <> '4E'
              AND LEFT(RTRIM(d.IDGLACCT),4) <> '2A7T'
              AND LEFT(RTRIM(d.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))
)
SELECT
    'ap_direct_bills'                            AS population,
    COUNT(*)                                     AS header_rows,
    CAST(SUM(f.AMTINVCHC) AS decimal(18,2))      AS gross_total,
    CAST(SUM(f.AMTTAXHC)  AS decimal(18,2))      AS tax_total,
    COUNT(DISTINCT RTRIM(f.IDVEND))              AS distinct_vendors
FROM f;


-- ============================================================================
-- AR (SALES) INVOICES - the receivable side, loaded by post_sage_invoices.py
--
-- READ THIS BEFORE TRUSTING THE COLUMN NAMES BELOW.
--
-- Everything above this line was written against a schema inventoried column
-- by column (migration-analysis/sage/01-sage-schema-inventory.md), and that
-- inventory records AR (54 tables) and OE (37 tables) as OUT OF SCOPE - the
-- migration moved payables only. The names in this section therefore come
-- from the AR migration brief, NOT from that inventory. They are ASSUMED.
--
-- They have since been CHECKED against the live company database by
-- `./post_sage_invoices.py ar-probe`, and the names below are the corrected
-- ones. Six of the brief's names were wrong, and the probe is what found them:
--   AROBL.IDTRXTYPE      -> TRXTYPEID (an invoice is 12 or 14, not 1, and the
--                           pair splits by SRCEAPPL: 12/AR is keyed straight
--                           into A/R, 14/OE comes from Order Entry)
--   ARCUS.LEGALNAME      -> does not exist; BRN carries the GSTIN (188 of 431
--                           customers), and IDTAXREGI1..5 are empty on ALL rows
--   OEINVD.INVDLINE      -> LINENUM
--   OEINVD.UNIT          -> INVUNIT
--   OEINVD.EXTINVNET     -> EXTINVMISC (the extended amount, despite the name)
--   OEINVD.RATETAX1      -> TRATE1
--   IESHPRGH.*           -> INVDOCNUM, PRTOFLOAD, SBNO, SBDATE, and it carries
--                           NO country at all - the destination comes from
--                           OEINVH.SHPCOUNTRY
-- Re-run ar-probe on any other company database before trusting them there.
--
-- WINDOW: unlike the AP queries, these are not bounded by a date range but by
-- the CUTOVER. AROBL holds the open documents; one still open at cutover is a
-- receivable the new system has to carry, so it loads as an invoice. One
-- settled before cutover belongs to the opening trial balance and must NOT be
-- re-posted - posting it would recognise its revenue a second time.
-- ============================================================================

-- @@name: ar_invoices_header
SET NOCOUNT ON;
SELECT
    RTRIM(o.IDCUST)                     AS customer,
    -- RTRIM only. Never LTRIM - see the note on bills_header: Sage holds
    -- ' WPL/25-26/07516' and 'WPL/25-26/07516' as two separate obligations
    -- with different balances.
    RTRIM(o.IDINVC)                     AS invoice,
    o.DATEINVC                          AS inv_date,
    o.DATEDUE                           AS due_date,
    CAST(o.AMTINVCHC AS decimal(19,4))  AS home_total,   -- home currency; ties the voucher
    CAST(o.AMTDUEHC  AS decimal(19,4))  AS home_open,    -- remaining; > 0 = unpaid
    RTRIM(o.CODECURN)                   AS currency,
    CAST(h.INVNETWTX AS decimal(19,4))  AS doc_total,    -- ASSUMED
    CAST(h.INRATE    AS decimal(19,7))  AS fx_rate,
    -- The export's destination. IESHPRGH has no country column at all.
    RTRIM(h.SHPCOUNTRY)                 AS dest_country,
    RTRIM(o.SRCEAPPL)                   AS srce,          -- AR-direct vs OE
    h.INVUNIQ                           AS oe_uniq
FROM AROBL o
-- LEFT JOIN, not JOIN: an invoice keyed straight into AR has no O/E header at
-- all. Those come back with a null rate and are HELD by the loader's own
-- identity check rather than silently dropped by an inner join, so they stay
-- countable in the reconciliation.
LEFT JOIN OEINVH h ON RTRIM(h.INVNUMBER) = RTRIM(o.IDINVC)
-- Both values are invoices (TRXTYPETXT '1'). The 12/AR ones have no O/E lines
-- and cannot be shaped, but they are admitted here ANYWAY so they stay
-- countable in the reconciliation instead of vanishing behind a narrower
-- predicate. Measured: 149,739 OE rows (1,827 open) / 28,983 AR rows (87 open).
WHERE o.TRXTYPEID IN (12, 14)
  AND o.DATEINVC  < 20260401     -- the cutover
  AND o.AMTDUEHC  > 0            -- unpaid IS a positive remaining balance
ORDER BY o.DATEINVC, o.IDCUST, o.IDINVC;


-- @@name: ar_invoices_lines
SET NOCOUNT ON;
SELECT
    RTRIM(o.IDCUST)                     AS customer,
    RTRIM(o.IDINVC)                     AS invoice,
    d.LINENUM                           AS line_no,
    RTRIM(d.ITEM)                       AS item,
    -- DESC is a reserved word, hence the brackets. CR/LF/TAB stripped for the
    -- same reason as on the AP lines: a newline inside a description once
    -- truncated a line and forced a revoke-and-repost.
    REPLACE(REPLACE(REPLACE(RTRIM(d.[DESC]), CHAR(13),' '), CHAR(10),' '), CHAR(9),' ')
                                        AS descr,
    RTRIM(d.INVUNIT)                    AS um,
    CAST(d.QTYSHIPPED AS decimal(19,4)) AS qty,
    CAST(d.UNITPRICE  AS decimal(19,6)) AS price,
    -- The extended amount, despite the name: verified line for line against
    -- the proven invoice, where 1612 x 4.17 = 6722.04 sits in EXTINVMISC and
    -- EXTOVER, TBASE1 and PRIAMOUNT are all zero.
    CAST(d.EXTINVMISC AS decimal(19,4)) AS ext,          -- pre-tax
    -- Cost of goods sold, ALREADY IN THE HOME CURRENCY - the proven invoice's
    -- four lines sum to INR 631,209.46 on a USD 26,405.41 document, so it must
    -- never be multiplied by the rate. Carried for reference and stamped into
    -- the line's metaData; NOT posted. A sales invoice books revenue and the
    -- debtor, not COGS - that is inventory's own journal.
    CAST(d.EXTICOST   AS decimal(19,4)) AS cogs,
    -- The rate Sage STATES. Defect 4.1 applies here exactly as on the AP
    -- side: read the rate, never divide tax by taxable to infer one.
    CAST(d.TRATE1     AS decimal(9,4))  AS rate_tax1,
    RTRIM(i.CATEGORY)                   AS category,
    RTRIM(i.ITEMNO)                     AS item_raw
FROM OEINVD d
JOIN OEINVH h ON h.INVUNIQ = d.INVUNIQ
JOIN AROBL  o ON RTRIM(o.IDINVC) = RTRIM(h.INVNUMBER) AND o.TRXTYPEID IN (12, 14)
-- On the unformatted number: OE states the formatted item ('ID-41354-X') and
-- ICITEM.ITEMNO holds it without separators, exactly as the goods path found.
LEFT JOIN ICITEM i ON RTRIM(i.ITEMNO) = REPLACE(RTRIM(d.ITEM), '-', '')
WHERE o.DATEINVC < 20260401
  AND o.AMTDUEHC > 0
ORDER BY o.IDCUST, o.IDINVC, d.LINENUM;


-- @@name: customers
-- The aliases are chosen to match the key names post_sage_bills.py's
-- registration_of() / resolve_state() / platform_country() already read off a
-- VENDOR row. That is deliberate: it lets a customer's registration, state and
-- country be resolved by the same evidence-first rules - GSTIN prefix wins,
-- free-text CODESTTE is corroborated not trusted - instead of a second, weaker
-- copy of them. Do not rename these to match ARCUS.
SET NOCOUNT ON;
SELECT
    RTRIM(c.IDCUST)    AS customer,
    RTRIM(c.NAMECUST)  AS name,
    RTRIM(c.TEXTSTRE1) AS street1,
    RTRIM(c.TEXTSTRE2) AS street2,
    RTRIM(c.TEXTSTRE3) AS street3,
    RTRIM(c.TEXTSTRE4) AS street4,
    RTRIM(c.NAMECITY)  AS city,
    RTRIM(c.CODESTTE)  AS state_raw,
    RTRIM(c.CODEPSTL)  AS pincode,
    RTRIM(c.CODECTRY)  AS country,
    RTRIM(c.NAMECTAC)  AS contact_person,
    RTRIM(c.TEXTPHON1) AS phone1,
    RTRIM(c.EMAIL1)    AS email1,
    -- BRN carries the GSTIN, as APVEN.BRN does on the payables side: 188 of
    -- 431 customers hold a well-formed one, and IDTAXREGI1..5 are populated on
    -- zero rows. ARCUS has no legal-name column, so legal_name is a literal -
    -- registration_of() then reads the key it expects and finds it empty.
    RTRIM(c.BRN)           AS brn_raw,
    CAST('' AS varchar(1)) AS legal_name,
    RTRIM(c.CODECURN)  AS currency
FROM ARCUS c
ORDER BY c.IDCUST;


-- @@name: ar_export_register
-- The export shipping register from the India localisation. No inventory in
-- this repo covers it, and the loader treats it as best-effort for that reason
-- - a rename here holds the exports with a precise reason instead of stopping
-- the whole load. The loading port and shipping bill are stated on an export,
-- so it cannot be posted without them. It carries NO destination country;
-- that comes from OEINVH.SHPCOUNTRY.
SET NOCOUNT ON;
SELECT RTRIM(g.INVDOCNUM) AS invoice,
       RTRIM(g.PRTOFLOAD) AS port_code,
       RTRIM(g.SBNO)      AS shipping_bill,
       g.SBDATE           AS shipping_bill_date
FROM IESHPRGH g
ORDER BY g.INVDOCNUM;


-- @@name: ar_control_counts
-- The receivable twin of control_counts: what the AR load has to tie to.
SET NOCOUNT ON;
SELECT
    'ar_open_at_cutover'                        AS population,
    COUNT(*)                                    AS header_rows,
    COUNT(DISTINCT RTRIM(o.IDCUST))             AS distinct_customers,
    CAST(SUM(o.AMTINVCHC) AS decimal(19,2))     AS invoiced_total,
    CAST(SUM(o.AMTDUEHC)  AS decimal(19,2))     AS open_total,
    MIN(o.DATEINVC)                             AS first_date,
    MAX(o.DATEINVC)                             AS last_date
FROM AROBL o
WHERE o.TRXTYPEID IN (12, 14) AND o.DATEINVC < 20260401 AND o.AMTDUEHC > 0;
