#!/usr/bin/env python3
"""Re-cut the Sage extract for a date window. Read-only against IDEDAT.

    ./extract_fy.py --window janapr --prove   # reproduce the committed v2 files
    ./extract_fy.py --window fy               # cut FY2025-26 (1 Apr 25 - 31 Mar 26)
    ./extract_fy.py --window fy --only Bills_Header,Sales_Header

PROMPT-full-year.md §2 asks for one change to the extract SQL - the date window -
and nothing else: "every column, join and classifier already written stays
exactly as it is". So this script does not contain a single line of SQL. It
reads the checked-in .sql files, rewrites the window literal, and runs them.

Three things earn it its own module rather than a sed one-liner.

1. **The window rewrite is checked, not hoped for.** A silent no-op rewrite
   would cut the OLD window and report FY counts against it, which looks like a
   successful run and is the most expensive way to be wrong here. Every file
   declares how many window predicates it must contain; a mismatch is fatal.

2. **One file's window must NOT become the FY window.** 06_ap_pay_hdr.sql has a
   second, deliberately WIDER predicate - a DATERMIT lookup spanning one month
   before the document window and two after, used only to find a payment mode.
   Narrowing it to the FY bounds would silently drop the payment mode of every
   payment remitted just outside the year. It widens proportionally instead.

3. **sqlcmd is not on either box, so the PSV writer is ours** - and a writer
   that formats a decimal differently from sqlcmd would produce files that look
   fine and reconcile wrongly. --prove re-cuts the Jan-Apr window and diffs
   against the committed extract-v2 files, which were produced by sqlcmd. The
   FY cut is only trustworthy because that diff is empty.

Output goes to ref/extract-fy/out/. Nothing here writes to SMEAssist.
"""

import argparse
import io
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "work"))

REF = os.path.join(HERE, "ref")
V1SQL = os.path.join(REF, "janapr-20260910", "sql")
V2SQL = os.path.join(REF, "extract-v2")
V2OUT = os.path.join(REF, "extract-v2", "out")

DELIM = "~"

# The window as the .sql files are checked in - the Jan-Apr cut.
BASE_FROM, BASE_TO = 20260101, 20260430
WINDOWS = {
    "janapr": (20260101, 20260430),
    "fy":     (20250401, 20260331),   # MAPPING.json window.from / window.to
}

# The wider DATERMIT lookup in 06_ap_pay_hdr.sql, and the margin it represents:
# one month before the document window, two months after. Kept as a margin so
# it tracks the window instead of being re-guessed.
WIDE_BASE = (20251201, 20260630)
WIDE_MARGIN_MONTHS = (-1, +2)

# name -> (sql dir, sql file, window predicates expected, expected FY rows)
#
# The sql dir is the answer to "which copy is canonical", not a default:
# extract-v2 supersedes janapr for the eight files it re-cut. Expected FY rows
# come from PROMPT-full-year.md §2; None means the file has no published FY
# figure yet and the count is reported without a verdict.
FILES = [
    ("Bills_Header",            V1SQL, "01_bills_hdr.sql",          1, 84220),
    ("Bills_Lines_PO",          V1SQL, "02_bills_lines_po.sql",     1, None),
    ("Bills_Lines_APDirect",    V1SQL, "03_bills_lines_apd.sql",    1, None),
    ("AP_Notes_Header",         V1SQL, "04_ap_notes_hdr.sql",       1, 6432),
    ("AP_Notes_Lines",          V1SQL, "05_ap_notes_lines.sql",     1, None),
    ("AP_Notes_Classification", V1SQL, "17_ap_notes_class.sql",     1, 6432),
    ("AP_Payments_Header",      V1SQL, "06_ap_pay_hdr.sql",         1, 14956),
    ("AP_Payment_Apps",         V2SQL, "07_ap_pay_apps.sql",        1, None),
    ("Sales_Header",            V1SQL, "08_sales_hdr.sql",          1, 15523),
    ("Sales_Lines",             V1SQL, "09_sales_lines.sql",        2, None),
    ("AR_Notes_Header",         V1SQL, "10_ar_notes_hdr.sql",       1, 687),
    ("AR_Notes_Lines",          V1SQL, "16_ar_notes_lines.sql",     2, None),
    ("AR_Receipts_Header",      V2SQL, "11_ar_rcpt_hdr.sql",        1, 1394),
    ("AR_Receipt_Apps",         V2SQL, "12_ar_rcpt_apps.sql",       1, None),
    ("Vendors_Needed",          V2SQL, "13_vendors.sql",            1, None),
    ("Customers_Needed",        V2SQL, "14_customers.sql",          1, None),
    ("Items_Needed",            V2SQL, "15_items.sql",              2, None),
    ("Banks_Needed",            V2SQL, "22_banks.sql",              2, 74),
    ("Expense_Heads",           V2SQL, "23_expense_heads.sql",      1, None),
    ("InvHseq_Crosswalk",       V2SQL, "24_invhseq_crosswalk.sql",  1, None),
    ("Chart_Of_Accounts",       V2SQL, "25_chart_of_accounts.sql",  0, 1610),
]


class ExtractError(Exception):
    """The cut is wrong. Never caught to carry on."""


def shift_months(yyyymmdd, months):
    y, m, d = yyyymmdd // 10000, (yyyymmdd // 100) % 100, yyyymmdd % 100
    m += months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y * 10000 + m * 100 + d


def retarget(sql, name, want_from, want_to, expect_n):
    """Rewrite the window literals. Returns (sql, how_many_rewritten).

    Refuses rather than guesses: if the file does not contain exactly the number
    of window predicates the registry declares, the registry and the SQL have
    drifted apart and a blind rewrite would cut the wrong period.
    """
    base = "%d AND %d" % (BASE_FROM, BASE_TO)
    n = sql.count(base)
    if n != expect_n:
        raise ExtractError(
            "%s: found %d window predicates %r, registry says %d. The SQL and this\n"
            "script have drifted - fix the registry, do not rewrite blindly."
            % (name, n, base, expect_n))
    out = sql.replace(base, "%d AND %d" % (want_from, want_to))

    # The wider DATERMIT lookup, if this file has one.
    wide = "%d AND %d" % WIDE_BASE
    nw = out.count(wide)
    if nw:
        lo = shift_months(want_from, WIDE_MARGIN_MONTHS[0])
        hi = shift_months(want_to, WIDE_MARGIN_MONTHS[1])
        out = out.replace(wide, "%d AND %d" % (lo, hi))
    return out, n, nw


def cell(v):
    """One value as sqlcmd -W -s"~" would have written it.

    None becomes empty rather than the literal 'NULL': measured, the committed
    v2 files contain zero NULL tokens across all ten files, because every
    nullable column in the SQL is already wrapped in ISNULL. If that ever stops
    being true --prove catches it as a diff.

    Trailing TABS are preserved. sqlcmd -W trims trailing spaces, and the SQL's
    RTRIM removes spaces only, so nine vendor names and one customer name keep
    a run of tabs from Sage. Stripping them here would be a silent edit to a
    name that gets posted as a contact, so they survive the extract and the
    loader decides.
    """
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    elif not isinstance(v, str):
        v = str(v)
    return v.rstrip(" \r\n")


_LEADING_ZERO = re.compile(r"^(-?)\.(\d+)$")


def _norm_cell(c):
    """Neutralise the two ways sqlcmd's text differs from ours, so --prove
    reports CONTENT differences instead of formatting noise.

    Both were measured against the committed files, not assumed:

    * sqlcmd prints a decimal below 1 with no leading zero - '.5100', not
      '0.5100'. 583 of 598 AR_Receipts_Header rows and 63 InvHseq_Crosswalk
      rows. Decimal() parses either, and '0.5100' is the safer thing to ship.
    * trailing tabs, which sqlcmd keeps and a naive rstrip() would eat.
    """
    c = c.rstrip(" \t")
    m = _LEADING_ZERO.match(c)
    if m:
        return "%s0.%s" % (m.group(1), m.group(2))
    # Every non-ASCII codepoint folds to '?' on BOTH sides. extract-v2 holds raw
    # CP1252 bytes that decode to U+FFFD, where this cut holds the real
    # character (en dash, bullet); folding lets the test prove the DATA matches
    # while the counts below report the encoding fix separately.
    return c if c.isascii() else "".join(ch if ord(ch) < 128 else "?" for ch in c)


def _read_rows(path):
    raw = io.open(path, "rb").read()
    # errors='replace' on purpose: extract-v2 is not valid UTF-8 everywhere
    # (sqlcmd wrote raw CP1252), and that is exactly what we want to surface.
    lines = [l for l in raw.decode("utf-8", "replace").split("\n") if l]
    bad_utf8 = sum(1 for l in raw.split(b"\n") if l and not _is_utf8(l))
    return lines, bad_utf8


def _is_utf8(b):
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def compare(new_path, old_path):
    """Row-multiset comparison. Neither file has an ORDER BY, so row order is
    whatever the query plan produced and is not a difference."""
    new_lines, new_bad = _read_rows(new_path)
    old_lines, old_bad = _read_rows(old_path)
    norm = lambda ls: [DELIM.join(_norm_cell(c) for c in l.split(DELIM)) for l in ls[1:]]
    new, old = norm(new_lines), norm(old_lines)
    lz = sum(1 for l in old_lines for c in l.split(DELIM) if _LEADING_ZERO.match(c))
    sn, so = sorted(new), sorted(old)
    return {"equal": sn == so, "ordered": new == old,
            "n_new": len(new), "n_old": len(old),
            "only_new": len(set(sn) - set(so)), "only_old": len(set(so) - set(sn)),
            "lz": lz, "mojibake": old_bad - new_bad}


def run_one(cn, name, sql_dir, sql_file, expect_n, want, outdir, log):
    path = os.path.join(sql_dir, sql_file)
    if not os.path.exists(path):
        raise ExtractError("%s: %s is missing" % (name, path))
    sql = io.open(path, encoding="utf-8", errors="replace").read()
    sql, n, nw = retarget(sql, name, want[0], want[1], expect_n)

    t0 = time.time()
    with cn.cursor() as cur:
        cur.execute(sql)
        # Temp-table staging means the row-returning SELECT is rarely the first
        # result set. Walk forward to the last set that has a description.
        desc, rows = cur.description, None
        while True:
            if cur.description:
                desc = cur.description
                rows = cur.fetchall()
            if not cur.nextset():
                break
        if rows is None:
            raise ExtractError("%s: the batch returned no result set" % name)

    cols = [d[0] for d in desc]
    dest = os.path.join(outdir, name + ".psv")
    bad = []
    with io.open(dest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(DELIM.join(cols) + "\n")
        for i, r in enumerate(rows, start=2):
            vals = [cell(v) for v in r]
            line = DELIM.join(vals)
            if line.count(DELIM) != len(cols) - 1 or "\n" in line or "\r" in line:
                bad.append(i)
            fh.write(line + "\n")

    secs = time.time() - t0
    if bad:
        raise ExtractError(
            "%s: %d rows carry an unescaped %r or a line break (first at line %d).\n"
            "Per the extract schema the SQL must strip it - do not repair it here."
            % (name, len(bad), DELIM, bad[0]))

    log("  %-26s %7d rows  %3d col  %6.1fs  window %d-%d%s"
        % (name, len(rows), len(cols), secs, want[0], want[1],
           "  (+%d wide)" % nw if nw else ""))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=sorted(WINDOWS), required=True)
    ap.add_argument("--only", help="comma-separated file names")
    ap.add_argument("--prove", action="store_true",
                    help="diff the result against ref/extract-v2/out (janapr only)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    want = WINDOWS[a.window]
    outdir = a.out or os.path.join(REF, "extract-%s" % a.window, "out")
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    logpath = os.path.join(HERE, "work", "extract_%s.log" % a.window)
    logfh = io.open(logpath, "a", encoding="utf-8")

    def log(msg):
        print(msg)
        logfh.write(msg + "\n")
        logfh.flush()

    wanted = set(a.only.split(",")) if a.only else None
    todo = [f for f in FILES if not wanted or f[0] in wanted]
    if wanted:
        unknown = wanted - set(f[0] for f in FILES)
        if unknown:
            raise ExtractError("unknown file(s): %s" % ", ".join(sorted(unknown)))

    import sage
    log("\n=== extract %s  window %d..%d  %s ==="
        % (a.window, want[0], want[1], time.strftime("%Y-%m-%d %H:%M:%S")))
    log("  host %s  db %s  -> %s" % (sage.CFG["SQL_HOST"], sage.CFG["SQL_DATABASE"], outdir))

    counts, failed = {}, []
    cn = sage.connect()
    try:
        for name, d, f, n, _exp in todo:
            try:
                counts[name] = run_one(cn, name, d, f, n, want, outdir, log)
            except Exception as exc:
                failed.append((name, str(exc).split("\n")[0]))
                log("  %-26s FAILED  %s" % (name, str(exc).splitlines()[0]))
    finally:
        cn.close()

    log("\n  %d files, %d rows" % (len(counts), sum(counts.values())))

    # Expected-count verdict, per PROMPT-full-year.md §2: "if a file does not
    # match, stop and say so rather than loading it".
    if a.window == "fy":
        log("\n  against PROMPT-full-year.md §2:")
        for name, _d, _f, _n, exp in todo:
            if exp is None or name not in counts:
                continue
            got = counts[name]
            log("    %-26s %7d  expected %7d  %s"
                % (name, got, exp, "OK" if got == exp else
                   "MISMATCH (%+d)" % (got - exp)))

    if a.prove:
        log("\n  --prove: content diff against the sqlcmd-produced extract-v2 files")
        bad = 0
        for name in sorted(counts):
            ref = os.path.join(V2OUT, name + ".psv")
            if not os.path.exists(ref):
                continue
            r = compare(os.path.join(outdir, name + ".psv"), ref)
            verdict = "SAME" if r["equal"] else "DIFFERS (+%d/-%d)" % (r["only_new"],
                                                                      r["only_old"])
            if not r["equal"]:
                bad += 1
            log("    %-26s %-18s rows %d/%d  order %s%s%s"
                % (name, verdict, r["n_new"], r["n_old"],
                   "same" if r["ordered"] else "differs",
                   "  leading-zero %d" % r["lz"] if r["lz"] else "",
                   "  mojibake-fixed %d" % r["mojibake"] if r["mojibake"] else ""))
        if bad:
            log("    %d file(s) differ in CONTENT. Do NOT trust the FY cut until explained."
                % bad)
        else:
            log("    Every file matches row-for-row. The PSV writer is proven.")

    if failed:
        log("\n  %d FAILED: %s" % (len(failed), ", ".join(n for n, _ in failed)))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExtractError as exc:
        sys.stderr.write("\nEXTRACT ERROR: %s\n" % exc)
        sys.exit(2)
