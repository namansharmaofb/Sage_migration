#!/usr/bin/env python3
"""
Post Sage AP-direct bills (Jan-Apr 2026) into SMEAssist.

A port of load_janapr_bills.ps1 with the six field-level defects fixed. The
PowerShell loader's payload builder IS the contract; everything here that
differs from it does so for a reason recorded inline against its defect number.

The source is Sage itself (read-only, SELECT only), not the .psv extract: the
extract carries no RATETAX column, and defect 4.1 cannot be fixed without it.

Phases:
    cleanup   remove the smoke-test leftovers and reconcile the bill counters
    masters   create the products and contacts the selected bills need
    dryrun    build every payload, post nothing, print grouped skip reasons
    post      create + verify, resumable
    verify    the definition-of-done queries, run against MySQL

Usage:
    export SME_TOKEN=<fresh auth-token>
    ./post_sage_bills.py dryrun
    ./post_sage_bills.py masters --pilot
    ./post_sage_bills.py post --pilot
    ./post_sage_bills.py verify
"""
import argparse, calendar, collections, json, os, random, re, sys, time
from decimal import Decimal as D, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")


# ============================================================================
# CONFIG
# ============================================================================

def _dotenv(path=os.path.join(HERE, ".env")):
    cfg = {}
    if os.path.exists(path):
        with open(path) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    cfg[k.strip()] = v.strip().strip("'\"")
    return cfg

ENV = _dotenv()
def cfg(key, default=None):
    return os.environ.get(key) or ENV.get(key) or default

# Org- and host-specific settings, deliberately without defaults: this script
# posts to a real ERP under a real tax identity, and a plausible-but-wrong
# fallback would file bills against the wrong org. Copy .env.example to .env.
REQUIRED = ("SME_BASE", "SME_ORG_ID", "SME_NAMESPACE", "SME_ORG_GSTIN",
            "SME_ORG_PAN")
_missing = [k for k in REQUIRED if not cfg(k)]
if _missing:
    sys.exit("Missing required settings: %s\n"
             "Copy .env.example to .env and fill them in, or export them."
             % ", ".join(_missing))

ORG_ID     = cfg("SME_ORG_ID")
API        = cfg("SME_BASE")
NAMESPACE  = cfg("SME_NAMESPACE")
ORG_GSTIN  = cfg("SME_ORG_GSTIN")
ORG_PAN    = cfg("SME_ORG_PAN")

# Devbox host for the ssh/MySQL reads below. Not required at import time: only
# the staging and verify paths use it, and the Sage path must still run without.
def db_host():
    h = cfg("SME_DB_HOST")
    if not h:
        raise Stop("SME_DB_HOST is not set: export it to the devbox host that "
                   "runs the smeassist MySQL.")
    return "root@" + h
ORG_CITY   = "Bengaluru"
ORG_STATE  = "KARNATAKA"        # matches COMPANY_ADDR below; the org's own state
ORG_STATE  = "KARNATAKA"
TOKEN      = cfg("SME_TOKEN") or cfg("SME_COOKIE")

DATE_FROM, DATE_TO = 20260101, 20260430

# One series name serves ONE financial year: the counter uniqueness key has no
# FY in it, so reusing a name across years collides.
SERIES_BY_FY = {"2025-2026": "SAGE", "2026-2027": "SAGE27"}

COMPANY_ADDRESS_ID = "1029168384857610867"
# Devbox-org-specific, per handover 7.6. Omitting it makes the server call
# ContactCategoryServiceImpl.findById(null) and every contact create dies with
# "Null key returned for cache operation".
CONTACT_CATEGORY = "1029658153383367321"
ROUNDOFF_GL   = "4E1M016"                              # 4.5 bill field, never a line
RCM_GL        = {"1L8TX14": "SGST", "1L8TX15": "CGST", "1L8TX16": "IGST"}

# Couriers and customs agents (four vendors: two international, two domestic)
# pay the import IGST on our behalf and bill it back. Sage carries the
# reimbursement as a 2A7T distribution line: 136 documents in the Jan-Apr window,
# Rs 705,072, every one of which used to fail assert_invariants because classify()
# dropped the line and AMTINVCHC then disagreed with 4E + tax + roundoff.
#
# It is money owed to the vendor, so it belongs in billAmount - but it carries no
# GST of its own, so it posts as a 0% line. Adding it closes the gap on all 136
# to the paise.
#
# The ledger MUST NOT come from item_ledger_for(): that endpoint mints an
# ITEM_DIRECT_EXPENSE head, and every 2A7T account is ACCTTYPE=B (balance sheet).
# Booking a recoverable asset into P&L is exactly the 1L6TA07 defect on the
# PO-matched path. These are the org's own leaf heads under
# "Balance with Government Authorities > Goods and Service Tax > GST Input",
# verified in financeAccount; the id is org-specific, like COMPANY_ADDRESS_ID.
#
# 2A7TX01/02/03 (domestic SGST/CGST/IGST Recoverable, 6 documents, Rs 6,903) have
# no honest home here: the org's domestic input heads are all rate-labelled and
# this line is 0%. They are HELD for a decision rather than guessed at.
PASSTHRU_GL = {
    "2A7TX04": ("1541311685238407168", "IGST Input (Import) @ 0.00%"),
}
# ============================================================================
# BILL TYPE - Sage's own account group decides it, per document
# ============================================================================
# Defect: billType was the literal "PURCHASE" on every bill. The whole
# AP-direct population is expense, so 6,082 of 6,092 posted documents carried
# the wrong type, and 226 SAGE ledgers were minted under Direct Expenses with
# not one under Indirect.
#
# GLAMF.ACCTGRPCOD is the authority and MUST be read per account. It cannot be
# inferred from the account code: 51 of 1,029 accounts carry a group that
# disagrees with their own prefix and 41 of those flip direct/indirect. Sage's
# Carriage Inward is the clearest case - the base account 4E2ME13 is group
# 4E4S while most of its children 4E2ME13-01..-92 are 4E2M. Lookups are keyed
# on ACCTFMTTD, the formatted code, because that is what APIBD.IDGLACCT holds.
BILLTYPE_BY_GROUP = {
    "4E2M": "DIRECT_EXPENSE",     # manufacturing and consumption
    "4E3E": "IN_DIRECT_EXPENSE",  # employee cost
    "4E4S": "IN_DIRECT_EXPENSE",  # selling and distribution
    "4E5O": "IN_DIRECT_EXPENSE",  # other / administrative
    "4E6F": "IN_DIRECT_EXPENSE",  # finance cost
}

# Group 4E1M is deliberately NOT in that table: it is the one heterogeneous
# group and routing it wholesale to PURCHASE misfiles the charge accounts. Of
# its 49 accounts only the COGS heads are purchases; Printing, Processing,
# Printing & Dyeing, Darning and Documentation-on-Imports are services bought
# in, and 4E1M016 is the round-off. Measured over the window, treating the
# group as PURCHASE would misfile 348 of the 365 non-round-off 4E1M lines,
# including all 280 Processing Charges lines carrying Rs 92.7 lakh.
#
# The account decides, and the stem is matched so segmented children follow
# their parent (4E1M030-01 Darning is the same account as 4E1M030).
PURCHASE_ACCOUNTS = {"4E1M001", "4E1M002", "4E1M003"}   # COGS heads only
DIRECT_ACCOUNTS = {
    "4E1M014",   # Printing
    "4E1M015",   # Processing Charges
    "4E1M024",   # Printing & Dyeing Charges
    "4E1M030",   # Darning Charges
    "4E1M031",   # Documentation Charges On Imports
}


def account_bill_type(acct, groups):
    """-> billType for one GL account, or None if nothing may be assumed.

    The group is read FIRST and decides which test applies, so that the stem
    rule below can never reach an account outside 4E1M.
    """
    acct = s(acct)
    grp = groups.get(acct)
    if grp != "4E1M":
        # Everything outside 4E1M is decided on the FORMATTED account and
        # NEVER on a stem. Segmented children do not follow their parent here:
        # 22 accounts disagree with their own base, and Carriage Inward is the
        # trap - base 4E2ME13 is 4E4S (indirect) while its children
        # 4E2ME13-01..-92 are 4E2M (direct). Widening the stem rule to this
        # branch would misfile every one of them. Counted per base group,
        # children disagreeing with their parent: 4E4S 12 of 65, 4E3E 6 of 358,
        # 4E5O 3 of 207, 4E2M 1 of 172, and 4E1M 0 of 19.
        return BILLTYPE_BY_GROUP.get(grp)

    # Inside 4E1M only. Stem-matching is sound HERE because this group is
    # measurably homogeneous - all 19 of its segmented children carry their
    # parent's group - not because stem-matching is sound in general. The
    # window bills 4E1M030-01, never the bare 4E1M030, so the stem is what
    # lets one entry cover both without listing every IDEPL suffix.
    stem = acct.split("-")[0]
    if stem in PURCHASE_ACCOUNTS:
        return "PURCHASE"
    if stem in DIRECT_ACCOUNTS:
        return "DIRECT_EXPENSE"
    # In the heterogeneous group but named by neither list. Refuse: the group
    # says nothing, and a guess here is what put COGS and Processing Charges
    # under the same head.
    return None

# The item-ledger mapping that goes with each bill type. ITEM_PURCHASE and
# ITEM_DIRECT_EXPENSE are measured across the platform (see item_ledger_for).
# getOrCreate/{referenceType} accepts ITEM_PURCHASE, ITEM_DIRECT_EXPENSE,
# ITEM_IN_DIRECT_EXPENSE, ITEM_SALE, ITEM_INCOME and ASSET_EXPENSE. The
# indirect one was always there - 2,960 mappings over 1,409 ledgers use it in
# production. Zero SAGE ledgers reached it only because item_ledger_for() was
# never called with it.
INDIRECT_ITEM_MAPPING = "ITEM_IN_DIRECT_EXPENSE"

# Accounts whose head is settled outside the account/group test, so a fresh run
# does not hold them. Neither ever reaches bill_type_of(): classify() keeps the
# round-off and the recoverable legs out of `exp`, and only `exp` votes.
#   4E1M016  the purchase round-off. v3 classes it ROUNDOFF, which is not a
#            billType. It rides in as a 0% line - Rs 57.45 over 537 lines in
#            the window - and is booked under the direct-expense head.
#   2A7TX04  import IGST reimbursed to the vendor. A balance-sheet recoverable
#            whose LINE carries the explicit PASSTHRU_GL ledger, so this
#            product's own item ledger never books anything.
SETTLED_ACCOUNTS = {
    ROUNDOFF_GL: "DIRECT_EXPENSE",
    "2A7TX04": "DIRECT_EXPENSE",
}

ITEM_MAPPING_BY_BILLTYPE = {
    "PURCHASE": "ITEM_PURCHASE",
    "DIRECT_EXPENSE": "ITEM_DIRECT_EXPENSE",
    "IN_DIRECT_EXPENSE": INDIRECT_ITEM_MAPPING,
}

# Sage keeps item HSN in an OPTIONAL FIELD, not on the item record:
# ICITEM.TARIFFCODE is empty on all 1,196,108 rows, while ICITEMO carries
# 971,675 HSNCODE rows of which 258,770 are non-empty. staging's sage_item
# never pulled it, so goods lines with a blank hsn were sent as hsnCode=null
# and refused one at a time with "HSN Code cannot be null".
SQL_ITEM_HSN = """
SET NOCOUNT ON;
SELECT RTRIM(ITEMNO) itemno, RTRIM(VALUE) hsn
  FROM ICITEMO WHERE RTRIM(OPTFIELD)='HSNCODE' AND RTRIM(VALUE)<>''
"""

# Last resort for the 572 items Sage has no HSN for ANYWHERE, almost all
# 4FASHL. Deliberately 9999, which is not a plausible goods HSN: a visible
# placeholder can be found and corrected later, a plausible-looking wrong code
# cannot. Flagged hsnIsDefault in metaData exactly like EXPENSE_SAC. The GST
# RATE is unaffected - read from Sage per line - so the tax stays correct.
GOODS_HSN_DEFAULT = "9999"

EXPENSE_SAC   = "996719"        # DEFAULT awaiting finance sign-off; flagged in metaData
PLACEHOLDER_MOBILE = "9999999999"

CROSSWALK = os.path.join(WORK, "crosswalk_live.json")
POSTED_LOG = os.path.join(WORK, "posted.log")
MAX_RETRY, THROTTLE = 8, 0.4

# The server rate-limits per ORGANISATION, not per connection, and answers 403
# "Rate limit exceeded" once you cross it. Measured on this devbox: a single
# connection sustains 116 requests in 25.1s - about 4.6/s - with no rejection
# at all. Six workers each pacing themselves at THROTTLE were therefore
# ATTEMPTING ~15-30/s against a ~5/s budget, so most calls were rejected,
# every worker backed off by the identical 2/4/8/16s, and they all returned
# together to collide again. More workers made the run slower, not faster.
#
# So pacing belongs in ONE place for the whole process, not in each thread.
API_RATE = 4.5


class RateGate:
    """Process-wide pacing. Threads take their turn from a single schedule.

    The lock is held only long enough to claim a slot, never across the sleep,
    so N threads pipeline their network latency instead of serialising on it.
    """

    def __init__(self, rate):
        import threading
        self.interval = 1.0 / float(rate)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def back_off(self, seconds):
        """Push the whole schedule out, so a 403 slows EVERY thread, not just
        the one that happened to receive it."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


GATE = RateGate(API_RATE)

# Legal Indian GST slabs, used only to VALIDATE the rate Sage states - never to
# invent one. Defect 4.1: read the rate, never divide.
LEGAL_SLABS = {D(x) for x in ("0", "0.1", "0.25", "1", "1.5", "3", "5", "6",
                              "7.5", "12", "18", "28")}


class Stop(Exception):
    """Unrecoverable. Stop the run; do not retry."""


class LookupFailed(Exception):
    """A read did not complete, so its result proves nothing. Distinct from a
    read that completed and found nothing - callers must not treat a throttled
    or errored lookup as evidence that a record is absent."""


def q2(x):
    return D(x or 0).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def s(v):
    return "" if v is None else str(v).strip()


# ============================================================================
# SAGE - READ ONLY. Every statement below is a SELECT.
# ============================================================================

def sage_query(sql, params=()):
    import pymssql
    with pymssql.connect(server=cfg("SQL_HOST"), port=int(cfg("SQL_PORT", 1433)),
                         user=cfg("SQL_USER"), password=cfg("SQL_PASSWORD"),
                         database=cfg("SQL_DATABASE"), login_timeout=20,
                         timeout=300) as cn:
        with cn.cursor(as_dict=True) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


# The purity filter and the RTRIM-not-LTRIM rule are lifted verbatim from
# extract.sql, which produced the .psv the proven run consumed.
SQL_HEADERS = """
SET NOCOUNT ON;
WITH b AS (
    SELECT * FROM APOBL
     WHERE IDTRXTYPE = 12 AND SRCEAPPL = 'AP'
       AND DATEINVC BETWEEN %d AND %d
),
f AS (
    SELECT b.* FROM b
     WHERE EXISTS (SELECT 1 FROM APIBD d
                    WHERE d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM)
       AND RTRIM(b.CODECURN) = 'INR'
       AND RTRIM(b.CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')
       AND NOT EXISTS (
           SELECT 1 FROM APIBD d
            WHERE d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM
              AND LEFT(RTRIM(d.IDGLACCT),2) <> '4E'
              AND LEFT(RTRIM(d.IDGLACCT),4) <> '2A7T'
              AND LEFT(RTRIM(d.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))
)
SELECT RTRIM(f.IDVEND) vendor, RTRIM(f.IDINVC) invoice,
       f.CNTBTCH, f.CNTITEM, f.DATEINVC bill_date, f.DATEINVCDU due_date,
       CAST(f.AMTINVCHC AS decimal(18,2)) gross,
       CAST(f.AMTTAXHC  AS decimal(18,2)) header_tax,
       RTRIM(f.CODETAXGRP) tax_group
  FROM f ORDER BY f.DATEINVC, f.IDVEND, f.IDINVC
""" % (DATE_FROM, DATE_TO)

# 4.1: RATETAX1..5 is the STATED rate; AMTTAX1..5 the amount per authority.
# 4.2: TEXTDESC is the only record of what was bought - IDITEM is empty on
#      every AP-direct line in this window.
# Lines come from idedat_staging.sage_ap_dist on the devbox, NOT from a second
# Sage read: the other session's pull.py re-pulled it on 2 Sep with the stated
# rate included (69,969 rows, zero null rates), and that is the agreed source of
# truth for the rate. Keyed on vendor_code + inv_number_raw, which staging takes
# from APIBH.
SQL_STAGING_LINES = """
SELECT vendor_code, inv_number_raw, cntline, gl_account,
       amt_dist AS amount, description AS descr,
       ratetax1, ratetax2, amttax1, amttax2
  FROM sage_ap_dist
"""

SQL_VENDORS = """
SET NOCOUNT ON;
WITH docs AS (
    SELECT DISTINCT RTRIM(IDVEND) vendor FROM APOBL
     WHERE IDTRXTYPE=12 AND SRCEAPPL='AP'
       AND DATEINVC BETWEEN %d AND %d AND RTRIM(CODECURN)='INR'
)
SELECT RTRIM(v.VENDORID) vendor,
       REPLACE(REPLACE(RTRIM(v.VENDNAME),CHAR(13),' '),CHAR(10),' ') name,
       REPLACE(REPLACE(RTRIM(v.LEGALNAME),CHAR(13),' '),CHAR(10),' ') legal_name,
       RTRIM(v.BRN) brn_raw,
       REPLACE(REPLACE(RTRIM(v.TEXTSTRE1),CHAR(13),' '),CHAR(10),' ') street1,
       -- APVEN has FOUR address lines and the town is as often on 2/3/4 as in
       -- NAMECITY: GLOBAL AIR TOURS keeps "KOLKATA" on line 2 and nothing in
       -- NAMECITY at all. Only line 1 was read, so resolve_state() never saw it.
       REPLACE(REPLACE(RTRIM(v.TEXTSTRE2),CHAR(13),' '),CHAR(10),' ') street2,
       REPLACE(REPLACE(RTRIM(v.TEXTSTRE3),CHAR(13),' '),CHAR(10),' ') street3,
       REPLACE(REPLACE(RTRIM(v.TEXTSTRE4),CHAR(13),' '),CHAR(10),' ') street4,
       RTRIM(v.NAMECITY) city, RTRIM(v.CODESTTE) state_raw,
       RTRIM(v.CODEPSTL) pincode, RTRIM(v.CODECTRY) country,
       RTRIM(v.NAMECTAC) contact_person, RTRIM(v.TEXTPHON1) phone1,
       RTRIM(v.EMAIL1) email1
  FROM APVEN v JOIN docs d ON d.vendor = RTRIM(v.VENDORID)
""" % (DATE_FROM, DATE_TO)

# ACCTFMTTD is the FORMATTED account code and is what APIBD.IDGLACCT holds;
# ACCTID is the unformatted one. They differ wherever the account has segments
# ('4E2ME02-14' vs '4E2ME0214'), and keying on ACCTID silently loses the name,
# leaving the product called after its own account code.
SQL_GL = """
SET NOCOUNT ON;
SELECT RTRIM(ACCTFMTTD) acct_code,
       REPLACE(REPLACE(RTRIM(ACCTDESC),CHAR(13),' '),CHAR(10),' ') description,
       RTRIM(ACCTGRPCOD) acct_group
  FROM GLAMF WHERE LEFT(RTRIM(ACCTFMTTD),2)='4E'
     OR LEFT(RTRIM(ACCTFMTTD),4)='2A7T'
"""


# ============================================================================
# SAGE FIELD DERIVATION
# ============================================================================

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
# Same pattern unanchored, for digging a GSTIN out of a labelled BRN field.
GSTIN_ANY_RE = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]")
PAN_RE   = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")

GST_STATE_CODES = {
    "01": "JAMMU_AND_KASHMIR", "02": "HIMACHAL_PRADESH", "03": "PUNJAB",
    "04": "CHANDIGARH", "05": "UTTARAKHAND", "06": "HARYANA", "07": "DELHI",
    "08": "RAJASTHAN", "09": "UTTAR_PRADESH", "10": "BIHAR", "11": "SIKKIM",
    "12": "ARUNACHAL_PRADESH", "13": "NAGALAND", "14": "MANIPUR",
    "15": "MIZORAM", "16": "TRIPURA", "17": "MEGHALAYA", "18": "ASSAM",
    "19": "WEST_BENGAL", "20": "JHARKHAND", "21": "ODISHA", "22": "CHHATTISGARH",
    "23": "MADHYA_PRADESH", "24": "GUJARAT", "25": "DAMAN_AND_DIU",
    "26": "DADRA_AND_NAGAR_HAVELI", "27": "MAHARASHTRA", "28": "ANDHRA_PRADESH",
    "29": "KARNATAKA", "30": "GOA", "31": "LAKSHADWEEP", "32": "KERALA",
    "33": "TAMIL_NADU", "34": "PUDUCHERRY", "35": "ANDAMAN_AND_NICOBAR_ISLANDS",
    "36": "TELANGANA", "37": "ANDHRA_PRADESH", "38": "LADAKH",
}
STATE_ENUM = set(GST_STATE_CODES.values()) | {"OTHER_COUNTRY", "UNKNOWN"}
STATE_ALIASES = {
    "TAMILNADU": "TAMIL_NADU", "KARNATAK": "KARNATAKA", "GUJRAT": "GUJARAT",
    "TELENGANA": "TELANGANA", "UP": "UTTAR_PRADESH", "UTTARPRADESH": "UTTAR_PRADESH",
    "NEWDELHI": "DELHI", "PONDICHERRY": "PUDUCHERRY", "ORISSA": "ODISHA",
    "UTTARANCHAL": "UTTARAKHAND",
}
# Postal-circle prefixes. Deliberately GENEROUS - a prefix maps to every state
# it can legally denote, because this table is only ever used to CORROBORATE a
# state that something else already proposed, never to choose one. Widening a
# set can only make corroboration harder, never wrong.
PIN2_STATES = {
    "11": {"DELHI"},
    "12": {"HARYANA"}, "13": {"HARYANA", "PUNJAB"},
    "14": {"PUNJAB"}, "15": {"PUNJAB"}, "16": {"PUNJAB", "CHANDIGARH"},
    "17": {"HIMACHAL_PRADESH"},
    "18": {"JAMMU_AND_KASHMIR"}, "19": {"JAMMU_AND_KASHMIR", "LADAKH"},
    "20": {"UTTAR_PRADESH"}, "21": {"UTTAR_PRADESH"}, "22": {"UTTAR_PRADESH"},
    "23": {"UTTAR_PRADESH"}, "24": {"UTTAR_PRADESH", "UTTARAKHAND"},
    "25": {"UTTAR_PRADESH"}, "26": {"UTTAR_PRADESH", "UTTARAKHAND"},
    "27": {"UTTAR_PRADESH"}, "28": {"UTTAR_PRADESH"},
    "30": {"RAJASTHAN"}, "31": {"RAJASTHAN"}, "32": {"RAJASTHAN"},
    "33": {"RAJASTHAN"}, "34": {"RAJASTHAN"},
    "36": {"GUJARAT"}, "37": {"GUJARAT"}, "38": {"GUJARAT"},
    "39": {"GUJARAT", "DADRA_AND_NAGAR_HAVELI", "DAMAN_AND_DIU"},
    "40": {"MAHARASHTRA", "GOA"}, "41": {"MAHARASHTRA"}, "42": {"MAHARASHTRA"},
    "43": {"MAHARASHTRA"}, "44": {"MAHARASHTRA"},
    "45": {"MADHYA_PRADESH"}, "46": {"MADHYA_PRADESH"}, "47": {"MADHYA_PRADESH"},
    "48": {"MADHYA_PRADESH", "CHHATTISGARH"},
    "49": {"CHHATTISGARH", "MADHYA_PRADESH"},
    "50": {"TELANGANA", "ANDHRA_PRADESH"}, "51": {"ANDHRA_PRADESH", "TELANGANA"},
    "52": {"ANDHRA_PRADESH", "TELANGANA"}, "53": {"ANDHRA_PRADESH"},
    "56": {"KARNATAKA"}, "57": {"KARNATAKA"}, "58": {"KARNATAKA"},
    "59": {"KARNATAKA"},
    "60": {"TAMIL_NADU", "PUDUCHERRY"}, "61": {"TAMIL_NADU"},
    "62": {"TAMIL_NADU"}, "63": {"TAMIL_NADU"}, "64": {"TAMIL_NADU"},
    "65": {"TAMIL_NADU"}, "66": {"TAMIL_NADU"},
    "67": {"KERALA"}, "68": {"KERALA", "LAKSHADWEEP"}, "69": {"KERALA"},
    "70": {"WEST_BENGAL"}, "71": {"WEST_BENGAL"}, "72": {"WEST_BENGAL"},
    "73": {"WEST_BENGAL", "SIKKIM"},
    "74": {"WEST_BENGAL", "ANDAMAN_AND_NICOBAR_ISLANDS"},
    "75": {"ODISHA"}, "76": {"ODISHA"}, "77": {"ODISHA"},
    "78": {"ASSAM"},
    "79": {"ARUNACHAL_PRADESH", "ASSAM", "MANIPUR", "MEGHALAYA", "MIZORAM",
           "NAGALAND", "TRIPURA"},
    "80": {"BIHAR"}, "81": {"BIHAR", "JHARKHAND"}, "82": {"BIHAR", "JHARKHAND"},
    "83": {"JHARKHAND", "BIHAR"}, "84": {"BIHAR"}, "85": {"BIHAR"},
}

# City -> state, for the handful of city names that actually appear in
# APVEN.NAMECITY on vendors whose CODESTTE is empty. Deliberately tiny and
# unambiguous: every entry is a city that belongs to exactly one state and is
# not a substring of anything else here. Like PIN2_STATES this is only ever
# used to CORROBORATE, never to decide alone.
CITY_STATE = {
    "BENGALURU": "KARNATAKA", "BANGALORE": "KARNATAKA",
    "BANGARPET": "KARNATAKA", "MYSORE": "KARNATAKA", "MYSURU": "KARNATAKA",
    "KOLKATA": "WEST_BENGAL", "CALCUTTA": "WEST_BENGAL",
    "NEWDELHI": "DELHI", "DELHI": "DELHI",
    "CHENNAI": "TAMIL_NADU", "COIMBATORE": "TAMIL_NADU",
    "MUMBAI": "MAHARASHTRA", "PUNE": "MAHARASHTRA",
    "HYDERABAD": "TELANGANA", "AHMEDABAD": "GUJARAT",
}

# Sage vendor codes that are not vendors at all: OTHX002..010 are
# "Reimb IDEPL <unit>", the company reimbursing its own employees per unit, and
# OTHEXP is "ONE TIME VENDOR - OTHER EXPENSES". They carry no address of any
# kind because there is no external party to hold one.
INTERNAL_VENDOR_RE = re.compile(r"(?i)^\s*(reimb\b|one\s*time\s*vendor)")

# Head-post-office pincode per state, used ONLY where Sage holds no usable
# pincode of its own and the state is already proven. address.pin_code is NOT
# NULL so something has to go in the column; this keeps it inside the state the
# GSTIN proves instead of parking a Tamil Nadu vendor on the org's Karnataka
# code. Every contact built this way is flagged pinCodeIsPlaceholder in
# metaData. Safe here because the platform does NOT derive state from pincode
# on this build - GET /address/pincode/{pin} 404s and the state we send is
# stored verbatim.
STATE_HEAD_PINCODE = {
    "JAMMU_AND_KASHMIR": "190001", "HIMACHAL_PRADESH": "171001",
    "PUNJAB": "160017", "CHANDIGARH": "160017", "UTTARAKHAND": "248001",
    "HARYANA": "134109", "DELHI": "110001", "RAJASTHAN": "302001",
    "UTTAR_PRADESH": "226001", "BIHAR": "800001", "SIKKIM": "737101",
    "ARUNACHAL_PRADESH": "791111", "NAGALAND": "797001", "MANIPUR": "795001",
    "MIZORAM": "796001", "TRIPURA": "799001", "MEGHALAYA": "793001",
    "ASSAM": "781001", "WEST_BENGAL": "700001", "JHARKHAND": "834001",
    "ODISHA": "751001", "CHHATTISGARH": "492001", "MADHYA_PRADESH": "462001",
    "GUJARAT": "380001", "DAMAN_AND_DIU": "396210",
    "DADRA_AND_NAGAR_HAVELI": "396230", "MAHARASHTRA": "400001",
    "ANDHRA_PRADESH": "520001", "KARNATAKA": "560001", "GOA": "403001",
    "LAKSHADWEEP": "682555", "KERALA": "682001", "TAMIL_NADU": "600001",
    "PUDUCHERRY": "605001", "ANDAMAN_AND_NICOBAR_ISLANDS": "744101",
    "TELANGANA": "500001", "LADAKH": "194101",
}

GSTIN_ENTITY_TO_PROFILE = {
    "C": "COMPANY", "P": "PROPRIETORSHIP", "F": "PARTNERSHIP",
    "H": "HINDU_UNDIVIDED_FAMILY", "A": "ASSOCIATION_OF_PERSONS", "T": "TRUST",
    "B": "BODY_OF_INDIVIDUALS", "L": "LOCAL_AUTHORITY",
    "G": "GOVERNMENT_AGENCY", "J": "ARTIFICIAL_JURIDICAL_PERSON",
}


def is_gstin(v):
    return bool(v) and bool(GSTIN_RE.match(s(v).upper()))


def extract_gstin(v):
    """-> the GSTIN held in a free-text BRN, or None.

    APVEN.BRN is free text and 7 vendors label it or space it out - "GST -
    29XXXPX0001X1ZN", "29XXXPX0005 X1ZV", "29 XXXHX0006X1ZI". Every one of them
    was falling through to the PAN branch, which reads the PAN out of the very
    GSTIN it just failed to recognise: they were being recorded as PAN-only when
    Sage says they are GST-registered, and losing their state with it.

    Whitespace is removed and the exact 15-character GSTIN pattern must still
    match - nothing is repaired or inferred. A truly malformed number (a missing
    character, "29XXXCX0002X12G") does not match and is left alone.
    """
    raw = s(v).upper()
    if GSTIN_RE.match(raw):
        return raw
    m_ = GSTIN_ANY_RE.search(re.sub(r"\s+", "", raw))
    return m_.group(0) if m_ else None


def needs_cin_lookup(gstin):
    """GSTINs whose 6th char is C or F route through a CIN/LLPIN lookup the
    devbox cannot answer (no masterIndiaGstDetails row, no Redis key)."""
    return is_gstin(gstin) and gstin[5] in ("C", "F")


def registration_of(v):
    country = s(v.get("country")).upper().replace(" ", "")
    brn = s(v.get("brn_raw")).upper()
    # A well-formed Indian GSTIN settles the question before CODECTRY gets a
    # vote. APVEN.CODECTRY is free text and holds city names - OTHI058 has
    # country "Mumbai" against GSTIN 27XXXCX0003X1Z3, and reading that as
    # INTERNATIONAL drops the registration number and the state with it.
    gstin = extract_gstin(brn)
    if gstin:
        return "GST", gstin
    if country and not country.startswith("INDIA"):
        return "INTERNATIONAL", None
    for cand in (v.get("legal_name"), v.get("brn_raw")):
        if cand:
            m = PAN_RE.search(s(cand).upper().replace(" ", ""))
            if m:
                return "PAN", m.group(0)
    return "WITHOUT_PAN_OR_GST", None


def resolve_state(v):
    """GSTIN prefix wins - CODESTTE is free text and holds city names."""
    brn = s(v.get("brn_raw")).upper()
    gstin = extract_gstin(brn)
    if gstin:
        name = GST_STATE_CODES.get(gstin[:2])
        if name:
            return name, "gstin"
    raw = s(v.get("state_raw"))
    if raw:
        key = re.sub(r"[^A-Z&.]", "", raw.upper())
        if key in STATE_ALIASES:
            return STATE_ALIASES[key], "text"
        und = re.sub(r"[^A-Z]+", "_", raw.upper()).strip("_")
        if und in STATE_ENUM:
            return und, "text"
        coll = re.sub(r"[^A-Z]", "", raw.upper())
        for nm in STATE_ENUM:
            if re.sub(r"[^A-Z]", "", nm) == coll:
                return nm, "text"
    country = s(v.get("country")).upper().replace(" ", "")
    if country and not country.startswith("INDIA"):
        return "OTHER_COUNTRY", "country"

    # Last resort, and only on CORROBORATION. A malformed BRN still carries a
    # readable GST state code in its first two digits ("29AAEM7984Q2Z9" fails
    # GSTIN_RE by one character but its 29 is not in doubt); the pincode's
    # postal circle is an independent field. Where the two agree, the state is
    # evidenced twice over and is not a guess. Where either is missing, or they
    # disagree, fall through to UNKNOWN and let the vendor be held - state is
    # the IGST vs CGST+SGST switch and a wrong one silently moves money.
    brn_state = GST_STATE_CODES.get(brn[:2]) if len(brn) >= 2 and brn[:2].isdigit() else None
    pin = re.sub(r"\D", "", s(v.get("pincode")))
    pin_states = PIN2_STATES.get(pin[:2]) if re.match(r"^[1-9][0-9]{5}$", pin) else None
    if brn_state and pin_states and brn_state in pin_states:
        return brn_state, "gstin-prefix+pincode"

    # Same rule, second pair of sources: NAMECITY names a city belonging to
    # exactly one state, and the pincode's postal circle independently agrees.
    # 13 of the 30 vendors Sage leaves stateless resolve here - MANJUNATHA .V
    # at BANGALORE/560045, GLOBAL AIR TOURS at KOLKATA/700004 - and a
    # disagreement still falls through to the hold rather than picking a side.
    city_state = None
    for fld in ("city", "street4", "street3", "street2", "street1"):
        blob = re.sub(r"[^A-Z]", "", s(v.get(fld)).upper())
        if not blob:
            continue
        hits = {st for city, st in CITY_STATE.items() if city in blob}
        # A line naming two different states decides nothing.
        if len(hits) == 1:
            city_state = hits.pop()
            break
    if city_state and pin_states and city_state in pin_states:
        return city_state, "city+pincode"

    # An internal account, not a vendor: "Reimb IDEPL 5", "ONE TIME VENDOR".
    # The company is reimbursing its own employees at its own unit, so the
    # place of supply is the org's own state - that is what the row means, not
    # a guess at a missing one. Guarded on the address being ENTIRELY empty, so
    # it can never swallow a real vendor that merely happens to be named oddly.
    # Verified against the book: these 10 codes carry 259 bills and NOT ONE of
    # them carries tax of any kind, forward or reverse, so the IGST vs
    # CGST+SGST switch this function exists to protect is not even in play.
    if INTERNAL_VENDOR_RE.match(s(v.get("name"))) and not any(
            s(v.get(k)) for k in ("street1", "city", "state_raw", "pincode")):
        return ORG_STATE, "internal-org-account"

    return "UNKNOWN", "unresolved"


def normalise_hsn(v):
    """-> an HSN the platform will accept, or None.

    product.hsnCode is validated "size must be between 2 and 8" and Sage holds
    677 goods lines (570 items) that break it: a trailing dot ("60062200."),
    a decimal tail ("60062200.00"), and full 10-digit ITC-HS codes
    ("5208420090"). Left alone these are refused one item at a time, part-way
    through a 17,000-product run.

    HSN is hierarchical - 2, then 4, 6, 8 digits - so the leading 8 digits of a
    longer ITC-HS code ARE the 8-digit HSN: 5407610000 -> 54076100, woven
    polyester, which is what the item is. Truncating is the standard reading of
    that code, not a guess. Anything after the first non-digit is dropped first,
    so "60062200.00" gives 60062200 rather than 6006220000.
    """
    raw = s(v)
    if not raw:
        return None
    m_ = re.match(r"\d+", raw.strip())
    if not m_:
        return None
    digits = m_.group(0)[:8]
    return digits if len(digits) >= 2 else None


def normalise_pincode(v):
    digits = re.sub(r"\D", "", s(v))
    return digits if re.match(r"^[1-9][0-9]{5}$", digits) else None


def sage_date_parts(v):
    t = s(v)
    if len(t) != 8 or not t.isdigit() or t == "00000000":
        return None
    return int(t[0:4]), int(t[4:6]), int(t[6:8])


def epoch_ms(v):
    """Epoch milliseconds at UTC midnight, from Sage's yyyyMMdd integer."""
    p = sage_date_parts(v)
    return None if p is None else calendar.timegm((p[0], p[1], p[2], 0, 0, 0)) * 1000


def financial_year(v):
    """Indian FY from the BILL's own date, never from today."""
    p = sage_date_parts(v)
    if p is None:
        return None
    y, m = p[0], p[1]
    return "%d-%d" % (y, y + 1) if m >= 4 else "%d-%d" % (y - 1, y)


def base_invoice(inv):
    """Strip the *N receipt suffix so the parts consolidate into one bill.
    'INV-26306296-1*N' -> 'INV-26306296-1', which is a DIFFERENT document from
    'INV-26306296': only a trailing *<digits> is a receipt suffix.

    RTRIM only, never LTRIM. extract.sql records why: Sage holds
    ' WPL/25-26/07516' and 'WPL/25-26/07516' as two separate obligations with
    different balances, so stripping the leading space merges two real
    documents. This deliberately departs from the prompt's 'trim both ends'.
    """
    inv = (inv or "").rstrip()
    head, sep, tail = inv.rpartition("*")
    return head if sep and head and tail.isdigit() else inv


# ============================================================================
# API CLIENT
# ============================================================================

class Api:
    """Backoff lives HERE, not in the caller: a burst of 403s with empty
    bodies is rate limiting, and it starts at an arbitrary record and hits
    every one after it."""

    def __init__(self, dry_run=False):
        import requests
        self.dry_run = dry_run
        self.session = requests.Session()
        self.calls = 0

    def _headers(self):
        if not TOKEN:
            raise Stop("SME_TOKEN is not set. Ask for a fresh token; never log in.")
        return {"Cookie": "auth-token=%s; organisationId=%s; selectedOrgId=%s"
                          % (TOKEN, ORG_ID, ORG_ID),
                "X-ORG-ID": ORG_ID, "X-NAMESPACE": NAMESPACE,
                "X-PLATFORM": "SME_ASSIST", "Content-type": "application/json"}

    def call(self, method, path, body=None):
        import requests
        if self.dry_run and method != "GET":
            return 0, {"dryRun": True}
        url = API + path
        for attempt in range(1, MAX_RETRY + 1):
            self.calls += 1
            GATE.wait()
            try:
                r = self.session.request(method, url, headers=self._headers(),
                                         data=json.dumps(body) if body is not None else None,
                                         timeout=180)
            except requests.RequestException as exc:
                if attempt >= MAX_RETRY:
                    return None, "connection error: %s" % exc
                time.sleep(min(30, 2 ** attempt))
                continue
            try:
                parsed = r.json()
            except ValueError:
                parsed = r.text[:2000]
            msg = str(parsed.get("errorMessage") or "") if isinstance(parsed, dict) else ""
            if (r.status_code == 403 or "Rate limit" in msg) and attempt < MAX_RETRY:
                # Jittered, and applied to the SHARED schedule. Without the
                # jitter every worker waited the identical 2/4/8/16s and came
                # back in lockstep to collide again; without back_off() the
                # other threads carried on pushing while this one waited.
                wait = min(30, 2 ** attempt) * (0.5 + random.random())
                GATE.back_off(wait)
                if attempt >= 3:
                    print("      rate limited - backing off %.1fs (attempt %d)"
                          % (wait, attempt), flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise Stop("HTTP 401 on %s %s - the token has expired. Ask for a "
                           "fresh one. %s" % (method, path, str(parsed)[:200]))
            # A 500 carrying a considered errorMessage is a REFUSAL, not a
            # fault: "GST Info not available. Please add first.", "Pan Can not
            # be null". Retrying one changes nothing and costs 2+4+8+16+30+30 =
            # ~90s of backoff, so over a few hundred held vendors it burns
            # hours to arrive at the same answer. Retry only a 500 the server
            # had nothing to say about, which is the transient one.
            if r.status_code >= 500 and attempt < MAX_RETRY and not msg:
                time.sleep(min(30, 2 ** attempt))
                continue
            return r.status_code, parsed
        return None, "exhausted retries"

    def get(self, p):
        return self.call("GET", p)

    def post(self, p, b=None):
        return self.call("POST", p, b)

    @staticmethod
    def ok(status, body):
        if status == 0:
            return True                      # dry run
        if status is None or not (200 <= status < 300):
            return False
        return not (isinstance(body, dict) and body.get("success") is False)

    @staticmethod
    def err(body):
        if isinstance(body, dict):
            return str(body.get("errorMessage") or body.get("message") or body)[:300]
        return str(body)[:300]

    @staticmethod
    def data(body):
        return body.get("data") if isinstance(body, dict) else None


# ============================================================================
# STATE - crosswalk of live ids, plus the append-only posted log
# ============================================================================

class State:
    def __init__(self):
        self.xw = {"products": {}, "contacts": {}, "series": {}}
        if os.path.exists(CROSSWALK):
            with open(CROSSWALK, encoding="utf-8-sig") as fh:
                self.xw.update(json.load(fh))
        self.posted = {}
        if os.path.exists(POSTED_LOG):
            with open(POSTED_LOG) as fh:
                for ln in fh:
                    if ln.strip():
                        self.posted[ln.split("||")[0]] = ln.strip()

    def save(self):
        os.makedirs(WORK, exist_ok=True)
        tmp = CROSSWALK + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.xw, fh, indent=1, sort_keys=True)
        os.replace(tmp, CROSSWALK)

    def mark(self, key, value):
        """Append and FLUSH immediately - an interrupted run must resume, not
        re-attempt."""
        os.makedirs(WORK, exist_ok=True)
        with open(POSTED_LOG, "a") as fh:
            fh.write("%s||%s\n" % (key, value))
            fh.flush()
            os.fsync(fh.fileno())
        self.posted[key] = value


# ============================================================================
# SOURCE: read Sage once, shape it into bills
# ============================================================================

HEADER_PSV = "/root/indiandesign/reference/Bills_JanApr_Header.psv"


def load_headers():
    """-> header rows for the window.

    Preferred source is Sage APOBL, filtered exactly as extract.sql filtered the
    proven run. sage_ap_obl in staging is NOT usable: it is an open-items pull
    (AMTDUEHC <> 0 OR DATEINVC >= cutover) holding 2,425 of the window's 11,256
    documents.

    <sage-host> is on Wi-Fi DHCP and drops off regularly. When it is unreachable,
    fall back to Bills_JanApr_Header.psv on the devbox - the read-only extract of
    this exact population, and the input the proven PowerShell run consumed. The
    source used is always printed, because the two must not be silently mixed.
    """
    try:
        print("reading Sage APOBL headers %s .. %s" % (DATE_FROM, DATE_TO), flush=True)
        heads = sage_query(SQL_HEADERS)
        print("  header source: Sage APOBL (live) - %d rows" % len(heads), flush=True)
        return heads
    except Exception as exc:                                    # noqa: BLE001
        print("  Sage unreachable (%s)" % str(exc).split("\n")[0][:80], flush=True)

    import subprocess
    proc = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                           db_host(), "cat %s" % HEADER_PSV],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode:
        raise Stop("Sage is unreachable AND the extract could not be read: %s"
                   % proc.stderr[:200])
    rows, hdr = [], None
    for raw in proc.stdout.splitlines():
        ln = raw.rstrip("\r")
        if hdr is None:
            hdr = ln.split("~"); continue
        if "~" not in ln:
            continue
        p = ln.split("~")
        p = p[:len(hdr)] + [""] * (len(hdr) - len(p))
        r = dict(zip(hdr, p))
        rows.append({"vendor": r["vendor"], "invoice": r["invoice"],
                     "bill_date": r["date"], "due_date": r["dueDate"],
                     "gross": r["gross"], "header_tax": r["headerTax"],
                     "tax_group": r["taxGroup"], "CNTBTCH": "", "CNTITEM": ""})
    print("  header source: Bills_JanApr_Header.psv (Sage down) - %d rows"
          % len(rows), flush=True)
    return rows


def _csv_rows(path):
    import csv
    with open(path) as fh:
        return list(csv.DictReader(fh))


def vendors_master():
    """-> {vendor code: row in the shape SQL_VENDORS returns}

    Sage first. <sage-host> is on Wi-Fi DHCP and drops off regularly, and when it
    is gone `masters` had no fallback at all - it simply could not run, which is
    what stopped the 2 Sep pipeline. output/vendors.csv is the read-only extract
    of this exact population (the same pull extract.sql produced), so it stands
    in cleanly. The source is always printed: the two must not be silently
    mixed.

    APVEN.TEXTPHON1/EMAIL1 are not in the extract, so contacts built from it get
    the placeholder mobile and no email. That is already flagged per contact by
    metaData.mobileIsPlaceholder.
    """
    try:
        rows = {s(v["vendor"]): v for v in sage_query(SQL_VENDORS)}
        print("  vendor master: Sage APVEN (live) - %d rows" % len(rows), flush=True)
        return rows
    except Exception as exc:                                    # noqa: BLE001
        print("  Sage unreachable (%s)" % str(exc).split("\n")[0][:80], flush=True)
    path = os.path.join(HERE, "output", "vendors.csv")
    if not os.path.exists(path):
        raise Stop("Sage is unreachable and %s is missing - cannot build "
                   "contacts without a vendor master." % path)
    rows = {}
    for r in _csv_rows(path):
        rows[s(r["vendor"])] = {
            "vendor": r["vendor"], "name": r["name"],
            "legal_name": r.get("legal_name", ""), "brn_raw": r.get("brn", ""),
            "street1": r.get("street1", ""), "street2": r.get("street2", ""),
            "street3": r.get("street3", ""), "street4": r.get("street4", ""),
            "city": r.get("city", ""),
            "state_raw": r.get("state_text", ""), "pincode": r.get("pincode", ""),
            "country": r.get("country", ""),
            "contact_person": r.get("contact_person", ""),
            "phone1": "", "email1": "",
        }
    print("  vendor master: output/vendors.csv (Sage down) - %d rows" % len(rows),
          flush=True)
    return rows


_GL_GROUPS = {}


def gl_groups():
    """-> {formatted account code: ACCTGRPCOD}, Sage first then the extract.

    Cached: classify() asks per document and the answer cannot change inside a
    run. Keyed on ACCTFMTTD for the reason given against BILLTYPE_BY_GROUP."""
    if _GL_GROUPS:
        return _GL_GROUPS
    try:
        for r in sage_query(SQL_GL):
            _GL_GROUPS[s(r["acct_code"])] = s(r["acct_group"])
        print("  GL groups: Sage GLAMF (live) - %d rows" % len(_GL_GROUPS),
              flush=True)
        return _GL_GROUPS
    except Exception as exc:                                    # noqa: BLE001
        print("  Sage unreachable for GL groups (%s)"
              % str(exc).split("\n")[0][:80], flush=True)
    path = os.path.join(HERE, "output", "gl_accounts.csv")
    if not os.path.exists(path):
        raise Stop("Sage is unreachable and %s is missing - the bill type "
                   "cannot be derived without ACCTGRPCOD." % path)
    for r in _csv_rows(path):
        # Both forms, so a line quoting the unformatted code still resolves.
        grp = s(r.get("acct_group"))
        _GL_GROUPS[s(r.get("acct_formatted"))] = grp
        _GL_GROUPS.setdefault(s(r.get("acct_id")), grp)
    _GL_GROUPS.pop("", None)
    print("  GL groups: output/gl_accounts.csv (Sage down) - %d rows"
          % len(_GL_GROUPS), flush=True)
    return _GL_GROUPS


def bill_type_of(exp):
    """-> (billType, note) | (None, reason) for one document's 4E lines.

    A bill carries ONE billType but its lines need not agree. The type follows
    the money: the head holding the largest absolute amount wins, and an exact
    tie goes to IN_DIRECT_EXPENSE - the conservative side, since it claims
    neither an input credit nor a cost of goods.

    Only expense lines vote. The round-off, the RCM payable legs (1L8TX*) and
    the recoverable input legs (2A7T*) are not the document's purpose and are
    already excluded from `exp` by classify().

    A mixed document is NOT split into two bills: that would break 1:1 with the
    source, duplicate the invoice number and split one payable in two. It does
    not need splitting, because every line carries its own financeAccountDto
    and the server auto-resolves a ledger only where that field is blank - so
    the minority lines keep their own head regardless of what the header says.
    """
    groups = gl_groups()
    tally, unknown = collections.OrderedDict(), []
    for l in exp:
        acct = s(l["gl"])
        bt = account_bill_type(acct, groups)
        if not bt:
            unknown.append(acct)
            continue
        tally[bt] = tally.get(bt, D(0)) + abs(D(l["amount"] or 0))
    if unknown:
        return None, ("no bill type for account %s - refusing to guess"
                      % ", ".join(sorted(set(unknown))[:5]))
    if not tally:
        return None, "no expense line carries an account group"
    # Sort by amount, then put IN_DIRECT_EXPENSE first among equals. `False`
    # sorts before `True`, so the key must be "is NOT indirect" to make the
    # indirect side win a tie.
    ranked = sorted(tally.items(),
                    key=lambda kv: (-kv[1], kv[0] != "IN_DIRECT_EXPENSE"))
    if len(ranked) == 1:
        return ranked[0][0], ""
    return ranked[0][0], ("mixed: " + ", ".join("%s %s" % (k, q2(v))
                                                for k, v in ranked))


def gl_names():
    """-> {formatted account code: description}, Sage first then the extract.

    Keyed on ACCTFMTTD, like SQL_GL - output/gl_accounts.csv carries both forms
    and acct_formatted is the one APIBD.IDGLACCT holds."""
    try:
        names = {s(r["acct_code"]): s(r["description"]) for r in sage_query(SQL_GL)}
        print("  GL names: Sage GLAMF (live) - %d rows" % len(names), flush=True)
        return names
    except Exception as exc:                                    # noqa: BLE001
        print("  Sage unreachable (%s)" % str(exc).split("\n")[0][:80], flush=True)
    path = os.path.join(HERE, "output", "gl_accounts.csv")
    if not os.path.exists(path):
        raise Stop("Sage is unreachable and %s is missing - products would be "
                   "named after their own account codes." % path)
    names = {s(r["acct_formatted"]): s(r["description"]) for r in _csv_rows(path)}
    print("  GL names: output/gl_accounts.csv (Sage down) - %d rows" % len(names),
          flush=True)
    return names


def staging_query(sql):
    """Read-only against idedat_staging on the devbox, over ssh."""
    import subprocess
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", db_host(),
         "mysql idedat_staging -N --raw --batch -e %s" % json.dumps(sql)],
        capture_output=True, text=True, timeout=600)
    if proc.returncode:
        raise Stop("staging query failed: %s" % proc.stderr[:400])
    return [ln.split("\t") for ln in proc.stdout.splitlines() if ln.strip()]


STAGING_COLS = ["vendor_code", "inv_number_raw", "cntline", "gl", "amount",
                "descr", "RATETAX1", "RATETAX2", "amttax1", "amttax2"]


def load_book():
    """-> OrderedDict keyed (vendor, base invoice) -> {headers, lines}

    Headers come from Sage APOBL, filtered exactly as extract.sql filtered the
    proven .psv run. They are NOT taken from staging.sage_ap_obl, which is an
    open-items pull (AMTDUEHC <> 0 OR DATEINVC >= cutover) and holds only 2,425
    of the window's 11,256 documents - joining to it silently drops ~79%.

    Lines come from staging.sage_ap_dist, which carries the stated rate.
    """
    heads = load_headers()

    print("reading idedat_staging.sage_ap_dist", flush=True)
    raw = staging_query(SQL_STAGING_LINES.strip().replace("\n", " "))
    print("  %d distribution rows in staging" % len(raw), flush=True)

    by_key = collections.defaultdict(list)
    nulls = 0
    for row in raw:
        r = dict(zip(STAGING_COLS, row))
        for c in ("RATETAX1", "RATETAX2", "amttax1", "amttax2", "amount"):
            if r[c] in ("NULL", "", None):
                if c.startswith("RATETAX"):
                    nulls += 1
                r[c] = "0"
        r["CNTLINE"] = r["cntline"]
        r["descr"] = "" if r["descr"] == "NULL" else r["descr"]
        by_key[(s(r["vendor_code"]), base_invoice(r["inv_number_raw"]))].append(r)
    if nulls:
        # The agreed rule: if a rate is ever NULL, stop and say so rather than
        # falling back to division.
        raise Stop("%d staging rows carry a NULL rate - pull.py's ap_dist re-pull "
                   "is incomplete. Stopping rather than dividing." % nulls)

    po = load_po_items()

    book = collections.OrderedDict()
    for h in heads:
        k = (s(h["vendor"]), base_invoice(h["invoice"]))
        b = book.setdefault(k, {"headers": [], "lines": by_key.get(k, []),
                                "po": po.get(k, [])})
        b["headers"].append(h)
    print("  %d distinct bills (%d header rows consolidated by the *N suffix)"
          % (len(book), len(heads) - len(book)), flush=True)
    return book


# ---------------------------------------------------------------------------
# PO ITEM DETAIL
# ---------------------------------------------------------------------------
# AP-direct distribution lines carry no quantity and no unit price: across the
# 46,156 lines in the Jan-Apr 2026 window, QTYINVC, UNITMEAS, BILLRATE and
# AMTCOST are zero or blank WITHOUT EXCEPTION, and IDITEM is empty on every one.
# The item detail is not lost, though - it lives on the purchase order, reached
# through APIBH.PONBR. Sage stores that number bare ('2523394') while the PO
# module stores it prefixed ('IDPO2523394'), which is why a naive equijoin
# finds nothing.
#
# The PO is an ORDER, not the invoice: an invoice may bill part of a PO, so the
# PO total and the invoice taxable often disagree (measured: 12 of 49 diverge,
# several by round -20,000 amounts). POINVL - the PO-invoice detail that WOULD
# be per-invoice - is empty for this population: these documents were keyed
# straight into AP and never went through PO invoicing.
#
# So the detail is adopted only when it reconciles to the paise against the
# distribution we are already posting. Anything else keeps the amount-only
# CHARGE line, because the bill total must equal Sage's.
#
# The IC item master (ICITEM) supplies the item's identity and stocking unit,
# but NOT its price: for the 624 items behind this window every STDCOST and
# COST1 is zero, and RECENTCOST/LASTCOST are inventory valuation per location -
# zero on the job-work items these bills are made of, and in no case the
# vendor's contracted rate. The rate is POPORL.UNITCOST and nowhere else.

SQL_PO_ITEMS = """
SET NOCOUNT ON;
SELECT RTRIM(b.IDVEND)    AS vendor,
       RTRIM(b.IDINVC)    AS invoice,
       RTRIM(l.ITEMNO)    AS item,
       RTRIM(ic.ITEMNO)   AS item_raw,
       RTRIM(l.ITEMDESC)  AS descr,
       RTRIM(ic.STOCKUNIT) AS stock_um,
       RTRIM(l.ORDERUNIT) AS um,
       CAST(l.OQORDERED AS decimal(18,4)) AS qty,
       CAST(l.UNITCOST  AS decimal(18,6)) AS unitcost,
       CAST(l.EXTENDED  AS decimal(18,2)) AS ext
  FROM APOBL b
  JOIN APIBH  a ON a.CNTBTCH = b.CNTBTCH AND a.CNTITEM = b.CNTITEM
  JOIN POPORH1 h ON RTRIM(h.PONUMBER) = 'IDPO' + RTRIM(a.PONBR)
  JOIN POPORL  l ON l.PORHSEQ = h.PORHSEQ
  -- POPORL.ITEMNO is the FORMATTED code, so it keys on ICITEM.FMTITEMNO. On
  -- ICITEM.ITEMNO it matches 20 of 12,854 lines; on FMTITEMNO, all 12,854.
  -- Same trap as ACCTID vs ACCTFMTTD on the GL accounts.
  LEFT JOIN ICITEM ic ON RTRIM(ic.FMTITEMNO) = RTRIM(l.ITEMNO)
 WHERE b.IDTRXTYPE = 12 AND b.SRCEAPPL = 'AP'
   AND b.DATEINVC BETWEEN %d AND %d
   AND RTRIM(a.PONBR) <> ''
 ORDER BY b.IDVEND, b.IDINVC, l.PORLSEQ
"""


PO_ITEMS_CACHE = os.path.join(WORK, "po_items_cache.json")


def load_po_items():
    """-> {(vendor, base invoice): [PO item line, ...]} for the window.

    Sage only - idedat_staging cannot stand in here. Its sage_goods_line is the
    PO-sourced receipt population (srce_appl 'PO'), and of the AP-sourced
    documents this query is about it holds exactly one, with 5 lines. So the
    read is cached on first success and reused when the Sage box is gone.

    WITHOUT SAGE AND WITHOUT A CACHE this returns empty, and that is a real but
    bounded loss: po_detail() then reports "no PO linked" and those bills post
    as one distribution line, quantity 1 at the whole amount, instead of the
    items Sage ordered. The measured population is 1,579 lines over 393 of the
    window's 11,256 documents (3.5%). THE AMOUNTS ARE UNAFFECTED - the line
    carries the same taxable, and po_detail() only ever admits single-GL
    documents so the ledger is identical either way. What is lost is the
    item-level narrative, and it is recoverable: revoke and repost those 393
    once Sage is reachable. The caller is warned loudly rather than silently
    given a thinner bill.
    """
    print("reading PO item detail (APIBH.PONBR -> POPORH1/POPORL)", flush=True)
    out = collections.defaultdict(list)
    try:
        rows = sage_query(SQL_PO_ITEMS % (DATE_FROM, DATE_TO))
        for r in rows:
            out[(s(r["vendor"]), base_invoice(r["invoice"]))].append(r)
        with open(PO_ITEMS_CACHE, "w") as fh:
            json.dump([{k: s(vv) for k, vv in r.items()} for r in rows], fh)
        print("  %d PO item lines covering %d documents (cached)"
              % (len(rows), len(out)), flush=True)
        return out
    except Exception as exc:                                    # noqa: BLE001
        print("  Sage unreachable (%s)" % str(exc).split("\n")[0][:80], flush=True)

    if os.path.exists(PO_ITEMS_CACHE):
        with open(PO_ITEMS_CACHE) as fh:
            rows = json.load(fh)
        for r in rows:
            out[(s(r["vendor"]), base_invoice(r["invoice"]))].append(r)
        print("  %d PO item lines covering %d documents (from cache %s)"
              % (len(rows), len(out), PO_ITEMS_CACHE), flush=True)
        return out

    print("  !! NO PO ITEM DETAIL. Sage is down and %s does not exist, so the\n"
          "     ~393 PO-matched AP bills will post as a single distribution\n"
          "     line each instead of their Sage items. Amounts and ledgers are\n"
          "     unaffected; the item breakdown is not. Re-run when Sage is up\n"
          "     to itemise them." % PO_ITEMS_CACHE, flush=True)
    return out


def po_detail(po_rows, exp, taxable, rates):
    """-> (item lines, None) when the PO detail may stand in for the amount
    lines, else (None, reason).

    Every gate here exists because failing it would misstate a posted bill.
    """
    if not po_rows:
        return None, "no PO linked"
    gls = {s(l["gl"]) for l in exp}
    if len(gls) != 1:
        # PO lines carry no GL of their own (POPORL.GLACEXPENS is blank on this
        # population), so they can only inherit one. Measured: every PO-linked
        # eligible bill in the window is single-GL, so this is a guard, not a
        # common path.
        return None, "PO detail spans %d GL accounts" % len(gls)
    rs = {rates[l["CNTLINE"]] for l in exp}
    if len(rs) != 1:
        return None, "PO detail would flatten %d distinct rates" % len(rs)
    ext = sum(D(str(r["ext"] or 0)) for r in po_rows)
    if q2(ext) != q2(taxable):
        # The PO covers more or less than this invoice bills.
        return None, ("PO total %s != invoice taxable %s" % (q2(ext), q2(taxable)))
    for r in po_rows:
        qty, uc, e = (D(str(r["qty"] or 0)), D(str(r["unitcost"] or 0)),
                      D(str(r["ext"] or 0)))
        if qty <= 0:
            return None, "PO line has no quantity"
        # Mirrors assert_invariants exactly: full-precision unit cost, and the
        # product rounded to the paise.
        if q2(uc * qty) != q2(e):
            return None, ("PO line %s x %s != extended %s" % (qty, uc, q2(e)))
    return po_rows, None


def line_rate(l):
    """4.1 - the STATED rate. RATETAX1..5 is the rate per tax AUTHORITY: for
    intra-state Sage books CGST and SGST as two authorities (9 + 9), for
    inter-state as one (18). Their sum is the total rate on the line, which is
    what SMEAssist wants - it splits the total itself from the party's state.

    Each authority is validated against its OWN amount. Sage truncates the tax
    per authority (1245.42 @ 9% = 112.0878 is booked as 112.08), so comparing
    the summed rate against the summed amount accumulates the truncation twice
    and reports a false mismatch on 392 otherwise-clean lines.

    -> (total_rate, stated_tax_amount, [problems])
    """
    base = D(l["amount"] or 0)
    rate, amt, problems = D(0), D(0), []
    # Slots 1 and 2 only: RATETAX3/4/5 are zero on every row in this window, and
    # staging carries just the two the re-pull added.
    for i in range(1, 3):
        # DECIMAL(10,4): Sage stores 9.0001 / 2.5002 / 17.9997. Round each
        # authority to 2 dp BEFORE it is used as a label or compared to a slab -
        # raw, 96 lines look off-slab; rounded, only 20 genuinely are.
        ri = D(str(l["RATETAX%d" % i] or 0)).quantize(D("0.01"))
        ai = D(l["amttax%d" % i] or 0)
        rate += ri
        amt += ai
        if ri == 0 and ai == 0:
            continue
        want = base * ri / 100
        if abs(want - ai) > D("0.01"):
            problems.append("authority %d states %s%% on %s implying %s, but "
                            "Sage booked %s" % (i, ri, base, q2(want), ai))
    return rate.quantize(D("0.01")), q2(amt), problems


def snap_to_slab(rate):
    """Only for reverse charge, where Sage states NO rate at all: RATETAX is
    zero on every line and the tax exists only as the 2A7TX/1L8TX distribution
    pair. Derive, then snap to a legal slab - and refuse rather than send a
    rate that is not one. -> (slab, distance) """
    best = min(LEGAL_SLABS, key=lambda sl: abs(sl - rate))
    return best, abs(best - rate)


def classify(bill):
    """Split a Sage document into the pieces the payload needs, or explain why
    it cannot be shaped. -> (shape, None) | (None, reason)"""
    h = bill["headers"][0]
    ll = bill["lines"]
    exp = [l for l in ll if s(l["gl"]).startswith("4E") and s(l["gl"]) != ROUNDOFF_GL]
    ro  = [l for l in ll if s(l["gl"]) == ROUNDOFF_GL]
    rcm = [l for l in ll if s(l["gl"]) in RCM_GL]
    inp = [l for l in ll if s(l["gl"]).startswith("2A7T")]
    if not exp:
        return None, "no 4E expense line"

    taxable  = q2(sum(D(l["amount"] or 0) for l in exp))
    roundoff = q2(sum(D(l["amount"] or 0) for l in ro))
    if taxable == 0:
        return None, "taxable is zero"

    # 4.3 - RCM is decided by Sage's OWN BOOKING: a distribution to the
    # 1L8TX14/15/16 payable accounts. Never by whether the vendor holds a
    # GSTIN; 82% of RCM bills come from vendors that do.
    is_rcm = bool(rcm)

    # 4.4 - RCM bills are grossed up, forward-charge bills are not.
    if is_rcm:
        # AMTTAXHC is ZERO on an RCM bill; the tax exists only as negatives on
        # the payable lines.
        tax = q2(abs(sum(D(l["amount"] or 0) for l in rcm)))
        bill_amount = q2(taxable + tax + roundoff)
    else:
        tax = q2(sum(D(h["header_tax"] or 0) for h in bill["headers"]))
        bill_amount = q2(sum(D(h["gross"] or 0) for h in bill["headers"]))

    # 4.1 - THE RATE. Two paths, and the run log says which one was used.
    #
    #   forward charge : Sage STATES the rate on the distribution line
    #                    (RATETAX1..5). Read it. Never divide.
    #   reverse charge : Sage states NOTHING - RATETAX is 0.00 on every line of
    #                    every RCM document, because the tax was keyed in by
    #                    hand as the 2A7TX input / 1L8TX payable pair. There is
    #                    no stated rate to read, so it is derived once for the
    #                    document and snapped to a legal slab; anything further
    #                    than 0.05 from one is refused, never rounded into.
    rates = {}
    if is_rcm:
        derived = (tax / taxable * 100) if taxable else D(0)
        slab, dist = snap_to_slab(derived)
        if dist > D("0.05"):
            return None, ("reverse charge: derived rate %s%% is not within 0.05 "
                          "of a legal slab - the document mixes rates and Sage "
                          "states none (needs APIBD.RATETAX)"
                          % derived.quantize(D("0.01")))
        rate_source = "derived+snapped (reverse charge states no rate)"
        for l in exp:
            rates[l["CNTLINE"]] = slab.quantize(D("0.01"))
    else:
        rate_source = "stated (APIBD.RATETAX1..5)"
        stated_tax = D(0)
        for l in exp:
            rate, amt, problems = line_rate(l)
            if problems:
                return None, "line %s: %s" % (l["CNTLINE"], problems[0])
            if rate not in LEGAL_SLABS:
                return None, ("line %s on %s states rate %s, which is not an "
                              "Indian GST slab" % (l["CNTLINE"], s(l["gl"]), rate))
            rates[l["CNTLINE"]] = rate.quantize(D("0.01"))
            stated_tax += amt
        # The rate labels must account for the tax the document carries. Sage's
        # per-authority truncation allows a paisa per authority per line.
        tol = max(D("0.05"), D("0.01") * len(exp) * 2)
        if abs(q2(stated_tax) - tax) > tol:
            return None, ("stated per-line tax %s disagrees with the document "
                          "tax %s" % (q2(stated_tax), tax))

    # ZERO-RATED LINES. The server ignores the gstAmount and roundOffAmount we
    # send and enforces its own identity - measured on every bill in the org,
    # without exception:
    #
    #     gstAmount  = SUM(line taxableAmount x line gstPercentage / 100)
    #     billAmount = taxableAmount + gstAmount + roundOffAmount
    #     roundOffAmount = the server's OWN nearest-rupee figure whenever
    #                      hasRoundOff is true; 0 when it is false, and then the
    #                      exact paise survive.
    #
    # So the only levers are per-line taxableAmount and gstPercentage, and
    # anything that has to reach billAmount has to arrive as a line.
    zero = []

    # 4.5 (revised) - the round-off is a LINE, not a bill field. Sage books
    # 4E1M016 as a distribution line and the account is ACCTTYPE=I, so a line
    # mirrors Sage exactly. As a bill field the server discarded our value and
    # substituted its own nearest-rupee figure, which put 101 of the 536
    # documents that carry one off Sage's gross - JOBW258|108 by a full rupee.
    # 188 of these round-offs are negative; billLineItem accepts that.
    for l in ro:
        if D(l["amount"] or 0) != 0:
            zero.append({"gl": s(l["gl"]), "amount": q2(l["amount"]),
                         "descr": s(l["descr"]), "ledger": None,
                         "why": "Sage 4E1M016 round-off"})

    # The import-IGST reimbursement, forward charge only. On a reverse-charge
    # document the 2A7T line is the INPUT LEG of the self-assessed pair and
    # equals the tax already (1084 of 1084 documents in the window), so emitting
    # it there would double-count the tax into taxable.
    if not is_rcm:
        # NET PER ACCOUNT, never line by line. 35 documents in the window book
        # the same recoverable twice, once positive and once negative, so each
        # account cancels itself out (OTHL493|62163/25-26 has 2A7TX01 +3263.69
        # and -3263.69, and 2A7TX02 the same). Those documents already tie to
        # AMTINVCHC and must gain no line - and must not be held for want of a
        # ledger they never use.
        net = collections.OrderedDict()
        for l in inp:
            gl = s(l["gl"])
            net[gl] = net.get(gl, D(0)) + D(l["amount"] or 0)
        for gl, amt in net.items():
            amt = q2(amt)
            if amt == 0:
                continue
            if gl not in PASSTHRU_GL:
                return None, ("%s nets %s, a balance-sheet recoverable with no "
                              "mapped ledger - refusing rather than booking it "
                              "to an expense head" % (gl, amt))
            led, led_name = PASSTHRU_GL[gl]
            descr = next((s(l["descr"]) for l in inp
                          if s(l["gl"]) == gl and s(l["descr"])), "")
            zero.append({"gl": gl, "amount": amt, "descr": descr, "ledger": led,
                         "why": "import IGST reimbursed to the vendor -> %s" % led_name})

    # What the payload must declare as taxableAmount: the expense plus every
    # zero-rated line. gstAmount is untouched by them, so
    # taxable_all + tax has to be Sage's own AMTINVCHC.
    taxable_all = q2(taxable + sum(z["amount"] for z in zero))
    if taxable_all + tax != bill_amount:
        return None, ("AMTINVCHC %s != 4E %s + zero-rated %s + tax %s - the "
                      "document carries a distribution this loader does not map"
                      % (bill_amount, taxable, q2(taxable_all - taxable), tax))

    # Item detail is an enrichment, never a gate: a document that cannot take
    # it still posts, on the amount-only CHARGE line it posted on before.
    items, item_why = po_detail(bill.get("po", []), exp, taxable, rates)

    # The bill type comes from Sage's account groups, never from a default.
    bill_type, type_note = bill_type_of(exp)
    if not bill_type:
        return None, type_note

    return {"header": h, "exp": exp, "roundoff": roundoff, "is_rcm": is_rcm,
            "bill_type": bill_type, "type_note": type_note,
            "taxable": taxable, "tax": tax, "bill_amount": bill_amount,
            "zero": zero, "taxable_all": taxable_all,
            "tax_group": s(h["tax_group"]), "rates": rates,
            "rate_source": rate_source,
            "items": items, "item_source": item_why or "PO %d line(s)" % len(items or ())}, None


# ============================================================================
# SOURCE 2: GOODS / RAW-MATERIAL BILLS (PO-matched)
# ============================================================================
# A different population from the AP-direct one above, and a better-behaved
# one. AP-direct documents state no quantity and no unit price anywhere, which
# is why those bills carry a synthesised "1 x whole amount" line unless a PO
# can be reconciled to them. Goods bills state both, exactly: across all 32,586
# rows of sage_goods_line, qty * unit_cost = extended WITHOUT EXCEPTION. There
# is no reconciliation gate here because there is nothing to reconcile.
#
# Everything is read from idedat_staging, not from Sage, so this path works
# while the Sage box is unreachable.
#
# The accounting differs and that difference is the point: buying fabric or
# thread debits INVENTORY (1L6T*), not expense (4E*). Measured over the window,
# 4THRED is 4,884 inventory lines against 0 expense, 4FABRI 1,042 against 13.
# So these post as billProcurementType MATERIAL with lineItemType GOODS, and
# their products are seeded against the inventory head the distribution names -
# booking them to a 4E expense head would misstate the purchase.

# Sage's unit of measure -> the platform's measurementUnit code. The full enum
# is the 45 rows of the measurementUnit table, NOT the handful already in use:
# NOS is in it, so the older "NOS is rejected" note was wrong - what fails is a
# code that is not in that table at all.
#
# Where Sage names a container the platform has no word for, the mapping falls
# back to how the thing is actually counted: a CONE, SHEET, REAM or COIL of
# thread is a number of them, so NOS. BOX10K/BOX5K name the pack size, not the
# unit - the quantity is still in boxes. Anything not listed posts as OTH
# rather than a guess, and the run prints it so it never degrades silently.
UNIT_MAP = {
    "NOS": "NOS", "PCS": "PCS", "MTRS": "MTR", "MTR": "MTR", "YRDS": "YDS",
    "KGS": "KGS", "SET": "SET", "BOX": "BOX", "BOX10K": "BOX", "BOX5K": "BOX",
    "ROLLS": "ROL", "ROLL": "ROL", "GROSS": "GRS", "PKT": "PAC",
    "LTRS": "LTR", "BOTTLE": "BTL", "SQMT": "SQM", "SQFT": "SQF",
    "CONES": "NOS", "SHEETS": "NOS", "REAM": "NOS", "COIL": "NOS",
    "KIT": "SET", "CTN": "CTN", "DOZ": "DOZ",
    # Not a unit of measure at all - an AMC period. Nothing in the enum fits.
    "YEAR": "OTH", "LNTH": "OTH",
}
UNITS_UNMAPPED = collections.Counter()


def platform_unit(raw):
    """-> a measurementUnit code. Records anything it had to fall back on."""
    u = s(raw).upper()
    if not u:
        return "OTH"
    hit = UNIT_MAP.get(u)
    if hit:
        return hit
    UNITS_UNMAPPED[u] += 1
    return "OTH"


RAW_MATERIAL_CATEGORIES = (
    "4FABRI", "4THRED", "4BUTON", "4ZIPER", "4LABEL", "4PACK", "4CARTN",
    "4ELAST", "4POLYB", "4ACCES", "4FASHL", "4FAINT", "4HANGR", "4VELCR",
)

SQL_GOODS_HDR = """
SELECT h.invhseq, h.vendor_code, h.inv_number_raw, h.inv_number_base,
       h.inv_date, h.due_date, h.doc_total, h.tax_total, h.po_number
  FROM sage_bill_hdr h
 WHERE h.inv_date BETWEEN %d AND %d
"""

SQL_GOODS_LINES = """
SELECT g.invhseq, g.invlseq, g.item_no, COALESCE(i.item_no_raw,''),
       g.item_desc, g.uom, COALESCE(i.stock_unit,''), COALESCE(i.category,''),
       g.qty, g.unit_cost, g.extended, g.rate_sum, COALESCE(g.hsn,'')
  FROM sage_goods_line g
  LEFT JOIN sage_item i ON i.item_no_fmt = g.item_no
"""

# The goods line carries no GL of its own; the AP distribution of the same
# document does. 17,910 of 18,047 goods documents have one.
SQL_GOODS_GL = """
SELECT d.vendor_code, d.inv_number_raw, d.gl_account, SUM(d.amt_dist)
  FROM sage_ap_dist d
 GROUP BY d.vendor_code, d.inv_number_raw, d.gl_account
"""

SQL_GOODS_SERVICE = """
SELECT s.invhseq, COUNT(*) FROM sage_service_line s GROUP BY s.invhseq
"""

# The additional-cost lines themselves. They carry an amount and their OWN
# stated GST rate (0 / 2.5 / 5 / 18) but no item, which is exactly the shape
# the AP-direct path already books against a CHARGE pseudo-item. The IGST /
# CGST / SGST coded rows are 0%-rated reimbursements, so they need no special
# handling - their stated rate is already zero.
SQL_GOODS_SERVICE_LINES = """
SELECT s.invhseq, s.invsseq, s.add_cost, s.description, s.amount, s.rate_sum
  FROM sage_service_line s
"""
GOODS_SVC_COLS = ["invhseq", "invsseq", "add_cost", "descr", "amount", "rate"]

GOODS_HDR_COLS = ["invhseq", "vendor", "invoice_raw", "invoice", "bill_date",
                  "due_date", "doc_total", "tax_total", "po_number"]
GOODS_LINE_COLS = ["invhseq", "invlseq", "item", "item_raw", "descr", "um",
                   "stock_um", "category", "qty", "unitcost", "ext", "rate",
                   "hsn"]


def _staging_rows(sql, cols):
    """staging_query leaks the mysql client's own chatter ('PAGER set to
    stdout') into stdout, which becomes a short bogus row. Drop anything that
    is not the width we asked for rather than letting it corrupt a dict."""
    out = []
    for r in staging_query(sql.strip().replace("\n", " ")):
        if len(r) == len(cols) and "PAGER" not in r[0]:
            out.append(dict(zip(cols, r)))
    return out


def load_goods_book():
    """-> OrderedDict keyed (vendor, base invoice) -> {header, lines, gl}."""
    print("reading idedat_staging.sage_bill_hdr", flush=True)
    heads = _staging_rows(SQL_GOODS_HDR % (DATE_FROM, DATE_TO), GOODS_HDR_COLS)
    print("  %d goods/PO headers in the window" % len(heads), flush=True)

    print("reading idedat_staging.sage_goods_line + sage_item", flush=True)
    lines = collections.defaultdict(list)
    for r in _staging_rows(SQL_GOODS_LINES, GOODS_LINE_COLS):
        lines[r["invhseq"]].append(r)
    print("  %d goods lines" % sum(len(v) for v in lines.values()), flush=True)

    gl = collections.defaultdict(dict)
    for r in _staging_rows(SQL_GOODS_GL, ["vendor", "invoice_raw", "gl", "amt"]):
        gl[(s(r["vendor"]), base_invoice(r["invoice_raw"]))][s(r["gl"])] = r["amt"]

    svc = {r["invhseq"] for r in _staging_rows(SQL_GOODS_SERVICE,
                                               ["invhseq", "n"])}
    svc_lines = collections.defaultdict(list)
    for r in _staging_rows(SQL_GOODS_SERVICE_LINES, GOODS_SVC_COLS):
        svc_lines[r["invhseq"]].append(r)
    print("  %d additional-cost lines on %d documents"
          % (sum(len(v) for v in svc_lines.values()), len(svc_lines)), flush=True)

    # MERGE the *N receipt parts, do not overwrite them. This was a plain
    # `book[k] = {...}`, so a goods document split across parts kept only the
    # LAST one and everything on the earlier parts was silently dropped:
    # FABI470|1173/2025-26 posted 10,183.95 against Sage's 815,867.96 because
    # part *1 carried 805,684.01 and was thrown away. 975 of the window's
    # goods documents are multi-part. The AP-direct loader above already
    # accumulates; this one did not.
    #
    # doc_total and tax_total are summed across parts, the lines and
    # additional-cost lines are concatenated, and the earliest part supplies
    # the dates. classify_goods() then ties the merged total to the merged
    # lines exactly as it does for a single-part document.
    book = collections.OrderedDict()
    for h in heads:
        k = (s(h["vendor"]), base_invoice(h["invoice_raw"]))
        b = book.get(k)
        if b is None:
            book[k] = {"header": dict(h), "lines": list(lines.get(h["invhseq"], [])),
                       "gl": gl.get(k, {}), "has_service": h["invhseq"] in svc,
                       "svc": list(svc_lines.get(h["invhseq"], [])),
                       "parts": 1}
            continue
        hdr = b["header"]
        hdr["doc_total"] = D(str(hdr["doc_total"] or 0)) + D(str(h["doc_total"] or 0))
        hdr["tax_total"] = D(str(hdr["tax_total"] or 0)) + D(str(h["tax_total"] or 0))
        b["lines"].extend(lines.get(h["invhseq"], []))
        b["svc"].extend(svc_lines.get(h["invhseq"], []))
        b["has_service"] = b["has_service"] or (h["invhseq"] in svc)
        b["parts"] += 1
    multi = sum(1 for b in book.values() if b["parts"] > 1)
    print("  %d goods documents (%d multi-part, merged across their *N parts)"
          % (len(book), multi), flush=True)
    return book


def classify_goods(bill, categories=None):
    """-> (shape, None) or (None, reason). Same shape contract as classify()."""
    h = bill["header"]
    lines = bill["lines"]
    if not lines:
        return None, "no goods line"
    cats = {s(l["category"]) for l in lines if s(l["category"])}
    if categories and not (cats & set(categories)):
        return None, "no line in the requested categories"

    # The inventory (or expense) head the distribution names. Only the expense
    # side is a candidate: the AP control account and the tax heads are the
    # other legs of the same voucher, not something to book a product against.
    heads = {g: a for g, a in bill["gl"].items()
             if not g.startswith(("2A7TX", "1L8TX", "2A1AP", "5"))}

    # A document carrying additional-cost lines names TWO heads, and they split
    # cleanly: the goods sit on an inventory head (1L6T / 1L7M / 1L7O) and the
    # carriage, handling and courier sit on a 4E expense head. Measured over
    # the window, 2,228 of the 2,245 such documents name exactly those two.
    #
    # These used to be refused outright on the grounds that the additional-cost
    # lines "carry no item and would not tie to the goods total". The first
    # half is true; the second is not. goods extended + service amount + tax
    # equals the document total on 2,264 of 2,264 - not most, every one. And a
    # line with an amount, a stated rate and a GL but no item is exactly what
    # the AP-direct path already books against a CHARGE pseudo-item.
    svc = [x for x in bill.get("svc", []) if q2(D(str(x["amount"] or 0))) != 0]
    svc_gl = None
    if svc:
        inv_heads = [g for g in heads if not g.startswith("4E")]
        exp_heads = [g for g in heads if g.startswith("4E")]
        if len(inv_heads) != 1 or len(exp_heads) != 1:
            return None, ("mixed document names %d inventory and %d expense "
                          "heads" % (len(inv_heads), len(exp_heads)))
        gl, svc_gl = inv_heads[0], exp_heads[0]
    else:
        if len(heads) != 1:
            return None, ("distribution names %d bookable GL accounts" % len(heads))
        gl = list(heads)[0]

    taxable = q2(sum(D(str(l["ext"] or 0)) for l in lines)
                 + sum(D(str(x["amount"] or 0)) for x in svc))
    tax = q2(D(str(h["tax_total"] or 0)))
    total = q2(D(str(h["doc_total"] or 0)))
    roundoff = q2(total - taxable - tax)
    if abs(roundoff) > 1:
        return None, ("goods total %s + tax %s != document total %s"
                      % (taxable, tax, total))

    items, rates = [], {}
    for l in lines:
        qty, uc, ext = (D(str(l["qty"] or 0)), D(str(l["unitcost"] or 0)),
                        q2(D(str(l["ext"] or 0))))
        if qty <= 0:
            return None, "goods line has no quantity"
        # Mirrors assert_invariants: full-precision unit cost, product to 2dp.
        if q2(uc * qty) != ext:
            return None, "goods line %s x %s != extended %s" % (qty, uc, ext)
        rate = q2(D(str(l["rate"] or 0)))
        if rate not in LEGAL_SLABS:
            return None, "line states rate %s, which is not an Indian GST slab" % rate
        key = "%s/%s" % (l["invhseq"], l["invlseq"])
        rates[key] = rate
        items.append(dict(l, ext=ext, qty=qty, unitcost=uc, gl=gl, rate=rate,
                          CNTLINE=key))

    # Additional-cost lines, on the expense head, at their own stated rate.
    # item is blank so build_payload's item_key() lookup misses and the line
    # falls back to the GL pseudo-item for svc_gl - the same CHARGE product the
    # AP-direct path uses. Quantity 1 at the whole amount, as Sage states no
    # quantity for a charge.
    for x in svc:
        amt = q2(D(str(x["amount"] or 0)))
        rate = q2(D(str(x["rate"] or 0)))
        if rate not in LEGAL_SLABS:
            return None, ("additional cost %s states rate %s, not a GST slab"
                          % (s(x["add_cost"]), rate))
        key = "SVC:%s/%s" % (x["invhseq"], x["invsseq"])
        rates[key] = rate
        items.append({"item": "", "item_raw": "", "um": "OTH", "stock_um": "OTH",
                      "descr": s(x["descr"]) or s(x["add_cost"]),
                      "category": "", "hsn": "",
                      "qty": D(1), "unitcost": amt, "ext": amt,
                      "gl": svc_gl, "rate": rate, "CNTLINE": key,
                      "invhseq": x["invhseq"], "invlseq": x["invsseq"]})

    hdr = {"bill_date": s(h["bill_date"]), "due_date": s(h["due_date"]) or s(h["bill_date"]),
           "CNTBTCH": s(h["invhseq"]), "CNTITEM": s(h["po_number"]),
           "tax_group": "GOODS"}
    # Same reasoning as the AP path: the server discards roundOffAmount and
    # substitutes its own nearest-rupee figure, so a round-off has to arrive as
    # a line or it does not arrive at all. Here it is a residual rather than a
    # Sage distribution line, but 4E1M016 "Round Off Value on Purchases" is the
    # account Sage uses for exactly this and it is present in staging's
    # sage_gl_acct, so the line hangs off it and the masters phase picks it up.
    zero = []
    if roundoff != 0:
        zero.append({"gl": ROUNDOFF_GL, "amount": roundoff, "ledger": None,
                     "descr": "Round off", "why": "goods document round-off"})
    taxable_all = q2(taxable + sum(z["amount"] for z in zero))
    if taxable_all + tax != total:
        return None, ("goods taxable %s + round-off %s + tax %s != document "
                      "total %s" % (taxable, roundoff, tax, total))

    return {"header": hdr, "exp": items, "roundoff": roundoff, "is_rcm": False,
            "taxable": taxable, "tax": tax, "bill_amount": total,
            "zero": zero, "taxable_all": taxable_all,
            "tax_group": "GOODS", "rates": rates,
            "rate_source": "stated per goods line (sage_goods_line.rate_sum)",
            "items": items,
            "item_source": "goods %d line(s), qty x unit cost exact" % len(items),
            # Goods really are purchases, and stated as such rather than left to
            # a default: these lines debit inventory (1L6T*), not expense, which
            # is the whole reason this path exists apart from AP-direct. The
            # account-group test does not apply - there is no 4E line to read.
            "bill_type": "PURCHASE", "type_note": "",
            "procurement": "MATERIAL", "line_type": "GOODS",
            "categories": sorted(cats)}, None


# ============================================================================
# MASTERS
# ============================================================================

_SKU_INDEX = {}
_SKU_LOCK = None


def product_index(api, refresh=False):
    """-> {skuCode: product} for the whole org, read once and cached.

    /product/products?searchKey=X DOES NOT SEARCH. It answers 200 and returns
    the org's products unfiltered - asking for SAGE-ID41002AIR01-ROL comes back
    with SAGE-4E4SD01, SAGE-4E2ME14 and 2,941 others, totalElements identical
    whatever the key. The previous code paged 10 x 100 of those looking for an
    exact match, so any product past the first 1,000 rows was invisible: create
    said "Sku Code already exists", adoption then found nothing, and the SKU was
    recorded BURNED. That is a 43% failure rate on a run whose products were all
    present in the database.

    So the listing is walked ONCE into a dict instead of being re-walked per
    item. It also costs ~30 calls for the whole run rather than 10 per lookup,
    which matters against a ~4.5/s budget.
    """
    import threading
    global _SKU_LOCK
    if _SKU_LOCK is None:
        _SKU_LOCK = threading.Lock()
    with _SKU_LOCK:
        if _SKU_INDEX and not refresh:
            return _SKU_INDEX
        if refresh:
            _SKU_INDEX.clear()
        page, seen = 0, 0
        while page < 200:
            st, body = api.get("/product/products?pageSize=200&pageNumber=%d" % page)
            if not api.ok(st, body):
                raise LookupFailed("product listing failed (HTTP %s) at page %d"
                                   % (st, page))
            d = api.data(body)
            rows = (d.get("content") or []) if isinstance(d, dict) else []
            for pr in rows:
                k = s(pr.get("skuCode"))
                if k:
                    _SKU_INDEX[k] = pr
            seen += len(rows)
            if not rows or (isinstance(d, dict) and d.get("last")):
                break
            page += 1
        print("  product index: %d SKUs across %d pages" % (len(_SKU_INDEX), page + 1),
              flush=True)
        return _SKU_INDEX


def remember_product(pr):
    """Add a just-created product so a later adoption in the same run sees it."""
    k = s((pr or {}).get("skuCode"))
    if k:
        _SKU_INDEX[k] = pr


def find_product_by_sku(api, sku):
    """-> the product with exactly this SKU, or None if the org has no such
    product. Raises LookupFailed if the question could not be answered."""
    return product_index(api).get(sku)


def _find_product_by_sku_paged(api, sku):
    page = 0
    while page < 10:
        st, body = api.get("/product/products?searchKey=%s&pageSize=100&pageNumber=%d"
                           % (sku, page))
        # A FAILED lookup is not an absent product. When the server answers 403
        # "Rate limit exceeded" - which it does under sustained load, past the
        # backoff cap - api.data() yields no rows, and returning None here made
        # that indistinguishable from "this SKU does not exist". The caller then
        # recorded the SKU as BURNED, permanently, on the strength of a
        # throttled request. That is where a ~7% burn rate came from on a run
        # that was merely being rate limited.
        if not api.ok(st, body):
            raise LookupFailed("%s: lookup failed (HTTP %s) - not proof of "
                               "absence" % (sku, st))
        d = api.data(body)
        rows = (d.get("content") or []) if isinstance(d, dict) else []
        for pr in rows:
            if s(pr.get("skuCode")) == sku:
                return pr
        if not rows or (isinstance(d, dict) and d.get("last")):
            return None
        page += 1
    return None


def item_ledger_for(api, product_id, mapping):
    """-> the finance account id for a product, creating it if needed.

    `mapping` is REQUIRED and has no default. It used to default to
    ITEM_DIRECT_EXPENSE, and because the AP-direct path never passed anything,
    every one of its products was minted under a Direct Expenses head - 226
    ledgers, with not one under Indirect. A default here is a silent
    misclassification, so there is none.

    Note the endpoint MINTS A SEPARATE LEDGER per reference type; it does not
    remap an existing one. Calling it again with a different mapping adds a
    correctly-headed ledger alongside the old one rather than moving it, which
    is what makes a mis-headed product repairable in place.

    The mapping type decides which accounting group the ledger is minted under,
    and they are not interchangeable. Measured across the platform:

        ITEM_DIRECT_EXPENSE -> Repairs & Maintenance, Direct Expenses,
                               Factory Maintenance
        ITEM_PURCHASE       -> Purchase Accounts, Raw material - Purchase,
                               Packing Material - Indigenous

    Job work is a direct expense and keeps the default. Buying fabric, thread
    or packing is a PURCHASE: booking it to a Direct Expenses head misstates it,
    which is exactly what the first ten MATERIAL bills did.
    """
    st, body = api.post("/financeAccountReferenceMapping/item/getOrCreate/"
                        "%s?referenceIds=%s" % (mapping, product_id))
    d = api.data(body)
    if isinstance(d, dict) and d.get(str(product_id)):
        return d[str(product_id)][0].get("financeAccountId")
    return None


def ensure_products(api, state, accounts, names=None, bill_type=None):
    """One CHARGE pseudo-item per Sage GL account, named from GLAMF.ACCTDESC.

    `names` lets the caller supply {account code: description} instead of
    reading Sage - the goods path takes it from staging.sage_gl_acct so it can
    run when the Sage box is unreachable.

    `bill_type` lets a caller that already knows the answer state it instead of
    deriving it per account. The goods path passes PURCHASE: its accounts are
    inventory heads (1L6T*), not 4E expense heads, so the account/group test
    would otherwise hold every one of them."""
    print("\n=== PRODUCTS ===", flush=True)
    if names is None:
        names = gl_names()
    groups = gl_groups()
    held = collections.Counter()
    remapped = collections.Counter()
    for acct in sorted(accounts):
        sku, name = "SAGE-" + acct, names.get(acct) or acct

        # The item ledger is minted under an accounting group, and which group
        # is decided HERE. An account whose group has no confirmed mapping is
        # HELD, never minted on a default - that default was the defect.
        # The account decides where it can; `bill_type` is the caller's
        # fallback for accounts the 4E test cannot speak to. The goods path
        # passes PURCHASE for its inventory heads (1L6T*), but the SAME call
        # now also carries the 4E expense heads of mixed documents' carriage
        # and handling - and those must follow their own group, not the
        # caller's default, or freight is booked as a purchase.
        bt = (SETTLED_ACCOUNTS.get(acct) or account_bill_type(acct, groups)
              or bill_type)
        if not bt:
            print("  HELD %s: no bill type for account (group %r)"
                  % (sku, groups.get(acct)))
            held["unmapped account"] += 1
            continue
        mapping = ITEM_MAPPING_BY_BILLTYPE.get(bt)
        if not mapping:
            print("  HELD %s: %s has no confirmed item-ledger mapping "
                  "(set INDIRECT_ITEM_MAPPING to release)" % (sku, bt))
            held[bt] += 1
            continue

        # ROUNDOFF_GL used to be skipped as "a bill field". It is a line now, so
        # it needs a product and a ledger like any other 4E account.
        rec = state.xw["products"].get(acct)
        if rec:
            if rec.get("itemMapping") == mapping:
                continue
            # The product exists but its ledger was minted under a different
            # head - every one of the 191 in the crosswalk predates this
            # routing and carries the ITEM_DIRECT_EXPENSE default. Without this
            # branch ensure_products would skip them and the bill would post
            # with the right billType onto a Direct Expenses ledger, which is
            # half a fix and reads as the defect still being present.
            #
            # getOrCreate MINTS A NEW LEDGER beside the old one rather than
            # remapping it, and sets accountingGroupName at creation (verified:
            # of 180 ITEM_IN_DIRECT_EXPENSE ledgers never modified since
            # creation, 179 carry a group). So this is additive and idempotent:
            # the stale ledger keeps its balance until its bills are revoked.
            led = item_ledger_for(api, rec["productId"], mapping)
            if not led:
                print("  FAIL remap %s -> %s" % (sku, mapping)); continue
            was = rec.get("ledger")
            rec.update({"ledger": str(led), "billType": bt,
                        "itemMapping": mapping, "priorLedger": was})
            state.save()
            remapped[mapping] += 1
            print("  remapped %s  %s  ledger %s -> %s" % (sku, mapping, was, led))
            continue

        # ADOPT BEFORE CREATE: a bare create on an existing SKU returns
        # "Sku Code already exists", and a -R2 retry would mint a duplicate
        # product with its own item ledger, fragmenting the chart of accounts.
        try:
            existing = find_product_by_sku(api, sku)
        except LookupFailed as exc:
            # Throttled, not absent. Leave the account for the next run rather
            # than deciding anything about it on a request that never landed.
            print("  DEFER %s: %s" % (sku, exc))
            held["lookup throttled"] += 1
            continue
        if existing:
            led = item_ledger_for(api, existing["productId"], mapping)
            if not led:
                print("  FAIL adopt %s: no item ledger" % sku); continue
            state.xw["products"][acct] = {
                "productId": str(existing["productId"]), "skuCode": sku,
                "ledger": str(led), "name": existing.get("productName") or name,
                # Recorded so a later run can tell which head a product was
                # minted under without re-reading the chart of accounts.
                "billType": bt, "itemMapping": mapping,
                "adopted": True}
            state.save()
            print("  adopted %s -> %s" % (sku, existing["productId"]))
            continue

        payload = {
            "productName": name, "skuCode": sku,
            # Must match what the bill line sends or create fails with
            # "Primary Unit for product does not match".
            "unit": "OTH", "unitOfMeasurement": "OTH",
            # CHARGE needs no catalog category - the platform's own seeded
            # pseudo-items (CH0001 Freight Charge) are exactly this shape.
            "typeOfStock": "CHARGE",
            # Sage AP-direct bills carry no HSN. A DEFAULT awaiting finance
            # sign-off, flagged as such in metaData - never presented as known.
            "hsnCode": EXPENSE_SAC,
            "isManageInventory": False,
            # The field is itemStatus, NOT status: "status" is not a property of
            # ProductCreateUpdateDto, so Jackson drops it and the row is stored
            # with a null status, invisible to every later lookup.
            "itemStatus": "ACTIVE",
            "isBulkUpload": True,
            "metaData": {"sageAccount": acct, "migrationSource": "IDEDAT",
                         "hsnIsDefault": "true"},
        }
        st, body = api.post("/product/", payload)
        if not api.ok(st, body):
            if "Sku Code already exists" in api.err(body):
                # The SKU is taken by a row that adoption cannot see - a
                # soft-deleted product, or one that failed hsnCode validation
                # after being inserted. Retrying as SAGE-<acct>-R2 is what
                # minted the duplicate SAGE-4E2ME07-R2 and split its expense
                # ledger in two, so DO NOT. Record the account as unusable;
                # eligible() then skips every bill that needs it, with a reason.
                print("  BURNED %s - SKU taken by a row adoption cannot see; "
                      "no -R2 variant is created" % sku)
                state.xw.setdefault("burned", {})[acct] = sku
                state.save()
                continue
            print("  FAIL %s: %s" % (sku, api.err(body))); continue
        if api.dry_run:
            continue
        pid = str(api.data(body).get("productId"))
        api.call("PATCH", "/product/%s/ACTIVE" % pid)
        # THE STEP THAT MATTERS: without it the bill line lands with a NULL
        # financeAccountId while still reporting success, and verify fails with
        # "No Finance Account Exists for product". Key on productId, not SKU.
        led = item_ledger_for(api, pid, mapping)
        if not led:
            print("  FAIL ledger for %s" % sku); continue
        state.xw["products"][acct] = {"productId": pid, "skuCode": sku,
                                      "ledger": str(led), "name": name,
                                      "billType": bt, "itemMapping": mapping}
        state.save()
        print("  created %s  %s  product=%s ledger=%s  %s"
              % (sku, name, pid, led, mapping))
    print("  products in crosswalk: %d" % len(state.xw["products"]))
    if remapped:
        print("  re-headed %d existing product ledger(s): %s"
              % (sum(remapped.values()), dict(remapped)))
    if held:
        print("  HELD %d account(s), nothing minted on a default:" % sum(held.values()))
        for reason, n in held.most_common():
            print("     %-24s %d" % (reason, n))


def party_ledger_for(api, contact_id):
    st, body = api.get("/financeAccount/minDetails/VENDOR/%s" % contact_id)
    d = api.data(body)
    if isinstance(d, list) and d:
        return d[0].get("financeAccountId"), d[0].get("leaf")
    return None, None


def pincode_state(api, pincode):
    """The platform derives the address state FROM THE PINCODE and overwrites
    whatever we send. Ask it first, so a disagreement is caught before it
    silently swaps IGST for CGST+SGST."""
    st, body = api.get("/address/pincode/%s" % pincode)
    d = api.data(body)
    if isinstance(d, dict):
        return s(d.get("state")).upper().replace(" ", "_") or None
    if isinstance(d, list) and d:
        return s(d[0].get("state")).upper().replace(" ", "_") or None
    return None


def load_cin_map():
    """-> {GSTIN: {corporateIdentificationNumber / limitedLiability...: value}}

    Built by work/build_cin_map.py from contacts other organisations on this
    devbox already hold for the same legal entity (matched on the PAN inside
    the GSTIN). The platform will not create a contact whose GSTIN 6th
    character is C or F without one, and its own registry lookup is dead here -
    GET /contact/gst/{gstin} answers "GST Info not available. Please add
    first." Absent file, or absent GSTIN, means the vendor is held exactly as
    before; nothing is ever invented to fill the gap.
    """
    path = os.path.join(HERE, "ref", "cin_by_gstin.json")
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh).get("identifiers", {})


def ensure_contacts(api, state, vendor_codes, rows=None):
    """Create a properly registered contact per Sage vendor, with its address,
    taxation row and party ledger. Withhold rather than guess.

    `rows` lets the caller supply the vendor master keyed by code, in the shape
    SQL_VENDORS returns; the goods path feeds it from staging.sage_vendor."""
    print("\n=== CONTACTS ===", flush=True)
    if rows is None:
        rows = vendors_master()
    cin_map = load_cin_map()
    held = []
    for code in sorted(vendor_codes):
        if state.xw["contacts"].get(code):
            continue
        v = rows.get(code)
        if not v:
            held.append((code, "no APVEN row")); continue
        name = s(v["name"]) or code
        reg_type, reg_no = registration_of(v)
        st_name, how = resolve_state(v)
        pin = normalise_pincode(v.get("pincode"))
        city = s(v.get("city")) or ORG_CITY

        # An unresolvable state moves a bill between IGST and CGST+SGST, so it
        # is HELD, never guessed.
        if st_name in ("UNKNOWN", "OTHER_COUNTRY"):
            held.append((code, "state %s (%s) - drives IGST vs CGST+SGST, never guessed"
                         % (st_name, how))); continue
        # address.pin_code is NOT NULL, so a vendor Sage never gave a pincode
        # needs one from somewhere. It is NOT load-bearing on this build: GET
        # /address/pincode/{pin} 404s, so the platform derives nothing from it,
        # and the state we send is stored verbatim (the org holds an address
        # filed OTHER_COUNTRY under a Chennai pincode). So fall back to the
        # head post office of the state the GSTIN has already proven - which
        # keeps the vendor in the right state, unlike the org's own 560059 -
        # and flag it, rather than hold 64 vendors and 671 bills over a field
        # that decides nothing here.
        pin_placeholder = False
        if not pin:
            pin = STATE_HEAD_PINCODE.get(st_name)
            pin_placeholder = True
            if not pin:
                held.append((code, "no usable pincode (%r) and no head pincode "
                                   "for state %s" % (s(v.get("pincode")), st_name)))
                continue
        # The platform demands a CIN (6th char C) or LLPIN (F) and its own
        # registry lookup is dead on this box. Supply the one another org here
        # already holds for the same legal entity; hold the vendor when there
        # is none. Inventing one would write a fabricated corporate identifier.
        cin_rec = cin_map.get(reg_no or "") or {}
        if needs_cin_lookup(reg_no) and not cin_rec:
            held.append((code, "GSTIN 6th char %s needs a CIN/LLPIN that exists "
                               "neither in Sage nor anywhere on the devbox"
                         % reg_no[5])); continue
        platform_state = pincode_state(api, pin)
        if platform_state and platform_state != st_name:
            held.append((code, "pincode %s resolves to %s but the GSTIN says %s - "
                               "posting would swap IGST for CGST+SGST"
                         % (pin, platform_state, st_name))); continue

        mobiles = [re.sub(r"\D", "", s(v.get("phone1")))[-10:]] if \
            len(re.sub(r"\D", "", s(v.get("phone1")))) >= 10 else []
        emails = [s(v["email1"])] if "@" in s(v.get("email1")) else []
        poc = s(v.get("contact_person")) or name

        payload = {
            "accountName": name, "companyName": name,
            "registrationType": reg_type,
            "contactInfoDtoList": [{
                "contactType": "VENDOR", "status": "ACTIVE",
                # A blank mobile list is rejected. Where Sage holds no number
                # the placeholder is used and FLAGGED - never invented.
                "mobileNumbers": mobiles or [PLACEHOLDER_MOBILE],
                "emailAddresses": emails, "pocName": poc,
                "contactCategory": {"categoryId": CONTACT_CATEGORY}}],
            # Omitting this entirely NPEs.
            "contactBusinessInfo": {"isMSME": False},
            # Validated but not persisted: required or you get "Address must be
            # present.", but the real address is created separately below.
            "addressDtoList": [{
                "addressTypes": ["SHIPPING_ADDRESS", "BILLING_ADDRESS"],
                "primaryAddress": True, "status": "ACTIVE", "partyType": "CONTACT",
                "addressLine1": s(v.get("street1")) or name, "city": city,
                "state": st_name, "pinCode": pin, "country": "INDIA"}],
            "metaData": {"migrationSource": "IDEDAT", "sageVendor": code,
                         "stateSource": how,
                         "mobileIsPlaceholder": str(not mobiles).lower(),
                         "pinCodeIsPlaceholder": str(pin_placeholder).lower()},
        }
        # Copied from another org's contact for the same legal entity, never
        # generated. Recorded in metaData so any row carrying one can be found
        # and re-sourced later.
        if cin_rec:
            for fld in ("corporateIdentificationNumber",
                        "limitedLiabilityPartnershipIdentificationNumber"):
                if cin_rec.get(fld):
                    payload["contactBusinessInfo"][fld] = cin_rec[fld]
                    payload["metaData"]["cinSource"] = "devbox:%s" % \
                        cin_rec.get("sourceOrganisationId", "?")
        # A PAN registration needs its number sent too. Only the GST branch set
        # it, so every PAN vendor was posted with registrationType PAN and no
        # number and came straight back as "Pan Can not be null" - 42 vendors,
        # 144 bills, all of them holding a PAN that registration_of() had
        # already recovered from LEGALNAME or BRN.
        if reg_type in ("GST", "PAN"):
            payload["registrationNumber"] = reg_no
            # Entity type lives at GSTIN[5], which IS PAN[3] - a GSTIN embeds
            # the PAN at characters 3-12. Index by which number we are holding,
            # never blindly at [5]: on a bare PAN that position is a digit and
            # every PAN vendor would silently profile as OTHERS.
            ent = reg_no[5] if reg_type == "GST" else reg_no[3]
            payload["contactBusinessInfo"]["profileType"] = \
                GSTIN_ENTITY_TO_PROFILE.get(ent, "OTHERS")

        st_code, body = api.post("/contact/", payload)
        # Several Sage vendor codes legitimately share one GSTIN. Expected, not
        # a failure: map this code onto the contact that already exists.
        if not api.ok(st_code, body) and "Already exists with registration Number" in api.err(body):
            _, lb = api.get("/contact/search?contactType=VENDOR&query=%s&pageSize=5" % reg_no)
            d = api.data(lb) or {}
            content = d.get("content") if isinstance(d, dict) else None
            if content:
                found = content[0]
                led, leaf = party_ledger_for(api, found["contactId"])
                addr = billing_address_for(api, found["contactId"])
                # billing_address_for() cannot succeed on this build: it reads
                # GET /contact/address/{id}/CONTACT, and that route does not
                # exist - the OpenAPI spec offers only POST for contact
                # addresses, and every read variant 404s. So the reuse branch
                # always fell through to the hold, taking 26 vendors and 744
                # bills with it (SELD339 alone is 392) even though the address
                # was sitting in the database all along.
                #
                # The sibling that created the contact is already in this very
                # crosswalk with the addressId its own create call returned.
                # One GSTIN is one contact, so that address IS this vendor's
                # address - no API and no second row needed.
                if not addr:
                    addr = next((c["addressId"] for c in state.xw["contacts"].values()
                                 if c.get("contactId") == str(found["contactId"])
                                 and c.get("addressId")), None)
                if led and addr:
                    state.xw["contacts"][code] = {
                        "contactId": str(found["contactId"]), "name": name,
                        "ledger": str(led), "addressId": str(addr),
                        "state": st_name, "registrationType": reg_type,
                        "gstin": reg_no or "", "reused": True}
                    state.save()
                    print("  %s reuses existing contact for GSTIN %s" % (code, reg_no))
                    continue
            held.append((code, "shares GSTIN %s with an existing contact that has "
                               "no usable ledger/address" % reg_no)); continue
        if not api.ok(st_code, body):
            held.append((code, "create failed: %s" % api.err(body))); continue
        if api.dry_run:
            continue
        cid = str(api.data(body).get("contactId"))

        # The real address. The field is addressPartyType, NOT partyType.
        st2, b2 = api.post("/contact/address/create", {
            "partyId": cid, "addressPartyType": "CONTACT",
            "addressTypes": ["BILLING_ADDRESS", "SHIPPING_ADDRESS"],
            "primaryAddress": True, "status": "ACTIVE",
            "addressLine1": s(v.get("street1")) or name, "city": city,
            "state": st_name, "pinCode": pin, "country": "INDIA",
            "organisationId": ORG_ID})
        addr_id = str((api.data(b2) or {}).get("addressId")) if api.ok(st2, b2) else None
        if not addr_id:
            held.append((code, "billing address failed: %s" % api.err(b2))); continue

        # Nothing else creates this row and its absence surfaces much later,
        # from the BILL module. Every Sage vendor is SUBJTOWTHH=0, so NONE.
        api.post("/taxation", {"organisationId": ORG_ID, "entityId": cid,
                               "taxationPartyType": "VENDOR", "taxType": "NONE",
                               "isDefault": True})
        led, leaf = party_ledger_for(api, cid)
        if not led:
            held.append((code, "party ledger was not created")); continue
        state.xw["contacts"][code] = {
            "contactId": cid, "name": name, "ledger": str(led), "ledgerIsLeaf": leaf,
            "addressId": addr_id, "state": st_name, "stateSource": how,
            "city": city, "pinCode": pin, "registrationType": reg_type,
            "gstin": reg_no or ""}
        state.save()
        print("  created %-9s %-38s %-14s %s ledger=%s"
              % (code, name[:38], st_name, reg_type, led))
    if held:
        with open(os.path.join(WORK, "contacts_held.json"), "w") as fh:
            json.dump([{"vendor": c, "reason": r} for c, r in held], fh, indent=1)
        print("  HELD %d vendors (work items, not failures) -> work/contacts_held.json"
              % len(held))
    print("  contacts in crosswalk: %d" % len(state.xw["contacts"]))
    return held


def billing_address_for(api, contact_id):
    st, body = api.get("/contact/address/%s/CONTACT" % contact_id)
    d = api.data(body)
    rows = d if isinstance(d, list) else (d or {}).get("content") or []
    for a in rows:
        if "BILLING_ADDRESS" in (a.get("addressTypes") or []):
            return a.get("addressId")
    return rows[0].get("addressId") if rows else None


# ============================================================================
# THE PAYLOAD - ported from load_janapr_bills.ps1
# ============================================================================

COMPANY_ADDR = {
    "addressId": COMPANY_ADDRESS_ID, "partyId": ORG_ID, "partyType": "ORGANISATION",
    "addressLine1": "48/1/2/3, Wonderblues Appare",
    "addressLine2": "Mylsandra Village, Bengaluru",
    "city": "Bengaluru", "state": "KARNATAKA", "pinCode": "560059",
    "country": "INDIA", "addressTypes": ["BILLING_ADDRESS"],
    "primaryAddress": True, "status": "ACTIVE", "organisationId": ORG_ID,
}


def series_number(api, sage_date):
    fy = financial_year(sage_date)
    name = SERIES_BY_FY.get(fy)
    if not name:
        raise Stop("no series configured for FY %s - one name serves ONE year" % fy)
    st, body = api.get("/counter/series/values?series=%s&counterType=BILL"
                       "&associatedEntityType=PAN&associatedEntityId=%s"
                       "&associatedFinancialYear=%s" % (name, ORG_PAN, fy))
    d = api.data(body) if api.ok(st, body) else None
    value = str(int(d["value"]) + 1) if d and d.get("value") is not None else "1"
    return {"series": name, "value": value, "suffix": None,
            "associatedEntityType": "PAN", "associatedEntityId": ORG_PAN,
            "associatedFinancialYear": fy}


def build_payload(api, key, shape, contact, products, items=None):
    vendor, invoice = key
    h = shape["header"]
    dt = s(h["bill_date"])
    due = s(h["due_date"]) if sage_date_parts(h["due_date"]) else dt

    # One entry per bill line: (product, gl, line ref, rate, taxable, qty,
    # unit price, text).
    #
    # Without PO detail a distribution line IS the bill line, quantity 1 at the
    # whole amount - Sage states no quantity and no rate for it (see
    # load_po_items). With PO detail, reconciled to the paise in po_detail(),
    # the line is the item Sage actually ordered, at its real quantity and unit
    # cost. The GL, and so the ledger, is unchanged either way: po_detail()
    # admits only single-GL documents, so every item inherits that one account.
    spec = []
    if shape.get("items"):
        gl = s(shape["exp"][0]["gl"])
        pr = products[gl]
        rate = shape["rates"][shape["exp"][0]["CNTLINE"]]
        for it in shape["items"]:
            amt = q2(D(str(it["ext"] or 0)))
            # A goods line states its own rate and its own GL; a PO item has
            # neither and inherits the document's.
            i_gl = s(it.get("gl")) or gl
            led_pr = products[i_gl] if i_gl in products else pr
            # A goods line points at the item's own product so that its unit
            # matches the product's primary unit; the ledger still comes from
            # the GL pseudo-item, so the voucher lands on the inventory head.
            i_pr = (items or {}).get(item_key(it))
            # The item product carries its own ITEM_PURCHASE ledger. It used
            # to fall back to the GL pseudo-item's "if it somehow has none",
            # and that fallback is how real goods came to book into an A/P
            # Clearing head: on the goods path the GL pseudo-item IS a Sage
            # clearing account (1L6T*/1L7*, every one ACCTTYPE=B), so a
            # ledgerless item silently posted balance-sheet money into P&L.
            # Measured: 23 lines, Rs 87,043.35, across two clearing accounts.
            #
            # An item WITHOUT a ledger is now refused by eligible_goods before
            # it reaches here; a service charge line legitimately has no item
            # product and keeps the GL pseudo-item, which for those lines is a
            # 4E expense head and correct.
            if i_pr and not i_pr.get("ledger"):
                raise Stop("%s|%s: item %s has no ledger and the GL head %s is "
                           "a balance-sheet clearing account - refusing to book "
                           "it into P&L" % (vendor, invoice, item_key(it), i_gl))
            line_pr = i_pr if i_pr else led_pr
            spec.append((line_pr, i_gl,
                         "PO:%s" % s(it["item"]),
                         it.get("rate", rate), amt,
                         D(str(it["qty"])), D(str(it["unitcost"])),
                         s(it["descr"]) or s(it["item"]) or pr["name"], it))
    else:
        for l in shape["exp"]:
            gl = s(l["gl"])
            pr = products[gl]
            amt = q2(l["amount"])
            # Resolved in classify(): stated for forward charge, derived+snapped
            # for reverse charge. Never recomputed here from the amounts.
            spec.append((pr, gl, s(l["CNTLINE"]), shape["rates"][l["CNTLINE"]],
                         amt, D(1), amt, s(l["descr"]) or pr["name"], None))

    lines = []
    for pr, gl, lineref, rate, amt, qty, unit_price, text, it in spec:
        # The platform compares this against the product's primary unit, so it
        # is read off the product, never computed independently.
        unit = s(pr.get("unit")) or "OTH"
        lines.append({
            "productId": pr["productId"], "skuCode": pr["skuCode"],
            "productName": pr["name"],
            "hsn": (s(it["hsn"]) or None) if it and it.get("hsn") else None,
            "unit": unit, "displayUnit": unit,
            "quantity": float(qty), "displayQuantity": float(qty),
            "unitPrice": float(unit_price), "itemPrice": float(amt),
            "taxableAmount": float(amt),
            "totalPrice": float(q2(amt * (1 + rate / 100))),
            # 4.1 - the rate Sage STATES on the line, never tax/taxable.
            "gstPercentage": float(rate),
            # Omitting cessType is a bare NPE at BillServiceImpl:2062.
            "cessType": "IN_RUPEES", "cessAmount": 0, "cessPercentage": 0,
            "lineItemType": shape.get("line_type", "CHARGE"),
            "isGstClaimable": True,
            # 4.3 - explicit bool on EVERY line. isFalse(null) is false, so a
            # null flag does NOT skip the RCM path: null means reverse charge.
            "isRcmEnabled": bool(shape["is_rcm"]),
            "discount": 0, "itemDiscount": 0,
            "taxableOtherCharge": 0,
            # 4.2 - Sage's own narration: the PO's ITEMDESC where the item
            # detail was adopted, else TEXTDESC, which is the only record of
            # what was bought on an amount-only line (IDITEM is empty on every
            # AP-direct line in this window). Product name only when blank.
            "description": text,
            # 4.6 - POST /bill/ persists billLineItem.financeAccountId ONLY
            # from here (BillLineItemConvertor:289); verify resolves the ledger
            # separately and never writes it back, so omitting this leaves the
            # column NULL and degrades the Tally export.
            "financeAccountDto": {"financeAccountId": pr["ledger"]},
            "metaData": dict({"sageGlAccount": gl, "sageDoc": invoice,
                              "sageVendor": vendor, "sageLine": lineref},
                             **({"sageItem": s(it["item_raw"]) or s(it["item"]),
                                 "sageItemFmt": s(it["item"]),
                                 "sageUnit": s(it["stock_um"]) or s(it["um"])}
                                if it else {})),
        })

    # The zero-rated lines: Sage's own round-off, and the import IGST a courier
    # paid on our behalf. Both have to be LINES because the server recomputes
    # gstAmount and roundOffAmount from the lines and ignores what we send.
    # gstPercentage is 0, so neither moves gstAmount; both move billAmount,
    # which is the point.
    for z in shape.get("zero", ()):
        pr = products[z["gl"]]
        amt = q2(z["amount"])
        unit = s(pr.get("unit")) or "OTH"
        lines.append({
            "productId": pr["productId"], "skuCode": pr["skuCode"],
            "productName": pr["name"], "hsn": None,
            "unit": unit, "displayUnit": unit,
            "quantity": 1.0, "displayQuantity": 1.0,
            "unitPrice": float(amt), "itemPrice": float(amt),
            "taxableAmount": float(amt), "totalPrice": float(amt),
            "gstPercentage": 0.0,
            "cessType": "IN_RUPEES", "cessAmount": 0, "cessPercentage": 0,
            "lineItemType": "CHARGE", "isGstClaimable": True,
            "isRcmEnabled": bool(shape["is_rcm"]),
            "discount": 0, "itemDiscount": 0, "taxableOtherCharge": 0,
            "description": z["descr"] or pr["name"],
            # A 2A7T line overrides the ledger: its product exists only to give
            # the line a productId, and its item-expense head must stay unused.
            # The round-off keeps its own 4E ledger, which is correct - 4E1M016
            # is ACCTTYPE=I.
            "financeAccountDto": {"financeAccountId": z["ledger"] or pr["ledger"]},
            "metaData": {"sageGlAccount": z["gl"], "sageDoc": invoice,
                         "sageVendor": vendor, "sageLine": "zero-rated",
                         "passthrough": z["why"]},
        })

    contact_addr = {
        "addressId": contact["addressId"], "partyId": contact["contactId"],
        "partyType": "CONTACT", "addressLine1": contact["name"],
        "city": contact.get("city") or ORG_CITY, "state": contact["state"],
        "pinCode": contact.get("pinCode") or "560059", "country": "INDIA",
        "addressTypes": ["BILLING_ADDRESS"],
        # Address DTOs are rejected without this; the error misleadingly reads
        # "Address : null is not active".
        "status": "ACTIVE", "organisationId": ORG_ID,
    }

    # 4.6 - billType is Sage's account group, not a constant. It used to be the
    # literal "PURCHASE" on every document; see BILLTYPE_BY_GROUP.
    bill_type = shape["bill_type"]

    payload = {
        "organisationId": ORG_ID, "billType": bill_type,
        # 5 - Sage IDINVC verbatim, after the *N strip.
        "billNumber": invoice,
        "billSeriesNumber": series_number(api, dt),
        "billDate": epoch_ms(dt), "dueDate": epoch_ms(due), "voucherDate": epoch_ms(dt),
        "billStatus": "ACTIVE",
        "billAmount": float(shape["bill_amount"]),
        # Expense + every zero-rated line. The server will recompute gstAmount
        # from the lines regardless of what we send here.
        "taxableAmount": float(shape.get("taxable_all", shape["taxable"])),
        "gstAmount": float(shape["tax"]),
        "contactDto": {"contactId": contact["contactId"], "contactType": "VENDOR",
                       "accountName": contact["name"], "companyName": contact["name"]},
        "contactType": "VENDOR", "contactFinanceAccountId": contact["ledger"],
        "gstUsedForBill": ORG_GSTIN,
        "companyBillingAddressDto": COMPANY_ADDR,
        "contactBillingAddressDto": contact_addr,
        "purchaseType": "DOMESTIC",
        # MATERIAL for a raw-material purchase, SERVICE for AP-direct job work.
        # The platform also accepts ASSET; all three are in use in this org.
        # Set below, and only on a PURCHASE.
        "billProcurementType": None,
        "billEntityMappingDtos": [{"entityType": "ADHOC_PURCHASE_BILL",
                                   "billEntityParentType": "ADHOC_PURCHASE_BILL",
                                   "status": "ACTIVE", "mappedAmount": 0}],
        "lineItemDtoList": lines,
        "conversionRate": 1.0, "currencyDto": {"currency": "INR"},
        "autoMapRemainingVoucher": False,
        # 4.5 (revised) - never send a round-off. hasRoundOff makes the server
        # discard our figure and substitute its own nearest-rupee one, which is
        # what put JOBW258|108 a rupee below Sage. With it false the exact paise
        # survive, and Sage's 4E1M016 rides in as a 0% line instead.
        "roundOffAmount": 0.0,
        "hasRoundOff": False,
        "expenditureDtoList": [],
        "metadata": {"sageVendor": vendor, "sageDoc": invoice,
                     "sageTaxGroup": shape["tax_group"],
                     "sageRcm": str(bool(shape["is_rcm"])),
                     "sageBatch": str(h["CNTBTCH"]), "sageItem": str(h["CNTITEM"]),
                     "sageBillType": bill_type,
                     "sageTypeNote": shape.get("type_note") or "",
                     "migrationSource": "IDEDAT"},
    }

    # billProcurementType is set on PURCHASE bills only: it is NULL on 100% of
    # the 24,299 DIRECT_EXPENSE and 34,342 IN_DIRECT_EXPENSE bills already in
    # production. Sending "SERVICE" on an expense bill invents a shape the
    # platform never uses.
    if bill_type == "PURCHASE":
        payload["billProcurementType"] = shape.get("procurement", "SERVICE")
    else:
        payload.pop("billProcurementType", None)
    return payload


def assert_invariants(payload, shape):
    """4.4 - assert per line and per bill, to the paise, and fail loudly."""
    problems = []
    total = D(0)
    for li in payload["lineItemDtoList"]:
        tx, ip = q2(li["taxableAmount"]), q2(li["itemPrice"])
        # NOT q2: billLineItem.unitPrice stores 6dp and a real unit cost often
        # needs them (33.18 over 64 units is 0.5184375, and 0.52 x 64 is 33.28,
        # a rupee out). Round the PRODUCT, never the rate that forms it.
        up = D(str(li["unitPrice"]))
        qty = D(str(li["quantity"]))
        if q2(up * qty) != tx:
            problems.append("unitPrice*quantity %s != taxableAmount %s" % (q2(up * qty), tx))
        if ip != tx:
            problems.append("itemPrice %s != taxableAmount %s" % (ip, tx))
        want = q2(tx * (1 + D(str(li["gstPercentage"])) / 100))
        if q2(li["totalPrice"]) != want:
            problems.append("totalPrice %s != taxable*(1+rate) %s" % (q2(li["totalPrice"]), want))
        total += tx
    want_taxable = q2(shape.get("taxable_all", shape["taxable"]))
    if q2(total) != want_taxable:
        problems.append("sum(line taxable) %s != bill taxable %s"
                        % (q2(total), want_taxable))
    # No roundOff term: it is a line now, already inside want_taxable.
    want_bill = q2(want_taxable + shape["tax"])
    if q2(payload["billAmount"]) != want_bill:
        problems.append("billAmount %s != taxable+tax %s"
                        % (q2(payload["billAmount"]), want_bill))
    # And the whole point of the exercise: it has to be Sage's own figure.
    if q2(payload["billAmount"]) != q2(shape["bill_amount"]):
        problems.append("billAmount %s != Sage AMTINVCHC %s"
                        % (q2(payload["billAmount"]), q2(shape["bill_amount"])))
    return problems


# ============================================================================
# SELECTION - which documents this run will attempt
# ============================================================================

def eligible(book, state, contacts_known):
    """-> (list of (key, shape), Counter of grouped skip reasons)"""
    out, skips = [], collections.Counter()
    for key, bill in book.items():
        shape, why = classify(bill)
        if why:
            skips[why if len(why) < 46 else why[:46] + " ..."] += 1
            continue
        vendor = key[0]
        c = contacts_known.get(vendor)
        if not c or not c.get("contactId"):
            skips["no contact built for vendor"] += 1
            continue
        if not c.get("addressId"):
            skips["contact has no billing address"] += 1
            continue
        # An unregistered person cannot legally charge GST, so a URP contact
        # whose bill carries forward-charge tax means the GSTIN is MISSING FROM
        # SAGE - posting it would claim input credit against a URP ledger. RCM
        # is different: we self-assess that tax, so a URP RCM bill is fine.
        if c.get("registrationType") == "WITHOUT_PAN_OR_GST" and \
                not shape["is_rcm"] and shape["tax"] > 0:
            skips["URP vendor with forward-charge GST - GSTIN missing in Sage"] += 1
            continue
        missing = sorted({s(l["gl"]) for l in shape["exp"]
                          if not state.xw["products"].get(s(l["gl"]))})
        if missing:
            skips["no product for GL account"] += 1
            continue
        out.append((key, shape))
    return out, skips


def pilot_pick(cands, want=10):
    """~10 bills chosen to COVER THE SHAPES, not the first 10: intra-state
    forward charge, inter-state, RCM, round-off, multi-line, and one each at
    5 / 12 / 18 percent."""
    def rates(shape):
        return set(shape["rates"].values())
    tests = [
        ("intra-state forward charge (CGST+SGST)",
         lambda k, sh: not sh["is_rcm"] and sh["tax_group"] == "LOCAL" and sh["tax"] > 0),
        ("inter-state forward charge (IGST)",
         lambda k, sh: not sh["is_rcm"] and sh["tax_group"] == "INTERSTATE" and sh["tax"] > 0),
        ("reverse charge (RCM)",       lambda k, sh: sh["is_rcm"]),
        ("carries a round-off",        lambda k, sh: sh["roundoff"] != 0),
        ("multi-line",                 lambda k, sh: len(sh["exp"]) > 1),
        ("rate 5%",                    lambda k, sh: D(5) in rates(sh)),
        ("rate 12%",                   lambda k, sh: D(12) in rates(sh)),
        ("rate 18%",                   lambda k, sh: D(18) in rates(sh)),
        ("mixed rates on one bill",    lambda k, sh: len(rates(sh)) > 1),
        ("zero-rated",                 lambda k, sh: sh["tax"] == 0),
    ]
    chosen, seen = [], set()
    for label, test in tests:
        for k, sh in cands:
            if k in seen:
                continue
            if test(k, sh):
                chosen.append((k, sh, label)); seen.add(k); break
    return chosen[:want]


# ============================================================================
# PHASES
# ============================================================================

def phase_cleanup(api, state, args):
    """Leave the org as the previous session should have: no smoke-test
    artefacts, one counter per series+FY."""
    print("\n=== CLEANUP ===", flush=True)
    rows = mysql("SELECT id, billNumber, billStatus FROM bill "
                 "WHERE organisationId=%s AND isDeleted+0=0" % ORG_ID,
                 ["id", "billNumber", "billStatus"])
    for r in rows:
        bid, num, status = r["id"], r["billNumber"], r["billStatus"]
        if not num.startswith("SMOKE"):
            print("  leaving bill %s %s (%s) - not a smoke-test artefact"
                  % (bid, num, status))
            continue
        if status != "REVOKED":
            st, b = api.call("PUT", "/bill/updateStatus/%s/REVOKED?remarks=migration+cleanup" % bid)
            print("  revoke %s: %s" % (num, "ok" if api.ok(st, b) else api.err(b)))
        st, b = api.call("DELETE", "/bill/%s" % bid)
        print("  delete bill %s %s: %s" % (bid, num, "ok" if api.ok(st, b) else api.err(b)))

    for c in mysql("SELECT id, accountName FROM contact "
                   "WHERE organisationId=%s AND isDeleted+0=0" % ORG_ID,
                   ["id", "accountName"]):
        if state.xw["contacts"] and any(
                v.get("contactId") == c["id"] for v in state.xw["contacts"].values()):
            continue
        st, b = api.call("DELETE", "/contact/%s" % c["id"])
        print("  delete contact %s %s: %s"
              % (c["id"], c["accountName"], "ok" if api.ok(st, b) else api.err(b)))

    # Duplicate counters: the smoke test created a second SAGE/SAGE27 row for a
    # series+FY that already had one, and GET /counter/series/values resolves
    # to the duplicate. Two counters for one key make the series value
    # ambiguous, so report them - they are not deletable through the API.
    ctr = mysql("SELECT id, series, value, associatedFinancialYear FROM counter "
                "WHERE organisationId=%s AND counterType='BILL' ORDER BY series, value+0" % ORG_ID,
                ["id", "series", "value", "fy"])
    bykey = collections.defaultdict(list)
    for c in ctr:
        bykey[(c["series"], c["fy"])].append(c)
    for k, v in sorted(bykey.items()):
        if len(v) > 1:
            print("  WARN %s %s has %d counter rows: %s - the API resolves one of "
                  "them; series values will continue from it"
                  % (k[0], k[1], len(v), ", ".join("%s=%s" % (c["id"], c["value"]) for c in v)))
    print("  cleanup done", flush=True)


def mysql(sql, cols=None):
    """Read-only helper - the definition of done is verified in MySQL, not from
    this script's own output. With `cols`, rows come back as dicts."""
    import subprocess
    # One shared ssh connection instead of a handshake per query: measured
    # 1.09s per call cold, ~0.43s multiplexed. readback_drift runs one query
    # per bill, so over 11,232 bills that is the difference between +3.4h and
    # +1.3h. ControlPath must stay short - it is a unix socket, capped near
    # 104 chars, and a long path fails with rc=255 and no error.
    p = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "-o", "ControlMaster=auto",
                        "-o", "ControlPath=/tmp/.sage-cm-%r",
                        "-o", "ControlPersist=600",
                        db_host(), "mysql smeassist -N --raw -e %s"
                        % json.dumps(sql)],
                       capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise Stop("mysql failed: %s" % p.stderr[:400])
    rows = [ln.split("\t") for ln in p.stdout.splitlines() if ln.strip()]
    return [dict(zip(cols, r)) for r in rows] if cols else rows


def readback_drift(api, bill_id, shape):
    """Ask the server what it stored and compare it with Sage. -> [problems]

    The server ignores the gstAmount and roundOffAmount in the payload and
    recomputes both from the lines, so a payload can be internally perfect and
    still land off Sage. Nothing checked that until now.
    """
    # This used GET /bill/{id}, which answers 200 with success:true and
    # data:[] - an empty list, not a row - for EVERY bill. isinstance(d, dict)
    # was therefore always False, this returned "unexpected read-back shape"
    # every time, and nothing was ever compared. Api.ok passes it (200 +
    # success:true) so nothing caught it either.
    #
    # The right route is /bill/detail/{billId}, documented in
    # ref/SAGE-TO-SMEASSIST-HANDOVER.txt:2027. It truncates lineItemDtoList to
    # one line - irrelevant here, the totals are what this compares - and for
    # lines there is /bill/lineItems/{billId}. Verified to return the same
    # taxable/gst/bill as the row in MySQL.
    st, body = api.get("/bill/detail/%s" % bill_id)
    if not api.ok(st, body):
        return ["could not read the bill back: %s" % api.err(body)]
    d = api.data(body)
    if not isinstance(d, dict):
        return ["unexpected read-back shape from /bill/detail: %r" % (d,)]
    got_txbl = q2(D(str(d.get("taxableAmount") or 0)))
    got_tax  = q2(D(str(d.get("gstAmount") or 0)))
    got_bill = q2(D(str(d.get("billAmount") or 0)))
    got_ro   = q2(D(str(d.get("roundOffAmount") or 0)))
    want_txbl = q2(shape.get("taxable_all", shape["taxable"]))
    out = []

    # Taxable is a plain sum of stated amounts - no rounding anywhere, so it is
    # asserted exactly. The server stores exactly SUM(line taxableAmount) and
    # does not round it: of 39 posted bills, 9 store pence and every one of
    # those matches its lines to the paisa.
    #
    # It looks like rounding on the bills posted BEFORE the round-off fix,
    # because taxable_all counts Sage's 4E1M016 round-off while those bills
    # never sent it as a line: JOBW258|105 stores 338441.00 against a
    # taxable_all of 338440.94, the 4E1M016 -0.06 being the whole difference.
    # Now that the round-off arrives as a line the two agree exactly, so this
    # stays strict - and it correctly reports those legacy bills.
    if got_txbl != want_txbl:
        out.append("stored taxable %s != %s" % (got_txbl, want_txbl))

    # Tax is NOT exact, and cannot be. The server derives gstAmount as
    # SUM(line taxable x line rate) while Sage truncates each authority
    # separately (fact 4), so the two disagree by up to a paisa per authority
    # per line. Measured over the Jan-Apr window: 503 forward documents differ,
    # every one of them by exactly 0.01, Rs 5.03 in total. The same tolerance
    # classify() already uses for the stated-tax gate is applied here, so a
    # genuine drift still reports and the truncation does not.
    tol = max(D("0.05"), D("0.01") * len(shape["exp"]) * 2)
    if not shape["is_rcm"]:
        # AMTTAXHC is authoritative only for forward charge. On reverse charge
        # the slab figure is expected to differ from Sage's rupee-rounded 1L8TX
        # and is reported per document as an RCM TAX VARIANCE instead.
        if abs(got_tax - shape["tax"]) > tol:
            out.append("stored gstAmount %s != Sage AMTTAXHC %s (tolerance %s)"
                       % (got_tax, shape["tax"], tol))
        if abs(got_bill - q2(shape["bill_amount"])) > tol:
            out.append("stored billAmount %s != Sage AMTINVCHC %s (tolerance %s)"
                       % (got_bill, q2(shape["bill_amount"]), tol))
    else:
        # The one thing that must still hold on RCM: the total is the taxable
        # plus whatever tax the server derived, and the taxable is Sage's.
        # roundOffAmount belongs in that identity - the server's own rule is
        # billAmount = taxableAmount + gstAmount + roundOffAmount (see the
        # ZERO-RATED LINES note in classify), and it substitutes its own
        # figure. Asserting the identity without it contradicts the server
        # whenever it sets one: on the 39 bills posted so far, 10 carry a
        # non-zero roundOffAmount and all 10 break txbl+gst==bill by up to
        # 0.40, while all 39 satisfy it once roundOff is included.
        if abs(got_bill - q2(got_txbl + got_tax + got_ro)) > D("0.01"):
            out.append("stored billAmount %s != taxable %s + gst %s + roundOff %s"
                       % (got_bill, got_txbl, got_tax, got_ro))
    return out


def phase_run(api, state, args, do_post):
    book = load_book()
    cands, skips = eligible(book, state, state.xw["contacts"])
    print("\n=== %s ===" % ("POST" if do_post else "DRY RUN"), flush=True)
    print("  rate source: staging sage_ap_dist ratetax1+ratetax2, rounded to 2dp\n"
          "               (forward charge = stated; reverse charge states no rate\n"
               "               and is derived from the 1L8TX pair, then snapped)")
    print("  %d bills shaped, %d skipped" % (len(cands), sum(skips.values())), flush=True)
    print("\n--- skip reasons, grouped and counted ---")
    for why, n in skips.most_common():
        print("   %-58s %d" % (why, n))
    with open(os.path.join(WORK, "skipped.json"), "w") as fh:
        json.dump(skips.most_common(), fh, indent=1)

    if args.pilot:
        picks = pilot_pick(cands, args.limit or 10)
        print("\n--- pilot: %d bills chosen to cover the shapes ---" % len(picks))
        work = [(k, sh) for k, sh, _ in picks]
        labels = {k: lb for k, _, lb in picks}
    else:
        work = cands[:args.limit] if args.limit else cands
        labels = {}

    results, failures, rcm_variance = [], [], []
    for key, shape in work:
        vendor, invoice = key
        tag = "%s|%s" % (vendor, invoice)
        prior = state.posted.get(tag)
        if prior and prior.rstrip().endswith("UNVERIFIED"):
            # Created but never verified, so the server never wrote the voucher
            # and the bill shows a payable with ZERO accounting impact. A
            # re-run used to skip any tag present in posted.log at all, which
            # meant these could never be repaired - they simply stayed broken.
            # Verify the bill that already exists instead of skipping it.
            bid = prior.split("||")[1]
            vst, vbody = api.post("/bill/%s/verify" % bid)
            if api.ok(vst, vbody):
                print("  %-38s RE-VERIFIED %s" % (tag, bid))
                state.mark(tag, bid)
            else:
                print("  %-38s re-verify failed: %s" % (tag, api.err(vbody)))
                failures.append((tag, "re-verify: %s" % api.err(vbody)))
            continue
        if prior:
            continue
        contact = state.xw["contacts"][vendor]
        payload = build_payload(api, key, shape, contact, state.xw["products"])
        bad = assert_invariants(payload, shape)
        if bad:
            print("  INVARIANT FAIL %s: %s" % (tag, "; ".join(bad)))
            failures.append((tag, "; ".join(bad)))
            continue
        rates = sorted({li["gstPercentage"] for li in payload["lineItemDtoList"]})
        print("\n  %-38s %-11s %s%s" % (tag, shape["tax_group"],
              "RCM " if shape["is_rcm"] else "", labels.get(key, "")))
        print("     billType=%s%s" % (payload["billType"],
              "  [%s]" % shape["type_note"] if shape.get("type_note") else ""))
        print("     taxable=%s tax=%s roundOff=%s bill=%s lines=%d rates=%s"
              % (shape["taxable"], shape["tax"], shape["roundoff"],
                 shape["bill_amount"], len(payload["lineItemDtoList"]), rates))
        print("     rate source: %s" % shape["rate_source"])
        print("     item source: %s" % shape.get("item_source", "n/a"))
        for z in shape.get("zero", ()):
            print("     zero-rated line: %s %s - %s" % (z["gl"], z["amount"], z["why"]))
        if shape["is_rcm"]:
            # The server derives gstAmount from the slab, so it cannot reproduce
            # a Sage RCM tax that was rounded to the rupee - and Sage rounds it
            # on 1081 of the window's 1084 RCM documents. No legal slab satisfies
            # both numbers, so this is logged per document, not "fixed".
            posted_tax = q2(sum(D(str(li["totalPrice"])) - D(str(li["taxableAmount"]))
                                for li in payload["lineItemDtoList"]))
            if posted_tax != shape["tax"]:
                print("     RCM TAX VARIANCE: Sage 1L8TX %s, posting %s (delta %s)"
                      " - Sage rounded to the rupee; the slab figure is exact"
                      % (shape["tax"], posted_tax, q2(posted_tax - shape["tax"])))
                rcm_variance.append({"doc": tag, "sage": str(shape["tax"]),
                                     "posted": str(posted_tax),
                                     "delta": str(q2(posted_tax - shape["tax"]))})
        if not do_post:
            results.append({"doc": tag, "stage": "dryrun", "ok": True})
            continue

        st, body = api.post("/bill/", payload)
        if not api.ok(st, body):
            # Scoped to org + contact + FY. Not a failure: a previous run
            # posted it and the log was lost.
            if "Bill number already exists" in api.err(body):
                print("     already posted - recording and moving on")
                state.mark(tag, "preexisting")
                continue
            print("     CREATE FAIL: %s" % api.err(body))
            failures.append((tag, "create: %s" % api.err(body)))
            continue
        bid = str(api.data(body).get("billId"))
        vst, vbody = api.post("/bill/%s/verify" % bid)
        if not api.ok(vst, vbody):
            print("     created %s  VERIFY FAIL: %s" % (bid, api.err(vbody)))
            state.mark(tag, "%s||UNVERIFIED" % bid)
            failures.append((tag, "verify: %s" % api.err(vbody)))
            continue
        print("     created %s  VERIFIED" % bid)
        # READ BACK. assert_invariants only ever compared the payload against
        # itself, so it could not see the server overriding gstAmount and
        # roundOffAmount - which is how JOBW258|108 came to sit a rupee below
        # Sage with every check green. Ask the server what it actually stored.
        drift = readback_drift(api, bid, shape)
        if drift:
            print("     POST-CHECK: %s" % "; ".join(drift))
            failures.append((tag, "readback: %s" % "; ".join(drift)))
        state.mark(tag, bid)
        results.append({"doc": tag, "billId": bid, "ok": not drift,
                        "drift": drift or None})

    print("\n  attempted=%d ok=%d failed=%d"
          % (len(results) + len(failures), len(results), len(failures)))
    if failures:
        with open(os.path.join(WORK, "failures.json"), "w") as fh:
            json.dump([{"doc": d, "why": w} for d, w in failures], fh, indent=1)
    if rcm_variance:
        print("\n  %d RCM documents carry a tax variance against Sage "
              "(Sage rounds RCM tax to the rupee) - work/rcm_variance.json"
              % len(rcm_variance))
        with open(os.path.join(WORK, "rcm_variance.json"), "w") as fh:
            json.dump(rcm_variance, fh, indent=1)
    return results


# Staging stands in for Sage on the goods path: same columns, different home,
# and reachable when the Sage box is not.
SQL_STG_VENDORS = """
SELECT vendor_code, vendor_name, legal_name_raw, brn_raw, street1,
       city, state, postal, country, contact_name, phone1
  FROM sage_vendor
"""
STG_VENDOR_COLS = ["vendor", "name", "legal_name", "brn_raw", "street1",
                   "city", "state_raw", "pincode", "country",
                   "contact_person", "phone1"]

# acctfmttd, not acctid - the formatted code is what the distribution names.
SQL_STG_GL = "SELECT acctfmttd, acctdesc FROM sage_gl_acct"


# The platform's TypeOfStock enum, read off the create endpoint's own error:
# CONSUMABLES, STORES_AND_SPARES, CHARGE, SERVICE, WORK_IN_PROGRESS,
# RAW_MATERIAL, PRODUCT, ASSET, PACKAGING_ITEM, RESOURCE.
PACKING_CATEGORIES = {"4PACK", "4CARTN", "4POLYB", "4HANGR"}

# Goods lines book a PURCHASE, not a direct expense. One ledger per item, which
# is granular by design: it gives per-item reporting at the cost of a large
# chart of accounts (~16,900 ledgers at full scope).
ITEM_LEDGER_MAPPING = "ITEM_PURCHASE"

# POST /product/ signals a SKU that is already taken with either of these.
PRODUCT_EXISTS_ERRORS = ("Sku Code already exists", "Resource already exists")


_ITEM_CATEGORIES = None


def item_categories():
    """-> {Sage ICCATG code: {"categoryId", "name", "typeOfStock"}}

    Built by work/build_item_categories.py, which creates one catalog category
    per Sage category and records the typeOfStock each should carry."""
    global _ITEM_CATEGORIES
    if _ITEM_CATEGORIES is None:
        path = os.path.join(HERE, "ref", "item_categories.json")
        if os.path.exists(path):
            with open(path) as fh:
                _ITEM_CATEGORIES = json.load(fh).get("categories", {})
        else:
            _ITEM_CATEGORIES = {}
    return _ITEM_CATEGORIES


def stock_type_for(categories):
    """-> (typeOfStock, catalog category id) for a Sage item category.

    This used to return RESOURCE for everything, on three premises that were
    all true when written and are all now false:

      - "this organisation owns no catalog categories at all" - it owns 28,
        one per in-scope Sage category, created from Sage's own ICCATG names
      - "categories cannot be created" - true of /product/category, which is
        GET-only, but POST /catalogCategory exists and is what made them
      - "RESOURCE is the only value that takes a real unit and a real unit
        price" - measured false: every type stores both

    Verified on this build (work/logs/probe-stocktypes.log): RAW_MATERIAL,
    PACKAGING_ITEM, WORK_IN_PROGRESS, PRODUCT, CONSUMABLES, STORES_AND_SPARES
    and SERVICE all create, all keep unit and unitPrice, and all get an item
    ledger. Only ASSET and RESOURCE need no category, and ASSET can get NO
    ledger under any mapping, so it cannot carry a bill line and is not used.

    Anything with no mapped category still falls back to RESOURCE, which needs
    none - so an item in an unexpected category loads as it always did rather
    than failing.
    """
    cats = item_categories()
    for c in categories or ():
        rec = cats.get(s(c))
        if rec:
            return rec["typeOfStock"], rec["categoryId"]
    return "RESOURCE", None


def item_key(it):
    """Products are keyed (item, unit), not item alone.

    The platform rejects a bill line whose unit differs from its product's
    primary unit ("Primary Unit for product does not match with lineItem"), so
    a product has to exist for each unit an item is actually bought in. Only
    217 of 17,129 items are ever bought in more than one, so this costs almost
    nothing and removes the failure mode entirely."""
    return "%s|%s" % (s(it["item"]), platform_unit(it.get("um") or it.get("stock_um")))


_ITEM_HSN = {}


def item_hsn_map():
    """-> {unformatted item number: HSN} from Sage's item optional fields.

    Cached. Sage being unreachable is not fatal: the caller still falls back
    through the line's own HSN, a sibling line, and then the placeholder."""
    if _ITEM_HSN:
        return _ITEM_HSN
    try:
        for r in sage_query(SQL_ITEM_HSN):
            _ITEM_HSN[s(r["itemno"])] = s(r["hsn"])
        print("  item HSN: Sage ICITEMO optional field - %d items"
              % len(_ITEM_HSN), flush=True)
    except Exception as exc:                                    # noqa: BLE001
        print("  item HSN: Sage unreachable (%s); line HSN + default only"
              % str(exc).split("\n")[0][:60], flush=True)
        _ITEM_HSN[""] = ""
    return _ITEM_HSN


def resolve_item_hsn(it, by_item=None):
    """-> (hsn, source). Never None; the platform refuses a null hsnCode.

    In order of authority: the HSN Sage states on the line, the HSN the SAME
    item carries on another line, Sage's item-master optional field, then the
    flagged placeholder."""
    own = normalise_hsn(it.get("hsn"))
    if own:
        return own, "line"
    raw = s(it.get("item_raw")) or s(it["item"]).replace("-", "")
    if by_item:
        sib = normalise_hsn(by_item.get(raw) or by_item.get(s(it["item"])))
        if sib:
            return sib, "sibling"
    m = item_hsn_map()
    master = normalise_hsn(m.get(raw) or m.get(s(it["item"])))
    if master:
        return master, "ICITEMO"
    return GOODS_HSN_DEFAULT, "DEFAULT"


def _item_payload(it, unit, sku, name, by_item=None):
    stock, cat_id = stock_type_for([s(it.get("category"))])
    _hsn, _hsn_src = resolve_item_hsn(it, by_item)
    p = {
        "productName": name[:200], "skuCode": sku,
        "unit": unit, "unitOfMeasurement": unit,
        "unitPrice": float(q2(D(str(it["unitcost"])))),
        "typeOfStock": stock,
        "hsnCode": _hsn,
        "gstPercentage": float(D(str(it["rate"]))),
        "isManageInventory": False,
        "itemStatus": "ACTIVE", "isBulkUpload": True,
        "metaData": {"sageItem": s(it.get("item_raw")) or s(it["item"]),
                     "sageItemFmt": s(it["item"]),
                     "sageCategory": s(it.get("category")),
                     "sageUnit": s(it.get("um")),
                     "hsnMissing": "true" if not s(it.get("hsn")) else "false",
                     "hsnSource": _hsn_src,
                     "hsnIsDefault": "true" if _hsn_src == "DEFAULT" else "false",
                     "migrationSource": "IDEDAT"},
    }
    # Every type except ASSET and RESOURCE is refused without one
    # ("Category is mandatory in case of Raw Material").
    if cat_id:
        p["categoryId"] = cat_id
    return p


def ensure_item_products_parallel(state, items, workers=6, by_item=None):
    """The same work as ensure_item_products, run across a pool.

    There is no bulk product endpoint - /product/bulk, /product/bulkUpload and
    /product/import all answer GET,HEAD,OPTIONS because they match the catch-all
    /product/{id} route, not a real handler - so a full master is ~17,000
    separate POSTs. Serially that measured at 7-10 a minute, i.e. well over a
    day; this exists so a full run is hours instead.

    Each worker gets its OWN Api, because Api holds a requests.Session and a
    Session is not safe to share across threads. The crosswalk is written under
    a lock and flushed periodically, so an interrupted run resumes rather than
    repeating: every product already in the crosswalk is skipped on the way in.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    xw = state.xw.setdefault("items", {})
    todo = [k for k in sorted(items) if not (xw.get(k) or {}).get("ledger")]
    print("\n=== ITEM PRODUCTS (parallel, %d workers) ===" % workers, flush=True)
    print("  %d needed, %d already built, %d to create"
          % (len(items), len(items) - len(todo), len(todo)), flush=True)
    if not todo:
        return

    lock = threading.Lock()
    local = threading.local()
    done = {"n": 0, "ok": 0, "fail": 0}

    def worker(key):
        if not hasattr(local, "api"):
            local.api = Api()
        api = local.api
        it = items[key]
        unit = platform_unit(it.get("um") or it.get("stock_um"))
        raw = s(it.get("item_raw")) or s(it["item"]).replace("-", "")
        sku = "SAGE-%s-%s" % (raw, unit)
        name = s(it.get("descr")) or s(it["item"])

        prior = xw.get(key)
        if prior and prior.get("productId"):
            # Built by an earlier run that predates the ledger step.
            led = item_ledger_for(api, prior["productId"], ITEM_LEDGER_MAPPING)
            rec = dict(prior, ledger=str(led)) if led else None
            why = None if led else "no ledger"
        else:
            st, body = api.post("/product/",
                                _item_payload(it, unit, sku, name, by_item))
            rec, why = None, None
            if api.ok(st, body):
                pid = str(api.data(body).get("productId"))
                api.call("PATCH", "/product/%s/ACTIVE" % pid)
                rec = {"productId": pid, "skuCode": sku, "name": name,
                       "unit": unit}
                remember_product({"skuCode": sku, "productId": pid,
                                  "productName": name})
            elif any(e in api.err(body) for e in PRODUCT_EXISTS_ERRORS):
                try:
                    existing = find_product_by_sku(api, sku)
                except LookupFailed:
                    # Deferred, NOT burned: the lookup never completed, so it
                    # says nothing about whether the product exists. Burning on
                    # this was turning sustained rate limiting into thousands of
                    # permanently unusable SKUs.
                    rec, why, deferred = None, None, True
                    existing = None
                if existing:
                    rec = {"productId": str(existing["productId"]),
                           "skuCode": sku, "unit": unit, "adopted": True,
                           "name": existing.get("productName") or name}
                elif why is None and not locals().get("deferred"):
                    why = "BURNED - SKU taken by a row adoption cannot see"
            else:
                why = api.err(body)[:80]
            # A product without a ledger posts a line with a NULL
            # financeAccountId and then fails verify, so it is not recorded.
            if rec:
                led = item_ledger_for(api, rec["productId"], ITEM_LEDGER_MAPPING)
                if led:
                    rec["ledger"] = str(led)
                else:
                    rec, why = None, "no ledger"

        with lock:
            done["n"] += 1
            if rec:
                xw[key] = rec
                done["ok"] += 1
            else:
                done["fail"] += 1
                state.xw.setdefault("burned", {})[key] = sku
                print("  FAIL %-30s %s" % (sku, why), flush=True)
            # Flush periodically: an interrupted run must resume, not repeat.
            if done["n"] % 50 == 0:
                state.save()
                print("  %d/%d  ok=%d fail=%d"
                      % (done["n"], len(todo), done["ok"], done["fail"]),
                      flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, todo))
    state.save()
    print("  created/adopted %d, failed %d, crosswalk now %d"
          % (done["ok"], done["fail"], len(xw)), flush=True)


def ensure_item_products(api, state, items):
    """One real product per (Sage item, unit) - carrying the item's unit price,
    HSN and GST rate, which the per-GL pseudo-items never could.

    A GL-account pseudo-item cannot hold any of these: it stands for a whole
    ledger head, so its unit is OTH and its unit price is necessarily 0. That
    is what forced every goods line to OTH and left the item master priced at
    zero. `items` is {key -> a representative goods line}.
    """
    print("\n=== ITEM PRODUCTS ===", flush=True)
    xw = state.xw.setdefault("items", {})
    for key in sorted(items):
        rec = xw.get(key)
        if rec and rec.get("ledger"):
            continue
        if rec:
            # Product exists from an earlier run that predates the ledger step.
            # Attach the ledger directly - no need to search for the SKU again.
            led = item_ledger_for(api, rec["productId"], ITEM_LEDGER_MAPPING)
            if led:
                rec["ledger"] = str(led)
                state.save()
                print("  ledger backfilled %s -> %s" % (rec["skuCode"], led))
            else:
                print("  FAIL ledger for %s" % rec["skuCode"])
            continue
        it = items[key]
        unit = platform_unit(it.get("um") or it.get("stock_um"))
        raw = s(it.get("item_raw")) or s(it["item"]).replace("-", "")
        sku = "SAGE-%s-%s" % (raw, unit)
        name = s(it.get("descr")) or s(it["item"])

        # CREATE FIRST, adopt only on collision. The GL pseudo-items search
        # before creating because their SKUs long predate this loader; these
        # are minted here under a naming scheme nothing else uses, so the
        # search is almost always a miss - and find_product_by_sku pages a
        # FUZZY endpoint up to ten times, which at one search per item is
        # hundreds of calls and minutes of rate-limit backoff for nothing.
        _stock, _cat_id = stock_type_for([s(it.get("category"))])
        _hsn, _hsn_src = resolve_item_hsn(it)
        payload = {
            "productName": name[:200], "skuCode": sku,
            "unit": unit, "unitOfMeasurement": unit,
            "unitPrice": float(q2(D(str(it["unitcost"])))),
            "typeOfStock": _stock,
            "hsnCode": _hsn,
            "gstPercentage": float(D(str(it["rate"]))),
            # Stock is not being migrated here, only the purchase.
            "isManageInventory": False,
            "itemStatus": "ACTIVE", "isBulkUpload": True,
            "metaData": {"sageItem": raw, "sageItemFmt": s(it["item"]),
                         "sageCategory": s(it.get("category")),
                         "sageUnit": s(it.get("um")),
                         "hsnMissing": "true" if not s(it.get("hsn")) else "false",
                         "migrationSource": "IDEDAT"},
        }
        if _cat_id:
            payload["categoryId"] = _cat_id
        st, body = api.post("/product/", payload)
        if not api.ok(st, body):
            # The create endpoint reports a taken SKU two different ways -
            # "Sku Code already exists" and "Resource already exists" - and the
            # second was silently skipping adoption, leaving the item with no
            # product and its bills unpostable.
            if any(e in api.err(body) for e in PRODUCT_EXISTS_ERRORS):
                try:
                    existing = find_product_by_sku(api, sku)
                except LookupFailed as exc:
                    print("  DEFER %s: %s" % (sku, exc)); continue
                if existing:
                    led = item_ledger_for(api, existing["productId"],
                                          ITEM_LEDGER_MAPPING)
                    xw[key] = {"productId": str(existing["productId"]),
                               "skuCode": sku, "unit": unit, "adopted": True,
                               "ledger": str(led) if led else None,
                               "name": existing.get("productName") or name}
                    state.save()
                    print("  adopted %s -> %s" % (sku, existing["productId"]))
                    continue
                # Taken by a row adoption cannot see. Same rule as the GL
                # pseudo-items: never mint a -R2 variant.
                print("  BURNED %s - SKU taken by a row adoption cannot see" % sku)
                state.xw.setdefault("burned", {})[key] = sku
                state.save()
                continue
            print("  FAIL %s: %s" % (sku, api.err(body)))
            continue
        if api.dry_run:
            continue
        pid = str(api.data(body).get("productId"))
        api.call("PATCH", "/product/%s/ACTIVE" % pid)
        # Without this the bill line lands with a NULL financeAccountId while
        # still reporting success, and verify fails with "No Finance Account
        # Exists for product".
        led = item_ledger_for(api, pid, ITEM_LEDGER_MAPPING)
        if not led:
            print("  FAIL ledger for %s" % sku)
            continue
        xw[key] = {"productId": pid, "skuCode": sku, "name": name, "unit": unit,
                   "ledger": str(led)}
        state.save()
        print("  created %-30s %-6s @ %-10s %s"
              % (sku, unit, payload["unitPrice"], name[:34]))
    print("  item products in crosswalk: %d" % len(xw))


def phase_goods_masters(api, state, args):
    """Build every master the goods post phase will need - in the order it
    needs them.

    Contacts come FIRST and the bill selection is recomputed afterwards,
    because eligible_goods() filters on contacts: choosing item products from
    the pre-contact `shaped` list builds them for a different set of bills than
    post will actually attempt, which is how an earlier run ended up with 12 of
    14 bills reporting "no item product".
    """
    cats = None if args.all_categories else RAW_MATERIAL_CATEGORIES
    book = load_goods_book()

    shaped = []
    for key, bill in book.items():
        sh, why = classify_goods(bill, cats)
        if not why:
            shaped.append((key, sh))

    names = {s(r["acctfmttd"]): s(r["acctdesc"])
             for r in _staging_rows(SQL_STG_GL, ["acctfmttd", "acctdesc"])}
    rows = {s(v["vendor"]): v
            for v in _staging_rows(SQL_STG_VENDORS, STG_VENDOR_COLS)}

    scope = shaped if args.all_items else shaped[:max(args.limit or 10, 1) * 8]
    print("\nmasters for %d candidate bills" % len(scope))
    ensure_products(api, state,
                    {s(l["gl"]) for _, sh in scope for l in sh["exp"]},
                    names=names, bill_type="PURCHASE")
    ensure_contacts(api, state, {k[0] for k, _ in scope}, rows=rows)

    # NOW the eligible set is knowable, and it is what post will work from.
    cands, _ = eligible_goods(book, state, state.xw["contacts"], cats)
    work = cands if args.all_items else cands[:args.limit or 10]
    print("\n%d bills are postable; building their item products" % len(work))

    # An item's HSN read off ANY of its lines, for items whose other lines
    # state one and this one does not. Exact, not inferred - same item, same
    # HSN - and it resolves 135 items before the master lookup is needed.
    by_item = {}
    for bill in book.values():
        for l in bill["lines"]:
            h = normalise_hsn(l.get("hsn"))
            if h:
                by_item.setdefault(s(l.get("item_raw")) or s(l["item"]), h)
                by_item.setdefault(s(l["item"]), h)

    reps = {}
    if args.all_items:
        # The product master carries ONE unitPrice, but Sage bills the same
        # (item, unit) at several prices: 1,211 pairs carry 2 distinct unit
        # costs in this window, and a long tail runs to 8. setdefault() kept
        # whichever row happened to be read first, which is arbitrary - the
        # master could end up advertising a price from January for an item
        # last bought in April.
        #
        # The LATEST price wins, by the invoice date of the bill the line sits
        # on. That is the item's current cost and the only defensible single
        # answer. It changes no accounting: every bill line still posts its own
        # stated unit price, and qty x unit_cost = extended is asserted per
        # line either way. This is the master-data default only.
        seen_date = {}
        for bill in book.values():
            bdate = s(bill["header"].get("bill_date"))
            for l in bill["lines"]:
                if cats and s(l.get("category")) not in cats:
                    continue
                if D(str(l["qty"] or 0)) <= 0:
                    continue
                it = dict(l, qty=D(str(l["qty"])), unitcost=D(str(l["unitcost"])),
                          rate=q2(D(str(l["rate"] or 0))),
                          ext=q2(D(str(l["ext"] or 0))))
                k = item_key(it)
                if k not in reps or bdate >= seen_date.get(k, ""):
                    reps[k], seen_date[k] = it, bdate
        print("  --all-items: %d distinct (item, unit) products across the whole "
              "goods population" % len(reps))
        print("     unit price taken from each item's LATEST bill in the window")
    else:
        for _, sh in work:
            for it in sh["exp"]:
                reps.setdefault(item_key(it), it)
        print("  %d distinct (item, unit) products needed" % len(reps))

    if args.all_items or len(reps) > 40:
        ensure_item_products_parallel(state, reps, workers=max(1, args.workers),
                                     by_item=by_item)
    else:
        ensure_item_products(api, state, reps)


def eligible_goods(book, state, contacts_known, categories):
    """-> (list of (key, shape), Counter of grouped skip reasons)"""
    out, skips = [], collections.Counter()
    for key, bill in book.items():
        sh, why = classify_goods(bill, categories)
        if why:
            skips[why] += 1
            continue
        if key[0] not in contacts_known:
            skips["no contact built for vendor"] += 1
            continue
        out.append((key, sh))
    return out, skips


def phase_goods(api, state, args, do_post):
    cats = None if args.all_categories else RAW_MATERIAL_CATEGORIES
    book = load_goods_book()
    cands, skips = eligible_goods(book, state, state.xw["contacts"], cats)

    print("\n=== GOODS %s ===" % ("POST" if do_post else "DRY RUN"), flush=True)
    print("  categories: %s" % ("ALL" if cats is None else ", ".join(cats)))
    print("  quantity and unit price are STATED per line; qty x unit cost ties")
    print("  to the extended amount on every row of sage_goods_line, so unlike")
    print("  the AP-direct path there is no reconciliation gate.")
    print("  %d bills shaped, %d skipped" % (len(cands), sum(skips.values())))
    print("\n--- skip reasons, grouped and counted ---")
    for why, n in skips.most_common(12):
        print("   %-58s %d" % (why[:58], n))

    work = cands[:args.limit] if args.limit else cands
    results, failures = [], []
    for key, shape in work:
        vendor, invoice = key
        tag = "%s|%s" % (vendor, invoice)
        prior = state.posted.get(tag)
        if prior and prior.rstrip().endswith("UNVERIFIED"):
            # Same repair as the AP-direct path: a bill created but never
            # verified has no voucher, and skipping it on every later run left
            # it that way permanently.
            bid = prior.split("||")[1]
            vst, vbody = api.post("/bill/%s/verify" % bid)
            if api.ok(vst, vbody):
                print("  %-34s RE-VERIFIED %s" % (tag, bid))
                state.mark(tag, bid)
            else:
                print("  %-34s re-verify failed: %s" % (tag, api.err(vbody)))
                failures.append((tag, "re-verify: %s" % api.err(vbody)))
            continue
        if prior:
            continue
        missing = sorted({s(l["gl"]) for l in shape["exp"]
                          if s(l["gl"]) not in state.xw["products"]})
        if missing:
            failures.append((tag, "no product for %s" % ", ".join(missing)))
            print("  %-34s no product for GL %s" % (tag, ", ".join(missing)))
            continue
        xwi = state.xw.get("items", {})
        # A LEDGER, not merely a product row. A product without one used to
        # pass this check and then inherit the clearing account's ledger.
        # Service charge lines (CNTLINE "SVC:...") carry no item by design and
        # are booked on their own 4E expense head, so they are exempt.
        no_item = sorted({item_key(l) for l in shape["exp"]
                          if not str(l.get("CNTLINE", "")).startswith("SVC:")
                          and not (xwi.get(item_key(l)) or {}).get("ledger")})
        if no_item:
            print("  %-34s no item product for %s" % (tag, ", ".join(no_item[:2])))
            failures.append((tag, "no item product for %s" % ", ".join(no_item)))
            continue
        contact = state.xw["contacts"][vendor]
        payload = build_payload(api, key, shape, contact, state.xw["products"],
                                items=xwi)
        bad = assert_invariants(payload, shape)
        if bad:
            print("  INVARIANT FAIL %s: %s" % (tag, "; ".join(bad)))
            failures.append((tag, "; ".join(bad)))
            continue
        rates = sorted({li["gstPercentage"] for li in payload["lineItemDtoList"]})
        print("\n  %-34s %s  %s" % (tag, shape["procurement"],
                                    ",".join(shape["categories"])))
        print("     taxable=%s tax=%s roundOff=%s bill=%s lines=%d rates=%s"
              % (shape["taxable"], shape["tax"], shape["roundoff"],
                 shape["bill_amount"], len(payload["lineItemDtoList"]), rates))
        for li in payload["lineItemDtoList"][:3]:
            print("       qty=%-11s unit=%-5s unitPrice=%-11s hsn=%-8s %s"
                  % (li["quantity"], li["unit"], li["unitPrice"],
                     li["hsn"] or "-", li["description"][:30]))
        if len(payload["lineItemDtoList"]) > 3:
            print("       ... %d more" % (len(payload["lineItemDtoList"]) - 3))
        if not do_post:
            results.append({"doc": tag, "stage": "dryrun", "ok": True})
            continue

        st, body = api.post("/bill/", payload)
        if not api.ok(st, body):
            if "Bill number already exists" in api.err(body):
                print("     already posted - recording and moving on")
                state.mark(tag, "preexisting")
                continue
            print("     CREATE FAIL: %s" % api.err(body))
            failures.append((tag, "create: %s" % api.err(body)))
            continue
        bid = str(api.data(body).get("billId"))
        vst, vbody = api.post("/bill/%s/verify" % bid)
        if not api.ok(vst, vbody):
            print("     created %s  VERIFY FAIL: %s" % (bid, api.err(vbody)))
            state.mark(tag, "%s||UNVERIFIED" % bid)
            failures.append((tag, "verify: %s" % api.err(vbody)))
            continue
        print("     created %s  VERIFIED" % bid)
        state.mark(tag, bid)
        results.append({"doc": tag, "billId": bid, "ok": True})

    print("\n  attempted=%d ok=%d failed=%d"
          % (len(results) + len(failures), len(results), len(failures)))
    if UNITS_UNMAPPED:
        print("  units with no measurementUnit code, posted as OTH: %s"
              % ", ".join("%s x%d" % (u, n)
                          for u, n in UNITS_UNMAPPED.most_common()))
    if failures:
        with open(os.path.join(WORK, "goods_failures.json"), "w") as fh:
            json.dump([{"doc": d, "why": w} for d, w in failures], fh, indent=1)
    return results


DOD = [
    ("every bill live and verified",
     "SELECT billStatus, entityLedgerVerificationStatus, COUNT(*) FROM bill "
     "WHERE organisationId={org} AND isDeleted+0=0 GROUP BY 1,2"),
    ("4.1 only real GST rates may exist (any 5.01 or 2.49 means the defect is back)",
     "SELECT DISTINCT li.gstPercentage FROM billLineItem li JOIN bill b ON b.id=li.billId "
     "WHERE b.organisationId={org} AND li.isDeleted+0=0 ORDER BY 1"),
    ("4.1 no ledger head minted at a fake rate",
     "SELECT accountingName FROM financeAccount WHERE organisationId={org} "
     "AND accountingName REGEXP 'Input|Payable RCM' AND isDeleted+0=0 ORDER BY 1"),
    ("4.6 no line may have a NULL ledger",
     "SELECT COUNT(*) FROM billLineItem li JOIN bill b ON b.id=li.billId "
     "WHERE b.organisationId={org} AND li.financeAccountId IS NULL AND li.isDeleted+0=0"),
    # description=productName is only a defect where Sage actually SUPPLIED a
    # TEXTDESC. The bare count reported 9 and every one was a 4E4SD03 line whose
    # TEXTDESC is the empty string in Sage, so the fallback to the account name
    # was correct. Join staging (same MySQL server) through the metaData we
    # stamped on each line, and count only genuine losses.
    ("4.2 line descriptions carry TEXTDESC, not the product name",
     "SELECT COUNT(*) lost_textdesc FROM billLineItem li "
     "JOIN bill b ON b.id=li.billId "
     "JOIN idedat_staging.sage_ap_dist d "
     "  ON d.vendor_code = JSON_UNQUOTE(JSON_EXTRACT(li.metaData,'$.sageVendor')) "
     " AND d.inv_number_raw = JSON_UNQUOTE(JSON_EXTRACT(li.metaData,'$.sageDoc')) "
     " AND d.cntline = JSON_UNQUOTE(JSON_EXTRACT(li.metaData,'$.sageLine')) "
     "WHERE b.organisationId={org} AND li.isDeleted+0=0 AND b.isDeleted+0=0 "
     "AND li.description = li.productName "
     "AND d.description IS NOT NULL AND TRIM(d.description) <> ''"),
    ("every voucher balances",
     "SELECT ve.referenceId, ROUND(SUM(CASE WHEN ve.transactionType='DEBIT' THEN ve.amount "
     "ELSE -ve.amount END),4) residual FROM voucherEntry ve JOIN bill b ON b.id=ve.referenceId "
     "WHERE b.organisationId={org} AND ve.isDeleted+0=0 GROUP BY 1 HAVING ABS(residual)>0.01"),
    # There is no `voucher` table - the voucher header lives on voucherEntry
    # (voucherId / voucherNumber / voucherType). The old query died on 1146.
    ("voucherNumber == billNumber on every document",
     "SELECT COUNT(*) mismatched FROM (SELECT DISTINCT ve.referenceId "
     "FROM voucherEntry ve JOIN bill b ON b.id=ve.referenceId "
     "WHERE b.organisationId={org} AND ve.isDeleted+0=0 AND b.isDeleted+0=0 "
     "AND ve.voucherNumber<>b.billNumber) x"),
]


def phase_verify(api, state, args):
    print("\n=== DEFINITION OF DONE (verified in MySQL, not from this script) ===")
    for label, sql in DOD:
        print("\n-- %s" % label)
        try:
            rows = mysql(sql.format(org=ORG_ID))
        except Stop as exc:
            print("   %s" % exc); continue
        if not rows:
            print("   (no rows - clean)")
        for r in rows[:40]:
            print("   " + " | ".join(r))


def phase_legs(api, state, args):
    """The side-by-side worth showing: our voucher legs beside Sage's own, for
    the documents named on the command line (or the last two posted)."""
    docs = args.docs or [v.split("||")[0] for v in
                         list(state.posted.keys())[-2:]]
    for tag in docs:
        vendor, _, invoice = tag.partition("|")
        rec = state.posted.get(tag, "")
        bid = rec.split("||")[0] if rec and rec[0].isdigit() else None
        print("\n" + "=" * 78)
        print("%s   %s" % (tag, "billId " + bid if bid else "(not posted)"))
        print("=" * 78)

        sage = sage_query("""
            SELECT RTRIM(d.IDGLACCT) gl, CAST(d.AMTDIST AS decimal(18,2)) amount,
                   d.RATETAX1, d.RATETAX2,
                   CAST(d.AMTTAX1 AS decimal(18,2)) t1,
                   CAST(d.AMTTAX2 AS decimal(18,2)) t2
              FROM APIBD d JOIN APOBL b
                ON d.CNTBTCH=b.CNTBTCH AND d.CNTITEM=b.CNTITEM
             WHERE b.IDTRXTYPE=12 AND b.SRCEAPPL='AP'
               AND RTRIM(b.IDVEND)=%s AND RTRIM(b.IDINVC)=%s
             ORDER BY d.CNTLINE""", (vendor, invoice))
        agg = collections.defaultdict(lambda: D(0))
        for r in sage:
            agg[s(r["gl"])] += D(r["amount"] or 0)
        print("\nSAGE distribution")
        dr = cr = D(0)
        for gl, amt in sorted(agg.items()):
            side = "DR" if amt >= 0 else "CR"
            print("   %-4s %-10s %14s" % (side, gl, abs(amt)))
            dr += amt if amt > 0 else D(0)
            cr += -amt if amt < 0 else D(0)
        print("   %-4s %-10s %14s" % ("", "party (bal)", q2(dr - cr)))

        if not bid:
            continue
        legs = mysql(
            "SELECT ve.transactionType, fa.accountingName, ve.amount FROM voucherEntry ve "
            "JOIN financeAccount fa ON fa.id=ve.financeAccountId "
            "WHERE ve.referenceId=%s AND ve.isDeleted+0=0 "
            "ORDER BY ve.transactionType DESC, fa.accountingName" % bid,
            ["side", "account", "amount"])
        print("\nSMEASSIST voucher legs")
        tot = collections.defaultdict(lambda: D(0))
        for l in legs:
            tot[(l["side"], l["account"])] += D(l["amount"])
        d_sum = c_sum = D(0)
        for (side, acct), amt in sorted(tot.items(), key=lambda x: (x[0][0] != "DEBIT", x[0][1])):
            print("   %-6s %-42s %14s" % (side[:2], acct[:42], q2(amt)))
            if side == "DEBIT":
                d_sum += amt
            else:
                c_sum += amt
        print("   %-6s %-42s %14s" % ("", "TOTAL DEBIT", q2(d_sum)))
        print("   %-6s %-42s %14s" % ("", "TOTAL CREDIT", q2(c_sum)))
        print("   %-6s %-42s %14s" % ("", "residual", q2(d_sum - c_sum)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["cleanup", "masters", "dryrun", "post",
                                      "verify", "legs",
                                      "goods-masters", "goods-dryrun", "goods-post"])
    ap.add_argument("--pilot", action="store_true",
                    help="~10 bills chosen to cover the shapes, not the first 10")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--token", default=None)
    ap.add_argument("--docs", nargs="*", help="vendor|invoice keys, for 'legs'")
    ap.add_argument("--all-categories", action="store_true",
                    help="goods phases: every item category, not just raw materials")
    ap.add_argument("--all-items", action="store_true",
                    help="goods-masters: build a product for EVERY (item, unit) in "
                         "the goods population, not just the selected bills' items")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel workers for --all-items (default 6)")
    args = ap.parse_args()

    global TOKEN
    if args.token:
        TOKEN = args.token

    os.makedirs(WORK, exist_ok=True)
    state = State()
    api = Api(dry_run=args.phase in ("dryrun", "goods-dryrun"))

    if args.phase.startswith("goods-"):
        if args.phase == "goods-masters":
            return phase_goods_masters(api, state, args)
        return phase_goods(api, state, args, do_post=(args.phase == "goods-post"))

    if args.phase == "cleanup":
        return phase_cleanup(api, state, args)
    if args.phase == "verify":
        return phase_verify(api, state, args)
    if args.phase == "legs":
        return phase_legs(api, state, args)
    if args.phase == "masters":
        book = load_book()
        # Build masters only for what this run intends to post: bulk master
        # creation is awkward to reverse, so it stays scoped to the selection.
        shaped = []
        for key, bill in book.items():
            sh, why = classify(bill)
            if not why:
                shaped.append((key, sh))
        if args.pilot:
            # Cover the shapes across ALL vendors, then build only those.
            #
            # pilot_pick returns at most one bill per shape test, so it alone is
            # too thin to build masters from: a vendor that has to be held, or a
            # GL account whose SKU is burned, removes a shape from the pilot
            # entirely with no candidate left to replace it. So build for the
            # shape representatives PLUS a spread of other bills across distinct
            # vendors, and let the post phase pick the final ten from whatever
            # ended up buildable.
            reps = pilot_pick(shaped, args.limit or 10)
            keys = [k for k, _, _ in reps]
            seen_vendors = {k[0] for k in keys}
            for k, sh in shaped:
                if len(keys) >= (args.limit or 10) * 5:
                    break
                if k not in keys and k[0] not in seen_vendors:
                    keys.append(k)
                    seen_vendors.add(k[0])
        else:
            keys = [k for k, _ in (shaped[:args.limit] if args.limit else shaped)]
        _sel = set(keys)
        accounts = ({s(l["gl"]) for k, sh in shaped if k in _sel for l in sh["exp"]}
                    | {z["gl"] for k, sh in shaped if k in _sel
                       for z in sh.get("zero", ())})
        vendors = {k[0] for k in keys}
        print("\nmasters needed for this selection: %d GL accounts, %d vendors"
              % (len(accounts), len(vendors)))
        ensure_products(api, state, accounts)
        ensure_contacts(api, state, vendors)
        return
    return phase_run(api, state, args, do_post=(args.phase == "post"))




if __name__ == "__main__":
    try:
        main()
    except Stop as exc:
        print("\nSTOP: %s" % exc, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
