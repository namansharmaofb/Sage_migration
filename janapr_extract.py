#!/usr/bin/env python3
"""Reader for the Jan-Apr 2026 buy/sell extract - the 22 tilde-delimited files.

This is the SOURCE layer only. It reads, validates and joins; it posts nothing
and needs neither a token nor a network. Run it directly to validate the whole
extract:

    ./janapr_extract.py

Three properties earn this its own module:

1. **Three filenames exist in BOTH extract directories** - Items_Needed,
   Vendors_Needed and Customers_Needed. The v1 copies are superseded: v1
   Items_Needed carries NO hsn column at all (HSN is mandatory on product
   create, so loading v1 blocks 94.4% of invoices) and one malformed row. The
   FILES registry below pins each name to exactly one directory so the wrong
   copy cannot be opened by accident.

2. **A field-count mismatch is fatal, never repaired.** Per
   SCHEMA-janapr-extract.md: "If not, a text field contains an unescaped
   delimiter or line break and the extract must be re-cut - do not attempt to
   repair it in the loader." A loader that patches around a short row invents
   data silently.

3. **The financial year comes from the document's own date.** The window
   straddles FY2025-2026 and FY2026-2027 and the counter series key has no FY
   fallback, so an earlier loader that hardcoded calendar-2026 logic misfiled
   everything before April. fy() takes a date and nothing else - there is no
   today() anywhere in this file.
"""

import io
import os
import sys
from decimal import Decimal

REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")
V1 = os.path.join(REF, "janapr-20260910")
V2 = os.path.join(REF, "extract-v2", "out")
FY = os.path.join(REF, "extract-fy", "out")

DELIM = "~"

# Which window the loader reads. "janapr" is the original four-month cut split
# across two directories; "fy" is the whole of FY2025-26 (1 Apr 25 - 31 Mar 26)
# produced by extract_fy.py, where every file lives in ONE directory and every
# file is the v2 SQL.
#
# Set with SAGE_WINDOW=fy. It is not the default: the Jan-Apr figures are the
# ones the existing posted logs, the pilot and every earlier report were
# measured against, so flipping the default silently would re-point all of them.
WINDOW = os.environ.get("SAGE_WINDOW", "janapr").strip().lower()
if WINDOW not in ("janapr", "fy"):
    raise SystemExit("SAGE_WINDOW must be 'janapr' or 'fy', not %r" % WINDOW)

# name -> (directory, expected rows). The row count is what the extract
# reported when it was cut; extract-v2/README.md asks for it to be re-checked
# every run, so a mismatch WARNS (the extract may legitimately be re-cut) while
# a field-count mismatch is fatal.
#
# The directory is not a default - it is the answer to "which copy is correct".
# Do not add a v1 fallback for the five v2 files.
FILES = {
    # --- superseded by extract v2: HSN, bank master, expense heads, contact fields
    "Items_Needed":              (V2, 19869),
    "Vendors_Needed":            (V2,   937),
    "Customers_Needed":          (V2,    64),
    "Banks_Needed":              (V2,    74),
    "Expense_Heads":             (V2,   279),
    # --- unchanged v1 files
    "Bills_Header":              (V1, 29745),
    "Bills_Lines_PO":            (V1, 32582),
    "Bills_Lines_APDirect":      (V1, 41471),
    "Sales_Header":              (V1,  7051),
    "Sales_Lines":               (V1, 12240),
    "AP_Notes_Header":           (V1,  2203),
    "AP_Notes_Lines":            (V1,  2659),
    "AP_Notes_Classification":   (V1,  2203),
    "AR_Notes_Header":           (V1,   232),
    "AR_Notes_Lines":            (V1,   383),
    "AP_Payments_Header":        (V1,  5003),
    "AP_Payment_Apps":           (V1, 80388),
    "AR_Receipts_Header":        (V1,   598),
    "AR_Receipt_Apps":           (V1, 20422),
}

# The FY cut: one directory, every file, row counts as extract_fy.py measured
# them. Bills_Header is 82,436 and NOT the brief's 84,220 - the difference is
# exactly the 1,784 type-12 documents sitting in PY (payment) batches, which
# 01_bills_hdr.sql excludes by design. See the load report.
FY_ROWS = {
    "Bills_Header": 82436, "Bills_Lines_PO": 93531,
    "Bills_Lines_APDirect": 103745, "AP_Notes_Header": 6432,
    "AP_Notes_Lines": 7538, "AP_Notes_Classification": 6432,
    "AP_Payments_Header": 14956, "AP_Payment_Apps": 204146,
    "Sales_Header": 15523, "Sales_Lines": 31788,
    "AR_Notes_Header": 687, "AR_Notes_Lines": 995,
    "AR_Receipts_Header": 1394, "AR_Receipt_Apps": 50152,
    "Vendors_Needed": 1398, "Customers_Needed": 99,
    "Items_Needed": 47737, "Banks_Needed": 74,
    "Expense_Heads": 344, "InvHseq_Crosswalk": 53370,
    "Chart_Of_Accounts": 1610,
}

if WINDOW == "fy":
    FILES = {name: (FY, rows) for name, rows in FY_ROWS.items()}


class ExtractError(Exception):
    """The extract is wrong. Never caught to carry on - see property 2 above."""


def path_of(name):
    if name not in FILES:
        raise ExtractError("unknown extract file %r - known: %s"
                           % (name, ", ".join(sorted(FILES))))
    d, _ = FILES[name]
    return os.path.join(d, name + ".psv")


def read(name, expect_rows=True):
    """Every row of one extract file, as dicts keyed by the header.

    Refuses to return anything at all if the file is missing or any row has the
    wrong field count. §1 of the brief: a script "must refuse to start if its
    prerequisite is missing rather than half-post".
    """
    p = path_of(name)
    if not os.path.exists(p):
        raise ExtractError(
            "%s is missing (%s).\nCopy the extract down first:\n"
            "  ssh root@10.22.0.165 'cd /root/indiandesign/reference && "
            "tar cf - janapr-20260910 extract-v2' | tar xf - -C ref/" % (name, p))

    with io.open(p, encoding="utf-8", errors="replace") as fh:
        head = fh.readline().rstrip("\r\n")
        if not head:
            raise ExtractError("%s: empty file" % name)
        cols = head.split(DELIM)
        n = len(cols)
        rows = []
        bad = []
        for i, line in enumerate(fh, start=2):
            line = line.rstrip("\r\n")
            if not line:
                continue
            vals = line.split(DELIM)
            if len(vals) != n:
                bad.append((i, len(vals)))
                if len(bad) > 5:
                    break
                continue
            rows.append(dict(zip(cols, vals)))

    if bad:
        detail = ", ".join("line %d has %d fields" % b for b in bad[:5])
        raise ExtractError(
            "%s: %s (header has %d). A text field carries an unescaped %r or a\n"
            "line break. Per SCHEMA-janapr-extract.md the extract must be RE-CUT -\n"
            "this loader will not repair it, because repairing it invents data."
            % (name, detail, n, DELIM))

    exp = FILES[name][1]
    if expect_rows and exp and len(rows) != exp:
        sys.stderr.write(
            "  WARNING %s: %d rows, expected %d. The extract may have been re-cut;\n"
            "          confirm that is intended before posting from it.\n"
            % (name, len(rows), exp))
    return rows


# ---------------------------------------------------------------- typed fields

def D(v, default="0"):
    """Decimal from an extract field. Blank is the default, never an error:
    the extract writes '' for absent amounts, and Decimal('') raises."""
    s = (v or "").strip()
    if not s:
        s = default
    try:
        return Decimal(s)
    except Exception:
        raise ExtractError("not a number: %r" % v)


def ymd(v):
    """decimal(9) YYYYMMDD -> (y, m, d). 0 and blank mean 'no date'."""
    s = (v or "").strip()
    if not s:
        return None
    # Sage writes these through sqlcmd as decimal, so '20260131' or '20260131.0'
    s = s.split(".")[0]
    if s in ("0", ""):
        return None
    if len(s) != 8 or not s.isdigit():
        raise ExtractError("not a YYYYMMDD date: %r" % v)
    return int(s[0:4]), int(s[4:6]), int(s[6:8])


def iso(v):
    """YYYYMMDD -> 'YYYY-MM-DD', or None."""
    t = ymd(v)
    return None if t is None else "%04d-%02d-%02d" % t


def epoch_ms(v):
    """YYYYMMDD -> epoch milliseconds at UTC midnight, or None.

    The payment and receipt DTOs take voucherDate/transactionDate as a Java
    long, not a string. Fixed at UTC midnight so a document's date cannot drift
    across a day boundary with the runner's timezone - these are accounting
    dates, not instants.
    """
    t = ymd(v)
    if t is None:
        return None
    import calendar
    return int(calendar.timegm((t[0], t[1], t[2], 0, 0, 0, 0, 0, 0))) * 1000


def fy(v):
    """Indian financial year of a document date: 'FY2025-2026'.

    §3.2: derive the FY from the DOCUMENT's own date, never from today. April
    starts the year, so a bill dated Nov 2025 is FY2025-2026 and one dated
    Jan 2026 is ALSO FY2025-2026 - which is the case an earlier loader got
    wrong by using calendar-2026 logic.
    """
    t = ymd(v)
    if t is None:
        return None
    y, m, _ = t
    start = y if m >= 4 else y - 1
    return "FY%d-%d" % (start, start + 1)


def truthy(v):
    """The extract's 1/0 flags. Note is_rcm must never reach the payload as
    None - §7: the guard is BooleanUtils.isFalse, so null falls INTO the
    reverse-charge branch and under-credits the vendor by exactly the tax."""
    return (v or "").strip() in ("1", "1.0", "Y", "y", "true", "True")


# ------------------------------------------------------------------- joins

def _key(r, cols):
    """One column keys on the BARE value, several on a tuple.

    Not cosmetic. Keying a single column as a 1-tuple reads fine at the index
    site and then silently matches NOTHING at the lookup site, because callers
    naturally write master[row["customer"]] with a string. That exact miss made
    every one of 7,051 invoices look absent from the customer master, which
    scored the URP-with-GST hold as zero and would have posted the held
    invoices. A silent join miss is far more dangerous than a KeyError, so the
    key shape matches how callers actually index.
    """
    return r[cols[0]] if len(cols) == 1 else tuple(r[c] for c in cols)


def by_key(rows, *cols):
    """Index rows -> list of rows (many per key)."""
    out = {}
    for r in rows:
        out.setdefault(_key(r, cols), []).append(r)
    return out


def one_per_key(rows, *cols):
    """Index rows expecting exactly one per key; raises if a key repeats.

    Used for the bill primary key (cntbtch, cntitem) and for master files
    keyed on the Sage code, where a duplicate would silently make the join
    non-deterministic.
    """
    out = {}
    for r in rows:
        k = _key(r, cols)
        if k in out:
            raise ExtractError("duplicate key %r on %s" % (k, "+".join(cols)))
        out[k] = r
    return out


def require_join(rows, master, col, what):
    """Every row's `col` must exist in `master`. Returns the missing values.

    The counterpart to _key: even with the right key shape, a join can miss
    because the extract genuinely lacks a master row, and that must be counted
    rather than skipped. Callers decide whether a miss is fatal.
    """
    missing = {}
    for r in rows:
        v = r[col]
        if v not in master:
            missing[v] = missing.get(v, 0) + 1
    if missing:
        sys.stderr.write("  %d distinct %s not in the master (%d rows)\n"
                         % (len(missing), what, sum(missing.values())))
    return missing


# ------------------------------------------------------------------ validation

def validate(verbose=True):
    """Read and check every registered file. Returns {name: rowcount}.

    This is the gate every posting script runs before its first write.
    """
    counts = {}
    problems = []
    for name in sorted(FILES):
        try:
            rows = read(name)
            counts[name] = len(rows)
            if verbose:
                exp = FILES[name][1]
                flag = "" if len(rows) == exp else "  (expected %d)" % exp
                where = {V2: "v2", FY: "fy"}.get(FILES[name][0], "v1")
                print("  %-26s %s %7d rows%s" % (name, where, len(rows), flag))
        except ExtractError as exc:
            problems.append("%s: %s" % (name, exc))
            if verbose:
                print("  %-26s FAILED" % name)
    if problems:
        raise ExtractError("the extract does not validate:\n\n" +
                           "\n\n".join(problems))
    return counts


def main():
    print("Sage extract [%s window] - %s" % (WINDOW, REF))
    print("  v1 %s\n  v2 %s\n" % (V1, V2))
    counts = validate()
    print("\n  %d files, %d rows total" % (len(counts), sum(counts.values())))

    # The FY split is the one derived fact worth printing here: it is what
    # decides how many counter series script 1 has to create, and getting it
    # from today() instead of the document date is a known past defect.
    hdr = read("Bills_Header")
    years = {}
    for r in hdr:
        years[fy(r["datebus"])] = years.get(fy(r["datebus"]), 0) + 1
    print("\n  Bills_Header by financial year of datebus:")
    for k in sorted(years, key=lambda x: (x is None, x)):
        print("    %-14s %6d" % (k, years[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
