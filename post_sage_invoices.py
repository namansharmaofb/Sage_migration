#!/usr/bin/env python3
"""
Post Sage AR (sales) invoices into SMEAssist.

The sibling of post_sage_bills.py, which does the payable side. Everything not
specific to a sales document is IMPORTED from it rather than copied - the API
client and its shared rate gate, the GSTIN / state / pincode / HSN derivation,
the item-product builder, the crosswalk and the append-only posted log - so a
defect fixed on one side is fixed on both.

What is genuinely different here, and why this is not "bills with the sign
flipped":

  * A sales invoice is PRICED in the customer's currency and BOOKED in the
    org's. The voucher is always in rupees, so the document is posted in INR at
    rate 1 with every line carrying its converted amount, and Sage's own
    home-currency total (AROBL.AMTINVCHC) is the figure it must tie to. One
    line absorbs the conversion residual - see inr_lines().

    Read off the backend, not inferred. EntityVoucherEntryCreateHelperService
    builds the SALES voucher from getVoucherBreakageForInvoice(), and:

        private BigDecimal getNetItemAmount(InvoiceLineItemDto dto) {
            return multiply(dto.getQuantity(), dto.getUnitPrice());
        }

    invoiceDto.getConversionRate() does not appear ANYWHERE in that class - it
    is read by the controller, by credit/debit notes and by the commercial
    invoice converters, and by nothing on the voucher path. So a rate sent here
    is silently ignored and a document-currency amount would post as rupees.
    The same method is why unitPrice carries six decimals and why the residual
    is absorbed there: quantity x unitPrice IS the revenue leg.
  * The counterparty is a BUYER, a different ledger (Sundry Debtors) from the
    VENDOR ledger (Creditors) the bills path resolves. A party that both buys
    and sells is ONE contact carrying BOTH roles, never a second contact -
    POST /contact/ refuses one anyway on the duplicate GSTIN/PAN, and the
    payables crosswalk this repo already holds is where such a party is found.
  * An export carries no GST. Under a Letter of Undertaking it is
    EXPORT_WITHOUT_PAYMENT at 0%, and the document ASSERTS the LUT number - so
    an export with no LUT in evidence is held, not posted under an exemption
    nobody can defend.

SAGE SCHEMA WARNING - read before the first run
-----------------------------------------------
The AP side of this repo was written against a schema inventoried column by
column, and that inventory (migration-analysis/sage/01-sage-schema-inventory.md)
records AR and OE as OUT OF SCOPE. The AR/OE names below therefore come from
the migration brief, not from the inventory: they are ASSUMED, and every one is
registered in SCHEMA below and marked `# ASSUMED` at its use.

Two things follow, both deliberate:

  * `ar-probe` checks every assumed table and column against INFORMATION_SCHEMA
    and prints the gaps with candidate names, instead of letting a query die
    halfway through a load. Every load runs the same check first and refuses to
    start on a gap.
  * And the check that makes the assumption SAFE rather than merely visible:
    classify_invoice() re-multiplies Sage's own stated document total by its own
    stated exchange rate and requires the product to equal AROBL.AMTINVCHC to
    the paise. If INVNETWTX or INRATE is the wrong column, every invoice is HELD
    with the delta printed. None is posted at a figure Sage does not state.

Phases:
    ar-probe     verify the AR/OE schema, report the population, write no data
    ar-selftest  run the arithmetic against known figures - no Sage, no API
    ar-masters   buyer contacts (+ BUYER role and Debtors ledger) and products
    ar-dryrun    build every payload, post nothing, group the skip reasons
    ar-post      create + verify, resumable
    ar-recon     posted INR total against Sage's own AROBL open total
    ar-legs      one invoice's voucher legs, beside Sage's own

Usage:
    export SME_TOKEN=<fresh auth-token>          # never log in; ask for one
    ./post_sage_invoices.py ar-probe
    ./post_sage_invoices.py ar-selftest
    ./post_sage_invoices.py ar-dryrun  --docs IDK2526E39840
    ./post_sage_invoices.py ar-masters --docs IDK2526E39840
    ./post_sage_invoices.py ar-post    --docs IDK2526E39840
    ./post_sage_invoices.py ar-recon
"""
import argparse, collections, datetime, json, os, re, sys
from decimal import Decimal as D, ROUND_HALF_UP

import post_sage_bills as B
from post_sage_bills import (Api, LookupFailed, Stop, epoch_ms, financial_year,
                             q2, s, sage_date_parts, sage_query)

HERE, WORK = B.HERE, B.WORK
ORG_ID, ORG_PAN, ORG_GSTIN = B.ORG_ID, B.ORG_PAN, B.ORG_GSTIN


# ============================================================================
# CONFIG
# ============================================================================

# Its own pair of files. They must not be shared with the payables run: that
# crosswalk keys contacts by Sage VENDOR code and this one by CUSTOMER code,
# and one process saving both would truncate whichever population it was not
# holding. The AP crosswalk is still READ - see ap_party_index().
CROSSWALK  = os.path.join(WORK, "crosswalk_ar.json")
POSTED_LOG = os.path.join(WORK, "posted_ar.log")
AP_CROSSWALK = B.CROSSWALK

# The cutover decides what this loader is FOR. Sage's AROBL carries the open
# documents; a document still open at cutover is a receivable the new system has
# to carry, so it loads as an invoice and its debtor leg stays open. A document
# already settled before cutover is part of the opening trial balance and must
# NOT be re-posted - posting it would recognise its revenue twice.
CUTOVER = int(B.cfg("SME_CUTOVER", "20260401"))

# Sales invoices resolve their counterparty as a BUYER, and the buyer category
# is a DIFFERENT id from the vendor one post_sage_bills.py uses. Both are
# org-specific, like COMPANY_ADDRESS_ID; overridable so this file needs no edit
# to run against another org.
BUYER_CATEGORY = B.cfg("SME_BUYER_CATEGORY", "1029658153400144541")

# Revenue, not purchase. financeAccountReferenceMapping mints the ledger under
# an accounting group chosen by the mapping type, so ITEM_PURCHASE here would
# book every sale into Purchase Accounts. The endpoint ADDS a ledger rather
# than moving the existing one, so a product the goods run already created for
# the purchase side keeps that ledger and gains this one.
ITEM_LEDGER_MAPPING = "ITEM_SALE"

# The org's Letter of Undertaking, under which an export goes out without
# payment of IGST. The org's own tax registration detail, so it belongs in .env
# beside SME_ORG_GSTIN - not read from a Sage table this repo has never
# inventoried. `ar-probe` reports what CSCOM holds so the two can be compared
# by eye; nothing is inferred from it.
#
# KEYED BY FINANCIAL YEAR, because a LUT is granted for ONE year and this
# population is not one year. Measured on the open documents: 152 shapeable
# exports spread across FY2021-22 (1), FY2023-24 (26), FY2024-25 (10) and
# FY2025-26 (115). A single number stamped on all of them would assert, on a
# 2021 invoice, an exemption under a document that did not exist until 2026 -
# and the invoice carries that number as its own igstexemptionNumber, so it is
# not a cosmetic field. An export whose year has no LUT is HELD.
#
#   SME_LUT=2024-2025:AD2904xxxxxxxxx@20240401,2025-2026:AD2904yyyyyyyyy@20250401
#
# SME_LUT_NUMBER/SME_LUT_DATE still work and register for the ONE year
# SME_LUT_DATE falls in - which is what a single LUT actually covers.
def _parse_luts():
    """-> {financial year: (number, yyyymmdd)}. Never spans a year it was not
    granted for."""
    out = {}
    for chunk in s(B.cfg("SME_LUT")).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        fy, _, rest = chunk.partition(":")
        num, _, date = rest.partition("@")
        if not (fy.strip() and num.strip() and sage_date_parts(date.strip())):
            raise Stop("SME_LUT entry %r is not <financial-year>:<number>@<yyyymmdd>"
                       % chunk)
        out[fy.strip()] = (num.strip(), date.strip())
    num, date = s(B.cfg("SME_LUT_NUMBER")), s(B.cfg("SME_LUT_DATE"))
    if num and sage_date_parts(date):
        out.setdefault(financial_year(date), (num, date))
    return out


LUT_BY_FY = _parse_luts()

# Gemini address enrichment. Defined on the payables side and imported here so
# one fence, one prompt and one confidence gate serve both loaders - see
# B.resolve_address_ai for why it exists at all and how it is bounded.
GEMINI_KEY   = B.GEMINI_KEY
GEMINI_MODEL = B.GEMINI_MODEL
resolve_address_ai = B.resolve_address_ai

# Enough of a POC to satisfy the platform. pocInfo.pocName is load-bearing:
# a null one NPEs in PDF generation AFTER the invoice row is written, and the
# create is rolled back with an error that names nothing.
DEFAULT_POC = "Accounts"

# counterType per document kind, mirroring InvoiceServiceImpl:798-805 exactly:
#
#     CounterType counterType = CounterType.INVOICE;
#     if      (invoiceType    == TRANSFER_INVOICE) counterType = TRANSFER_INVOICE;
#     else if (invoiceSubType == ISD_INVOICE)      counterType = ISD_INVOICE;
#     else if (salesType      == INTERNATIONAL)    counterType = EXPORT_INVOICE;
#
# The domestic one is plain INVOICE. It was SALES_INVOICE here, guessed from
# the shape of the other names, and CounterType has no such constant - the
# server answered "No enum constant ...CounterType.SALES_INVOICE" on the first
# domestic document of the run. Note TRANSFER wins over EXPORT, which is why
# classify_invoice tests the PAN before the destination.
COUNTER_TYPE = {"EXPORT": "EXPORT_INVOICE",
                "DOMESTIC": "INVOICE",
                "TRANSFER": "TRANSFER_INVOICE"}


class CounterFailed(Exception):
    """This document kind cannot be numbered. A document-level problem, not a
    run-level one: every other kind in the selection is still postable, so it
    must not stop the run."""

# Export scrip sales do not belong on an invoice at all: a ROSCTL/RoDTEP scrip
# is a credit to an asset head (2A7SL20), not revenue against a buyer, so it
# posts as a journal voucher. Detected and held rather than quietly booked as a
# sale, which would overstate turnover.
SCRIP_RE = re.compile(r"(?i)\b(ROSCTL|RODTEP|MEIS|SEIS|DBK|DUTY\s*DRAW|SCRIP)\b")


# ============================================================================
# SAGE - READ ONLY. Every statement below is a SELECT.
#
# SCHEMA is the single registry of what this loader assumes exists. It is
# checked before every load and reported by ar-probe. When reality differs,
# fix the name HERE and in the query below it - the check will tell you if you
# miss one.
# ============================================================================

SCHEMA = {
    # Open receivables. The AP inventory documents APOBL in detail; AROBL is
    # its receivable twin and these are the columns that path's experience says
    # to expect. IDTRXTYPE is the one to confirm from DATA, not from memory:
    # the AP side records that an earlier script used IDTRXTYPE=1 for a payable
    # invoice and matched zero rows, because there it is 12. ar-probe prints the
    # distribution so the value below is settled by evidence.
    "AROBL": ["IDCUST", "IDINVC", "TRXTYPEID", "SRCEAPPL", "DATEINVC",
              "DATEDUE", "AMTINVCHC", "AMTDUEHC", "CODECURN"],
    # Customer master. Aliased in SQL_CUSTOMERS to exactly the key names
    # B.registration_of / B.resolve_state / B.platform_country already read, so
    # a customer resolves through the same evidence-first logic as a vendor.
    # BRN carries the GSTIN, exactly as APVEN.BRN does on the payables side -
    # settled by counting: 188 of 431 customers hold a well-formed GSTIN there,
    # and the India-localisation IDTAXREGI1..5 columns are populated on ZERO
    # rows. ARCUS has no LEGALNAME at all, so registration_of() falls back to
    # BRN alone and a customer with neither is WITHOUT_PAN_OR_GST - which is a
    # registration type, not a hold.
    "ARCUS":  ["IDCUST", "NAMECUST", "TEXTSTRE1", "TEXTSTRE2", "TEXTSTRE3",
               "TEXTSTRE4", "NAMECITY", "CODESTTE", "CODEPSTL", "CODECTRY",
               "NAMECTAC", "TEXTPHON1", "EMAIL1", "BRN", "CODECURN"],
    # O/E invoice header: the document currency, its total and its rate.
    # SHPCOUNTRY is the export's destination. It is NOT in the shipping
    # register, which the brief said carried it: IESHPRGH has no country column
    # at all. The ship-to country is where the goods went, which is the thing.
    "OEINVH": ["INVUNIQ", "INVNUMBER", "CUSTOMER", "INVDATE", "INRATE",
               "INVNETWTX", "SHPCOUNTRY"],
    # O/E invoice detail: the only record of what was actually sold.
    # EXTINVMISC is the extended amount despite the name, verified against the
    # proven invoice line for line: 1612 x 4.17 = 6722.04 sits in EXTINVMISC,
    # while EXTOVER, TBASE1 and PRIAMOUNT are all zero on it. TRATE1 is the
    # stated tax rate; LINENUM is Sage's own line number (32, 64, 96, 128).
    # Tax is stated PER AUTHORITY, not once: an intra-state sale carries
    # TRATE1=2.5 SGST and TRATE2=2.5 CGST, which is one 5% slab split in two.
    # Reading TRATE1 alone reported "2.50 is not a legal slab" on 22 of the 32
    # shapeable documents. TAMOUNT holds the tax Sage itself computed.
    "OEINVD": ["INVUNIQ", "LINENUM", "ITEM", "DESC", "INVUNIT", "QTYSHIPPED",
               "UNITPRICE", "EXTINVMISC", "EXTICOST",
               "TRATE1", "TRATE2", "TRATE3", "TRATE4", "TRATE5",
               "TAMOUNT1", "TAMOUNT2", "TAMOUNT3", "TAMOUNT4", "TAMOUNT5",
               "TAUTH1", "TAUTH2", "TAUTH3", "TAUTH4", "TAUTH5"],
    # Item master, for the category the product create needs. Shared with the
    # payables goods path, which is the point: one product per Sage item.
    "ICITEM": ["ITEMNO", "CATEGORY"],
}

# Least certain of the lot - an export shipping register from the India
# localisation, which no inventory in this repo covers. Kept OUT of SCHEMA so a
# rename holds only the exports (with a precise reason) instead of refusing the
# whole load, and ar-probe always dumps its real columns.
SCHEMA_BEST_EFFORT = {
    "IESHPRGH": ["INVDOCNUM", "PRTOFLOAD", "SBNO", "SBDATE"],
}

SQL_TABLE_COLUMNS = """
SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_NAME IN ('AROBL','ARCUS','OEINVH','OEINVD','ICITEM','IESHPRGH',
                      'AROBP','CSCOM')
"""

# One row per OPEN receivable at cutover. AMTDUEHC > 0 IS the definition of
# unpaid - a document Sage still shows a balance for. DATEINVC < CUTOVER keeps
# out anything raised after the changeover, which belongs to the new system, not
# to a migration.
#
# LEFT JOIN, not JOIN: an invoice keyed into AR directly has no OE header at
# all. Those come back with a null rate and are HELD by the rate check rather
# than silently dropped by an inner join, so they stay countable.
# TRXTYPEID 12 and 14 are BOTH invoices (TRXTYPETXT '1'); the pair splits by
# SRCEAPPL exactly as APOBL's did on the payables side - 12/AR is keyed
# straight into A/R, 14/OE comes from Order Entry. Only the OE ones have
# OEINVD lines, so only they can be shaped; the 12/AR ones are admitted here
# ANYWAY so they stay countable in the reconciliation and are held with a
# reason, rather than silently disappearing behind a narrower predicate.
# Measured: 149,739 OE rows (1,827 still open) against 28,983 AR rows (87 open).
#
# {OPEN} is filled in by load_ar_book: `AND o.AMTDUEHC > 0` normally, and
# nothing at all under --include-settled. See the flag's help.
SQL_HEADERS = """
SET NOCOUNT ON;
SELECT
    RTRIM(o.IDCUST)                     AS customer,
    -- RTRIM only. Never LTRIM: extract.sql records that Sage holds
    -- ' WPL/25-26/07516' and 'WPL/25-26/07516' as two separate obligations
    -- with different balances, so trimming the left merges two real documents.
    RTRIM(o.IDINVC)                     AS invoice,
    o.DATEINVC                          AS inv_date,
    o.DATEDUE                           AS due_date,
    CAST(o.AMTINVCHC AS decimal(19,4))  AS home_total,   -- home currency, ties the voucher
    CAST(o.AMTDUEHC  AS decimal(19,4))  AS home_open,    -- remaining; > 0 = unpaid
    RTRIM(o.CODECURN)                   AS currency,
    CAST(h.INVNETWTX AS decimal(19,4))  AS doc_total,    -- ASSUMED
    CAST(h.INRATE    AS decimal(19,7))  AS fx_rate,
    -- The export's destination, from the O/E ship-to. IESHPRGH carries no
    -- country column, so this is where it comes from.
    RTRIM(h.SHPCOUNTRY)                 AS dest_country,
    RTRIM(o.SRCEAPPL)                   AS srce,
    h.INVUNIQ                           AS oe_uniq
FROM AROBL o
LEFT JOIN OEINVH h ON RTRIM(h.INVNUMBER) = RTRIM(o.IDINVC)
WHERE o.TRXTYPEID IN (12, 14)
  AND o.DATEINVC  < %s
  {OPEN}
ORDER BY o.DATEINVC, o.IDCUST, o.IDINVC
"""

# DESC is a reserved word, hence the brackets. The item is joined to ICITEM on
# the UNFORMATTED number - OE states the formatted one ('ID-41354-X') and
# ICITEM.ITEMNO holds it without separators, exactly as the goods path found.
SQL_LINES = """
SET NOCOUNT ON;
SELECT
    RTRIM(o.IDCUST)                     AS customer,
    RTRIM(o.IDINVC)                     AS invoice,
    d.LINENUM                           AS line_no,
    RTRIM(d.ITEM)                       AS item,
    RTRIM(d.[DESC])                     AS descr,
    RTRIM(d.INVUNIT)                    AS um,
    CAST(d.QTYSHIPPED  AS decimal(19,4)) AS qty,
    CAST(d.UNITPRICE   AS decimal(19,6)) AS price,
    -- The extended amount, despite the name. See the note on SCHEMA.
    CAST(d.EXTINVMISC  AS decimal(19,4)) AS ext,         -- pre-tax
    -- Cost of goods sold, and ALREADY IN THE HOME CURRENCY: the proven
    -- invoice's four lines sum to INR 631,209.46 against a USD 26,405.41
    -- document. Never multiplied by the rate. Reference only - a sales invoice
    -- books revenue and the debtor, not COGS.
    CAST(d.EXTICOST    AS decimal(19,4)) AS cogs,
    -- The rate and the tax Sage STATES, per authority. Defect 4.1: read them,
    -- never divide tax by taxable to infer a rate. TAUTH names the authority
    -- ('SGST', 'CGST', 'IGST') and is how an unused slot is told from a real
    -- 0% one.
    CAST(d.TRATE1   AS decimal(9,4))  AS trate1,
    CAST(d.TRATE2   AS decimal(9,4))  AS trate2,
    CAST(d.TRATE3   AS decimal(9,4))  AS trate3,
    CAST(d.TRATE4   AS decimal(9,4))  AS trate4,
    CAST(d.TRATE5   AS decimal(9,4))  AS trate5,
    CAST(d.TAMOUNT1 AS decimal(19,4)) AS tamount1,
    CAST(d.TAMOUNT2 AS decimal(19,4)) AS tamount2,
    CAST(d.TAMOUNT3 AS decimal(19,4)) AS tamount3,
    CAST(d.TAMOUNT4 AS decimal(19,4)) AS tamount4,
    CAST(d.TAMOUNT5 AS decimal(19,4)) AS tamount5,
    RTRIM(d.TAUTH1) AS tauth1, RTRIM(d.TAUTH2) AS tauth2,
    RTRIM(d.TAUTH3) AS tauth3, RTRIM(d.TAUTH4) AS tauth4,
    RTRIM(d.TAUTH5) AS tauth5,
    RTRIM(i.CATEGORY)                   AS category,
    RTRIM(i.ITEMNO)                     AS item_raw
FROM OEINVD d
JOIN OEINVH h ON h.INVUNIQ = d.INVUNIQ
JOIN AROBL  o ON RTRIM(o.IDINVC) = RTRIM(h.INVNUMBER) AND o.TRXTYPEID IN (12, 14)
LEFT JOIN ICITEM i ON RTRIM(i.ITEMNO) = REPLACE(RTRIM(d.ITEM), '-', '')
WHERE o.DATEINVC < %s
  {OPEN}
ORDER BY o.IDCUST, o.IDINVC, d.LINENUM
"""

# Aliases chosen to match B.registration_of / B.resolve_state / B.platform_country
# key for key. That is what lets a customer be resolved by the same
# evidence-first rules as a vendor instead of a second, weaker copy of them.
SQL_CUSTOMERS = """
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
    RTRIM(c.BRN)       AS brn_raw,
    -- ARCUS has no legal-name column, so there is no second place to find a
    -- PAN in. Selected as a literal so registration_of() reads the key it
    -- expects and simply finds it empty.
    CAST('' AS varchar(1)) AS legal_name,
    RTRIM(c.CODECURN)  AS currency
FROM ARCUS c
"""

# Best effort, by design - see SCHEMA_BEST_EFFORT.
# The loading port and the shipping bill. NOT the destination country - this
# table has no country column; that comes from OEINVH.SHPCOUNTRY.
SQL_EXPORT = """
SET NOCOUNT ON;
SELECT RTRIM(g.INVDOCNUM) AS invoice,
       RTRIM(g.PRTOFLOAD) AS port_code,
       RTRIM(g.SBNO)      AS shipping_bill,
       g.SBDATE           AS shipping_bill_date
FROM IESHPRGH g
"""

SQL_OPEN_CONTROL = """
SET NOCOUNT ON;
SELECT COUNT(*)                          AS docs,
       COUNT(DISTINCT RTRIM(o.IDCUST))   AS customers,
       CAST(SUM(o.AMTINVCHC) AS decimal(19,2)) AS home_total,
       CAST(SUM(o.AMTDUEHC)  AS decimal(19,2)) AS home_open,
       MIN(o.DATEINVC)                   AS first_date,
       MAX(o.DATEINVC)                   AS last_date
FROM AROBL o
WHERE o.TRXTYPEID IN (12, 14) AND o.DATEINVC < %s AND o.AMTDUEHC > 0
"""


def verify_schema(best_effort=False):
    """-> {table: [missing columns]}. Raises Stop when a required name is gone.

    Called before every load. The point is that a renamed column is reported
    ONCE, by name, with candidates - not discovered as a half-finished query on
    the twentieth invoice of a run that has already created masters.
    """
    have = collections.defaultdict(set)
    for r in sage_query(SQL_TABLE_COLUMNS):
        have[s(r["tbl"]).upper()].add(s(r["col"]).upper())
    want = dict(SCHEMA)
    if best_effort:
        want.update(SCHEMA_BEST_EFFORT)
    gaps = {}
    for tbl, cols in sorted(want.items()):
        if tbl not in have:
            gaps[tbl] = ["<the whole table is absent>"]
            continue
        missing = [c for c in cols if c.upper() not in have[tbl]]
        if missing:
            gaps[tbl] = missing
    return gaps, have


def report_gaps(gaps, have):
    """Print the gaps with candidate names, so the fix is one edit away."""
    for tbl, missing in sorted(gaps.items()):
        print("  %s" % tbl)
        for col in missing:
            if col.startswith("<"):
                print("      %s" % col)
                continue
            stem = col[:3].upper()
            cand = sorted(c for c in have.get(tbl, ()) if c.startswith(stem))
            print("      %-14s MISSING%s" % (col, ("  candidates: " +
                  ", ".join(cand[:8])) if cand else ""))


# ============================================================================
# SOURCE: read Sage once, shape it into invoices
# ============================================================================

OPEN_ONLY = "AND o.AMTDUEHC > 0"


def load_ar_book(include_settled=False):
    """-> {(customer, invoice): {"header":..., "lines":[...], "export":...}}

    `include_settled` lifts the unpaid filter. It exists for ONE reason: the
    document that proves this path end to end - the Columbia export - is
    settled in Sage (AMTDUEHC = 0), so the cutover rule correctly excludes it
    from the load. Reproducing it and loading the migration are two different
    questions, and this is how the first one gets asked without weakening the
    rule that answers the second.
    """
    open_sql = "" if include_settled else OPEN_ONLY
    # One round trip for both tiers: the required tables, which stop the load,
    # and the best-effort export register, which only holds the exports.
    all_gaps, have = verify_schema(best_effort=True)
    gaps = {t: c for t, c in all_gaps.items() if t in SCHEMA}
    if gaps:
        print("\n=== SAGE AR SCHEMA ===")
        report_gaps(gaps, have)
        raise Stop("the AR/OE schema does not match what this loader assumes "
                   "(see above). These names came from the migration brief, not "
                   "from an inventory - fix them in SCHEMA and the query beside "
                   "it, then re-run. Nothing has been read or written.")

    heads = sage_query(SQL_HEADERS.replace("{OPEN}", open_sql), (CUTOVER,))
    lines = sage_query(SQL_LINES.replace("{OPEN}", open_sql), (CUTOVER,))
    print("  Sage AROBL before cutover %s%s: %d documents, %d O/E lines"
          % (CUTOVER, "" if include_settled else " and still open",
             len(heads), len(lines)), flush=True)

    book = {}
    for h in heads:
        key = (s(h["customer"]), s(h["invoice"]))
        # A number that appears twice would mean the LEFT JOIN on the invoice
        # number matched two O/E headers. Keep the first and say so; silently
        # overwriting would pick one document's rate for another's amounts.
        if key in book:
            print("  WARN %s|%s appears twice in AROBL x OEINVH - keeping the "
                  "first; the invoice number does not identify one O/E header"
                  % key)
            continue
        book[key] = {"key": key, "header": h, "lines": [], "export": None}
    for l in lines:
        rec = book.get((s(l["customer"]), s(l["invoice"])))
        if rec is not None:
            rec["lines"].append(l)

    # Export details, best effort: a rename here holds the exports with a
    # reason instead of stopping the whole load.
    try:
        if "IESHPRGH" in all_gaps:
            raise Stop("IESHPRGH columns %s" % ", ".join(all_gaps["IESHPRGH"]))
        by_inv = {s(r["invoice"]): r for r in sage_query(SQL_EXPORT)}
        for rec in book.values():
            rec["export"] = by_inv.get(rec["key"][1])
        print("  export shipping register: %d invoices matched in IESHPRGH"
              % sum(1 for r in book.values() if r["export"]), flush=True)
    except Exception as exc:                                    # noqa: BLE001
        note = str(exc).split("\n")[0][:120]
        for rec in book.values():
            rec["export_error"] = note
        print("  export shipping register UNAVAILABLE (%s) - every export will "
              "be held, since destination, port and shipping bill are stated on "
              "the document" % note, flush=True)
    return book


_CUSTOMERS = None


def customers_master():
    global _CUSTOMERS
    if _CUSTOMERS is None:
        _CUSTOMERS = {s(r["customer"]): r for r in sage_query(SQL_CUSTOMERS)}
        print("  ARCUS: %d customers" % len(_CUSTOMERS), flush=True)
    return _CUSTOMERS


# ============================================================================
# THE CONVERSION - where a sales invoice is not a bill
# ============================================================================

def inr_lines(lines, fx, target):
    """-> [(amount_inr, unit_price_inr, qty)], summing EXACTLY to `target`.

    `target` is the figure the LINE AMOUNTS have to make: Sage's home total on
    a zero-rated export, where the lines are the whole document, and the
    pre-tax taxable value on a GST invoice, where the tax sits beside them.

    The invoice ledger does not apply conversionRate, so the document is booked
    in INR and each line has to carry its own converted amount. Two facts
    decide the arithmetic:

      * The voucher must tie to Sage's own total. Converting each line
        independently does not get there - rounding each of n lines to the
        paise leaves a residual of up to n/2 paise against it. So the LAST line
        takes the residual: every earlier line is its own converted amount, and
        the last is whatever is left of the target. On the proven Columbia
        export the per-line roundings happen to cancel and the residual is
        zero - which is exactly why this cannot be left to chance, since
        nothing about that invoice says so in advance. `ar-selftest` prints the
        residual it actually finds rather than asserting a figure.
      * The ledger leg is quantity x unitPrice, not the line amount. So the
        unit price is derived from the line's INR amount and kept at SIX
        decimals, which is what billLineItem.unitPrice stores - rounding it to
        the paise moves the voucher (the payables path records 33.18 over 64
        units becoming a rupee out that way).

    A 6dp unit price moves the product in steps of qty x 1e-6, so any paise
    target is reachable while qty <= 10,000. Above that the residual may not be
    representable - which assert_ar_invariants catches, and the invoice is held
    with the delta rather than posted a paise out.
    """
    out, acc, n = [], D(0), len(lines)
    for i, l in enumerate(lines):
        qty = D(str(l["qty"] or 0))
        if i < n - 1:
            amt = q2(D(str(l["ext"] or 0)) * fx)
        else:
            amt = q2(target) - acc
        acc += amt
        up = (amt / qty).quantize(D("0.000001"), rounding=ROUND_HALF_UP) \
            if qty else D(0)
        out.append((amt, up, qty))
    return out


TAX_SLOTS = (1, 2, 3, 4, 5)


def line_gst_rate(l):
    """-> (rate, None) or (None, why). The rate Sage STATES, never derived.

    SUMMED ACROSS AUTHORITIES. Sage states tax per authority, and an
    intra-state sale is one slab split in two: TAUTH1='SGST' at TRATE1=2.5 and
    TAUTH2='CGST' at TRATE2=2.5 IS the 5% slab. Reading TRATE1 alone reported
    "2.50 is not a legal slab" and held 22 of the 32 shapeable documents. The
    payables path reached the same conclusion from the other direction - its
    rate source is recorded as "ratetax1+ratetax2".

    Defect 4.1 still governs: the rate is read, never inferred by dividing tax
    by taxable, and a total that is not a legal slab is a hold rather than
    something to snap into shape - on a sale the rate is what the customer was
    actually charged.
    """
    rate, seen = D(0), False
    for i in TAX_SLOTS:
        if not s(l.get("tauth%d" % i)):
            continue                      # an unused slot, not a real 0% one
        seen = True
        rate += D(str(l.get("trate%d" % i) or 0))
    if not seen:
        return None, "OEINVD names no tax authority - TAUTH1..5 are all blank"
    rate = q2(rate)
    if rate not in B.LEGAL_SLABS:
        auths = ", ".join("%s@%s" % (s(l.get("tauth%d" % i)),
                                     l.get("trate%d" % i))
                          for i in TAX_SLOTS if s(l.get("tauth%d" % i)))
        return None, ("stated GST rates do not total a legal slab, and a sale "
                      "is charged at a real rate - %s from %s" % (rate, auths))
    return rate, None


def line_gst_amount(l):
    """-> the tax Sage itself computed on this line, summed across authorities.

    Sage rounds EACH AUTHORITY to the paise, so an intra-state line at 5% on
    1,005.00 carries 25.13 SGST + 25.13 CGST = 50.26, where one 5% slab on the
    same base gives 50.25. That one-paise difference is not an error in either
    system; it is what separates a figure the platform can reproduce from the
    figure Sage states. Both are carried, and where they disagree the invoice
    is held rather than posted at a total Sage does not state.
    """
    return q2(sum(D(str(l.get("tamount%d" % i) or 0)) for i in TAX_SLOTS))


# Values that OEINVH.SHPCOUNTRY actually holds and that underscoring alone does
# not turn into a name the platform knows. Every entry is taken from the live
# population, not imagined: 'US' and 'PA' are ISO 3166-1 alpha-2 (the only two
# 2-letter codes present), 'Polonia' is Poland, and 'Republic of Korea' is the
# official name of the country the platform files as SOUTH_KOREA - which the
# payables path already verified exists there.
#
# Anything NOT here still falls through to underscoring, and a name the enum
# does not know is refused BY NAME at create ("not one of the values accepted
# for Enum class: [ALGERIA, GIBRALTAR, ...]"), which is how these four were
# found in the first place.
SHIP_COUNTRY_ALIASES = {
    "US": "UNITED_STATES",
    "PA": "PANAMA",
    "POLONIA": "POLAND",
    "REPUBLICOFKOREA": "SOUTH_KOREA",
}


def destination_country(raw):
    """-> the platform's country enum for a free-text Sage country.

    The known map first, then Sage's own text underscored and upper-cased,
    which is what the proven loader did. Unlike a vendor's country this cannot
    move money: an export under LUT is 0% whichever country it lands in, and a
    name the platform's 242-value enum does not know is refused outright by the
    API with a message that names it. So a loud failure, not a silent one.
    """
    key = re.sub(r"[^A-Z]", "", s(raw).upper())
    if not key:
        return None
    return (SHIP_COUNTRY_ALIASES.get(key) or B.COUNTRY_ENUM.get(key)
            or re.sub(r"[^A-Z0-9]+", "_", s(raw).upper()).strip("_"))


def pan_of(reg_type, reg_no):
    """The PAN inside a GSTIN (characters 3-12), or the PAN itself."""
    if reg_type == "GST" and reg_no and len(reg_no) >= 12:
        return reg_no[2:12]
    return reg_no if reg_type == "PAN" else None


def classify_invoice(rec, cust):
    """-> (shape, None) or (None, why). Nothing taxable is ever guessed."""
    h = rec["header"]
    code, invoice = rec["key"]
    if not rec["lines"]:
        return None, ("no O/E lines: keyed straight into A/R rather than raised "
                      "in Order Entry, so Sage holds no record of what was "
                      "sold - SRCEAPPL=%s" % (s(h.get("srce")) or "?"))

    home = q2(D(str(h["home_total"] or 0)))
    if home <= 0:
        return None, "AROBL.AMTINVCHC is not positive"

    curn = s(h["currency"]).upper() or "INR"
    doc  = D(str(h["doc_total"] or 0))
    fx   = D(str(h["fx_rate"] or 0))
    if curn == "INR":
        # A home-currency invoice has no conversion to state, and Sage may
        # leave INRATE at 0 or 1 indifferently. Pin it, and the identity check
        # below then requires INVNETWTX to equal AMTINVCHC - the same evidence
        # with the multiplication removed.
        fx = D(1)
    if fx <= 0:
        return None, ("no exchange rate stated on a foreign-currency invoice - "
                      "INRATE=%r, currency %s" % (h["fx_rate"], curn))
    if doc <= 0:
        return None, ("OEINVH states no document total - INVNETWTX=%r"
                      % h["doc_total"])

    # THE CHECK that makes the assumed column names safe. Sage states the
    # document total, the rate and the home total independently, so their
    # product has to agree to the paise. If INVNETWTX or INRATE is the wrong
    # column this fires on every invoice and not one is posted at a figure Sage
    # does not state.
    if q2(doc * fx) != home:
        return None, ("Sage disagrees with itself: document total x rate is not "
                      "the home total - INVNETWTX %s x INRATE %s = %s against "
                      "AMTINVCHC %s, delta %s"
                      % (doc, fx, q2(doc * fx), home, q2(doc * fx - home)))

    # Per line. The EXTENSION is the money - it is what feeds AMTINVCHC and
    # what the customer was charged - so it is authoritative and the posted
    # unit price is derived from it.
    #
    # Sage does NOT maintain quantity x price = extension, and this used to be
    # asserted as though it did, which held 5 real documents. Measured on the
    # open population: IDEPLACC2025-010 states UNITPRICE 0.000018 against an
    # extension of 2,850.00 over 30,000 units (0.095 each), and
    # IDEPL2526FAB006 states 5.754 against 238.79 over 41.4 KGS (5.768 each).
    # Neither carries a discount - DISCPER, INVDISC and HDRDISC are all zero -
    # and neither PRIUNTPRC nor UNITCONV closes the gap either. So the identity
    # simply does not hold in this data, and asserting it was asserting
    # something about Sage that is not true.
    #
    # Nothing is lost by dropping it: the wrong-column check the assertion was
    # really doing is done better by the header identity above, which tests
    # against the document TOTAL. The divergence is recorded per line so it
    # stays visible.
    price_notes = []
    for l in rec["lines"]:
        qty, price = D(str(l["qty"] or 0)), D(str(l["price"] or 0))
        ext = D(str(l["ext"] or 0))
        if qty <= 0:
            return None, ("a sales line cannot be booked at zero quantity - "
                          "line %s states %s" % (s(l["line_no"]), qty))
        if q2(qty * price) != q2(ext):
            price_notes.append("line %s: Sage states unit price %s, which over "
                               "%s gives %s against its own extension of %s"
                               % (s(l["line_no"]), price, qty, q2(qty * price),
                                  q2(ext)))
        if SCRIP_RE.search(s(l["item"])) or SCRIP_RE.search(s(l["descr"])):
            return None, ("export scrip belongs on a journal voucher, not an "
                          "invoice: it credits an asset head (2A7SL20) and is "
                          "not revenue against a buyer - line %s, %s"
                          % (s(l["line_no"]), s(l["item"]) or s(l["descr"])))

    reg_type, reg_no = B.registration_of(cust)
    pan = pan_of(reg_type, reg_no)
    # The ship-to on the document first, then the customer's own country. Both
    # are checked so the hold below is truthful about having looked: measured
    # on the open population, 22 documents have a blank SHPCOUNTRY and NOT ONE
    # of their customers carries a CODECTRY either, so this recovers nothing
    # today - but a blank ship-to beside a known customer country is a real
    # shape, and taking the second-best evidence is better than holding over a
    # field that is 0% under LUT anyway.
    dest = destination_country(h.get("dest_country"))
    dest_src = "OEINVH.SHPCOUNTRY"
    if not dest:
        dest, dest_src = destination_country(cust.get("country")), "ARCUS.CODECTRY"
    foreign_dest = bool(dest) and dest != "INDIA"

    # Inter-unit self-invoice: the same legal entity on both sides of the
    # document. Not a sale - it is stock moving between the org's own units -
    # so the platform files it as a TRANSFER_INVOICE off its own counter.
    if pan and pan == ORG_PAN:
        kind = "TRANSFER"
    elif reg_type == "GST":
        kind = "DOMESTIC"
    elif foreign_dest or curn != "INR":
        # THE DOCUMENT decides this, not the customer master. ARCUS leaves
        # CODECTRY empty on many export customers - Columbia, the very invoice
        # this path was proven on, among them - so B.registration_of() reads
        # them as WITHOUT_PAN_OR_GST rather than INTERNATIONAL. Taking the
        # customer master's word for it put 83 of the 259 open documents into
        # the domestic branch, where the foreign currency then held every one.
        #
        # The ship-to country and the invoice currency are stated ON the
        # document, and a shipping bill exists for it in the export register.
        # That is what an export is; a blank field in a master file is not
        # evidence against it.
        kind = "EXPORT"
    else:
        # An Indian customer, in rupees, with no GSTIN: a domestic sale to an
        # unregistered buyer, which is a real shape and not a gap.
        kind = "DOMESTIC"

    rates, tax, export = {}, D(0), None
    if kind == "EXPORT":
        # Export under a Letter of Undertaking: zero-rated, and the document
        # asserts the LUT. Everything asserted has to be in evidence - and a
        # LUT is granted for ONE financial year, so the one that matches THIS
        # invoice's year is the only one that may be asserted on it.
        inv_fy = financial_year(s(h["inv_date"]))
        lut = LUT_BY_FY.get(inv_fy)
        if not lut:
            return None, ("no LUT configured for this invoice's financial year, "
                          "and EXPORT_WITHOUT_PAYMENT asserts one ON the "
                          "invoice - FY %s needed, .env has %s"
                          % (inv_fy, ", ".join(sorted(LUT_BY_FY)) or "none"))
        if rec.get("export_error"):
            return None, ("export shipping register unavailable (%s) - "
                          "destination, port and shipping bill are stated on "
                          "the document" % rec["export_error"])
        if not dest:
            return None, ("no destination country anywhere on an export - "
                          "OEINVH.SHPCOUNTRY is %r and ARCUS.CODECTRY is %r, "
                          "and an export invoice states where the goods went"
                          % (s(h.get("dest_country")), s(cust.get("country"))))
        g = rec.get("export")
        if not g:
            return None, ("no IESHPRGH row for this invoice - the loading port "
                          "and shipping bill are stated on an export")
        if not s(g.get("port_code")):
            return None, "IESHPRGH names no loading port (PRTOFLOAD)"
        export = {"dest": dest, "dest_source": dest_src,
                  "port": s(g.get("port_code")),
                  "shipping_bill": s(g.get("shipping_bill")),
                  "shipping_bill_date": s(g.get("shipping_bill_date")),
                  "lut_number": lut[0], "lut_date": lut[1], "lut_fy": inv_fy}
        rates = {s(l["line_no"]): D(0) for l in rec["lines"]}
        taxable_target = home                      # zero-rated: lines ARE the total
    else:
        # Domestic and inter-unit both carry GST at the rate Sage states, and
        # the line amounts are PRE-tax - so the lines sum to the taxable value,
        # not to AMTINVCHC. That only closes in the home currency, because
        # taxable + tax then has to equal AMTINVCHC and there is no conversion
        # residual to place. A GST invoice billed in a foreign currency is a
        # real shape (SEZ supplies) but it needs a decision about where the
        # residual belongs relative to the tax, so it is held rather than
        # assumed into one.
        if curn != "INR":
            return None, ("%s invoice to a %s-registered customer: taxable + "
                          "tax has to tie to AMTINVCHC and the conversion "
                          "residual would have to land either side of the tax "
                          "- that is a decision, not a default"
                          % (curn, reg_type))
        for l in rec["lines"]:
            r, why = line_gst_rate(l)
            if why:
                return None, "%s (line %s)" % (why, s(l["line_no"]))
            rates[s(l["line_no"])] = r
        taxable_target = q2(sum(q2(D(str(l["ext"] or 0)) * fx)
                                for l in rec["lines"]))
        # The tax Sage STATES, per line per authority. This is the figure that
        # built AMTINVCHC, so it is the one the tie is tested against.
        sage_tax = q2(sum(line_gst_amount(l) for l in rec["lines"]))
        # And the tax the PLATFORM will derive: one slab per line on its own
        # base. It cannot reproduce a figure Sage rounded per authority.
        posted_tax = q2(sum(q2(q2(D(str(l["ext"] or 0)) * fx)
                               * rates[s(l["line_no"])] / 100)
                            for l in rec["lines"]))
        if q2(taxable_target + sage_tax) != home:
            return None, ("taxable + Sage's own stated GST does not tie to the "
                          "home total - %s + %s = %s against AMTINVCHC %s, "
                          "delta %s"
                          % (taxable_target, sage_tax,
                             q2(taxable_target + sage_tax), home,
                             q2(taxable_target + sage_tax - home)))
        if posted_tax != sage_tax:
            # Sage rounds SGST and CGST to the paise SEPARATELY, so 5% on
            # 1,005.00 becomes 25.13 + 25.13 = 50.26 where one slab gives
            # 50.25. The platform recomputes gstAmount from the line's single
            # rate and ignores whatever is sent, so no legal slab satisfies
            # both figures. The payables path met the same wall on reverse
            # charge and chose to post and log; a sale is HELD instead, because
            # the alternative is a receivable filed at a total Sage does not
            # state, and one paise wrong in the ledger is far harder to find
            # later than one line in a report.
            return None, ("Sage rounds each tax authority separately, so no "
                          "single slab reproduces its total - Sage states GST "
                          "%s on taxable %s, the platform derives %s, delta %s"
                          % (sage_tax, taxable_target, posted_tax,
                             q2(posted_tax - sage_tax)))
        tax = sage_tax

    inr = inr_lines(rec["lines"], fx, taxable_target)

    dt = s(h["inv_date"])
    if not sage_date_parts(dt):
        return None, "AROBL.DATEINVC is not a date (%r)" % h["inv_date"]
    due = s(h["due_date"]) if sage_date_parts(h["due_date"]) else dt
    return {
        "key": rec["key"], "header": h, "lines": rec["lines"], "inr": inr,
        "kind": kind, "rates": rates,
        "invoice_type": "TRANSFER_INVOICE" if kind == "TRANSFER" else "TAX_INVOICE",
        "counter_type": COUNTER_TYPE[kind],
        "sales_type": "INTERNATIONAL" if kind == "EXPORT" else "DOMESTIC",
        # NEVER None. InvoiceServiceImpl.checkValidationForSmeAssistInvoice
        # does `switch (dto.getSupplyType())` with no null guard, and a switch
        # on a null enum is a bare NullPointerException - which is what every
        # domestic document failed with until this was set.
        #
        # The values are the platform's own, taken from what it already holds:
        # B2B on 310,419 domestic invoices and BILL_OF_SUPPLY on 11,179. A bill
        # of supply is precisely a supply on which no tax is charged, so the
        # split is by whether this document actually carries GST rather than by
        # anything about the buyer.
        "supply_type": ("EXPORT_WITHOUT_PAYMENT" if kind == "EXPORT"
                        else ("B2B" if tax > 0 else "BILL_OF_SUPPLY")),
        "export": export, "currency": curn, "fx": fx,
        "doc_total": q2(doc), "home_total": home, "taxable": q2(taxable_target),
        "tax": q2(tax), "open": q2(D(str(h["home_open"] or 0))),
        # NOT multiplied by the rate: EXTICOST is already the home-currency
        # figure. Verified on the proven invoice - its four lines sum to INR
        # 631,209.46 on a USD 26,405.41 document, so applying the rate here
        # would have inflated the recorded cost 92-fold.
        "cogs": q2(sum(D(str(l["cogs"] or 0)) for l in rec["lines"])),
        "inv_date": dt, "due_date": due,
        "credit_days": credit_days(dt, due),
        "registration": reg_type, "gstin": reg_no or "",
        "international": kind == "EXPORT",
        "price_notes": price_notes,
    }, None


def credit_days(dt, due):
    a, b = sage_date_parts(dt), sage_date_parts(due)
    if not a or not b:
        return 0
    return max(0, (datetime.date(*b) - datetime.date(*a)).days)


# ============================================================================
# MASTERS - buyers and sale products
# ============================================================================

def _ap_crosswalk():
    if not os.path.exists(AP_CROSSWALK):
        return {}
    try:
        with open(AP_CROSSWALK, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def ap_party_index():
    """-> ({gstin: rec}, {contactId: addressId}) from the PAYABLES crosswalk.

    The most useful thing the AP run leaves behind for this one. A party that
    both buys from us and sells to us is ONE contact with two ledgers, and
    POST /contact/ refuses a second on the duplicate GSTIN - so a customer the
    bills path already created is found here before any API call is made, and
    needs only its BUYER role and its Debtors ledger added.

    The addressId map exists for the same reason it does on the AP side: there
    is no working route to read a contact's addresses back on this build, and
    the sibling that created the contact recorded the id its own create
    returned. One GSTIN is one contact, so that address IS this party's.
    """
    by_gstin, addr = {}, {}
    for rec in (_ap_crosswalk().get("contacts") or {}).values():
        if rec.get("gstin"):
            by_gstin.setdefault(rec["gstin"], rec)
        if rec.get("contactId") and rec.get("addressId"):
            addr.setdefault(str(rec["contactId"]), str(rec["addressId"]))
    return by_gstin, addr


def buyer_ledger_for(api, contact_id):
    """The BUYER ledger - Sundry Debtors - and never the VENDOR one. A
    dual-role party has both, and posting a sale against the Creditors ledger
    would put the receivable on the wrong side of the balance sheet."""
    st, body = api.get("/financeAccount/minDetails/BUYER/%s" % contact_id)
    d = api.data(body)
    if isinstance(d, list) and d:
        return str(d[0].get("financeAccountId")), d[0].get("leaf")
    return None, None


def _address_types(a):
    """addressTypes comes back as plain strings on some routes and as
    {value: ...} objects on others."""
    out = []
    for t in (a.get("addressTypes") or []):
        out.append(s(t.get("value")) if isinstance(t, dict) else s(t))
    return out


def buyer_address_for(api, contact_id, ap_addr):
    st, body = api.get("/org/address/list?partyId=%s&addressPartyType=CONTACT"
                       "&status=ACTIVE" % contact_id)
    d = api.data(body)
    rows = d if isinstance(d, list) else (d or {}).get("content") or []
    for a in rows:
        if "BILLING_ADDRESS" in _address_types(a) and a.get("addressId"):
            return str(a["addressId"])
    if rows and rows[0].get("addressId"):
        return str(rows[0]["addressId"])
    return ap_addr.get(str(contact_id))


def add_buyer_role(api, contact_id, name):
    """-> (ok, note). Adds the BUYER role to a contact that lacks it.

    Buyer and vendor are two roles on ONE contact, each with its own ledger.
    The alternative - a second contact for the same legal entity - is refused
    by POST /contact/ on the duplicate GSTIN, and would split one party's
    balances across two ledgers even if it were not.
    """
    st, body = api.get("/contact/details/%s" % contact_id)
    if not api.ok(st, body):
        return False, "cannot read contact %s: %s" % (contact_id, api.err(body))
    d = api.data(body) or {}
    infos = [i for i in (d.get("contactInfoDtoList") or []) if isinstance(i, dict)]
    if any(s(i.get("contactType")) == "BUYER" for i in infos):
        return True, "already a BUYER"
    had = ",".join(sorted({s(i.get("contactType")) for i in infos} - {""}))
    # Carry the party's real mobile over rather than stamping the placeholder
    # onto a contact that already has one.
    mobiles = next((i.get("mobileNumbers") for i in infos
                    if i.get("mobileNumbers")), None) or [B.PLACEHOLDER_MOBILE]
    infos = infos + [{"contactType": "BUYER", "status": "ACTIVE",
                      "mobileNumbers": mobiles, "pocName": name,
                      "contactCategory": {"categoryId": BUYER_CATEGORY}}]
    st, body = api.call("PUT", "/contact/%s/update" % contact_id,
                        {"contactId": contact_id, "contactInfoDtoList": infos})
    if not api.ok(st, body):
        return False, "add BUYER role failed: %s" % api.err(body)
    return True, "added BUYER role (was %s)" % (had or "no role")


def ensure_buyers(api, state, codes, rows=None, enrich=True, intl_codes=()):
    """A contact per Sage customer, carrying the BUYER role, its Debtors ledger
    and a billing address. Withhold rather than guess.

    `intl_codes` are the customers whose own INVOICES say they are abroad - a
    foreign ship-to country or a foreign currency on the document. ARCUS leaves
    CODECTRY blank on many export customers, so the master alone reads them as
    domestic-unregistered; the document is the better evidence and it is
    classify_invoice that has it.
    """
    print("\n=== BUYERS ===", flush=True)
    if rows is None:
        rows = customers_master()
    xw = state.xw.setdefault("buyers", {})
    ap_gstin, ap_addr = ap_party_index()
    cin_map = B.load_cin_map()
    held = []
    for code in sorted(codes):
        if xw.get(code, {}).get("ledger"):
            continue
        cust = rows.get(code)
        if not cust:
            held.append((code, "no ARCUS row")); continue
        name = s(cust["name"]) or code
        reg_type, reg_no = B.registration_of(cust)
        st_name, how = B.resolve_state(cust)
        international = (reg_type == "INTERNATIONAL"
                         or st_name == "OTHER_COUNTRY" or code in intl_codes)
        if international and reg_type != "INTERNATIONAL":
            # A party whose own invoices say it is abroad MUST be filed
            # INTERNATIONAL, whatever the customer master's blanks imply.
            # ContactServiceImpl OVERWRITES country from the org for every
            # other registration type, so the country sent is silently
            # discarded - measured on the first real contact this loader
            # created: MOUNTAIN HARDWEAR went in as WITHOUT_PAN_OR_GST with
            # country UNITED_STATES and came back stored as INDIA, a US buyer
            # filed in India with nothing reported.
            #
            # registrationNumber goes with it: the platform's shape for a
            # foreign party is INTERNATIONAL + NULL number + profileType
            # OTHERS + state UNKNOWN, which is what the payables path verified
            # across 1,517 existing contacts and what the proven sales script
            # sends.
            reg_type, reg_no = "INTERNATIONAL", None

        # UNKNOWN is not OTHER_COUNTRY - the payables path records why these
        # must not share a branch. UNKNOWN means we cannot tell which INDIAN
        # state this is, and on a SALE that is the place of supply: it decides
        # IGST against CGST+SGST on the org's own output tax. Held.
        if st_name == "UNKNOWN" and not international:
            held.append((code, "state %s (%s) - on a sale this is the place of "
                               "supply, which decides IGST vs CGST+SGST"
                         % (st_name, how))); continue

        # --- resolve an existing party BEFORE creating anything -------------
        cid = None
        via = None
        if reg_no and ap_gstin.get(reg_no):
            cid, via = str(ap_gstin[reg_no]["contactId"]), "payables crosswalk"
        if not cid and B.is_gstin(reg_no):
            st, body = api.get("/contact/gst/%s" % reg_no)
            got = (api.data(body) or {}).get("contactId") if api.ok(st, body) else None
            if got:
                cid, via = str(got), "GST lookup"
        if not cid:
            st, body = api.get("/contact/search?query=%s&pageSize=10" % _q(name))
            d = api.data(body) or {}
            content = d.get("content") if isinstance(d, dict) else None
            hit = next((c for c in (content or [])
                        if s(c.get("accountName")).upper() == name.upper()), None)
            if hit and hit.get("contactId"):
                cid, via = str(hit["contactId"]), "name match"

        if cid:
            ok, note = add_buyer_role(api, cid, name)
            if not ok:
                held.append((code, note)); continue
            led, leaf = buyer_ledger_for(api, cid)
            addr = buyer_address_for(api, cid, ap_addr)
            if not led:
                held.append((code, "reused contact %s via %s but it has no BUYER "
                                   "ledger" % (cid, via))); continue
            if not addr:
                held.append((code, "reused contact %s via %s but no billing "
                                   "address can be read back for it"
                             % (cid, via))); continue
            xw[code] = {"contactId": cid, "name": name, "ledger": str(led),
                        "ledgerIsLeaf": leaf, "addressId": str(addr),
                        "state": st_name, "registrationType": reg_type,
                        "gstin": reg_no or "", "reused": via,
                        # Only what Sage actually says. Defaulting an unknown
                        # country to INDIA made the crosswalk claim Columbia
                        # was Indian while the platform had correctly stored it
                        # as UNITED_STATES - a local field that contradicts the
                        # database is worse than an absent one.
                        "country": (destination_country(cust.get("country"))
                                    if international else "INDIA")}
            state.save()
            print("  %-10s reused %s via %-19s %s" % (code, cid, via, note))
            continue

        # --- brand-new party ------------------------------------------------
        country, addr_src = "INDIA", "sage"
        line1 = s(cust.get("street1"))
        city  = s(cust.get("city"))
        pin   = B.normalise_pincode(cust.get("pincode"))
        state_send = st_name
        if international:
            # NOT B.platform_country(): that map is deliberately strict because
            # a VENDOR's country decides how an import bill is treated, so an
            # unmapped name is held there rather than guessed. It holds
            # 'SINGAPORE' and 'SWEDEN', which this org really sells to.
            #
            # On a sale the stakes are different: an export under LUT is 0%
            # whichever country it lands in, the platform's country enum has
            # 242 values so these are in it, and a name it does not know is
            # refused BY NAME at create - a loud failure on one contact, not a
            # silent mis-posting. So the permissive mapping is used here, for
            # the same reason the strict one is used there.
            country = destination_country(cust.get("country"))
            need_addr = not (line1 and city)
            # Ask ONCE and use the one answer for both the country and the
            # address, rather than paying for two calls on the same entity.
            enr = (resolve_address_ai(name)
                   if enrich and (not country or need_addr) else None)
            if not country and enr:
                country = destination_country(enr.get("country"))
            if country == "INDIA":
                # The invoices say abroad (foreign ship-to or currency) and the
                # country says India. Enrichment guessing "India" from a name
                # like "BD DESIGNS PVT LTD" is exactly how that happens, and an
                # INTERNATIONAL contact filed in India is a contradiction the
                # platform will store without complaint. Decide it, do not
                # average it.
                held.append((code, "the invoices say this customer is abroad "
                                   "but its country resolves to INDIA - one of "
                                   "the two is wrong and neither is safe to "
                                   "assume - source %s"
                             % (("gemini:%s" % s(enr.get("confidence")))
                                if enr and not s(cust.get("country"))
                                else "ARCUS.CODECTRY")))
                continue
            if not country:
                held.append((code, "no country for an export customer, and the "
                                   "platform files a foreign party under one - "
                                   "ARCUS.CODECTRY is %r and %s"
                             % (s(cust.get("country")),
                                "enrichment is off (--no-enrich or no "
                                "GEMINI_API_KEY)" if not enrich or not GEMINI_KEY
                                else "enrichment could not identify the entity")))
                continue
            # The platform files foreign addresses under state UNKNOWN, not
            # OTHER_COUNTRY - verified on this box across 1,517 contacts.
            state_send = "UNKNOWN"
            if need_addr:
                if enr:
                    line1 = s(enr.get("addressLine1")) or "%s, %s" % (
                        s(enr.get("city")), s(enr.get("country")))
                    city = s(enr.get("city"))
                    addr_src = "gemini-enriched:%s" % s(enr.get("confidence"))
                else:
                    line1 = line1 or "Address not available in source"
                    city = city or "Unknown"
                    addr_src = "placeholder-no-source"
            # A foreign postal code is RESOLVED against the platform's own geo
            # table, and a miss is refused at address create with "Invalid
            # Zipcode" - which is what took three buyers here and 21 vendors on
            # the payables side. Settle it before the contact exists, and reuse
            # the enrichment answer already in hand rather than paying twice.
            pin, pin_src = B.verified_foreign_pincode(
                api, cust, country, name, enrich=enrich, enr=enr)
            if not pin:
                held.append((code, pin_src)); continue
            if not pin_src.startswith("sage"):
                addr_src = "%s+pincode:%s" % (addr_src, pin_src)
        else:
            if not pin:
                pin = B.STATE_HEAD_PINCODE.get(state_send)
                addr_src = "state-head-pincode"
                if not pin:
                    held.append((code, "no usable pincode (%r) and no head "
                                       "pincode for state %s"
                                 % (s(cust.get("pincode")), state_send)))
                    continue
            line1 = line1 or name
            city = city or B.ORG_CITY
            # India only: a foreign postal code has no Indian state to agree
            # with, so running this on one can only produce a false hold.
            plat = B.pincode_state(api, pin)
            if plat and plat != state_send:
                held.append((code, "pincode %s resolves to %s but the GSTIN says "
                                   "%s - posting would swap IGST for CGST+SGST"
                             % (pin, plat, state_send))); continue

        cin_rec = cin_map.get(reg_no or "") or {}
        if B.needs_cin_lookup(reg_no) and not cin_rec:
            held.append((code, "GSTIN 6th char %s needs a CIN/LLPIN that exists "
                               "neither in Sage nor anywhere on the devbox"
                         % reg_no[5])); continue

        digits = re.sub(r"\D", "", s(cust.get("phone1")))
        mobiles = [digits[-10:]] if len(digits) >= 10 else []
        emails = [s(cust["email1"])] if "@" in s(cust.get("email1")) else []
        payload = {
            "accountName": name, "companyName": name,
            "registrationType": reg_type,
            "contactInfoDtoList": [{
                "contactType": "BUYER", "status": "ACTIVE",
                "mobileNumbers": mobiles or [B.PLACEHOLDER_MOBILE],
                "emailAddresses": emails,
                "pocName": s(cust.get("contact_person")) or name,
                "contactCategory": {"categoryId": BUYER_CATEGORY}}],
            "contactBusinessInfo": {"isMSME": False},
            # Validated but not persisted - required or the create answers
            # "Address must be present."; the real one is created below.
            "addressDtoList": [{
                "addressTypes": ["BILLING_ADDRESS", "SHIPPING_ADDRESS"],
                "primaryAddress": True, "status": "ACTIVE",
                "partyType": "CONTACT", "addressLine1": line1, "city": city,
                "state": state_send, "pinCode": pin, "country": country}],
            "metaData": {"migrationSource": "IDEDAT", "sageCustomer": code,
                         "stateSource": how, "addressSource": addr_src,
                         "sageCountry": s(cust.get("country")),
                         "mobileIsPlaceholder": str(not mobiles).lower()},
        }
        if cin_rec:
            for fld in ("corporateIdentificationNumber",
                        "limitedLiabilityPartnershipIdentificationNumber"):
                if cin_rec.get(fld):
                    payload["contactBusinessInfo"][fld] = cin_rec[fld]
                    payload["metaData"]["cinSource"] = "devbox:%s" % \
                        cin_rec.get("sourceOrganisationId", "?")
        if reg_type in ("GST", "PAN"):
            payload["registrationNumber"] = reg_no
            # Entity type is GSTIN[5], which IS PAN[3]. Index by which number
            # is in hand - on a bare PAN that position is a digit.
            ent = reg_no[5] if reg_type == "GST" else reg_no[3]
            payload["contactBusinessInfo"]["profileType"] = \
                B.GSTIN_ENTITY_TO_PROFILE.get(ent, "OTHERS")
        if international:
            payload["contactBusinessInfo"]["profileType"] = "OTHERS"
            payload["country"] = country
            # The document currency, not INR: unlike a bill, a sales invoice
            # states what the customer was actually invoiced in, and the
            # contact has to name it or the create is refused.
            payload["currencies"] = sorted({"INR", s(cust.get("currency")).upper()
                                            or "INR"})

        # A previous run may have created this contact and then failed on its
        # address. DELETE /contact/{id} does not exist on this build, so that
        # contact is still there - creating another would give one party two
        # contacts, which is the thing this whole path exists to avoid.
        orphans = state.xw.setdefault("orphans", {})
        if orphans.get(code):
            cid = str(orphans[code])
            print("  %-10s resuming the contact a previous run left behind (%s)"
                  % (code, cid))
            st_code, body = 200, {"data": {"contactId": cid}}
        else:
            st_code, body = api.post("/contact/", payload)
        if not api.ok(st_code, body) and \
                "Already exists with registration Number" in api.err(body):
            # Several Sage customer codes legitimately share one GSTIN, and a
            # party already created on the payables side lands here too when
            # the crosswalk lookup above missed it.
            st, lb = api.get("/contact/search?query=%s&pageSize=5" % _q(reg_no))
            d = api.data(lb) or {}
            content = d.get("content") if isinstance(d, dict) else None
            if content and content[0].get("contactId"):
                found = str(content[0]["contactId"])
                ok, note = add_buyer_role(api, found, name)
                led, _leaf = buyer_ledger_for(api, found) if ok else (None, None)
                addr = buyer_address_for(api, found, ap_addr) if led else None
                if led and addr:
                    xw[code] = {"contactId": found, "name": name,
                                "ledger": str(led), "addressId": str(addr),
                                "state": st_name, "registrationType": reg_type,
                                "gstin": reg_no or "", "reused": "duplicate GSTIN",
                                "country": country}
                    state.save()
                    print("  %-10s reuses existing contact for GSTIN %s"
                          % (code, reg_no))
                    continue
            held.append((code, "shares GSTIN %s with an existing contact that "
                               "has no usable BUYER ledger/address" % reg_no))
            continue
        if not api.ok(st_code, body):
            held.append((code, "create failed: %s" % api.err(body))); continue
        if api.dry_run:
            continue
        cid = str((api.data(body) or {}).get("contactId"))

        # The real address. The field is addressPartyType, NOT partyType.
        def _make_address(pincode):
            st_, b_ = api.post("/contact/address/create", {
                "partyId": cid, "addressPartyType": "CONTACT",
                "addressTypes": ["BILLING_ADDRESS", "SHIPPING_ADDRESS"],
                "primaryAddress": True, "status": "ACTIVE",
                "addressLine1": line1, "city": city, "state": state_send,
                "pinCode": pincode, "country": country,
                "organisationId": ORG_ID})
            # str(None) is the truthy string "None" - take the value out first
            # and only stringify something that is actually there.
            a_ = (api.data(b_) or {}).get("addressId") if api.ok(st_, b_) else None
            return st_, b_, (str(a_) if a_ else None)

        # No blind retry on "Invalid Zipcode" any more: 999077 is not the
        # universal placeholder that retry assumed - it resolves for HONG_KONG
        # and CHINA and nowhere else, which is why it still lost three buyers.
        # The code is verified against the platform's table above instead.
        st2, b2, addr_id = _make_address(pin)
        if not addr_id:
            # DELETE /contact/{id} is a 404 on this build, so there is no
            # rollback to attempt - the payables path's roll-back branch simply
            # cannot fire here and would only add a failed call and a
            # misleading "orphan left behind" note to every message.
            #
            # The contact exists and is unusable without an address, so it is
            # RECORDED instead. The next run picks it up above and finishes it,
            # rather than minting a second contact for the same party.
            state.xw.setdefault("orphans", {})[code] = cid
            state.save()
            held.append((code, "billing address failed, and the contact cannot "
                               "be deleted on this build - %s (contact %s is "
                               "recorded and will be resumed, not duplicated, "
                               "on the next run)" % (api.err(b2), cid)))
            continue

        # Nothing else creates this row and its absence surfaces much later,
        # from the invoice module. taxationPartyType is BUYER here, not VENDOR.
        api.post("/taxation", {"organisationId": ORG_ID, "entityId": cid,
                               "taxationPartyType": "BUYER", "taxType": "NONE",
                               "isDefault": True})
        led, leaf = buyer_ledger_for(api, cid)
        if not led:
            held.append((code, "BUYER ledger was not created")); continue
        xw[code] = {"contactId": cid, "name": name, "ledger": str(led),
                    "ledgerIsLeaf": leaf, "addressId": addr_id,
                    "state": state_send, "stateSource": how, "city": city,
                    "pinCode": pin, "registrationType": reg_type,
                    "gstin": reg_no or "", "country": country,
                    "addressSource": addr_src}
        state.xw.get("orphans", {}).pop(code, None)
        state.save()
        print("  created %-10s %-36s %-14s %s ledger=%s"
              % (code, name[:36], state_send, reg_type, led))
    if held:
        with open(os.path.join(WORK, "buyers_held.json"), "w") as fh:
            json.dump([{"customer": c, "reason": r} for c, r in held], fh, indent=1)
        print("  HELD %d customers (work items, not failures) -> "
              "work/buyers_held.json" % len(held))
    print("  buyers in crosswalk: %d" % len(xw))
    return held


def _q(v):
    from urllib.parse import quote
    return quote(s(v), safe="")


def ensure_sale_products(api, state, items):
    """One product per (Sage item, unit), carrying an ITEM_SALE revenue ledger.

    Deliberately the SAME (item, unit) key and the same SAGE-<item>-<unit> SKU
    the payables goods path uses, because it is the same Sage item master. A
    product that path already created is ADOPTED here and given a revenue
    ledger beside its purchase one - financeAccountReferenceMapping adds a
    ledger rather than moving the existing one, so nothing on the purchase side
    is disturbed. Minting a second product for the same item would split one
    item's reporting across two SKUs and burn the natural one.
    """
    print("\n=== SALE PRODUCTS ===", flush=True)
    # "items", the same key the payables crosswalk uses for the same kind of
    # record, keyed the same way - so the two files stay comparable by eye.
    xw = state.xw.setdefault("items", {})
    ap_items = _ap_crosswalk().get("items") or {}
    for key in sorted(items):
        rec = xw.get(key)
        if rec and rec.get("ledger"):
            continue
        it = items[key]
        unit = B.platform_unit(it.get("um") or it.get("stock_um"))
        raw = s(it.get("item_raw")) or s(it["item"]).replace("-", "")
        sku = "SAGE-%s-%s" % (raw, unit)
        name = s(it.get("descr")) or s(it["item"])

        pid = (rec or {}).get("productId")
        adopted = (rec or {}).get("adopted")
        if not pid and ap_items.get(key, {}).get("productId"):
            pid, adopted = str(ap_items[key]["productId"]), "payables crosswalk"
        if not pid:
            try:
                existing = B.find_product_by_sku(api, sku)
            except LookupFailed as exc:
                print("  DEFER %s: %s" % (sku, exc)); continue
            if existing:
                pid, adopted = str(existing["productId"]), "sku search"
                name = existing.get("productName") or name
        if not pid:
            # B._item_payload reads unitcost, rate and hsn, and an OEINVD row
            # carries none of those names. Adapt rather than widen the SQL:
            # the product master wants an INR unit price (fx comes attached to
            # the representative line by phase_masters), the rate Sage states
            # on the line, and no line HSN at all - which is correct, because
            # resolve_item_hsn then falls through to Sage's own ICITEMO item
            # master rather than to a value invented here.
            it = dict(it,
                      unitcost=q2(D(str(it.get("price") or 0))
                                  * D(str(it.get("fx") or 1))),
                      rate=D(str(it.get("rate_tax1") or 0)),
                      hsn=None)
            payload = B._item_payload(it, unit, sku, name)
            st, body = api.post("/product/", payload)
            if not api.ok(st, body):
                if any(e in api.err(body) for e in B.PRODUCT_EXISTS_ERRORS):
                    try:
                        existing = B.find_product_by_sku(api, sku)
                    except LookupFailed as exc:
                        print("  DEFER %s: %s" % (sku, exc)); continue
                    if existing:
                        pid, adopted = str(existing["productId"]), "sku collision"
                    else:
                        # Taken by a row adoption cannot see. Same rule as the
                        # payables path: never mint a -R2 variant.
                        print("  BURNED %s - SKU taken by a row adoption cannot "
                              "see" % sku)
                        state.xw.setdefault("burned", {})[key] = sku
                        state.save()
                        continue
                else:
                    print("  FAIL %s: %s" % (sku, api.err(body))); continue
            else:
                if api.dry_run:
                    continue
                pid = str((api.data(body) or {}).get("productId"))
                # POST /product/ saves status NULL; force it ACTIVE.
                api.call("PATCH", "/product/%s/ACTIVE" % pid)
        led = B.item_ledger_for(api, pid, ITEM_LEDGER_MAPPING)
        if not led:
            # Without this the line lands with a NULL financeAccountId while
            # still reporting success, and verify fails with "No Finance
            # Account Exists for product".
            print("  FAIL revenue ledger for %s" % sku); continue
        xw[key] = {"productId": str(pid), "skuCode": sku, "unit": unit,
                   "name": name, "ledger": str(led), "ledgerType": ITEM_LEDGER_MAPPING}
        if adopted:
            xw[key]["adopted"] = adopted
        state.save()
        print("  %-9s %-30s %-6s ledger=%s"
              % ("adopted" if adopted else "created", sku, unit, led))
    print("  sale products in crosswalk: %d" % len(xw))


# ============================================================================
# THE PAYLOAD
# ============================================================================

PICKUP_ADDR = {"addressId": B.COMPANY_ADDRESS_ID, "partyId": ORG_ID,
               "partyType": "ORGANISATION", "status": "ACTIVE"}


def series_number(api, sage_date, counter_type):
    """The document's own counter. One series name serves ONE financial year -
    the payables path records that the counter's uniqueness key has no year in
    it, so a name reused across years collides."""
    fy = financial_year(sage_date)
    name = B.SERIES_BY_FY.get(fy)
    if not name:
        raise CounterFailed("no series configured for FY %s - one name serves "
                            "ONE year (post_sage_bills.SERIES_BY_FY)" % fy)
    path = ("series=%s&counterType=%s&associatedEntityType=PAN"
            "&associatedEntityId=%s&associatedFinancialYear=%s"
            % (name, counter_type, ORG_PAN, fy))
    st, body = api.get("/counter/series/values?%s" % path)
    d = api.data(body) if api.ok(st, body) else None
    if d and d.get("value") is not None:
        value = str(int(d["value"]) + 1)
    else:
        # First document of this counterType in this year: the series has to
        # exist before a value can be taken from it.
        if not api.dry_run:
            st2, b2 = api.post("/counter/?%s&value=1" % path)
            if not api.ok(st2, b2):
                raise CounterFailed(
                    "counter series %s/%s for FY %s could not be created: %s"
                    % (name, counter_type, fy, api.err(b2)))
        value = "1"
    return {"series": name, "value": value,
            "associatedEntityType": "PAN", "associatedEntityId": ORG_PAN,
            "associatedFinancialYear": fy}


def build_invoice_payload(api, shape, buyer, products):
    code, invoice = shape["key"]
    fx, curn = shape["fx"], shape["currency"]
    lines = []
    for l, (amt, up, qty) in zip(shape["lines"], shape["inr"]):
        pr = products[B.item_key(l)]
        rate = shape["rates"][s(l["line_no"])]
        unit = s(pr.get("unit")) or "OTH"
        hsn, hsn_src = B.resolve_item_hsn(l)
        text = s(l["descr"]) or s(l["item"]) or pr["name"]
        if curn != "INR":
            # The document is booked in INR, so the line no longer shows what
            # the customer was actually invoiced. Keep it on the line itself,
            # not only in metadata, because this is what a human reconciling
            # against the Sage document reads.
            text = "%s (line %s) | orig %s %s @ %s" % (
                text, s(l["line_no"]), curn, q2(D(str(l["ext"] or 0))), fx)
        lines.append({
            "productId": pr["productId"], "skuCode": pr["skuCode"],
            "productName": pr["name"], "description": text,
            "quantity": float(qty), "displayQuantity": float(qty),
            "unit": unit, "displayUnit": unit,
            # Six decimals, and derived from the line's INR amount - the
            # voucher's revenue leg is quantity x unitPrice, not the line
            # amount, so rounding this to the paise moves the voucher.
            "unitPrice": float(up),
            "itemPrice": float(amt), "taxableAmount": float(amt),
            "totalPrice": float(q2(amt * (1 + rate / 100))),
            "gstPercentage": float(rate),
            # The payables path records "omitting cessType is a bare NPE at
            # BillServiceImpl:2062", and the invoice side has the same shape:
            # InvoiceInfoConverter (the PDF path, which runs INSIDE the create
            # and rolls it back on failure) sums getCessAmount() across every
            # line. A null there is the same bare NPE with no field named.
            "cessType": "IN_RUPEES", "cessAmount": 0, "cessPercentage": 0,
            "discount": 0, "itemDiscount": 0, "discountAmount": 0,
            "taxableOtherCharge": 0,
            "hsn": hsn, "lineItemType": "GOODS",
            # Persisted from here only; leaving it out lands a NULL ledger on
            # the line and verify then refuses the document.
            "financeAccountDto": {"financeAccountId": pr["ledger"]},
            "metaData": {"sageInvoice": invoice, "sageCustomer": code,
                         "sageLine": s(l["line_no"]),
                         "sageItem": s(l.get("item_raw")) or s(l["item"]),
                         "sageItemFmt": s(l["item"]),
                         "sageUnit": s(l.get("um")),
                         "sageDocAmount": str(q2(D(str(l["ext"] or 0)))),
                         # Sage's OWN stated unit price, which does not always
                         # reproduce its own extension. Kept so the difference
                         # is findable from the document.
                         "sageUnitPrice": str(D(str(l["price"] or 0))),
                         "sageCogs": str(q2(D(str(l["cogs"] or 0)))),
                         "hsnSource": hsn_src,
                         "migrationSource": "IDEDAT"},
        })

    buyer_addr = {"addressId": buyer["addressId"], "partyId": buyer["contactId"],
                  # Re-checked on every address object; the refusal for a
                  # missing one misleadingly reads "Address : null is not active".
                  "partyType": "CONTACT", "status": "ACTIVE"}
    payload = {
        "organisationId": ORG_ID,
        "invoiceNumber": dict(series_number(api, shape["inv_date"],
                                            shape["counter_type"]),
                              documentNumber=invoice),
        "buyer": {"contactId": buyer["contactId"], "accountName": buyer["name"],
                  "companyName": buyer["name"],
                  "registrationType": buyer["registrationType"]},
        # The BUYER ledger - Sundry Debtors. A dual-role party also has a
        # Creditors ledger, and putting a receivable there would land it on the
        # wrong side of the balance sheet.
        "buyerFinanceAccountId": buyer["ledger"],
        # A null pocName NPEs in PDF generation AFTER the invoice row is
        # written, and the create is rolled back with an error naming nothing.
        "pocInfo": {"pocName": DEFAULT_POC},
        "billingAddress": buyer_addr, "shippingAddress": dict(buyer_addr),
        "vendorBillingAddress": B.COMPANY_ADDR,
        "pickupAddress": PICKUP_ADDR,                      # mandatory
        "invoiceDate": epoch_ms(shape["inv_date"]),
        "dueDate": epoch_ms(shape["due_date"]),
        "voucherDate": epoch_ms(shape["inv_date"]),
        # A payment term is mandatory AND has to be one the converter can act
        # on. InvoiceConverter.convertFrom RECOMPUTES dueDate from it, and its
        # branches are guarded by hasCredit()/hasCreditDays()/hasUponDelivery()/
        # hasAdvance(), every one of which requires a value GREATER THAN ZERO
        # (PaymentTermDto.isValidValue). A term that satisfies none of them -
        # {credit:100, creditDays:0}, which is what Sage's DATEDUE = DATEINVC
        # produces - falls past every branch and NPEs, exactly as a null term
        # does. Proven against the live API: creditDays 0 gives
        # NullPointerException, creditDays 30 creates.
        #
        # So a document with no credit period is sent as advance 100, whose
        # branch sets dueDate = invoiceDate. That IS what Sage is saying when
        # DATEDUE equals DATEINVC: the whole amount is due at once, with no
        # credit period to run.
        "paymentTerm": ({"credit": 100, "creditDays": shape["credit_days"]}
                        if shape["credit_days"] > 0 else {"advance": 100}),
        # Anything else and verify posts ZERO legs - the document exists and
        # has no accounting impact at all.
        "invoiceStatus": "ACTIVE",
        "invoiceType": shape["invoice_type"], "invoiceSubType": "MATERIAL",
        "transactionType": "REGULAR",
        "salesType": shape["sales_type"],
        # Booked in INR at rate 1. The voucher path never reads
        # conversionRate (EntityVoucherEntryCreateHelperService has no
        # invoiceDto.getConversionRate() call at all), so a rate here would be
        # silently ignored and the voucher would carry document-currency
        # figures as rupees.
        "currencyDto": {"currency": "INR"}, "conversionRate": 1,
        "totalItemPrice": float(shape["taxable"]),
        "gstPrice": float(shape["tax"]),
        "totalPrice": float(shape["home_total"]),
        # Never send a round-off: hasRoundOff makes the server substitute its
        # own nearest-rupee figure and discard the exact paise.
        "hasRoundOff": False,
        "invoiceLineItemDtos": lines,
        "remarks": "Sage %s | %s" % (invoice, (
            "orig %s %s @ %s = INR %s" % (curn, shape["doc_total"], fx,
                                          shape["home_total"])
            if curn != "INR" else "INR %s" % shape["home_total"])),
        "metadata": {"migrationSource": "IDEDAT", "sageInvoice": invoice,
                     "sageCustomer": code,
                     "sageHomeAmount": str(shape["home_total"]),
                     "sageOpenAmount": str(shape["open"]),
                     "sageDocCurrency": curn,
                     "sageDocAmount": str(shape["doc_total"]),
                     "sageRate": str(fx), "sageCogs": str(shape["cogs"]),
                     "sageKind": shape["kind"],
                     "sageRegistration": shape["registration"]},
    }
    # Set for EVERY invoice, not only exports - see the note in
    # classify_invoice: the server switches on it without a null check.
    payload["supplyType"] = shape["supply_type"]
    if shape["kind"] == "EXPORT":
        g = shape["export"]
        payload.update({
            "supplyType": shape["supply_type"],
            "originCountryDto": {"country": "INDIA"},
            "destinationCountryDto": {"country": g["dest"]},
            "loadingPortCode": g["port"],
            # Lowercase-acronym keys. igstExemption is silently ignored.
            "igstexemption": "UNDER_LUT",
            "igstexemptionNumber": g["lut_number"],
            "igstexemptionDate": epoch_ms(g["lut_date"]),
        })
        payload["metadata"]["lutFinancialYear"] = g["lut_fy"]
        payload["metadata"]["sageDestinationSource"] = g["dest_source"]
        payload["metadata"]["sageShippingBill"] = g["shipping_bill"]
        payload["metadata"]["sageShippingBillDate"] = g["shipping_bill_date"]
        payload["remarks"] += " | SB %s" % g["shipping_bill"]
    # NOTE what is deliberately NOT here. The payload above is the shape that
    # is PROVEN to post and verify on this box, and that proof is an EXPORT
    # invoice. A domestic tax invoice may additionally want the buyer's GSTIN
    # and a place of supply on the document itself - but on this build the
    # platform resolves both from the contact, and adding a field this service
    # does not know is a 400 on every document rather than a better one. So the
    # GSTIN rides in metadata, where it costs nothing and is searchable, and if
    # verify ever asks for it on the document the refusal will name the field.
    if shape["gstin"]:
        payload["metadata"]["sageBuyerGstin"] = shape["gstin"]
    return payload


def assert_ar_invariants(payload, shape):
    """Assert per line and per invoice, to the paise, and fail loudly.

    The one that matters most is the last: the figure posted has to be Sage's
    own AMTINVCHC. Everything above it exists to make a failure say WHERE the
    paise went missing rather than only that they did.
    """
    problems, total, tax, legs = [], D(0), D(0), D(0)
    for li in payload["invoiceLineItemDtos"]:
        tx, ip = q2(li["taxableAmount"]), q2(li["itemPrice"])
        up, qty = D(str(li["unitPrice"])), D(str(li["quantity"]))
        # The revenue leg, and this is literally how the backend computes it:
        # getNetItemAmount() is multiply(quantity, unitPrice). NOT q2 on
        # unitPrice itself - it stores 6dp and a converted unit price needs
        # them; round the PRODUCT, never the rate that forms it.
        #
        # getTaxableAmount() is the same product plus taxableOtherCharge minus
        # discountAmount, so the platform DERIVES taxableAmount too and ignores
        # what is sent. Asserting q2(unitPrice x quantity) == taxableAmount is
        # what makes the derived figure equal the intended one.
        if q2(up * qty) != tx:
            problems.append("line %s: unitPrice x quantity %s != taxableAmount %s"
                            % (li["metaData"]["sageLine"], q2(up * qty), tx))
        if ip != tx:
            problems.append("itemPrice %s != taxableAmount %s" % (ip, tx))
        want = q2(tx * (1 + D(str(li["gstPercentage"])) / 100))
        if q2(li["totalPrice"]) != want:
            problems.append("totalPrice %s != taxable x (1+rate) %s"
                            % (q2(li["totalPrice"]), want))
        total += tx
        tax += want - tx
        legs += q2(up * qty)
    if q2(total) != shape["taxable"]:
        problems.append("sum(line taxable) %s != invoice taxable %s"
                        % (q2(total), shape["taxable"]))
    if q2(tax) != shape["tax"]:
        problems.append("sum(line GST) %s != invoice GST %s"
                        % (q2(tax), shape["tax"]))
    if q2(shape["taxable"] + shape["tax"]) != shape["home_total"]:
        problems.append("taxable %s + GST %s != home total %s"
                        % (shape["taxable"], shape["tax"], shape["home_total"]))
    # The voucher itself: the revenue legs have to equal the debtor leg.
    if q2(legs) + shape["tax"] != shape["home_total"]:
        problems.append("revenue legs %s + GST %s != debtor leg %s"
                        % (q2(legs), shape["tax"], shape["home_total"]))
    if q2(payload["totalPrice"]) != q2(shape["header"]["home_total"]):
        problems.append("totalPrice %s != Sage AMTINVCHC %s"
                        % (q2(payload["totalPrice"]),
                           q2(shape["header"]["home_total"])))
    return problems


# ============================================================================
# SELECTION
# ============================================================================

def reason_group(why):
    """The stable claim at the front of a hold reason, without its figures.

    Every reason is written as "<claim> - <the numbers>", so grouping on the
    claim collapses a hundred documents that failed the same way into one
    countable line. Grouping on the whole string put each distinct delta in a
    group of its own, which is how a real pattern hides in the noise."""
    head = why.split(" - ")[0]
    return head if len(head) <= 100 else head[:97] + "..."


def shape_book(book, docs=None):
    """-> (list of shapes, Counter of grouped hold reasons, list of held rows)"""
    cust = customers_master()
    out, held, rows = [], collections.Counter(), []
    for key in sorted(book):
        if docs and key[1] not in docs and "%s|%s" % key not in docs:
            continue
        c = cust.get(key[0])
        why = None
        shape = None
        if not c:
            why = "no ARCUS row for the customer - %s" % key[0]
        else:
            shape, why = classify_invoice(book[key], c)
        if why:
            held[reason_group(why)] += 1
            rows.append({"customer": key[0], "invoice": key[1],
                         "home_total": str(q2(D(str(
                             book[key]["header"]["home_total"] or 0)))),
                         "open": str(q2(D(str(
                             book[key]["header"]["home_open"] or 0)))),
                         "reason": why})
            continue
        out.append(shape)
    return out, held, rows


def eligible(shapes, state):
    """Only what this run can actually post: masters present, not yet posted."""
    buyers = state.xw.get("buyers") or {}
    products = state.xw.get("items") or {}
    ready, skips = [], collections.Counter()
    for sh in shapes:
        code, invoice = sh["key"]
        buyer = buyers.get(code) or {}
        if not buyer.get("ledger"):
            skips["buyer %s has no contact/ledger yet (run ar-masters)" % code] += 1
            continue
        # An export has to go out to a party the platform holds as foreign.
        # ContactServiceImpl overwrites country from the ORG for every
        # registration type except INTERNATIONAL, so a buyer filed any other
        # way is stored in India however it was sent - and an
        # EXPORT_WITHOUT_PAYMENT invoice against an Indian-registered,
        # GST-less buyer is a tax document that contradicts itself.
        #
        # ensure_buyers now files these correctly at CREATE time. This catches
        # the ones already in the crosswalk from before that fix, which cannot
        # be repaired through the API on this build - PUT /contact/{id}/update
        # answers "address can not be null", then "Already business Info
        # exists", then a bare NullPointerException, and DELETE /contact/{id}
        # does not exist at all. So they are skipped and named, not posted.
        if sh["kind"] == "EXPORT" and buyer.get("registrationType") != "INTERNATIONAL":
            skips["buyer %s is registered %s, but this is an export - the "
                  "platform files a non-INTERNATIONAL party in INDIA. Repair "
                  "the contact by hand." % (code, buyer.get("registrationType"))] += 1
            continue
        missing = [B.item_key(l) for l in sh["lines"]
                   if not products.get(B.item_key(l), {}).get("ledger")]
        if missing:
            skips["%d item(s) have no sale product/ledger yet (run ar-masters)"
                  % len(missing)] += 1
            continue
        ready.append(sh)
    return ready, skips


# ============================================================================
# PHASES
# ============================================================================

SQL_PROBE_TRXTYPE = """
SELECT o.TRXTYPEID AS t, MIN(RTRIM(o.TRXTYPETXT)) AS txt,
       RTRIM(o.SRCEAPPL) AS srce, COUNT(*) AS n,
       SUM(CASE WHEN o.AMTDUEHC > 0 THEN 1 ELSE 0 END) AS open_docs,
       CAST(SUM(o.AMTDUEHC) AS decimal(19,2)) AS open_amt
  FROM AROBL o GROUP BY o.TRXTYPEID, RTRIM(o.SRCEAPPL)
 ORDER BY o.TRXTYPEID, RTRIM(o.SRCEAPPL)
"""
# The SAME filters as SQL_OPEN_CONTROL. Without them this counted every open
# AROBL row of any transaction type and any date, so it reported 1,920
# documents beside a control total of 259 - two numbers that look like they
# should agree and cannot.
SQL_PROBE_CURN = """
SELECT RTRIM(o.CODECURN) AS c, COUNT(*) AS n,
       CAST(SUM(o.AMTDUEHC) AS decimal(19,2)) AS open_amt
  FROM AROBL o
 WHERE o.TRXTYPEID IN (12, 14) AND o.DATEINVC < %s AND o.AMTDUEHC > 0
 GROUP BY RTRIM(o.CODECURN) ORDER BY COUNT(*) DESC
"""


def phase_probe(api, state, args):
    """Verify the schema this loader assumes, and report the population.

    Read-only, and the first thing to run on a new Sage box: the AR/OE column
    names come from the migration brief rather than from an inventory, and this
    settles every one of them - by evidence, not by memory."""
    print("\n=== SAGE AR SCHEMA ===")
    gaps, have = verify_schema(best_effort=True)
    for tbl in sorted(set(SCHEMA) | set(SCHEMA_BEST_EFFORT)):
        n = len(have.get(tbl, ()))
        mark = "GAPS" if tbl in gaps else "ok  "
        print("  %-4s %-9s %d columns" % (mark, tbl, n))
    if gaps:
        print("\n--- what does not match, with candidates ---")
        report_gaps(gaps, have)
        print("\n  Fix these in SCHEMA and in the query beside it. Until then a")
        print("  load refuses to start: %s"
              % ("required tables affected" if set(gaps) & set(SCHEMA)
                 else "only the best-effort export register, so exports hold "
                      "and the rest can still load"))
    else:
        print("\n  every assumed column exists")

    # By evidence, never from memory. The payables side records an earlier
    # script using IDTRXTYPE = 1 for a payable invoice and matching zero rows,
    # because in APOBL an invoice is 12. Here the loader takes TRXTYPEID 12 and
    # 14 - both of which carry TRXTYPETXT '1' - and only the OE ones have
    # OEINVD lines. This is the table that says so.
    print("\n=== AROBL TRXTYPEID x SRCEAPPL (which rows are invoices) ===")
    print("  %-5s %-5s %-5s %9s %9s %18s"
          % ("TRXID", "TXT", "SRCE", "rows", "open", "open amount"))
    try:
        for r in sage_query(SQL_PROBE_TRXTYPE):
            print("  %-5s %-5s %-5s %9s %9s %18s"
                  % (r["t"], r["txt"], r["srce"] or "-", r["n"], r["open_docs"],
                     r["open_amt"]))
        print("     this loader takes TRXTYPEID 12 and 14. Only SRCEAPPL='OE'")
        print("     rows carry O/E lines; 'AR' rows are held with a reason.")
    except Exception as exc:                                    # noqa: BLE001
        print("  unavailable: %s" % str(exc).split("\n")[0][:120])

    print("\n=== OPEN AT CUTOVER %s (what this loader would load) ===" % CUTOVER)
    try:
        for r in sage_query(SQL_OPEN_CONTROL, (CUTOVER,)):
            print("  documents      %s" % r["docs"])
            print("  customers      %s" % r["customers"])
            print("  invoiced (INR) %s" % r["home_total"])
            print("  open     (INR) %s" % r["home_open"])
            print("  date spread    %s .. %s" % (r["first_date"], r["last_date"]))
        print("\n  currency of those documents:")
        for r in sage_query(SQL_PROBE_CURN, (CUTOVER,)):
            print("    %-5s %5s docs   open %16s"
                  % (r["c"] or "(blank)", r["n"], r["open_amt"]))
    except Exception as exc:                                    # noqa: BLE001
        print("  unavailable: %s" % str(exc).split("\n")[0][:120])

    # The LUT comes from .env, not from Sage - it is the org's own registration
    # detail. Report what CSCOM holds so the two can be compared by eye.
    print("\n=== LUT ===")
    print("  .env SME_LUT_NUMBER=%s  SME_LUT_DATE=%s"
          % (LUT_NUMBER or "(unset - exports will be held)", LUT_DATE or "(unset)"))
    cs = sorted(c for c in have.get("CSCOM", ())
                if re.search(r"LUT|GST|REG|BOND|EXPO", c))
    print("  CSCOM columns that might carry it: %s"
          % (", ".join(cs) if cs else "none matching LUT/GST/REG/BOND/EXPO"))

    if args.all_columns:
        path = os.path.join(WORK, "ar-schema.json")
        os.makedirs(WORK, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({t: sorted(c) for t, c in have.items()}, fh, indent=1)
        print("\n  every column of every probed table -> %s" % path)
    print("\n  probe done. Nothing was written to Sage or to SMEAssist.")


def _fixture(lines, home, doc, fx, curn, exp=None, dt=20260325, dest="FRANCE"):
    return {"key": ("C1", "INV1"), "lines": lines, "export": exp,
            "header": {"inv_date": dt, "due_date": 20260424,
                       "home_total": home, "home_open": home,
                       "currency": curn, "doc_total": doc, "fx_rate": fx,
                       "dest_country": dest, "srce": "OE"}}


def _fline(ln, qty, price, ext, rate=None, item="ID41354X",
           descr="MENS SHIRTS"):
    """A Sage O/E line. `rate` None means Sage names NO tax authority; a number
    is split SGST/CGST the way an intra-state sale really is, with each half's
    tax rounded to the paise separately - which is what makes the rounding
    guard below reproduce the real thing rather than a contrived one."""
    l = {"line_no": ln, "item": item, "item_raw": item.replace("-", ""),
         "descr": descr, "um": "PCS", "qty": qty, "price": price, "ext": ext,
         "cogs": 0, "category": ""}
    for i in TAX_SLOTS:
        l["tauth%d" % i], l["trate%d" % i], l["tamount%d" % i] = "", 0, 0
    if rate is not None:
        half = D(str(rate)) / 2
        for i, auth in ((1, "SGST"), (2, "CGST")):
            l["tauth%d" % i] = auth
            l["trate%d" % i] = half
            l["tamount%d" % i] = q2(D(str(ext)) * half / 100)
    return l


# Masked in the repo's own style: the state code and the 6th character - the
# two positions this code actually tests - are the parts that matter.
_INTL = {"name": "COLUMBIA SPORTSWEAR COMPANY", "country": "USA",
         "brn_raw": "", "legal_name": "", "state_raw": "", "pincode": "",
         "city": "", "street1": "", "street2": "", "street3": "",
         "street4": "", "contact_person": "", "phone1": "", "email1": "",
         "currency": "USD"}
_DOM = dict(_INTL, name="ACME TEXTILES PVT LTD", country="INDIA",
            brn_raw="29AABCA1234C1ZX", state_raw="KARNATAKA",
            pincode="560059", city="Bengaluru", currency="INR")
_EXP = {"port_code": "INKAT1", "shipping_bill": "1916068",
        "shipping_bill_date": 20260328}


def _payload_for(shape, cust_name, reg):
    """Push a shape all the way through the real payload builder."""
    prod = {B.item_key(l): {"productId": "P", "skuCode": "S",
                            "name": "MENS SHIRTS",
                            "unit": B.platform_unit("PCS"), "ledger": "L"}
            for l in shape["lines"]}
    buyer = {"contactId": "C", "name": cust_name, "ledger": "D",
             "addressId": "A", "registrationType": reg, "state": "UNKNOWN",
             "country": "UNITED_STATES"}
    return build_invoice_payload(_StubApi(), shape, buyer, prod)


def phase_selftest(api, state, args):
    """Run the conversion arithmetic and every guard against known figures.

    No Sage, no API, no token - so this runs anywhere, and run_all.sh gates on
    it. It is the one part of the loader that can be verified without the
    environment, and the part where a defect is SILENT: a voucher that misses
    Sage by a paise still posts, still verifies, and is only found much later
    by reconciliation. The reference figures are the proven Columbia export,
    USD 26,405.41 at 92.55 = INR 24,43,820.70 over four lines.
    """
    global LUT_BY_FY
    # FY2025-2026, which is the year the reference invoice (25-Mar-2026) is in.
    LUT_BY_FY = dict(LUT_BY_FY, **{"2025-2026": ("AD290426001291E", "20250401")})
    # Short-circuit the ICITEMO lookup so nothing reaches for Sage.
    B._ITEM_HSN.setdefault("", "")
    fails = []

    # ---------------------------------------------------- the proven invoice
    print("\n=== 1. THE PROVEN EXPORT INVOICE, END TO END ===")
    lines = [_fline(ln, qty, price, ext)
             for ln, qty, price, ext in ((32, 1612, "4.17", "6722.04"),
                                         (64, 1078, "4.23", "4559.94"),
                                         (96, 2998, "4.12", "12351.76"),
                                         (128, 681, "4.07", "2771.67"))]
    shape, why = classify_invoice(
        _fixture(lines, "2443820.70", "26405.41", "92.55", "USD", _EXP), _INTL)
    if why:
        print("  classify FAILED: %s" % why)
        return 1
    print("  kind=%s salesType=%s supplyType=%s counter=%s type=%s"
          % (shape["kind"], shape["sales_type"], shape["supply_type"],
             shape["counter_type"], shape["invoice_type"]))
    print("  doc %s %s @ %s -> INR %s   taxable=%s gst=%s"
          % (shape["currency"], shape["doc_total"], shape["fx"],
             shape["home_total"], shape["taxable"], shape["tax"]))
    payload = _payload_for(shape, _INTL["name"], "INTERNATIONAL")
    print("\n  line   quantity     INR amount   unitPrice (6dp)  qty x unitPrice"
          "   direct")
    legs = D(0)
    for li, l in zip(payload["invoiceLineItemDtos"], lines):
        up, qty = D(str(li["unitPrice"])), D(str(li["quantity"]))
        legs += q2(up * qty)
        print("  %-6s %9s %14s %17s %16s %8s"
              % (li["metaData"]["sageLine"], qty, q2(li["taxableAmount"]), up,
                 q2(up * qty), q2(D(str(l["ext"])) * shape["fx"])))
    print("\n  revenue legs %s   debtor leg %s   Sage AMTINVCHC %s"
          % (q2(legs), q2(payload["totalPrice"]), shape["home_total"]))
    bad = assert_ar_invariants(payload, shape)
    if bad:
        for b in bad:
            print("    FAIL %s" % b)
        fails.append("proven export invoice")
    else:
        print("  every invariant holds to the paise. The voucher ties to Sage.")

    # ------------------------------- the residual, where it does NOT cancel
    # On the Columbia figures the per-line roundings happen to cancel, so that
    # invoice alone would pass even with the absorption removed. This one does
    # not: 0.99 x 3.37 = 3.3363 -> 3.34, while each 0.33 x 3.37 = 1.1121 ->
    # 1.11 and three of those make 3.33. One paise, which is the whole point.
    print("\n=== 2. THE CONVERSION RESIDUAL, WHERE ROUNDING DOES NOT CANCEL ===")
    sh2, why2 = classify_invoice(_fixture(
        [_fline(32, 3, "0.11", "0.33"), _fline(64, 3, "0.11", "0.33"),
         _fline(96, 3, "0.11", "0.33")], "3.34", "0.99", "3.37", "USD", _EXP),
        _INTL)
    if why2:
        print("  FAIL classify: %s" % why2)
        fails.append("residual")
    else:
        amts = [a for a, _u, _q in sh2["inr"]]
        print("  line amounts %s   sum %s   Sage %s"
              % ([str(a) for a in amts], sum(amts), sh2["home_total"]))
        pl2 = _payload_for(sh2, _INTL["name"], "INTERNATIONAL")
        bad2 = assert_ar_invariants(pl2, sh2)
        if sum(amts) != D("3.34") or amts[0] == amts[-1] or bad2:
            print("  FAIL %s" % (bad2 or "the residual was not absorbed"))
            fails.append("residual")
        else:
            print("  the last line absorbed the paise and every invariant holds")

    # -------------------------------------------------- domestic GST invoice
    print("\n=== 3. DOMESTIC GST INVOICE ===")
    sh3, why3 = classify_invoice(_fixture(
        [_fline(32, 100, "600.00", "60000.00", 18),
         _fline(64, 200, "200.00", "40000.00", 18)],
        "118000.00", "118000.00", 1, "INR"), _DOM)
    if why3:
        print("  FAIL classify: %s" % why3)
        fails.append("domestic")
    else:
        pl3 = _payload_for(sh3, _DOM["name"], "GST")
        bad3 = assert_ar_invariants(pl3, sh3)
        # supplyType is NOT export-only: the server switches on it without a
        # null guard, so every invoice carries one. What must not leak onto a
        # domestic document is the LUT/export block.
        leaked = [k for k in ("igstexemption", "igstexemptionNumber",
                              "loadingPortCode", "destinationCountryDto")
                  if k in pl3]
        print("  taxable=%s gst=%s total=%s counter=%s supplyType=%s"
              % (sh3["taxable"], sh3["tax"], q2(pl3["totalPrice"]),
                 sh3["counter_type"], pl3.get("supplyType")))
        if bad3 or leaked or sh3["tax"] != D("18000.00") \
                or pl3.get("supplyType") != "B2B":
            print("  FAIL %s" % (bad3 or leaked and "export block leaked: %s"
                                 % leaked or "supplyType=%s"
                                 % pl3.get("supplyType")))
            fails.append("domestic")
        else:
            print("  taxable + GST ties to AMTINVCHC; B2B; no export/LUT block")

    # ------------------------- an extension Sage does not derive from price
    # Real and measured: IDEPLACC2025-010 states UNITPRICE 0.000018 against an
    # extension of 2,850.00 over 30,000 units, with no discount anywhere on
    # the line. This must POST - the extension is the money - and must say so.
    print("\n=== 3b. AN EXTENSION THAT quantity x price DOES NOT GIVE ===")
    sh3b, why3b = classify_invoice(_fixture(
        [_fline(32, 30000, "0.000018", "2850.00")], "263767.50", "2850.00",
        "92.55", "USD", _EXP), _INTL)
    if why3b:
        print("  FAIL it was held, and it should post: %s" % why3b)
        fails.append("extension divergence")
    else:
        pl3b = _payload_for(sh3b, _INTL["name"], "INTERNATIONAL")
        bad3b = assert_ar_invariants(pl3b, sh3b)
        li = pl3b["invoiceLineItemDtos"][0]
        print("  posted: qty=%s unitPrice=%s taxable=%s   Sage's own price kept "
              "as %s" % (li["quantity"], li["unitPrice"], li["taxableAmount"],
                         li["metaData"]["sageUnitPrice"]))
        print("  note recorded: %s" % (sh3b["price_notes"] or "NONE"))
        if bad3b or not sh3b["price_notes"]:
            print("  FAIL %s" % (bad3b or "the divergence was not recorded"))
            fails.append("extension divergence")
        else:
            print("  the extension posts, the divergence is recorded, "
                  "invariants hold")

    # ----------------------------------------------------------- the guards
    # Every one of these is a hold rather than a guess, and the reason is what
    # a person reads in work/ar_held.json.
    print("\n=== 4. THE GUARDS - each must HOLD, never post ===")
    cases = [
        ("a wrong total/rate column",
         _fixture([_fline(32, 100, "1.00", "100.00")], "9999.00", "100.00",
                  "92.55", "USD", _EXP), _INTL, "Sage disagrees with itself"),
        ("a line at zero quantity",
         _fixture([_fline(32, 0, "1.00", "0.00")], "9255.00", "100.00",
                  "92.55", "USD", _EXP), _INTL, "zero quantity"),
        ("an export scrip",
         _fixture([_fline(32, 1, "100.00", "100.00", item="ROSCTL-SCRIP",
                          descr="ROSCTL scrip sale")], "9255.00", "100.00",
                  "92.55", "USD", _EXP), _INTL, "journal voucher"),
        ("an export with no shipping register",
         _fixture([_fline(32, 100, "1.00", "100.00")], "9255.00", "100.00",
                  "92.55", "USD", None), _INTL, "no IESHPRGH row"),
        ("an export with no destination anywhere",
         _fixture([_fline(32, 100, "1.00", "100.00")], "9255.00", "100.00",
                  "92.55", "USD", _EXP, dest=""),
         dict(_INTL, country=""), "no destination country anywhere"),
        ("a GST rate that is not a legal slab",
         _fixture([_fline(32, 100, "1.00", "100.00", "17.5")], "117.50",
                  "117.50", 1, "INR"), _DOM, "not total a legal slab"),
        ("a domestic line naming no tax authority",
         _fixture([_fline(32, 100, "1.00", "100.00")], "100.00",
                  "100.00", 1, "INR"), _DOM, "names no tax authority"),
        ("per-authority rounding no slab can reproduce",
         _fixture([_fline(32, 100, "10.05", "1005.00", 5)], "1055.26",
                  "1055.26", 1, "INR"), _DOM,
         "rounds each tax authority separately"),
        ("a GST invoice in a foreign currency",
         _fixture([_fline(32, 100, "1.00", "100.00", 18)], "10920.90",
                  "118.00", "92.55", "USD", None), _DOM, "that is a decision"),
        ("an invoice with no O/E lines",
         _fixture([], "100.00", "100.00", 1, "INR"), _DOM, "no O/E lines"),
    ]
    for label, rec, cust, want in cases:
        got = classify_invoice(rec, cust)[1]
        ok = want in (got or "")
        print("  %-4s %-38s %s" % ("ok" if ok else "FAIL", label,
                                   (got or "(ACCEPTED - it should not be)")[:60]))
        if not ok:
            fails.append(label)

    # An inter-unit self-invoice is the same legal entity on both sides.
    if len(ORG_PAN) == 10:
        own = dict(_DOM, name="INTER-UNIT", brn_raw="29%s1ZX" % ORG_PAN)
        sh5, why5 = classify_invoice(_fixture(
            [_fline(32, 100, "1.00", "100.00", 5)], "105.00", "105.00", 1,
            "INR"), own)
        ok = not why5 and sh5["invoice_type"] == "TRANSFER_INVOICE" \
            and sh5["counter_type"] == "TRANSFER_INVOICE"
        print("  %-4s %-38s %s" % ("ok" if ok else "FAIL",
                                   "same PAN -> TRANSFER_INVOICE",
                                   why5 or "invoiceType=%s counter=%s"
                                   % (sh5["invoice_type"], sh5["counter_type"])))
        if not ok:
            fails.append("transfer invoice")

    # An export in a year the configured LUTs do not cover. This is the guard
    # that matters most in practice: the LUT supplied with the brief is dated
    # 1-Apr-2026 and NOT ONE of the 152 shapeable exports is in FY2026-27.
    got = classify_invoice(_fixture(
        [_fline(32, 100, "1.00", "100.00")], "9255.00", "100.00", "92.55",
        "USD", _EXP, dt=20231115), _INTL)[1]
    ok = "no LUT configured for this invoice" in (got or "")
    print("  %-4s %-38s %s" % ("ok" if ok else "FAIL",
                               "an export in a year with no LUT",
                               (got or "(ACCEPTED)")[:60]))
    if not ok:
        fails.append("LUT-per-year guard")

    LUT_BY_FY = {}
    got = classify_invoice(_fixture(
        [_fline(32, 100, "1.00", "100.00")], "9255.00", "100.00", "92.55",
        "USD", _EXP), _INTL)[1]
    ok = "no LUT configured" in (got or "")
    print("  %-4s %-38s %s" % ("ok" if ok else "FAIL",
                               "an export with no LUT at all",
                               (got or "(ACCEPTED)")[:60]))
    if not ok:
        fails.append("LUT guard")

    print("\n%s" % ("SELF TEST FAILED: " + ", ".join(fails) if fails
                    else "SELF TEST PASSED - %d checks" % (16 + len(cases))))
    return 1 if fails else 0


class _StubApi:
    """Enough Api surface for the self test, and no network at all."""
    dry_run = True

    def get(self, p):
        return 200, {"data": None}

    def post(self, p, b=None):
        return 200, {"data": {}}

    def call(self, m, p, b=None):
        return 200, {"data": {}}

    @staticmethod
    def ok(st, body):
        return True

    @staticmethod
    def data(body):
        return body.get("data") if isinstance(body, dict) else None

    @staticmethod
    def err(body):
        return str(body)[:300]


def _selection(state, args):
    docs = set(args.docs or ())
    if args.include_settled and not docs:
        raise Stop("--include-settled needs --docs. It lifts the unpaid filter, "
                   "which is the rule that keeps already-settled documents out "
                   "of the load - so it is for reproducing ONE named document, "
                   "never for widening the population.")
    book = load_ar_book(include_settled=args.include_settled)
    shapes, held, held_rows = shape_book(book, docs or None)
    print("\n  %d invoices shaped, %d held" % (len(shapes), sum(held.values())))
    if held:
        # With the amount, because "34 held" and "34 held, INR 61 lakh" are
        # different facts and only the second one sizes the work.
        by_group = collections.defaultdict(lambda: D(0))
        for r in held_rows:
            by_group[reason_group(r["reason"])] += D(r["home_total"])
        print("\n--- held, grouped and counted (work items, not failures) ---")
        print("   %-92s %5s %16s" % ("reason", "docs", "invoiced INR"))
        for why, n in held.most_common():
            print("   %-92s %5d %16s" % (why[:92], n, q2(by_group[why])))
        os.makedirs(WORK, exist_ok=True)
        with open(os.path.join(WORK, "ar_held.json"), "w") as fh:
            json.dump(held_rows, fh, indent=1)
        print("\n   per-document detail -> work/ar_held.json")
    notes = [(sh["key"], n) for sh in shapes for n in sh.get("price_notes", ())]
    if notes:
        # Not a failure: the extension is what was charged and is what posts.
        # Recorded because a unit price that differs from Sage's own is the
        # kind of thing someone will query, and it should be findable.
        print("\n  %d line(s) across %d invoices state a unit price that does "
              "not reproduce their own extension." % (len(notes),
              len({k for k, _ in notes})))
        print("   The EXTENSION posts, and the unit price is derived from it; "
              "Sage's stated one is\n   kept on the line's metaData -> "
              "work/ar_price_notes.json")
        with open(os.path.join(WORK, "ar_price_notes.json"), "w") as fh:
            json.dump([{"customer": k[0], "invoice": k[1], "note": n}
                       for k, n in notes], fh, indent=1)
    if args.limit:
        shapes = shapes[:args.limit]
    return shapes


def phase_masters(api, state, args):
    shapes = _selection(state, args)
    # Scoped to what this run intends to post: master creation is awkward to
    # reverse, so it does not run ahead of the selection.
    codes = {sh["key"][0] for sh in shapes}
    items = {}
    for sh in shapes:
        for l in sh["lines"]:
            # The line alone does not know the document's currency, and the
            # product master is priced in the org's. Carry the rate across.
            items.setdefault(B.item_key(l), dict(l, fx=sh["fx"],
                                                 currency=sh["currency"]))
    intl = {sh["key"][0] for sh in shapes if sh["international"]}
    print("\nmasters needed for this selection: %d customers (%d of them abroad, "
          "per their own invoices), %d (item, unit) pairs"
          % (len(codes), len(intl), len(items)))
    ensure_buyers(api, state, codes, enrich=not args.no_enrich, intl_codes=intl)
    ensure_sale_products(api, state, items)


def phase_run(api, state, args, do_post):
    shapes = _selection(state, args)
    ready, skips = eligible(shapes, state)
    print("\n=== %s ===" % ("POST" if do_post else "DRY RUN"), flush=True)
    if do_post and args.include_settled:
        print("  WARNING: --include-settled. Any document below whose Sage "
              "balance is\n           already ZERO is part of the opening trial "
              "balance, and posting\n           it creates a receivable that "
              "should not exist. This is a\n           reproduction aid, not a "
              "migration step.", flush=True)
    print("  %d postable, %d waiting on masters" % (len(ready), sum(skips.values())))
    for why, n in skips.most_common():
        print("   %-100s %d" % (why, n))

    buyers = state.xw.get("buyers") or {}
    products = state.xw.get("items") or {}
    results, failures = [], []
    for sh in ready:
        code, invoice = sh["key"]
        tag = "%s|%s" % (code, invoice)
        prior = state.posted.get(tag)
        if prior and prior.rstrip().endswith("UNVERIFIED"):
            # Created but never verified: the server never wrote the voucher, so
            # the invoice shows a receivable with ZERO accounting impact. Repair
            # it rather than skipping it forever.
            iid = prior.split("||")[1]
            vst, vbody = api.post("/invoice/%s/verify" % iid)
            if api.ok(vst, vbody):
                print("  %-34s RE-VERIFIED %s" % (tag, iid))
                state.mark(tag, iid)
            else:
                print("  %-34s re-verify failed: %s" % (tag, api.err(vbody)))
                failures.append((tag, "re-verify: %s" % api.err(vbody)))
            continue
        if prior:
            continue

        try:
            payload = build_invoice_payload(api, sh, buyers[code], products)
        except CounterFailed as exc:
            # One document kind cannot be numbered. Record it and carry on -
            # this used to raise Stop, and a single unusable counter type
            # aborted the whole run partway through, leaving 16 perfectly good
            # documents unposted with no summary written.
            print("  %-34s COUNTER: %s" % (tag, exc))
            failures.append((tag, "counter: %s" % exc))
            continue
        bad = assert_ar_invariants(payload, sh)
        if bad:
            print("  INVARIANT FAIL %s: %s" % (tag, "; ".join(bad)))
            failures.append((tag, "; ".join(bad)))
            continue
        print("\n  %-34s %-9s %s" % (tag, sh["kind"], buyers[code]["name"][:34]))
        print("     %s %s @ %s -> INR %s   taxable=%s gst=%s lines=%d"
              % (sh["currency"], sh["doc_total"], sh["fx"], sh["home_total"],
                 sh["taxable"], sh["tax"], len(payload["invoiceLineItemDtos"])))
        if sh["export"]:
            print("     export to %s via %s, SB %s, LUT %s"
                  % (sh["export"]["dest"], sh["export"]["port"],
                     sh["export"]["shipping_bill"], sh["export"]["lut_number"]))
        if not do_post:
            results.append({"doc": tag, "stage": "dryrun", "ok": True})
            continue

        st, body = api.post("/invoice/", payload)
        if not api.ok(st, body):
            msg = api.err(body)
            low = msg.lower()
            # Only when the refusal is about THIS DOCUMENT's number. A bare
            # "already exists" test would also swallow a refusal about some
            # other entity - an address, a product - and mark the invoice
            # posted when nothing was, losing it silently and for good.
            # "Order Number already exists" is how this service words a
            # duplicate invoice number - observed on the live API. It names
            # neither "invoice" nor "document", so the narrower test missed it
            # and would have recorded a real duplicate as a create failure.
            if "already exists" in low and (invoice.lower() in low
                                            or "invoice" in low
                                            or "document" in low
                                            or "order number" in low):
                # A previous run posted it and the log was lost. Not a failure.
                print("     already posted (%s) - recording and moving on" % msg[:80])
                state.mark(tag, "preexisting")
                continue
            print("     CREATE FAIL: %s" % api.err(body))
            failures.append((tag, "create: %s" % api.err(body)))
            continue
        d = api.data(body) or {}
        # Take the value out FIRST and only stringify something that is there:
        # str(None) is the TRUTHY string "None", and the payables path records
        # what that cost when a 2xx came back with no id in the body - an
        # unusable id committed to the log as though it were real.
        _iid = d.get("invoiceId") or d.get("id")
        if not _iid:
            print("     CREATE returned %s with no invoiceId: %s"
                  % (st, str(body)[:200]))
            failures.append((tag, "create: 2xx with no invoiceId in the body"))
            continue
        iid = str(_iid)
        vst, vbody = api.post("/invoice/%s/verify" % iid)
        if not api.ok(vst, vbody):
            print("     created %s  VERIFY FAIL: %s" % (iid, api.err(vbody)))
            state.mark(tag, "%s||UNVERIFIED" % iid)
            failures.append((tag, "verify: %s" % api.err(vbody)))
            continue
        print("     created %s  VERIFIED" % iid)
        state.mark(tag, iid)
        results.append({"doc": tag, "invoiceId": iid,
                        "inr": str(sh["home_total"]), "ok": True})

    print("\n  attempted=%d ok=%d failed=%d"
          % (len(results) + len(failures), len(results), len(failures)))
    if failures:
        with open(os.path.join(WORK, "ar_failures.json"), "w") as fh:
            json.dump([{"doc": d, "why": w} for d, w in failures], fh, indent=1)
        print("  failures -> work/ar_failures.json")
    return results


# Receipt applications. Best effort, and reported rather than posted: a
# collection after cutover settles its invoice through POST /receipt/ with the
# invoice in references[], and that payload contract is not proven on this box.
# Quantifying the backlog is honest; inventing the payload is not.
SQL_RECEIPTS_AFTER = """
SET NOCOUNT ON;
SELECT COUNT(*) AS n, CAST(SUM(p.AMTPAYMHC) AS decimal(19,2)) AS amt
  FROM AROBP p WHERE p.DATERMIT >= %s
"""


def phase_recon(api, state, args):
    """Sage's own open total against what this loader posted."""
    print("\n=== AR RECONCILIATION ===")
    ctl = sage_query(SQL_OPEN_CONTROL, (CUTOVER,))[0]
    print("  Sage AROBL open at cutover %s" % CUTOVER)
    print("    documents %s   customers %s" % (ctl["docs"], ctl["customers"]))
    print("    invoiced  %16s (AMTINVCHC)" % ctl["home_total"])
    print("    open      %16s (AMTDUEHC)" % ctl["home_open"])

    posted = {k: v for k, v in state.posted.items()
              if not v.rstrip().endswith("UNVERIFIED")}
    unverified = {k for k, v in state.posted.items()
                  if v.rstrip().endswith("UNVERIFIED")}
    book = load_ar_book()
    total = D(0)
    for tag in posted:
        code, _, invoice = tag.partition("|")
        rec = book.get((code, invoice))
        if rec:
            total += D(str(rec["header"]["home_total"] or 0))
    print("\n  posted by this loader")
    print("    documents %s" % len(posted))
    print("    invoiced  %16s (Sage AMTINVCHC of the posted set)" % q2(total))
    gap = q2(D(str(ctl["home_total"] or 0))) - q2(total)
    print("    NOT YET POSTED %11s across %d documents"
          % (gap, int(ctl["docs"]) - len(posted)))
    if unverified:
        print("    %d created but UNVERIFIED - they carry a receivable with ZERO"
              " accounting impact until re-run" % len(unverified))

    for path, label in ((os.path.join(WORK, "ar_held.json"), "held"),
                        (os.path.join(WORK, "buyers_held.json"), "buyers held"),
                        (os.path.join(WORK, "ar_failures.json"), "failures")):
        if os.path.exists(path):
            with open(path) as fh:
                try:
                    n = len(json.load(fh))
                except ValueError:
                    n = "?"
            print("    %-12s %s -> %s" % (label, n, path))

    # ---- BOTH SIDES, per document ---------------------------------------
    # Sage's own figure, what the platform STORED, and what the voucher
    # actually posted. The three have to agree, and only the third one is the
    # ledger - an invoice row can look right while its voucher is absent
    # (created but never verified) or unbalanced.
    if posted:
        print("\n  two-sided check: Sage vs the stored invoice vs the voucher")
        # State stores the WHOLE log line as the value ("<tag>||<id>"), so the
        # id is field 1. Taking field 0 gives the tag back, which left the id
        # list empty and produced "IN ()" - a SQL syntax error that read like
        # the check itself was broken.
        ids = {}
        for tag, v in posted.items():
            parts = v.split("||")
            iid = parts[1] if len(parts) > 1 else ""
            if iid.isdigit():
                ids[iid] = tag
        rows = []
        try:
            rows = B.mysql(
                "SELECT i.id, i.invoiceStatus, i.totalPrice, "
                "  COALESCE(SUM(CASE WHEN ve.transactionType LIKE 'DEB%%' "
                "    THEN ve.amount END), 0), "
                "  COALESCE(SUM(CASE WHEN ve.transactionType LIKE 'CRE%%' "
                "    THEN ve.amount END), 0), COUNT(ve.id) "
                "FROM invoice i LEFT JOIN voucherEntry ve "
                "  ON ve.referenceId = i.id AND (ve.isDeleted+0) = 0 "
                "WHERE i.id IN (%s) GROUP BY i.id, i.invoiceStatus, i.totalPrice"
                % ",".join(sorted(ids)))
        except Exception as exc:                                # noqa: BLE001
            print("    unavailable (%s)" % str(exc).split("\n")[0][:90])
        bad, checked = [], 0
        for iid, status, total, dr, cr, nlegs in rows:
            tag = ids.get(iid, "?")
            code, _, invoice = tag.partition("|")
            rec = book.get((code, invoice))
            sage = q2(D(str(rec["header"]["home_total"] or 0))) if rec else None
            dr, cr, total = q2(D(dr)), q2(D(cr)), q2(D(total))
            checked += 1
            why = []
            # totalPrice is stored to 6dp and the platform DERIVES it as
            # quantity x unitPrice, so a unit price that does not divide the
            # amount exactly leaves sub-paise dust (18168.49 over 36 units
            # stores as 18168.490008). The LEDGER rounds to the paise and is
            # exact, so the comparison is made at the paise on both.
            # Compared at the PAISE on both sides. The platform derives
            # totalPrice and the voucher amount as quantity x unitPrice at the
            # 6dp the column stores, so an amount that does not divide exactly
            # leaves sub-paise dust - 18,168.49 over 36 units is stored as
            # 18,168.490008. That is the platform's own arithmetic, it rounds
            # to the same paise in every report, and DR still equals CR.
            if sage is not None and total != sage:
                why.append("stored total %s != Sage %s" % (total, sage))
            if int(nlegs) == 0:
                why.append("NO VOUCHER - %s" % status)
            elif dr != cr:
                why.append("voucher unbalanced DR %s CR %s" % (dr, cr))
            elif sage is not None and dr != sage:
                why.append("voucher DR %s != Sage %s" % (dr, sage))
            if why:
                bad.append({"doc": tag, "invoiceId": iid, "why": "; ".join(why)})
        print("    %d checked, %d agree, %d disagree"
              % (checked, checked - len(bad), len(bad)))
        for b in bad[:15]:
            print("      %-34s %s" % (b["doc"], b["why"]))
        if bad:
            with open(os.path.join(WORK, "ar_two_sided.json"), "w") as fh:
                json.dump(bad, fh, indent=1)
            print("    -> work/ar_two_sided.json")

    # The receipts backlog, so the work is visible and quantified.
    print("\n  collections after cutover (these settle an invoice through "
          "POST /receipt/, which this loader does NOT post)")
    try:
        r = sage_query(SQL_RECEIPTS_AFTER, (CUTOVER,))[0]
        print("    AROBP applications %s, INR %s" % (r["n"], r["amt"]))
    except Exception as exc:                                    # noqa: BLE001
        print("    unavailable (%s) - AROBP is not inventoried in this repo"
              % str(exc).split("\n")[0][:90])


def phase_legs(api, state, args):
    """Our voucher legs beside Sage's own, for the invoices named."""
    docs = args.docs or [k for k in list(state.posted.keys())[-2:]]
    for tag in docs:
        code, _, invoice = tag.partition("|")
        if not invoice:
            code, invoice = "", tag
        rec = next((v for k, v in state.posted.items()
                    if k.endswith("|%s" % invoice)), "")
        iid = rec.split("||")[0] if rec[:1].isdigit() else None
        print("\n" + "=" * 78)
        print("%s   %s" % (invoice, "invoiceId " + iid if iid else "(not posted)"))
        print("=" * 78)
        if not iid:
            continue
        if rec.rstrip().endswith("UNVERIFIED"):
            # Not "no legs" - NO VOUCHER. The create wrote the invoice and
            # verify never ran, so there is nothing to show and the receivable
            # has zero accounting impact. Re-run ar-post to finish it.
            print("   created but NEVER VERIFIED - no voucher exists yet. "
                  "Re-run ar-post to verify it.")
            continue
        # FILTER isDeleted=0: PUT /invoice/{id}/REVOKED soft-deletes the legs,
        # so an unfiltered read shows a corrected document's old legs too.
        legs = B.mysql(
            "SELECT ve.transactionType, fa.accountingName, ve.amount "
            "FROM voucherEntry ve JOIN financeAccount fa "
            "ON fa.id=ve.financeAccountId WHERE ve.referenceId=%s "
            "AND ve.isDeleted+0=0 ORDER BY ve.transactionType DESC, "
            "fa.accountingName" % iid, ["side", "account", "amount"])
        tot = collections.defaultdict(lambda: D(0))
        for l in legs:
            tot[(l["side"], l["account"])] += D(l["amount"])
        dr = cr = D(0)
        for (side, acct), amt in sorted(tot.items(),
                                        key=lambda x: (x[0][0] != "DEBIT", x[0][1])):
            print("   %-6s %-42s %14s" % (side[:2], acct[:42], q2(amt)))
            if side.upper().startswith("DEB"):
                dr += amt
            else:
                cr += amt
        print("   %-6s %-42s %14s" % ("", "DR total", q2(dr)))
        print("   %-6s %-42s %14s" % ("", "CR total", q2(cr)))
        if q2(dr) != q2(cr):
            print("   UNBALANCED by %s" % q2(dr - cr))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["ar-probe", "ar-selftest", "ar-masters",
                                      "ar-dryrun", "ar-post", "ar-recon",
                                      "ar-legs"])
    ap.add_argument("--docs", nargs="*",
                    help="Sage invoice numbers (or customer|invoice) to limit to")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--token", default=None)
    ap.add_argument("--no-enrich", action="store_true",
                    help="never ask Gemini for a missing export address; take "
                         "the flagged placeholder instead")
    ap.add_argument("--include-settled", action="store_true",
                    help="with --docs: also consider a document Sage already "
                         "shows settled. For reproducing one named invoice "
                         "end to end; NOT part of the load")
    ap.add_argument("--all-columns", action="store_true",
                    help="ar-probe: dump every column of every probed table")
    args = ap.parse_args()

    if args.token:
        B.TOKEN = args.token

    os.makedirs(WORK, exist_ok=True)
    state = B.State(crosswalk=CROSSWALK, posted_log=POSTED_LOG)
    api = Api(dry_run=args.phase in ("ar-dryrun", "ar-probe"))

    if args.phase == "ar-probe":
        return phase_probe(api, state, args)
    if args.phase == "ar-selftest":
        return phase_selftest(api, state, args)
    if args.phase == "ar-masters":
        return phase_masters(api, state, args)
    if args.phase == "ar-recon":
        return phase_recon(api, state, args)
    if args.phase == "ar-legs":
        return phase_legs(api, state, args)
    return phase_run(api, state, args, do_post=(args.phase == "ar-post"))


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Stop as exc:
        print("\nSTOP: %s" % exc, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
