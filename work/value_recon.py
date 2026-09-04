#!/usr/bin/env python
"""Field-level value reconciliation: SAGE vs SMEASSIST.

Read-only on both sides. Answers one question - *does any value differ?* - and
writes every difference it finds to work/value-mismatches.json.

  .venv/bin/python work/value_recon.py                # full run
  .venv/bin/python work/value_recon.py --detail       # + print the first 40
  .venv/bin/python work/value_recon.py --limit 500    # sample while iterating
  .venv/bin/python work/value_recon.py --check gl_amount,gst_rate

This is deliberately NOT work/reconcile.py's job done twice:

  reconcile.py   counts, and compares ONE number per document (billAmount).
  value_recon.py compares every value that crossed: totals, dates, the amount
                 on each Sage GL account, each line's stated rate, quantity and
                 unit price, the RCM flag, the round-off, and the voucher legs.

It re-derives NOTHING from classify()/build_payload(). Both sides are read raw -
Sage from APOBL + idedat_staging, SMEAssist from its own MySQL - so a defect in
the posting logic cannot hide by being applied consistently to both sides. The
only thing borrowed from the loader is the join key (base_invoice) and the
posted.log billId map, because those *are* the correspondence being checked.

Sage universe: load_headers() (APOBL, or the .psv extract when Sage is down) -
11,256 AP-direct documents, filtered exactly as extract.sql filtered the proven
run. Deliberately NOT idedat_staging.sage_ap_obl, which work/reconcile.py uses.
Measured 4 Sep 2026, consolidated on the *N suffix: ap_obl holds 12,781 of these
documents and APOBL 11,256, with every APOBL document present in ap_obl and
1,525 extra ones that are not. The extras are what the extraction filter
excludes - non-INR, the VAT/NRVAT tax groups, and documents whose distribution
falls outside 4E / 2A7T / 1L8TX14-16. They were never in scope to post, so
counting them makes 'in_sage_not_posted' 1,525 too high.

Join key: billId out of posted.log, never billNumber. Short invoice numbers
('3', '007', '188') recur across vendors and keying on them hands one vendor
another vendor's amount.
"""
import argparse, collections, datetime, json, os, subprocess, sys
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P

OUT = os.path.join(P.WORK, "value-mismatches.json")

# Which distribution heads must show up as a posted LINE, and therefore inside
# taxableAmount. Derived from Sage's own bookkeeping, not from the loader:
#
#   2A1AP*  the A/P control - it is the credit leg, the party ledger's job.
#   1L8TX*  the reverse-charge payable - self-assessed tax, never taxable value.
#   5*      revenue. Not the expense side of a purchase.
#   2A7TX*  the input-tax head, and the one that depends on the document:
#           - FORWARD charge: Sage carries the document's own GST in the APOBL
#             header (AMTTAXHC), and a 2A7TX distribution line is something
#             else - the import IGST a courier paid and billed on. That is
#             vendor-payable value, so it belongs in taxableAmount.
#             OTH359|BLR611051: 4E2ME33 2,213 + 2A7TX04 3,503 + 4E4SD05 1,100
#             = 6,816 taxable, + 198 tax = 7,014 = AMTINVCHC exactly.
#           - REVERSE charge: the 2A7TX line IS the input leg of the
#             self-assessed pair and equals the tax (1,084 of 1,084 documents in
#             the window). Counting it would double the tax into taxable.
NEVER_TAXABLE = ("2A1AP", "1L8TX", "5")
RCM_HEAD = "1L8TX"
INPUT_HEAD = "2A7TX"
ROUNDOFF_HEAD = "4E1M016"


def taxable_heads(d):
    """-> {gl: amount} for the heads that must appear as posted lines."""
    skip = NEVER_TAXABLE + ((INPUT_HEAD,) if d["is_rcm"] else ())
    return {g: a for g, a in d["gl"].items() if not g.startswith(skip)}

# What each check means, carried into the JSON so the report explains itself.
CHECKS = {
    "bill_amount":   ("bill.billAmount vs Sage AMTINVCHC / doc_total", "high"),
    "taxable":       ("bill.taxableAmount vs the Sage expense-head total", "high"),
    "gst_amount":    ("bill.gstAmount vs Sage AMTTAXHC (forward) / 1L8TX (RCM)", "high"),
    "gl_amount":     ("amount posted on each Sage GL account", "high"),
    "gst_rate":      ("line gstPercentage vs Sage's stated RATETAX1+2", "high"),
    "quantity":      ("line quantity vs the Sage goods line", "medium"),
    "unit_price":    ("line unitPrice vs the Sage goods line unit cost", "medium"),
    "bill_date":     ("bill.billDate vs Sage DATEINVC", "medium"),
    "rcm_flag":      ("line isRcmEnabled vs Sage's 1L8TX booking", "high"),
    "line_sum":      ("SUM(line taxableAmount) vs bill.taxableAmount", "high"),
    "identity":      ("billAmount == taxable + gst + roundOff", "high"),
    "roundoff":      ("stored roundOffAmount vs the Sage 4E1M016 line", "low"),
    "illegal_rate":  ("gstPercentage is not a legal GST slab", "high"),
    "null_ledger":   ("line financeAccountId is NULL", "medium"),
    "gl_split":      ("one Sage GL account booked to two ledger heads", "medium"),
    "voucher_balance": ("voucher DEBIT - CREDIT is not zero", "high"),
    "voucher_number":  ("voucherNumber != billNumber", "low"),
    "duplicate":     ("one Sage document posted as two live bills", "high"),
}


def q(sql, db="smeassist"):
    """Read-only MySQL over a multiplexed ssh connection.

    --batch WITHOUT --raw: descriptions carry newlines and tabs, and --raw
    passes them through verbatim, which splits one row across several lines and
    silently corrupts every column after it. --batch escapes them instead.
    """
    p = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "-o", "ControlMaster=auto",
                        "-o", "ControlPath=/tmp/.sage-cm-%r",
                        "-o", "ControlPersist=600",
                        P.db_host(), "mysql %s -N --batch -e %s"
                        % (db, json.dumps(sql))],
                       capture_output=True, text=True, timeout=900)
    if p.returncode:
        raise SystemExit("%s query failed: %s" % (db, p.stderr[:400]))
    return [ln.split("\t") for ln in p.stdout.splitlines() if ln.strip()]


def dec(v):
    """MySQL NULL arrives as the four characters 'NULL'."""
    if v in (None, "", "NULL", "\\N"):
        return D(0)
    try:
        return D(str(v))
    except Exception:                                           # noqa: BLE001
        return D(0)


def q2(v):
    return P.q2(dec(v))


def yyyymmdd(v):
    """billDate is stored as epoch ms on some rows and a datetime on others.
    Accept both rather than guessing, and return Sage's own int form."""
    v = (v or "").strip()
    if not v or v in ("NULL", "0"):
        return None
    if v.isdigit() and len(v) >= 12:                 # epoch milliseconds
        d = datetime.datetime.fromtimestamp(int(v) / 1000.0,
                                            datetime.timezone.utc)
        return int(d.strftime("%Y%m%d"))
    digits = "".join(c for c in v[:10] if c.isdigit())
    return int(digits) if len(digits) == 8 else None


class Report:
    def __init__(self):
        self.rows = []
        self.by_check = collections.Counter()
        self.docs = set()

    def add(self, check, doc, field, sage, sme, bill_id=None, note=None,
            delta=None, severity=None):
        what, sev = CHECKS.get(check, ("", "medium"))
        sev = severity or sev
        if delta is None and isinstance(sage, (int, float)) \
                and isinstance(sme, (int, float)):
            delta = round(sme - sage, 2)
        self.by_check[check] += 1
        self.docs.add(doc)
        self.rows.append({k: v for k, v in (
            ("check", check), ("severity", sev), ("doc", doc),
            ("billId", bill_id), ("field", field),
            ("sage", sage), ("smeassist", sme), ("delta", delta),
            ("note", note)) if v is not None})


# ---------------------------------------------------------------- SAGE SIDE
def sage_side(limit=None):
    """-> {(vendor, base invoice): doc}. Raw Sage, no loader logic."""
    docs = {}

    def slot(k):
        return docs.setdefault(k, {
            "gross": D(0), "header_tax": D(0), "date": None, "gl": {},
            "rates": collections.defaultdict(set), "items": {},
            "is_rcm": False, "rcm_tax": D(0), "roundoff": D(0),
            "nlines": 0, "source": None})

    print("SAGE  headers", flush=True)
    for h in P.load_headers():
        k = (P.s(h["vendor"]), P.base_invoice(h["invoice"]))
        d = slot(k)
        d["gross"] += q2(h["gross"])
        d["header_tax"] += q2(h["header_tax"])
        d["source"] = "ap_direct"
        dt = yyyymmdd(str(h["bill_date"]))
        # A document split across *N parts keeps the EARLIEST part's date -
        # the same part the loader consolidates onto.
        if dt and (d["date"] is None or dt < d["date"]):
            d["date"] = dt
    ap_n = len(docs)
    print("  %d AP-direct documents" % ap_n, flush=True)

    print("SAGE  goods headers", flush=True)
    # Pulled ONCE and kept: the invhseq -> document map below needs the same
    # 18k rows, and this query crosses an ssh hop.
    goods_heads = P._staging_rows(P.SQL_GOODS_HDR % (P.DATE_FROM, P.DATE_TO),
                                  P.GOODS_HDR_COLS)
    for h in goods_heads:
        k = (P.s(h["vendor"]), P.base_invoice(h["invoice_raw"]))
        d = slot(k)
        d["gross"] += q2(h["doc_total"])
        d["header_tax"] += q2(h["tax_total"])
        d["source"] = "goods" if d["source"] in (None, "goods") else "mixed"
        dt = yyyymmdd(str(h["bill_date"]))
        if dt and (d["date"] is None or dt < d["date"]):
            d["date"] = dt
    print("  %d documents after goods" % len(docs), flush=True)

    print("SAGE  distribution lines (sage_ap_dist)", flush=True)
    for r in P._staging_rows(
            "SELECT vendor_code, inv_number_raw, cntline, gl_account, amt_dist,"
            " ratetax1, ratetax2 FROM sage_ap_dist",
            ["vendor", "invoice", "cntline", "gl", "amount", "r1", "r2"]):
        k = (P.s(r["vendor"]), P.base_invoice(r["invoice"]))
        if k not in docs:
            continue                       # outside the window
        d = docs[k]
        gl = P.s(r["gl"])
        amt = q2(r["amount"])
        d["gl"][gl] = d["gl"].get(gl, D(0)) + amt
        d["rates"][P.s(r["cntline"])].add(q2(dec(r["r1"]) + dec(r["r2"])))
        if gl.startswith(RCM_HEAD):
            d["is_rcm"] = True
            d["rcm_tax"] += amt
        if gl.startswith(ROUNDOFF_HEAD):
            d["roundoff"] += amt
        if not gl.startswith(NEVER_TAXABLE):
            d["nlines"] += 1

    print("SAGE  goods lines (qty / unit cost)", flush=True)
    hseq = {h["invhseq"]: (P.s(h["vendor"]), P.base_invoice(h["invoice_raw"]))
            for h in goods_heads}
    for r in P._staging_rows(P.SQL_GOODS_LINES, P.GOODS_LINE_COLS):
        k = hseq.get(r["invhseq"])
        if not k or k not in docs:
            continue
        # Keyed on the item exactly as the poster stamps it into metaData
        # (sageItem = item_raw or item). A document may bill the same item on
        # two lines, so quantity and extended value accumulate; unit cost is
        # only comparable when they agree, and is dropped when they do not.
        it = P.s(r["item_raw"]) or P.s(r["item"])
        cur = docs[k]["items"].setdefault(
            it, {"qty": D(0), "ext": D(0), "unitcost": None, "mixed": False})
        cur["qty"] += q2(r["qty"])
        cur["ext"] += q2(r["ext"])
        uc = q2(r["unitcost"])
        if cur["unitcost"] is None:
            cur["unitcost"] = uc
        elif cur["unitcost"] != uc:
            cur["mixed"] = True

    # The RCM tax head is booked as an equal-and-opposite pair; take the
    # magnitude, not the residual.
    for d in docs.values():
        d["rcm_tax"] = abs(d["rcm_tax"])
        d["expected"] = taxable_heads(d)
        d["taxable"] = sum(d["expected"].values(), D(0))
    if limit:
        docs = dict(list(docs.items())[:limit])
    return docs


# ----------------------------------------------------------- SMEASSIST SIDE
def sme_side(org):
    print("SMEASSIST  bills", flush=True)
    bills = {}
    for r in q("SELECT id, billNumber, billAmount, taxableAmount, gstAmount, "
               "COALESCE(roundOffAmount,0), billType, billStatus, billDate "
               "FROM bill WHERE organisationId='%s' AND isDeleted+0=0" % org):
        if len(r) != 9 or r[7] != "ACTIVE":
            continue
        bills[r[0]] = {"number": r[1], "amount": q2(r[2]), "taxable": q2(r[3]),
                       "gst": q2(r[4]), "roundoff": q2(r[5]), "type": r[6],
                       "date": yyyymmdd(r[8])}
    print("  %d ACTIVE bills" % len(bills), flush=True)

    print("SMEASSIST  bill lines", flush=True)
    lines = collections.defaultdict(list)
    for r in q(
            "SELECT li.billId, "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(li.metaData,'$.sageGlAccount')),''), "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(li.metaData,'$.sageLine')),''), "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(li.metaData,'$.sageItem')),''), "
            "li.gstPercentage, li.taxableAmount, li.quantity, li.unitPrice, "
            "COALESCE(li.financeAccountId,''), "
            # isRcmEnabled is bit(1). Selected bare it arrives as the raw byte
            # \x00 / \x01, never '0'/'1', so every string test reads False and
            # the check reports every RCM line as wrong. +0 forces it numeric.
            "COALESCE(li.isRcmEnabled+0,0) "
            "FROM billLineItem li JOIN bill b ON b.id=li.billId "
            "WHERE li.organisationId='%s' AND li.isDeleted+0=0 "
            "AND b.isDeleted+0=0 AND b.billStatus='ACTIVE'" % org):
        if len(r) != 10:
            continue
        lines[r[0]].append({"gl": r[1], "lineref": r[2], "item": r[3],
                            "rate": q2(r[4]), "taxable": q2(r[5]),
                            "qty": dec(r[6]), "unitprice": dec(r[7]),
                            "ledger": r[8], "rcm": r[9]})
    print("  %d lines on %d bills"
          % (sum(len(v) for v in lines.values()), len(lines)), flush=True)

    print("SMEASSIST  voucher legs", flush=True)
    residual = {r[0]: q2(r[1]) for r in q(
        "SELECT ve.referenceId, ROUND(SUM(CASE WHEN ve.transactionType='DEBIT' "
        "THEN ve.amount ELSE -ve.amount END),4) FROM voucherEntry ve "
        "JOIN bill b ON b.id=ve.referenceId WHERE b.organisationId='%s' "
        "AND ve.isDeleted+0=0 AND b.isDeleted+0=0 AND b.billStatus='ACTIVE' "
        "GROUP BY 1 HAVING ABS(SUM(CASE WHEN ve.transactionType='DEBIT' "
        "THEN ve.amount ELSE -ve.amount END))>0.01" % org) if len(r) == 2}

    vnum = {r[0]: (r[1], r[2]) for r in q(
        "SELECT DISTINCT ve.referenceId, ve.voucherNumber, b.billNumber "
        "FROM voucherEntry ve JOIN bill b ON b.id=ve.referenceId "
        "WHERE b.organisationId='%s' AND ve.isDeleted+0=0 AND b.isDeleted+0=0 "
        "AND b.billStatus='ACTIVE' AND ve.voucherNumber<>b.billNumber" % org)
        if len(r) == 3}
    return bills, lines, residual, vnum


def posted_map():
    """-> {(vendor, base invoice): [billId, ...]}"""
    out = collections.defaultdict(list)
    with open(P.POSTED_LOG) as fh:
        for ln in fh:
            parts = ln.strip().split("||")
            if len(parts) >= 2 and parts[1].isdigit():
                v, _, inv = parts[0].partition("|")
                out[(P.s(v), P.base_invoice(inv))].append(parts[1])
    return out


# --------------------------------------------------------------- COMPARISON
def compare(docs, bills, lines, residual, vnum, posted, rep, wanted):
    def on(check):
        return not wanted or check in wanted

    compared = clean = 0
    seen_bill_ids = set()
    gl_to_ledger = collections.defaultdict(set)

    for key, d in sorted(docs.items()):
        doc = "|".join(key)
        ids = [b for b in posted.get(key, []) if b in bills]
        if not ids:
            continue
        if len(ids) > 1 and on("duplicate"):
            rep.add("duplicate", doc, "billId", None, None,
                    note="live bills: %s" % ", ".join(ids))
        bid = ids[0]
        b = bills[bid]
        seen_bill_ids.update(ids)
        ls = lines.get(bid, [])
        compared += 1
        before = len(rep.rows)

        # Tax cannot be exact and must not be asserted as such. Sage truncates
        # each authority separately while the server derives gstAmount as
        # SUM(line taxable x line rate), so the two disagree by up to a paisa
        # per authority per line. Same tolerance readback_drift uses.
        tol = max(D("0.05"), D("0.01") * max(len(ls), d["nlines"]) * 2)

        # --- taxable. Exact: both sides are a plain sum of stated amounts,
        # with no rounding step anywhere between them.
        if on("taxable") and b["taxable"] != d["taxable"]:
            rep.add("taxable", doc, "taxableAmount", float(d["taxable"]),
                    float(b["taxable"]), bid)

        if d["is_rcm"]:
            # Sage's AMTINVCHC on a reverse-charge document IS the taxable and
            # AMTTAXHC is zero; SMEAssist grosses up to taxable + self-assessed
            # GST. Comparing billAmount here reports every RCM bill as wrong.
            if on("gst_amount") and abs(b["gst"] - d["rcm_tax"]) > tol:
                gap = abs(b["gst"] - d["rcm_tax"])
                # Under a rupee this is the recorded derive-and-snap decision
                # showing through: the rate is snapped to a legal slab and the
                # tax recomputed from it, while Sage's 1L8TX pair carries an
                # exact rupee figure at an implied 4.999x%. Reported, because
                # it IS a value difference, but not as a defect.
                known = gap <= D("1.00")
                rep.add("gst_amount", doc, "gstAmount", float(d["rcm_tax"]),
                        float(b["gst"]), bid,
                        severity="low" if known else "high",
                        note="RCM: Sage 1L8TX booking vs the self-assessed "
                             "figure recomputed from the snapped rate"
                             + (" (sub-rupee: the recorded snap consequence)"
                                if known else ""))
        else:
            if on("gst_amount") and abs(b["gst"] - d["header_tax"]) > tol:
                rep.add("gst_amount", doc, "gstAmount", float(d["header_tax"]),
                        float(b["gst"]), bid, note="tolerance %s" % tol)
            if on("bill_amount") and abs(b["amount"] - d["gross"]) > tol:
                delta = b["amount"] - d["gross"]
                # A sub-rupee gap that the round-off accounts for is the server
                # discarding the 4E1M016 line and recomputing nearest-rupee on
                # its own - a known, separate, low-severity finding. Anything
                # larger is real money on the wrong side.
                if abs(delta) <= D("1.00") and d["roundoff"]:
                    if on("roundoff"):
                        rep.add("roundoff", doc, "roundOffAmount",
                                float(d["roundoff"]), float(b["roundoff"]), bid,
                                note="server discards the Sage round-off line "
                                     "and recomputes nearest-rupee; vendor is "
                                     "credited %s" % P.q2(delta))
                else:
                    rep.add("bill_amount", doc, "billAmount", float(d["gross"]),
                            float(b["amount"]), bid, note="tolerance %s" % tol)

        # --- the server's own identity. It substitutes its own roundOffAmount,
        # so the identity must include it or every rounded bill reports.
        if on("identity") and abs(b["amount"] - (b["taxable"] + b["gst"]
                                                 + b["roundoff"])) > D("0.01"):
            rep.add("identity", doc, "billAmount", None, float(b["amount"]), bid,
                    note="taxable %s + gst %s + roundOff %s = %s"
                         % (b["taxable"], b["gst"], b["roundoff"],
                            P.q2(b["taxable"] + b["gst"] + b["roundoff"])),
                    delta=float(P.q2(b["amount"] - b["taxable"] - b["gst"]
                                     - b["roundoff"])))

        if on("bill_date") and d["date"] and b["date"] and b["date"] != d["date"]:
            rep.add("bill_date", doc, "billDate", d["date"], b["date"], bid,
                    delta=0)

        # --- lines
        if ls:
            lsum = sum((l["taxable"] for l in ls), D(0))
            if on("line_sum") and P.q2(lsum) != b["taxable"]:
                rep.add("line_sum", doc, "SUM(line.taxableAmount)",
                        float(b["taxable"]), float(P.q2(lsum)), bid,
                        note="header taxableAmount vs its own lines")

            per_gl = collections.defaultdict(lambda: D(0))
            for l in ls:
                if l["gl"]:
                    per_gl[l["gl"]] += l["taxable"]
                    if l["ledger"]:
                        gl_to_ledger[l["gl"]].add(l["ledger"])
                if on("null_ledger") and not l["ledger"]:
                    rep.add("null_ledger", doc, "financeAccountId", None, None,
                            bid, note="line on GL %s, taxable %s"
                                      % (l["gl"] or "?", l["taxable"]))
                if on("illegal_rate") and l["rate"] not in P.LEGAL_SLABS:
                    rep.add("illegal_rate", doc, "gstPercentage", None,
                            float(l["rate"]), bid,
                            note="not a legal GST slab")
                if on("rcm_flag"):
                    got = str(l["rcm"]).strip() == "1"
                    if got != d["is_rcm"]:
                        rep.add("rcm_flag", doc, "isRcmEnabled",
                                d["is_rcm"], got, bid, delta=0,
                                note="Sage %s a 1L8TX leg"
                                     % ("books" if d["is_rcm"] else "books no"))
                # Stated rate, per line. Sage may state two rates for one
                # cntline across *N parts; the posted rate must be one of them.
                if on("gst_rate") and l["lineref"] and l["lineref"] in d["rates"]:
                    want = d["rates"][l["lineref"]]
                    if want and l["rate"] not in want and not d["is_rcm"]:
                        rep.add("gst_rate", doc, "gstPercentage",
                                float(sorted(want)[0]), float(l["rate"]), bid,
                                note="Sage line %s states %s"
                                     % (l["lineref"],
                                        "/".join(str(x) for x in sorted(want))))

            # --- amount on each Sage GL account. The strongest value check:
            # totals can agree while money sits on the wrong head.
            if on("gl_amount"):
                for gl, amt in sorted(d["expected"].items()):
                    if amt == 0:
                        # An account that nets to zero across a +/- pair (35
                        # documents do) correctly gains no line.
                        continue
                    got = per_gl.get(gl)
                    if got is None:
                        rep.add("gl_amount", doc, "GL %s" % gl, float(amt), 0.0,
                                bid, note="Sage books this head; no line "
                                          "carries it")
                    elif P.q2(got) != P.q2(amt):
                        rep.add("gl_amount", doc, "GL %s" % gl, float(amt),
                                float(P.q2(got)), bid)
                for gl, amt in sorted(per_gl.items()):
                    if gl and gl not in d["expected"]:
                        rep.add("gl_amount", doc, "GL %s" % gl, 0.0,
                                float(P.q2(amt)), bid,
                                note="posted on a head Sage does not book")

            # --- goods: quantity and unit price, per ITEM
            per_item = collections.defaultdict(lambda: {"qty": D(0), "px": set()})
            for l in ls:
                if l["item"]:
                    per_item[l["item"]]["qty"] += l["qty"]
                    per_item[l["item"]]["px"].add(P.q2(l["unitprice"]))
            for item, got in sorted(per_item.items()):
                it = d["items"].get(item)
                if not it:
                    continue
                if on("quantity") and P.q2(got["qty"]) != P.q2(it["qty"]):
                    rep.add("quantity", doc, "quantity %s" % item,
                            float(P.q2(it["qty"])), float(P.q2(got["qty"])), bid)
                # A unit price is a rate, so it does not add up across lines:
                # every distinct value posted for the item has to be Sage's.
                # Skipped where Sage itself bills the item at two rates.
                if on("unit_price") and not it["mixed"] \
                        and it["unitcost"] is not None \
                        and got["px"] != {P.q2(it["unitcost"])}:
                    rep.add("unit_price", doc, "unitPrice %s" % item,
                            float(it["unitcost"]),
                            float(sorted(got["px"])[0]), bid,
                            note="posted at %s" % "/".join(
                                str(x) for x in sorted(got["px"])))

        if on("voucher_balance") and bid in residual:
            rep.add("voucher_balance", doc, "DEBIT - CREDIT", 0.0,
                    float(residual[bid]), bid)
        if on("voucher_number") and bid in vnum:
            rep.add("voucher_number", doc, "voucherNumber", vnum[bid][1],
                    vnum[bid][0], bid, delta=0)

        if len(rep.rows) == before:
            clean += 1

    # One Sage GL account fragmented across two ledger heads. Org-wide, not
    # per document - it only shows up when the whole population is in view.
    if on("gl_split"):
        for gl, leds in sorted(gl_to_ledger.items()):
            if len(leds) > 1:
                rep.add("gl_split", "(org-wide)", "GL %s" % gl, 1, len(leds),
                        note="ledger ids: %s" % ", ".join(sorted(leds)))
    return compared, clean, seen_bill_ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--check", help="comma-separated subset of the checks")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    wanted = set(x.strip() for x in args.check.split(",")) if args.check else None
    if wanted:
        unknown = wanted - set(CHECKS)
        if unknown:
            raise SystemExit("unknown check(s): %s\nknown: %s"
                             % (", ".join(sorted(unknown)),
                                ", ".join(sorted(CHECKS))))

    org = P.ORG_ID
    docs = sage_side(args.limit)
    bills, lines, residual, vnum = sme_side(org)
    posted = posted_map()
    rep = Report()

    compared, clean, seen = compare(docs, bills, lines, residual, vnum,
                                    posted, rep, wanted)

    missing = [ "|".join(k) for k in docs
                if not [b for b in posted.get(k, []) if b in bills] ]
    extra = [] if args.limit else [
        {"billId": b, "billNumber": bills[b]["number"],
         "billAmount": float(bills[b]["amount"])}
        for b in bills if b not in seen]

    AMOUNT_CHECKS = ("bill_amount", "taxable", "gst_amount", "gl_amount",
                     "roundoff")
    worst = collections.defaultdict(float)
    for r in rep.rows:
        if r["check"] in AMOUNT_CHECKS:
            worst[r["doc"]] = max(worst[r["doc"]], abs(r.get("delta") or 0))
    at_risk = sum(worst.values())
    by_check_value = {}
    for c in AMOUNT_CHECKS:
        rows = [r for r in rep.rows if r["check"] == c]
        if rows:
            by_check_value[c] = {
                "rows": len(rows),
                "documents": len({r["doc"] for r in rows}),
                "abs_value": round(sum(abs(r.get("delta") or 0)
                                       for r in rows), 2)}
    src = collections.Counter(d["source"] for d in docs.values())
    summary = {
        "sage_documents": len(docs),
        "sage_ap_direct": src.get("ap_direct", 0),
        "sage_goods": src.get("goods", 0),
        "sage_ap_direct_and_goods": src.get("mixed", 0),
        "smeassist_bills_active": len(bills),
        "compared": compared,
        "documents_clean": clean,
        "documents_with_a_mismatch": len(rep.docs - {"(org-wide)"}),
        "mismatch_rows": len(rep.rows),
        "in_sage_not_in_smeassist": len(missing),
        "in_smeassist_not_in_sage": ("not assessed under --limit" if args.limit
                                     else len(extra)),
        # The largest amount difference per document, summed - never the sum
        # of the rows: one lost document reports under bill_amount, taxable AND
        # gl_amount, and adding those triples the same rupees.
        "value_at_risk": round(float(at_risk), 2),
        "by_check": dict(rep.by_check.most_common()),
        "value_by_check": by_check_value,
        "by_severity": dict(collections.Counter(
            r["severity"] for r in rep.rows).most_common()),
    }
    order = {"high": 0, "medium": 1, "low": 2}
    rep.rows.sort(key=lambda r: (order.get(r["severity"], 3),
                                 -abs(r.get("delta") or 0), r["doc"]))
    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"from": P.DATE_FROM, "to": P.DATE_TO},
        "organisationId": org,
        "checks": {k: {"what": v[0], "severity": v[1]} for k, v in CHECKS.items()},
        "checks_run": sorted(wanted) if wanted else sorted(CHECKS),
        "summary": summary,
        "mismatches": rep.rows,
        "in_sage_not_in_smeassist": missing[:5000],
        "in_smeassist_not_in_sage": extra[:5000],
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)

    w = max(len(k) for k in summary)
    print("\n" + "=" * (w + 22))
    print("SAGE  vs  SMEASSIST   value mismatches".center(w + 22))
    print("=" * (w + 22))
    for k, v in summary.items():
        if isinstance(v, dict):
            continue
        flag = "   <-- INVESTIGATE" if isinstance(v, int) and v and k in (
            "documents_with_a_mismatch", "in_smeassist_not_in_sage") else ""
        print("  %-*s %12s%s" % (w, k, format(v, ",") if isinstance(v, int)
                                 else v, flag))
    if rep.by_check:
        # Severity is per row, so print the mix actually found, not the check's
        # default: 588 of the 595 gst_amount rows are the sub-rupee RCM snap.
        sev = collections.defaultdict(collections.Counter)
        for r in rep.rows:
            sev[r["check"]][r["severity"]] += 1
        print("-" * (w + 22))
        for k, n in rep.by_check.most_common():
            mix = " ".join("%s:%d" % (x.upper(), c) for x, c
                           in sorted(sev[k].items()))
            print("  %-*s %12s   %s" % (w, k, format(n, ","), mix))
    print("=" * (w + 22))
    if args.detail:
        print("\nfirst 40 mismatches:")
        for r in rep.rows[:40]:
            print("  %-14s %-34s %-22s sage=%14s sme=%14s"
                  % (r["check"], r["doc"][:34], str(r["field"])[:22],
                     r.get("sage"), r.get("smeassist")))
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
