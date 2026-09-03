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
