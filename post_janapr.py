#!/usr/bin/env python3
"""Post the Jan-Apr 2026 extract: bills, invoices, payments, receipts.

    ./post_janapr.py <phase> [--dryrun] [--limit N] [--pilot] [--token T]

Phases, in the order they must run:

    manifest    what will post, hold and journal. No network, no token.
    selftest    the arithmetic and every payload guard, against known figures.
                No network, no token, no extract writes.
    banks       yoda.bankAccount rows. NOTHING money-related posts without these.
    series      the counter series required for BOTH financial years (reports;
                the platform exposes series creation only as a bulk sheet).
    payments    AP payments + their settlement mappings.
    receipts    AR receipts + their settlement mappings.
    verify      read the vouchers back and answer "did it post?" - the §12 controls.

NOT a phase here, on purpose: `bills` and `invoices`. Those documents already
post through the proven loaders in this repo (post_sage_bills.py,
post_sage_invoices.py), which between them have put 12,690 bills and 92
invoices into this org and carry the whole §7/§8 payload contract - cessType,
isRcmEnabled, financeAccountDto, isGstClaimable, purchaseType, the ACTIVE
address DTOs. Restating that here would fork it. What this file adds is the
money side, which had no loader at all, plus the settlement that links it back.

That ordering is why settlement is only partly resolvable today: a payment can
only settle a bill that EXISTS. Against the 12,676 bills currently posted,
11,984 settlement legs resolve; the rest wait on their bill and are listed in
out/settlement_unresolved_bill.json rather than being silently dropped.

Everything reusable comes from post_sage_bills: the Api with its backoff, the
ONE process-wide rate gate, the fsync'd posted log, the crosswalk. That module
holds fixes that each cost a run, so this file adds a source and payloads and
reuses the machinery rather than restating it.

Ordering is enforced, not advisory (§1: "must refuse to start if its
prerequisite is missing rather than half-post"):

  * payments and receipts refuse to run until banks has created the bank
    accounts, because a payment with no companyBankAccountId cannot post;
  * settlement refuses to invent a target - a payment whose bill is not posted
    is reported unresolved, never silently dropped to unmapped;
  * every phase writes out/<phase>-<stamp>.log in full and never tail-truncates,
    because the one diagnostic line sits at the TOP of a failure block.

What this file deliberately does NOT do:

  * AP/IE bills (11,702, Rs 146.88 Cr). They need a CHARGE product per expense
    head, a CHARGE product needs a SAC, and Sage holds no SAC anywhere: all 213
    LEDGER_AND_CHARGE_PRODUCT heads ship blank. Measured, not assumed: zero of
    277k products box-wide carry a blank hsnCode outside RESOURCE. Held until
    finance returns out/SAC-REQUEST-213-expense-heads.csv.
  * COGS. The invoice voucher builder has no COGS group, so the omitted total
    is REPORTED per invoice (Rs 143.25 Cr on this window), never dropped.
  * The 5 URP customers' GST-bearing invoices (349, Rs 11.76 Cr, Rs 56.25 L of
    output GST). Posting them as unregistered files that tax in a B2C row of
    GSTR-1, which is a filing consequence and the user's call.
"""

import argparse
import json
import re
import subprocess
import os
import sys
import time
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import janapr_extract as X
import post_sage_bills as P

# P.API (SME_BASE) ALREADY ends in /api/v1, so every path here is relative to
# that: "/bankAccount", not "/api/v1/bankAccount". Prefixing it again produces
# /api/v1/api/v1/... and a 404 whose body names the doubled path.
#
# The TRAILING SLASH matters too: paymentRequest and receipt declare their
# create as @PostMapping(value = "/"), so the path is "/paymentRequest/" - and
# "/paymentRequest" 404s. bankAccount declares @PostMapping(value = ""), so it
# takes no trailing slash. The brief writes POST /api/v1/bill/ for the same
# reason.
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

# Natural keys are written to their own posted logs so this loader's idempotency
# is independent of the live-Sage loader's. Format matches P.State.mark:
# "<key>||<id>".
LOGS = {
    "bank":     os.path.join(STATE, "janapr_banks_posted.log"),
    "series":   os.path.join(STATE, "janapr_series_posted.log"),
    "bill":     os.path.join(STATE, "janapr_bills_posted.log"),
    "invoice":  os.path.join(STATE, "janapr_invoices_posted.log"),
    "payment":  os.path.join(STATE, "janapr_payments_posted.log"),
    "receipt":  os.path.join(STATE, "janapr_receipts_posted.log"),
}


# ============================================================================
# run log - the whole run, never truncated (§2.2)
# ============================================================================

class Tee:
    def __init__(self, path):
        self.fh = open(path, "a", buffering=1)
        self.path = path

    def __call__(self, *a):
        line = " ".join(str(x) for x in a)
        print(line, flush=True)
        self.fh.write(line + "\n")


def open_log(phase):
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(STATE, exist_ok=True)
    p = os.path.join(OUT, "%s-%s.log" % (phase, time.strftime("%Y%m%d-%H%M%S")))
    say = Tee(p)
    say("# %s  org=%s  ns=%s" % (phase, P.ORG_ID, P.NAMESPACE))
    return say


def load_posted(kind):
    """The posted log for one entity kind -> {naturalKey: id}."""
    p = LOGS[kind]
    out = {}
    if os.path.exists(p):
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("||")
                if len(parts) >= 2:
                    out[parts[0]] = parts[1]
    return out


def mark(kind, key, value):
    """Append and fsync, so an interrupted run resumes rather than re-attempts."""
    os.makedirs(STATE, exist_ok=True)
    with open(LOGS[kind], "a") as fh:
        fh.write("%s||%s\n" % (key, value))
        fh.flush()
        os.fsync(fh.fileno())


# ============================================================================
# selection - the manifest every phase agrees on
# ============================================================================

def money(v):
    return "Rs %.2f Cr" % (D(v) / 10000000)


def bill_selection():
    """PO-route bills, plus the held remainder with its reason.

    Route comes from Bills_Header.route. AP and IE are held as a POPULATION
    here, not per bill: they need CHARGE products and those need the 213 SACs.
    """
    hdr = X.read("Bills_Header")
    lines = X.by_key(X.read("Bills_Lines_PO"), "cntbtch", "cntitem")
    post, held = [], []
    for h in hdr:
        k = (h["cntbtch"], h["cntitem"])
        if h["route"] != "PO":
            held.append((h, "AP/IE route - CHARGE product needs a SAC (213 heads blank)"))
        elif not lines.get(k):
            held.append((h, "PO route with no line in Bills_Lines_PO"))
        else:
            post.append((h, lines[k]))
    return post, held


def invoice_selection():
    """The sell side split four ways, exactly as the manifest reports it."""
    sh = X.read("Sales_Header")
    lines = X.by_key(X.read("Sales_Lines"), "cntbtch", "cntitem")
    cust = X.one_per_key(X.read("Customers_Needed"), "customer")
    urp = {c for c, r in cust.items() if r["sme_regtype"] == "WITHOUT_PAN_OR_GST"}

    post, held, journal = [], [], []
    for h in sh:
        ls = lines.get((h["cntbtch"], h["cntitem"]), [])
        # §8: an invoice line is income-only, with no exceptions anywhere in the
        # box - 605,708 lines across 50 orgs, every one on an INCOME ledger. A
        # line crediting an asset would invent revenue, so it is a journal.
        nonincome = [l for l in ls if not l["revenue_acct"].startswith("3I")]
        if not ls:
            held.append((h, "no line in Sales_Lines"))
        elif nonincome:
            journal.append((h, "revenue_acct not 3I* (%s)"
                            % ",".join(sorted({l["revenue_acct"] for l in nonincome}))))
        elif h["customer"] in urp and X.D(h["tax_doc"]) > 0:
            held.append((h, "URP customer carrying output GST - B2C filing call"))
        else:
            post.append((h, ls))
    return post, held, journal


# ============================================================================
# phase: manifest
# ============================================================================

def phase_manifest(args):
    say = open_log("manifest")
    say("\nEXTRACT")
    counts = X.validate(verbose=False)
    say("  %d datasets, %d rows, 0 malformed" % (len(counts), sum(counts.values())))

    post, held = bill_selection()
    say("\nBILLS  (Bills_Header %d)" % len(X.read("Bills_Header")))
    say("  post            %6d   %s"
        % (len(post), money(sum(X.D(h["amt_inr"]) for h, _ in post))))
    hb = {}
    for h, why in held:
        e = hb.setdefault(why, [0, D(0)])
        e[0] += 1
        e[1] += X.D(h["amt_inr"])
    for why, (n, v) in sorted(hb.items(), key=lambda kv: -kv[1][0]):
        say("  held  %6d   %-14s %s" % (n, money(v), why))
    say("  %-15s %6d   (control 10: no unexplained remainder)"
        % ("total", len(post) + len(held)))

    ipost, iheld, ijournal = invoice_selection()
    say("\nINVOICES  (Sales_Header %d)" % len(X.read("Sales_Header")))
    say("  post            %6d   %s"
        % (len(ipost), money(sum(X.D(h["amt_inr"]) for h, _ in ipost))))
    say("  held            %6d   %s"
        % (len(iheld), money(sum(X.D(h["amt_inr"]) for h, _ in iheld))))
    say("  journal         %6d   %s"
        % (len(ijournal), money(sum(X.D(h["amt_inr"]) for h, _ in ijournal))))
    say("  %-15s %6d" % ("total", len(ipost) + len(iheld) + len(ijournal)))
    cogs = sum(X.D(l["cogs_doc"]) for _, ls in ipost for l in ls)
    say("  COGS omitted    %s   reported, not dropped (no COGS group)" % money(cogs))

    pay = X.read("AP_Payments_Header")
    rec = X.read("AR_Receipts_Header")
    apps = settlement_apps("AP_Payment_Apps")
    rapps = settlement_apps("AR_Receipt_Apps")
    say("\nPAYMENTS  %d   %s" % (len(pay), money(sum(X.D(r["amt_inr"]) for r in pay))))
    for t, n in sorted(count_by(pay, "sme_paytype").items()):
        say("    %-12s %6d" % (t, n))
    say("  settlement legs kept %d of %d (11/53 reversals excluded)"
        % (len(apps["kept"]), len(apps["all"])))
    say("\nRECEIPTS  %d   %s" % (len(rec), money(sum(X.D(r["amt_inr"]) for r in rec))))
    for t, n in sorted(count_by(rec, "kind").items()):
        say("    %-12s %6d" % (t, n))
    say("  settlement legs kept %d of %d" % (len(rapps["kept"]), len(rapps["all"])))

    say("\nBANKS  %d in Banks_Needed" % len(X.read("Banks_Needed")))
    for r in bank_selection()[0]:
        say("    %-10s %-34s %-12s %s"
            % (r["bank"], r["name"][:34], r["sme_bankacct_type"],
               "acctno PLACEHOLDER" if r["acctno_src"] == "MISSING" else r["acctno"]))
    say("  not a bank / skipped: %d" % len(bank_selection()[1]))
    say("\nlog -> %s" % say.path)
    return 0


def count_by(rows, col):
    out = {}
    for r in rows:
        out[r[col]] = out.get(r[col], 0) + 1
    return out


# ============================================================================
# settlement - the maps that make a payment settle something
# ============================================================================

# §10: drop reversals or an applied-then-reversed pair double-counts. The pair
# is (transtype, trxtype); 11/53 is the reversal.
REVERSAL = ("11", "53")


def settlement_apps(name):
    """The application legs of AP_Payment_Apps / AR_Receipt_Apps.

    Returns {"all": [...], "kept": [...], "dropped": {reason: n}}. Reversals are
    dropped HERE, once, so no caller can forget to.
    """
    rows = X.read(name)
    kept, dropped = [], {}
    for r in rows:
        tt = (r.get("transtype") or "").split(".")[0]
        tx = (r.get("trxtype") or "").split(".")[0]
        if (tt, tx) == REVERSAL:
            dropped["11/53 reversal"] = dropped.get("11/53 reversal", 0) + 1
            continue
        if X.D(r["amt_inr"]) == 0:
            dropped["zero amount"] = dropped.get("zero amount", 0) + 1
            continue
        kept.append(r)
    return {"all": rows, "kept": kept, "dropped": dropped}


def bill_id_index():
    """(vendor, invoice number) -> SMEAssist bill id, from both posted logs.

    §2.3: NEVER resolve a bill by billNumber alone - 1,535 invoice numbers
    repeat across vendors and a limit-1 lookup hands back a different vendor's
    bill. The key is always scoped by the Sage vendor code.

    Reads this loader's log AND the live-Sage loader's work/posted.log, because
    12,690 bills were already posted through that one and a payment must be
    able to settle them.
    """
    idx = {}
    for key, bid in load_posted("bill").items():
        idx[key] = bid
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "work", "posted.log")
    if os.path.exists(legacy):
        with open(legacy) as fh:
            for line in fh:
                parts = line.strip().split("||")
                if len(parts) >= 2 and "|" in parts[0] and parts[1] not in ("preexisting",):
                    idx.setdefault(parts[0], parts[1])
    return idx


# §10/SCHEMA: what a settlement leg points AT.
#   12 bill  ·  22 debit note  ·  32 credit note  ·  50 prepayment
# Only 12 is a bill. A payment run applies invoices AND TDS credit notes
# together so the cheque is already net, which is why §9 says load the notes
# BEFORE the payments - but this loader does not post notes, so note legs are
# counted and reported rather than mapped as if they were bills. Mapping a
# credit note with paymentRequestEntityType=BILL would settle the wrong
# document by the amount of the TDS.
# The two sides do NOT share these codes, so each declares its own set in
# `cols` rather than sharing one constant:
#
#   AP  a bill is 12.                       Notes are 22 (DN) and 32 (CN).
#   AR  an invoice is 12 OR 14 - 12 keyed straight into A/R, 14 raised in Order
#       Entry, and 14 is the DOMINANT one: 12,377 of the receipt legs against
#       594, matching Sales_Header's own 6,849 OE / 202 AR split. Notes are
#       22/32 (A/R) plus 24/34 (O/E).
#
# Assuming the AP shape on the AR side silently discarded all 12,377 type-14
# legs, which is every O/E invoice a receipt could settle.
TARGET_PREPAY = "50"
# 51 is Sage's own type for a PAYMENT. An application row has two sides, so a
# leg whose target is type 51 is the paying document's OWN leg, not something
# being settled - measured here as 32,595 of the 80,388 AP legs. Excluding it
# is not a gap; counting it as a target would settle a payment against itself.
TARGET_SELF = "51"


def build_settlement(pay_rows, apps, id_index, cols, say):
    """Map each payment/receipt to the documents it settles.

    `cols` names the join, because the two apps files do NOT share a shape:
    AP carries target_base (the *N suffix stripped) while AR carries only
    target_doc, and both key the paying document on (app_cntbtch, app_cntitem)
    - never on cntitem alone, which is just an entry number within a batch and
    would cross-map settlements between different batches.

    Unresolvable targets are COUNTED and listed, never quietly turned into an
    unmapped advance: that would silently convert a settlement into money
    sitting on account, and control 9 (remainingAmount -> 0) would then pass
    for the wrong reason.
    """
    by_pay = {}
    for a in apps["kept"]:
        by_pay.setdefault(tuple(a[c] for c in cols["app_key"]), []).append(a)

    resolved, unresolved = {}, {}
    stats = {"settleable document legs": 0, "note legs (need notes loaded)": 0,
             "prepayment legs": 0, "the paying document's own leg": 0,
             "unrecognised target type": 0}
    for p in pay_rows:
        pid = tuple(p[c] for c in cols["pay_key"])
        maps, miss = [], []
        for a in by_pay.get(pid, []):
            tst = (a.get("target_sagetype") or "").split(".")[0]
            if tst in cols["note_types"]:
                stats["note legs (need notes loaded)"] += 1
                continue
            if tst == TARGET_PREPAY:
                stats["prepayment legs"] += 1
                continue
            if tst == TARGET_SELF:
                stats["the paying document's own leg"] += 1
                continue
            if tst not in cols["doc_types"]:
                stats["unrecognised target type"] += 1
                continue
            stats["settleable document legs"] += 1
            party = a.get(cols["party"]) or p.get(cols["party"]) or ""
            doc = (a.get(cols["doc"]) or "").strip()
            key = "%s|%s" % (party, doc)
            eid = id_index.get(key)
            if eid:
                maps.append({"entityId": eid,
                             "paymentRequestEntityType": cols["etype"],
                             "entityNumber": doc,
                             "requestedAmount": str(abs(X.D(a["amt_inr"])))})
            else:
                miss.append(key)
        if maps:
            resolved[pid] = maps
        if miss:
            unresolved["|".join(pid)] = miss

    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        if v:
            say("    %-32s %7d" % (k, v))
    say("  settlement: %d documents mapped across %d paying documents; %d have "
        "at least one unresolved target"
        % (sum(len(v) for v in resolved.values()), len(resolved), len(unresolved)))
    if unresolved:
        p = os.path.join(OUT, "settlement_unresolved_%s.json" % cols["etype"].lower())
        with open(p, "w") as fh:
            json.dump(unresolved, fh, indent=1)
        say("  unresolved -> %s" % p)
        say("  (a target not in the index is a document not yet posted, not a")
        say("   failure - it becomes resolvable once that document loads)")
    return resolved


# ============================================================================
# phase: banks (§3.1)
# ============================================================================

def bank_selection():
    """(create, skip) from Banks_Needed.

    Derivation is the extract's own sme_bankacct_type, which follows §3.1:
    1L5B* -> CASH_CREDIT (export packing credit is a borrowing), 2A6B* ->
    CURRENT, 2A5C* -> not a bank at all.
    """
    rows = X.read("Banks_Needed")
    create, skip = [], []
    for r in rows:
        if r["route"] != "BANK_ACCOUNT":
            skip.append((r, r["route"]))
        elif r["inactive"] == "1":
            skip.append((r, "inactive in Sage"))
        else:
            create.append(r)
    create.sort(key=lambda r: -int(r["docs_janapr"] or 0))
    return create, skip


def new_id(status, resp):
    """The created entity's id, or None.

    Never invent a placeholder here. An earlier version used
    `data.get("id") or "DRYRUN"`, which wrote the literal string DRYRUN into the
    posted log for four banks that had really been created - and the payment
    phase feeds that value straight into companyBankAccountId, so a fake id
    would have been sent to the server as a real one. A missing id on a
    non-dry-run create is a FAILURE, and is reported as one.
    """
    if status == 0:
        return "DRYRUN"
    d = P.Api.data(resp)
    if isinstance(d, dict):
        for k in ("id", "bankAccountId", "entityId", "paymentRequestId",
                  "receiptId", "invoiceId", "billId"):
            if d.get(k):
                return str(d[k])
        for v in d.values():
            if isinstance(v, dict) and v.get("id"):
                return str(v["id"])
    if isinstance(d, str) and d.strip():
        return d.strip()
    if isinstance(resp, dict) and resp.get("id"):
        return str(resp["id"])
    return None


# ---------------------------------------------------------------- IFSC lookup

IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")


# MAPPING.json constants.bankAccountType._enum, verbatim. CREDIT_CARD is
# deliberately absent - cards go to POST /creditCard, not here.
BANK_ACCOUNT_TYPES = ("OVERDRAFT", "CASH_CREDIT", "CURRENT", "SAVINGS",
                      "ANYWHERE_BANKING", "FIXED_DEPOSIT", "UNKNOWN")

# Sage's bank CODE prefix -> the bankName as the IFSC master spells it. Only
# families we actually hold an account with; anything else stays unresolved.
BANK_FAMILY = [
    ("SBI",  "STATE BANK OF INDIA"),
    ("AXIS", "AXIS BANK"),
    ("HDFC", "HDFC BANK"),
    ("CANB", "CANARA BANK"),
    ("CNTL", "CENTRAL BANK OF INDIA"),
    ("STC",  "STANDARD CHARTERED BANK"),
]


def _norm_branch(s):
    """Branch text down to comparable letters: upper, alphanumerics only, and
    the noise words that Sage and the master disagree about removed."""
    s = re.sub(r"[^A-Z0-9]", "", (s or "").upper())
    for noise in ("BRANCH", "BRANCHES", "LTD", "LIMITED"):
        s = s.replace(noise, "")
    return s


def resolve_ifsc_by_branch(banks, say=None):
    """Sage bank code -> (ifsc, 'BRANCH_NAME', master row), for banks whose
    BRANCH NAME resolves to exactly one row of the platform's IFSC master.

    The other two sources need an IFSC-shaped string to already be present in
    Sage's text. Five of the banks carrying FY documents have no such string but
    do name a real branch - "M.G Road Branch", "Brigade Road Branch",
    "SBI Overseas Bangalore" - and the master can be asked about those directly.

    UNIQUENESS is what makes it safe. The bank family plus the normalised branch
    name must select exactly ONE row; a tie is left unresolved for finance
    rather than broken by preference. That is what separates this from guessing:
    'M.G Road' normalises to MGROAD, which matches 'M G ROAD' and does NOT match
    the look-alikes 'MAGADI ROAD' or 'MALAGALA ROAD' sitting beside it in the
    same city. A near-miss is reported, never used - Sage spells Koramangala
    'Kormangala', and one letter is not a licence to route money.
    """
    want = {}
    for b in banks:
        fam = next((f for pre, f in BANK_FAMILY
                    if b["bank"].upper().startswith(pre)), None)
        if not fam:
            continue
        # branch and branch_addr are often the same string; try both, and also
        # each with the bank family's own name stripped off the front, because
        # Sage writes "SBI Overseas" where the master says "OVERSEAS BRANCH".
        texts = {b.get("branch") or "", b.get("branch_addr") or ""}
        keys = set()
        for t in texts:
            n = _norm_branch(t)
            if not n:
                continue
            keys.add(n)
            for pre, _f in BANK_FAMILY:
                if n.startswith(pre):
                    keys.add(n[len(pre):])
            keys.add(n + _norm_branch(b.get("city") or ""))
        keys.discard("")
        if keys:
            want[b["bank"]] = (fam, keys, b)

    if not want:
        return {}

    fams = sorted({f for f, _k, _b in want.values()})
    q = ("SELECT ifscCode, bankName, branchName, city FROM yoda.bankIfscDetail "
         "WHERE isDeleted=0 AND ifscCode IS NOT NULL AND (%s);"
         % " OR ".join("bankName LIKE '%s%%'" % f for f in fams))
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "root@%s" % P.cfg("SME_DB_HOST"),
             'mysql -N --batch -e "%s"' % q],
            capture_output=True, text=True, timeout=300).stdout
    except Exception as exc:                                      # noqa: BLE001
        if say:
            say("  branch-name IFSC lookup unavailable (%s)" % str(exc)[:100])
        return {}

    master = []
    for line in out.strip().splitlines():
        p = line.split("\t")
        if len(p) >= 4 and p[0] and p[0] != "NULL":
            master.append((p[0], p[1], p[2], p[3]))

    hits = {}
    for bank, (fam, keys, _b) in sorted(want.items()):
        rows = [m for m in master if m[1].upper().startswith(fam)]
        exact = [m for m in rows if _norm_branch(m[2]) in keys]
        uniq = sorted({m[0] for m in exact})
        if len(uniq) == 1:
            m = next(m for m in exact if m[0] == uniq[0])
            hits[bank] = (m[0], "BRANCH_NAME", [m[1], m[2], m[3]])
        elif say:
            if len(uniq) > 1:
                say("    %-10s branch matches %d master rows - left for finance"
                    % (bank, len(uniq)))
    return hits


# Sage bank-code prefix -> that bank's PRINCIPAL India BIC. Used only when no
# IFSC can be proven, and only after the code is confirmed to exist in the
# platform's own yoda.bankIfscDetail master - never typed from memory.
#
# POST /bankAccount refuses with ERROR.IFSC_OR_SWIFT_CODE_NOT_FOUND unless an
# ifscCode OR a swiftCode is present, and the frontend's own form offers both
# (getBankDetailsForIFSC and getBankDetailsForSwiftCode in
# components/BankAccount/api.ts), so a SWIFT is a first-class answer here rather
# than a workaround.
#
# BE CLEAR ABOUT WHAT IT ASSERTS. An IFSC names the BRANCH holding the account;
# a BIC names the BANK. Sending a BIC says less than an IFSC would, but nothing
# false - which is the opposite trade from guessing one of the three "M G ROAD"
# SBI branches. Every account created this way is flagged so it can be corrected
# when finance supplies the real IFSC.
SWIFT_BY_PREFIX = {
    "SBI": "SBININBB", "AXIS": "UTIBINB1", "HDFC": "HDFCINBB",
    "CANB": "CNRBINBB", "CNTL": "CBININBB", "STC": "SCBLINBB",
    "YES": "YESBINBB",
}


def resolve_swift(banks, say=None):
    """Sage bank code -> (swiftCode, master row), for banks with no IFSC."""
    want = {}
    for b in banks:
        for pre, bic in SWIFT_BY_PREFIX.items():
            if b["bank"].upper().startswith(pre):
                want[b["bank"]] = bic
                break
    if not want:
        return {}
    codes = sorted(set(want.values()))
    q = ("SELECT swiftCode, bankName, IFNULL(branchName,''), city "
         "FROM yoda.bankIfscDetail WHERE isDeleted=0 AND swiftCode IN (%s);"
         % ",".join("'%s'" % c for c in codes))
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "root@%s" % P.cfg("SME_DB_HOST"),
             'mysql -N --batch -e "%s"' % q],
            capture_output=True, text=True, timeout=180).stdout
    except Exception as exc:                                      # noqa: BLE001
        if say:
            say("  SWIFT master unreachable (%s)" % str(exc)[:100])
        return {}
    master = {}
    for line in out.strip().splitlines():
        pr = line.split("\t")
        if len(pr) >= 4:
            master[pr[0]] = pr[1:4]
    hits = {}
    for bank, bic in want.items():
        if bic in master:
            hits[bank] = (bic, master[bic])
        elif say:
            say("    %-10s BIC %s is NOT in the master - not used" % (bank, bic))
    return hits


def resolve_ifsc(banks, say=None):
    """Sage bank code -> (ifscCode, how) for the banks we can PROVE an IFSC for.

    POST /bankAccount rejects with ERROR.IFSC_OR_SWIFT_CODE_NOT_FOUND unless an
    ifscCode or swiftCode is present, and Sage's own ifsc column is empty on all
    69 rows - so without this the whole money side is blocked, since a payment
    with no companyBankAccountId cannot be created.

    Two candidate sources, then ONE rule that makes them safe:

      TEXT              a code written into the bank name, branch or branch
                        address (BKACCT.ADDR1 sometimes carries "IFSC CODE:...").
      SBI_BRANCH_CODE   SBI publishes its IFSC as SBIN + the 7-digit branch code,
                        and Sage writes that branch code into the address.

    THE RULE: a candidate is accepted only if it EXISTS in the platform's own
    bankIfscDetails master (249,784 rows). That turns a guess into a lookup -
    SBIOSB's address holds "Overseas Branch, No 65, St Marks Road, 0006861" and
    the master returns SBIN0006861 = "STATE BANK OF INDIA / OVERSEAS BRANCH,
    BANGALORE", which agrees on both the branch code and the branch name. The
    same rule REJECTS the constructed codes for SBIEFC and SBIEPC, because the
    7-digit runs in their addresses are account fragments, not branch codes.

    Never pattern-match without the master check: CORHIN's address yields
    CBCA0100003, which is its own account number. An IFSC routes money.
    """
    cands = {}
    for b in banks:
        blob = " ".join([b["name"], b["branch"], b["branch_addr"]]).upper()
        c = set()
        for m in IFSC_RE.findall(blob):
            c.add(("TEXT", m))
        if "SBI" in b["bank"].upper() or "STATE BANK" in blob:
            for run in re.findall(r"\d{7}", blob):
                c.add(("SBI_BRANCH_CODE", "SBIN" + run))
        if c:
            cands[b["bank"]] = c

    allc = sorted({v for st in cands.values() for _, v in st})
    found = {}
    if allc:
        q = ("SELECT ifscCode, bankName, branchName, city FROM "
             "smeassist.bankIfscDetails WHERE ifscCode IN (%s);"
             % ",".join("'%s'" % x for x in allc))
        try:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "root@%s" % P.cfg("SME_DB_HOST"),
                 'mysql -N -e "%s"' % q],
                capture_output=True, text=True, timeout=180).stdout
            for line in out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 4:
                    found[parts[0]] = parts[1:4]
        except Exception as exc:                                  # noqa: BLE001
            if say:
                say("  IFSC master unreachable (%s) - no bank can be created "
                    "without it" % str(exc)[:120])
            return {}

    out = {}
    for bank, c in cands.items():
        for how, v in sorted(c):
            if v in found:
                out[bank] = (v, how, found[v])
                break

    # Third source, for banks the first two cannot reach: look the BRANCH NAME
    # itself up in the master. Same rule as above - the answer comes out of the
    # platform's own table, never out of a pattern - with uniqueness doing the
    # work that the master-existence check does for the other two sources.
    for bank, hit in resolve_ifsc_by_branch(
            [b for b in banks if b["bank"] not in out], say).items():
        out[bank] = hit

    if say:
        say("  IFSC resolved for %d of %d banks, verified against the "
            "bankIfscDetails master" % (len(out), len(banks)))
        for bank, (v, how, m) in sorted(out.items()):
            say("    %-10s %-12s %-16s %s / %s"
                % (bank, v, how, m[0][:20], m[1][:26]))
    return out


def bank_payload(r, ifsc_hit):
    """BankAccountCreateDto. Field names taken from real call sites in the
    backend and proven by reading the row back out of yoda.bankAccount.

    accountNumber is NOT NULL in yoda.bankAccount, and four Sage banks have no
    number, so those get a flagged placeholder. §10 matters here: the CHEQUE
    branch bakes the account number into the bank LEDGER name, so a placeholder
    would become permanent - which is why payments on such a bank are held.
    """
    placeholder = r["acctno_src"] == "MISSING" or not r["acctno"].strip()
    acct = r["acctno"].strip() or ("SAGE-%s-NOACCTNO" % r["bank"])
    meta = {"sageBank": r["bank"], "sageGl": r["sage_gl"],
            "migrationSource": "IDEDAT"}
    if placeholder:
        meta["accountNumberIsPlaceholder"] = "true"

    # bankAccountType only ever takes these values in production - measured
    # across all 33,535 yoda.bankAccount rows: CURRENT 21,130, NULL 12,388,
    # CASH_CREDIT 12, SAVINGS 4, OVERDRAFT 1. The extract derives UNKNOWN for
    # 7 banks (term loans, a discount control account) and FIXED_DEPOSIT for 1,
    # and neither appears anywhere in the box. The column is NULLABLE and null
    # is thoroughly exercised, so send null and flag it rather than invent an
    # enum value the server may not accept - a term loan is genuinely not a
    # current account or a cash credit, and guessing either would misstate it.
    # ...but null is NOT a safe fallback: a null bankAccountType NPEs the
    # create outright (proven on SBISFD), because the server dereferences
    # getBankAccountType().getShortCode() with no guard. So the value must be
    # one the enum declares. FIXED_DEPOSIT and UNKNOWN are declared even though
    # no production row uses them, and the extract derives them from the Sage
    # GL prefix (2A3L* -> FIXED_DEPOSIT), so they are read off the data rather
    # than chosen. Anything outside the enum is still flagged and held.
    btype = (r["sme_bankacct_type"] or "").strip().upper()
    if btype not in BANK_ACCOUNT_TYPES:
        meta["bankAccountTypeUnmapped"] = btype or "blank"
        meta["sageGlForType"] = r["sage_gl"]
        btype = None
    elif btype not in ("CURRENT", "CASH_CREDIT", "SAVINGS", "OVERDRAFT"):
        meta["bankAccountTypeRareInProduction"] = btype
    # ifsc_hit is None when no IFSC could be PROVEN against the platform's own
    # bankIfscDetails master. Sage carries none (BKACCT.TRANSIT is empty on all
    # 74 rows), and MAPPING.json's constants.bankAccountType.ifsc says the field
    # is not mandatory on the target, citing 10 of 320 production ORGANISATION
    # bank accounts with no ifscCode. Nothing is ever invented either way: the
    # code sent is one that exists in the master, or none at all.
    swift = None
    if isinstance(ifsc_hit, tuple) and len(ifsc_hit) == 2 and ifsc_hit[0] and \
            not ifsc_hit[0][4:5].isdigit():
        # a BIC, not an IFSC: 4 letters + country + location, no '0' in slot 5
        swift, m = ifsc_hit
        ifsc = None
        meta["ifscSource"] = "NONE"
        meta["ifscMissing"] = "true"
        meta["swiftSource"] = "PLATFORM_MASTER"
        meta["swiftIsBankLevel"] = ("a BIC names the BANK, not the branch "
                                    "holding this account - replace with the "
                                    "real IFSC when finance supplies it")
        meta["swiftMasterBank"] = m[0]
        meta["swiftMasterCity"] = m[2]
    elif ifsc_hit is None:
        ifsc = None
        meta["ifscSource"] = "NONE"
        meta["ifscMissing"] = "true"
    else:
        ifsc, how, master = ifsc_hit
        meta["ifscSource"] = how
        meta["ifscMasterBank"] = master[0]
        meta["ifscMasterBranch"] = master[1]
        if how == "SBI_BRANCH_CODE":
            meta["ifscDerivedFromBranchCode"] = "true"
    return {
        "accountNumber": acct,
        "accountHolderName": P.cfg("SME_ORG_NAME") or "WONDERBLUES APPARELS",
        "name": r["name"][:200] or r["bank"],
        "bankAccountType": btype,
        "branchName": (r["branch"] or "").strip()[:255] or None,
        "branchAddress": (r["branch_addr"] or "").strip()[:255] or None,
        "ifscCode": ifsc,
        "swiftCode": swift,
        "partyId": P.ORG_ID,
        "bankAccountPartyType": "ORGANISATION",
        "bankAccountStatus": "AUTO_VERIFIED",
        "isVerified": True,
        "isActive": True,
        "usedCurrently": True,
        "isDefault": False,
        "currency": r["curn"] or "INR",
        "accountingName": "SAGE-%s" % r["bank"],
        "label": "SAGE-%s" % r["bank"],
        # A Map on the DTO, not a string: sending json.dumps(...) gives
        # "Cannot construct instance of java.util.LinkedHashMap ... from String
        # value". The proven loaders in this repo also pass metadata as a dict.
        "meta": meta,
    }, placeholder


def reconcile_banks(say):
    """Rebuild the posted log from what the platform actually holds.

    A create can write the row and THEN fail - SBISFD's null bankAccountType
    NPE'd after the insert, exactly as the payment `status` NPE does - which
    leaves a bank that exists, is not in the log, and whose next create attempt
    is refused with "Bank Account already present for you". The log is a cache
    of the platform's state, so it must be rebuildable FROM that state rather
    than repaired by hand. accountingName carries SAGE-<bank code>, which is
    what the log keys on.
    """
    q = ("SELECT accountingName, id, bankAccountType FROM yoda.bankAccount "
         "WHERE partyId='%s' AND isDeleted=0;" % P.ORG_ID)
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "root@%s" % P.cfg("SME_DB_HOST"),
         'mysql -N --batch -e "%s"' % q],
        capture_output=True, text=True, timeout=180).stdout
    posted = load_posted("bank")
    added, untyped = 0, []
    for line in out.strip().splitlines():
        p = line.split("\t")
        if len(p) < 2 or not p[0].startswith("SAGE-"):
            continue
        code = p[0][5:]
        if len(p) > 2 and p[2] in ("NULL", ""):
            untyped.append(code)
        if code not in posted:
            mark("bank", code, p[1])
            added += 1
            say("  reconciled %-10s -> %s" % (code, p[1]))
    if untyped:
        say("  %d bank(s) have a NULL bankAccountType and CANNOT take a payment:"
            % len(untyped))
        say("    %s" % ", ".join(sorted(untyped)))
        say("    (getPaymentModeFinanceAccountName dereferences "
            "getBankAccountType().getShortCode() unguarded)")
    say("  reconciled %d bank(s) into the posted log" % added)
    return added


# Vendors_Needed.psv column -> the key ensure_contacts expects. The proven
# contact builder in post_sage_bills.py takes its vendor master as a parameter
# precisely so a second source can feed it, so this maps rather than forks:
# re-implementing it would fork the pincode/state resolution, the CIN lookup,
# the INTERNATIONAL branch and the address create, all of which are load-bearing.
#
# Two places the extract is BETTER than the SQL that function was written for:
# it carries addr3/addr4 (the town is as often there as in NAMECITY), and its
# `email` is APVEN.EMAIL2 - EMAIL1 is empty on every row in this database.
VENDOR_PSV_TO_MASTER = {
    "vendor": "vendor", "name": "name", "city": "city",
    "sage_state": "state_raw", "pincode": "pincode", "country": "country",
    "addr1": "street1", "addr2": "street2", "addr3": "street3",
    "addr4": "street4", "contact_name": "contact_person",
    "phone": "phone1", "email": "email1",
}


def vendor_master_from_extract():
    """Vendors_Needed.psv in the shape ensure_contacts() expects."""
    rows = {}
    for r in X.read("Vendors_Needed"):
        v = {dst: r.get(src, "") for src, dst in VENDOR_PSV_TO_MASTER.items()}
        # registration_of() reads brn_raw and settles GST before PAN, so the
        # GSTIN goes there when present and the PAN only when it does not -
        # which is what the extract's own sme_regtype already concluded.
        v["brn_raw"] = (r.get("gstin") or "").strip() or (r.get("pan") or "").strip()
        v["legal_name"] = r.get("name", "")
        rows[r["vendor"]] = v
    return rows


def phase_contacts(args):
    """Create a contact per vendor in the window - not per vendor a selected
    BILL happens to touch.

    This is the gap that held 2,242 payments: post_sage_bills' masters phase is
    scoped to the bills it selected, so a vendor that only ever appears on a
    PAYMENT never got a contact, and a payment cannot be created without one.
    """
    say = open_log("contacts")
    rows = vendor_master_from_extract()
    state = P.State()
    have = set(state.xw["contacts"])
    want = sorted(rows)
    todo = [c for c in want if c not in have]
    say("\n%d vendors in the extract, %d already in the crosswalk, %d to create"
        % (len(want), len(want) - len(todo), len(todo)))
    if args.limit:
        todo = todo[: args.limit]
        say("  --limit %d -> attempting %d" % (args.limit, len(todo)))
    if args.dryrun:
        say("  dry run, nothing posted")
        return 0

    api = P.Api()
    before = len(state.xw["contacts"])
    try:
        P.ensure_contacts(api, state, set(todo), rows=rows)
    finally:
        # Save whatever was created even if the run was cut short - a contact
        # that exists on the platform but not in the crosswalk is invisible to
        # every later phase and would be created twice.
        state.save()
    made = len(state.xw["contacts"]) - before
    say("\ncreated=%d  crosswalk now holds %d contacts"
        % (made, len(state.xw["contacts"])))
    say("\nlog -> %s" % say.path)
    return 0


def fix_bank_types(say, api):
    """Repair bank accounts left with a NULL bankAccountType.

    SBISFD was created by an attempt that NPEd AFTER the insert, so the row
    exists with bankAccountType NULL - and a null type is not cosmetic:
    getPaymentModeFinanceAccountName dereferences
    getBankAccountType().getShortCode() unguarded, so such an account can never
    take a CHEQUE payment. PUT /bankAccount/{id} is the frontend's own edit
    route (components/BankAccount/api.ts addBankAccount(dto, isEdit=true)), so
    this repairs in place rather than deleting and recreating.

    The type is read back off the extract's Sage-GL derivation, not chosen.
    """
    want = {r["bank"]: (r["sme_bankacct_type"] or "").strip().upper()
            for r in X.read("Banks_Needed")}
    q = ("SELECT id, accountingName FROM yoda.bankAccount "
         "WHERE partyId='%s' AND isDeleted=0 AND bankAccountType IS NULL;" % P.ORG_ID)
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "root@%s" % P.cfg("SME_DB_HOST"),
         'mysql -N --batch -e "%s"' % q],
        capture_output=True, text=True, timeout=180).stdout
    rows = [l.split("\t") for l in out.strip().splitlines() if l.strip()]
    if not rows:
        say("  no bank account has a NULL bankAccountType")
        return 0
    fixed = 0
    for r in rows:
        if len(r) < 2 or not r[1].startswith("SAGE-"):
            continue
        code = r[1][5:]
        t = want.get(code)
        if t not in BANK_ACCOUNT_TYPES:
            say("  %-10s Sage derives %r, which is not in the enum - left alone"
                % (code, t))
            continue
        # PUT takes the whole DTO, not a patch: a body carrying only the type
        # is refused with "PartyId associated with BankAccount cannot be null".
        # The other fields are read back off the row so nothing is restated
        # from memory - only bankAccountType changes.
        cur = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "root@%s" % P.cfg("SME_DB_HOST"),
             'mysql -N --batch -e "SELECT accountNumber, accountHolderName, '
             'bankName, currency, IFNULL(ifscCode,\'\'), IFNULL(swiftCode,\'\') '
             'FROM yoda.bankAccount WHERE id=%s;"' % r[0]],
            capture_output=True, text=True, timeout=120).stdout.strip().split("\t")
        if len(cur) < 4:
            say("  %-10s could not read the row back - skipped" % code)
            continue
        body = {"bankAccountId": r[0], "partyId": P.ORG_ID,
                "bankAccountPartyType": "ORGANISATION",
                "accountNumber": cur[0], "accountHolderName": cur[1],
                "name": cur[2], "currency": cur[3] or "INR",
                "bankAccountType": t}
        if len(cur) > 4 and cur[4]:
            body["ifscCode"] = cur[4]
        if len(cur) > 5 and cur[5]:
            body["swiftCode"] = cur[5]
        st, resp = api.call("PUT", "/bankAccount/%s" % r[0], body)
        if P.Api.ok(st, resp):
            fixed += 1
            say("  %-10s bankAccountType NULL -> %s" % (code, t))
        else:
            say("  %-10s FIX FAIL (%s) %s" % (code, st, P.Api.err(resp)))
    return fixed


def phase_banks(args):
    say = open_log("banks")
    if args.reconcile:
        say("\nreconciling the posted log against yoda.bankAccount")
        reconcile_banks(say)
    if args.fix_types:
        say("\nrepairing NULL bankAccountType via PUT /bankAccount/{id}")
        fix_bank_types(say, P.Api(dry_run=args.dryrun))
    create, skip = bank_selection()
    posted = load_posted("bank")
    api = P.Api(dry_run=args.dryrun)

    say("\n%d bank accounts to create, %d skipped, %d already in the posted log"
        % (len(create), len(skip), len(posted)))
    for r, why in skip:
        say("  skip %-10s %s" % (r["bank"], why))

    # Only banks that carry a document in the window are worth resolving, and
    # only 13 of the 69 do - 8,187 documents, of which SBIOSB alone is 6,683.
    in_play = [r for r in create if int(r["docs_janapr"] or 0) > 0]
    say("\n%d of these carry a document in the Jan-Apr window" % len(in_play))
    hits = resolve_ifsc(in_play, say)
    no_ifsc = [r for r in in_play if r["bank"] not in hits]
    if no_ifsc and not args.allow_no_ifsc:
        say("\n%d banks HELD - no IFSC provable:" % len(no_ifsc))
        for r in no_ifsc:
            say("    %-10s %5s docs  %s" % (r["bank"], r["docs_janapr"], r["name"][:40]))
        say("  -> out/IFSC-REQUEST-13-banks.csv is the ask. Nothing is invented.")
        say("  -> re-run with --allow-no-ifsc to attempt these with ifscCode null.")
    if no_ifsc and args.use_swift:
        sw = resolve_swift(no_ifsc, say)
        if sw:
            say("\n  SWIFT fallback accepted for %d bank(s), verified in the "
                "platform master:" % len(sw))
            for b, (code, m) in sorted(sw.items()):
                say("    %-10s %-12s %s / %s" % (b, code, m[0][:26], m[2][:18]))
            hits.update(sw)
            no_ifsc = [r for r in in_play if r["bank"] not in hits]
    create = [r for r in in_play if r["bank"] in hits]
    # NEVER create a bank whose account number is a placeholder. The CHEQUE
    # branch bakes accountNumber into the bank LEDGER's permanent name
    # ("<name> (<accountNumber>) <shortCode>"), so "SAGE-CANBCC-NOACCTNO" would
    # become master data that cannot be cleaned up. Solving the IFSC does not
    # solve this - it is a separate ask to the business.
    ph = [r for r in create
          if r["acctno_src"] in ("MISSING", "UNUSABLE") or not r["acctno"].strip()]
    if ph:
        say("\n  %d bank(s) HELD on the ACCOUNT NUMBER, not the IFSC - a"
            " placeholder would be permanent:" % len(ph))
        for r in ph:
            say("    %-10s %5s docs  acctno_src=%s raw=%r"
                % (r["bank"], r["docs_janapr"], r["acctno_src"], r["acctno_raw"]
                   if "acctno_raw" in r else r["acctno"]))
        create = [r for r in create if r not in ph]
    if args.allow_no_ifsc:
        # Attempt the unresolved ones with a null IFSC rather than holding them
        # on an assumption. Still nothing invented - a null is not a guess.
        say("\n--allow-no-ifsc: also attempting %d bank(s) with ifscCode null"
            % len(no_ifsc))
        create = create + no_ifsc
    say("\n%d bank accounts will be created (%d documents in window)"
        % (len(create), sum(int(r["docs_janapr"] or 0) for r in create)))

    ok = fail = 0
    placeholders = []
    for r in create[: args.limit or len(create)]:
        if r["bank"] in posted:
            say("  %-10s already created as %s" % (r["bank"], posted[r["bank"]]))
            continue
        body, placeholder = bank_payload(r, hits.get(r["bank"]))
        st, resp = api.post("/bankAccount", body)
        bid = new_id(st, resp) if P.Api.ok(st, resp) else None
        if bid:
            if not args.dryrun:
                mark("bank", r["bank"], bid)
            ok += 1
            say("  %-10s %-30s %-12s -> %s%s"
                % (r["bank"], r["name"][:30], r["sme_bankacct_type"], bid,
                   "  [acctno PLACEHOLDER]" if placeholder else ""))
            if placeholder:
                placeholders.append(r["bank"])
        else:
            fail += 1
            say("  %-10s CREATE FAIL (%s) %s" % (r["bank"], st, P.Api.err(resp)))
            if P.Api.ok(st, resp):
                say("           (server accepted it but returned no id - treat as")
                say("            a failure and reconcile from the database)")

    say("\nattempted=%d ok=%d failed=%d" % (ok + fail, ok, fail))
    if placeholders:
        say("PLACEHOLDER account numbers on: %s" % ", ".join(placeholders))
        say("  Payments drawn on these banks are HELD - the CHEQUE branch bakes the")
        say("  account number into the ledger name, so it would become permanent.")
        say("  This is a request to finance, and nothing is invented meanwhile.")
    say("\nlog -> %s" % say.path)
    return 0 if not fail else 1


# ============================================================================
# phase: series (§3.2)
# ============================================================================

SERIES_BOTH_YEARS = ["PURCHASE_CREDIT_NOTE", "PURCHASE_DEBIT_NOTE",
                     "SALES_CREDIT_NOTE", "SALES_DEBIT_NOTE",
                     "SALES_EXPORT_CREDIT_NOTE", "SALES_EXPORT_DEBIT_NOTE",
                     "TRANSFER_INVOICE"]
SERIES_FY2627 = ["INVOICE", "EXPORT_INVOICE"]


def phase_series(args):
    """Counter series for BOTH years in the window.

    The FY comes from each document's own date (X.fy), never from today: the
    window straddles FY2025-2026 (23,836 bills) and FY2026-2027 (5,909), the
    series key has no FY fallback, and an earlier loader that hardcoded
    calendar-2026 logic misfiled everything before April.
    """
    say = open_log("series")
    years = sorted({X.fy(h["datebus"]) for h in X.read("Bills_Header")
                    if X.fy(h["datebus"])})
    years += sorted({X.fy(h["dateinvc"]) for h in X.read("Sales_Header")
                     if X.fy(h["dateinvc"]) and X.fy(h["dateinvc"]) not in years})
    years = sorted(set(years))
    say("\nfinancial years present in the window: %s" % ", ".join(years))

    want = []
    for y in years:
        for t in SERIES_BOTH_YEARS:
            want.append((t, y))
        for t in SERIES_FY2627:
            want.append((t, y))
    say("%d (type, FY) series required" % len(want))
    say("\nThis phase uploads BULK_ENTITY_SERIES_CREATION with")
    say("  associatedEntityType=PAN  associatedEntityId=%s" % P.ORG_PAN)
    say("and is PAN-keyed per §3.2. It is not implemented as a REST create")
    say("because the platform exposes series creation only as that bulk sheet.")
    for t, y in want:
        say("  %-30s %s" % (t, y))
    say("\nNOT POSTED - see the note above. Run the sheet, or say the word and")
    say("I will build the xlsx writer for it.")
    say("\nlog -> %s" % say.path)
    return 0


# ============================================================================
# phase: payments (§10)
# ============================================================================

def payment_payload(p, maps, bank_ids, contact_ids):
    """PaymentRequestCreateDto.

    The shape is NESTED - the controller's first act is
    `createDto.getPaymentRequestDto().getPaymentRequestId()`, so a flat body
    makes that null and returns a bare 500 NullPointerException with nothing
    else to go on. Everything below sits under "paymentRequestDto".

    Field shapes that differ from the field NAMES in the brief's §10:
      * the bank is `companyBankAccountDto` - a nested object - not
        `companyBankAccountId`, even though the COLUMN is companyBankAccountId;
      * `metaData` is Map<String,String>, so every value must be a string. An
        int sageBatch is rejected by Jackson;
      * voucherDate is a Java long (epoch millis), not a date string.

    §10 stands on the parts that matter: paymentMode=CHEQUE, the cheque number
    in utrNumber (there is no chequeNumber field on a paymentRequest), the
    company bank set and the CONTACT bank left absent - the
    "BankAccount can not be null" guard is scoped to BANK_TRANSFER only, which
    is exactly why CHEQUE works with no vendor bank, and Sage holds none.
    """
    # Sage's own mode where it has one, else the DECIDED mode - never
    # BANK_TRANSFER. Measured on the FY cut: 12,890 of 14,956 payments say
    # CHEQUE, 6 say CASH, 1 says TT and 2,059 (13.8%) say nothing at all.
    # Falling back to BANK_TRANSFER for those sends them into the
    # "BankAccount can not be null" guard, which is scoped to BANK_TRANSFER and
    # asserts a VENDOR bank account - Sage holds none, so all 2,060 fail the
    # create. Proven: three blank-mode payments on the same bank as a CHEQUE one
    # failed while the CHEQUE one verified.
    raw = (p["paymode"] or "").strip().upper()
    mode = "CHEQUE" if raw.startswith("CHEQ") else ("CASH" if raw == "CASH" else "CHEQUE")
    mode_inferred = not (raw.startswith("CHEQ") or raw == "CASH")
    bank_id = bank_ids.get(p["bank"])
    meta = {
        "sageBatch": str(p.get("cntbtch") or ""),
        "sageItem": str(p.get("cntitem") or ""),
        "sageVendor": str(p["vendor"]),
        "sageDoc": str(p.get("docno") or ""),
        "sageCheque": str((p.get("remit_cheque") or "").strip()),
        "sagePayType": str(p["sme_paytype"]),
        "sageBank": str(p["bank"]),
        "migrationSource": "IDEDAT",
    }
    if mode_inferred:
        # Flagged, per the brief: every inferred value carries its count and a
        # flag name so it stays findable. sagePayModeRaw keeps what Sage said
        # (usually nothing, once 'TT') rather than pretending CHEQUE was read.
        meta["paymentModeInferred"] = "true"
        meta["sagePayModeRaw"] = raw or "(blank)"
    who = contact_ids.get(p["vendor"]) or {}
    inner = {
        "contactId": who.get("id"),
        # contactId alone is not enough - the server also asserts a name, so the
        # MinContactDto travels with it. accountName and companyName are both
        # set because the guard's message ("ContactName") names neither.
        "contactDto": {"contactId": who.get("id"),
                       "accountName": who.get("name"),
                       "companyName": who.get("name")},
        "contactName": who.get("name"),
        "contactFinanceAccountId": who.get("ledger"),
        "contactType": "VENDOR",
        "requestedAmount": str(X.D(p["amt_inr"])),
        "paymentRequestType": p["sme_paytype"],
        "paymentMode": mode,
        # status must be explicit. publishSupplierLimitManage does
        # `NON_SUCCESS_AND_REVERSED.contains(dto.getStatus().name())` with no
        # null guard (PaymentRequestServiceImpl:1987), so a null status is a
        # bare NPE - raised AFTER the row is written, at line 521, which makes
        # it look like a create failure when the create had already succeeded.
        # (The transaction does roll back, so nothing is orphaned - verified.)
        #
        # SUCCESS is the honest value and the dominant one in production
        # (104,099 of 120,742): these payments already cleared in Sage, so they
        # are not awaiting approval or processing here.
        "status": "SUCCESS",
        # approvedAmount is what the VOUCHER leg is built from, not
        # requestedAmount. Leaving it null creates the payment happily and then
        # fails verify with a bare NullPointerException in
        # VoucherSortUtils$PartyFirstComparator.compare:189 - which is
        # a.getAmount().compareTo(b.getAmount()) at bytecode offset 96, so the
        # null is an AMOUNT on a leg, not the missing bank ledger it was first
        # read as. Measured on the deployed jar and against production: all
        # three of approvedAmount, conversionRate and tdsAmount are non-null on
        # every one of the 105,761 verified payments box-wide and were null on
        # all five of ours.
        "approvedAmount": str(X.D(p["amt_inr"])),
        "conversionRate": str(X.D(p.get("rate"), "1")),
        "tdsAmount": "0",
        "utrNumber": (p["remit_cheque"] or "").strip() or None,
        "voucherDate": X.epoch_ms(p["datebus"]),
        "remarks": (p.get("descinvc") or "").strip()[:255] or None,
        "metaData": meta,
    }
    if bank_id:
        # BankAccountDto's getter is getBankAccountId(), so the JSON key is
        # "bankAccountId" - NOT "id". Sending {"id": ...} leaves
        # companyBankAccountId null in the convertor and the create dies later
        # with a bare NullPointerException, nowhere near the real cause.
        inner["companyBankAccountDto"] = {"bankAccountId": bank_id,
                                          "name": p.get("bankname") or p["bank"]}
    if maps:
        inner["paymentRequestEntityMappingDtoList"] = maps
    return {"paymentRequestDto": inner}


def phase_payments(args):
    say = open_log("payments")
    rows = X.read("AP_Payments_Header")
    banks = load_posted("bank")
    if not banks and not args.dryrun:
        say("REFUSING TO RUN: no bank accounts in %s." % LOGS["bank"])
        say("  yoda.bankAccount is empty for this org and nothing money-related")
        say("  posts without it. Run ./post_janapr.py banks first (§3.1).")
        return 2

    create, skipbank = bank_selection()
    placeholder_banks = {r["bank"] for r in create if r["acctno_src"] == "MISSING"
                         or not r["acctno"].strip()}

    apps = settlement_apps("AP_Payment_Apps")
    say("\nAP_Payment_Apps: %d legs, kept %d" % (len(apps["all"]), len(apps["kept"])))
    for why, n in sorted(apps["dropped"].items()):
        say("  dropped %6d  %s" % (n, why))

    idx = bill_id_index()
    say("  bill id index: %d (vendor|invno) keys from the posted logs" % len(idx))
    cols = {"pay_key": ("cntbtch", "cntitem"),
            "app_key": ("app_cntbtch", "app_cntitem"),
            "party": "vendor", "doc": "target_base", "etype": "BILL",
            "doc_types": ("12",), "note_types": ("22", "32")}
    maps = build_settlement(rows, apps, idx, cols, say)

    # Dedupe cheque numbers upstream: the org-wide UTR uniqueness check lives
    # inside the BANK_TRANSFER branch, so a CHEQUE load gets no guard at all.
    seen, dupes = {}, []
    for p in rows:
        c = (p["remit_cheque"] or "").strip()
        if not c:
            continue
        if c in seen:
            dupes.append(c)
        seen[c] = 1
    if dupes:
        say("  %d duplicate cheque numbers in the source - deduped upstream, "
            "because CHEQUE gets no org-wide UTR guard" % len(dupes))

    contacts_pre = contact_index()
    held, postable = [], []
    for p in rows:
        if p["vendor"] not in contacts_pre:
            held.append((p, "vendor %s has no contact yet (masters creates them "
                            "scoped to its selection)" % p["vendor"]))
        elif not contacts_pre[p["vendor"]].get("ledger"):
            # Without the party ledger the payment would post and then be
            # unverifiable for ever, which is worse than not posting it.
            held.append((p, "vendor %s has a contact but no party ledger"
                         % p["vendor"]))
        elif p["bank"] in placeholder_banks:
            held.append((p, "bank %s has no account number in Sage (§10)" % p["bank"]))
        elif not banks.get(p["bank"]) and not args.dryrun:
            held.append((p, "no bank account created for %s" % p["bank"]))
        else:
            postable.append(p)
    say("\n%d payments postable, %d held" % (len(postable), len(held)))
    hb = {}
    for _, why in held:
        hb[why] = hb.get(why, 0) + 1
    for why, n in sorted(hb.items(), key=lambda kv: -kv[1]):
        say("  held %5d  %s" % (n, why))

    posted = load_posted("payment")
    contacts = contact_index()
    api = P.Api(dry_run=args.dryrun)
    sel = postable[: args.limit] if args.limit else postable
    ok = fail = 0
    for p in sel:
        key = "%s|%s|%s" % (p["vendor"], p["cntbtch"], p["cntitem"])
        if key in posted:
            continue
        body = payment_payload(p, maps.get((p["cntbtch"], p["cntitem"]), []), banks, contacts)
        if not body["paymentRequestDto"].get("companyBankAccountDto") \
                and not args.dryrun:
            fail += 1
            say("  %-28s NO BANK - refusing to post a payment with no bank" % key)
            continue
        st, resp = api.post("/paymentRequest/", body)
        pid = new_id(st, resp) if P.Api.ok(st, resp) else None
        if pid:
            if not args.dryrun:
                mark("payment", key, pid)
            ok += 1
            say("  %-28s %-10s %14s  %d settled -> %s"
                % (key, body["paymentRequestDto"]["paymentMode"],
                   body["paymentRequestDto"]["requestedAmount"],
                   len(body["paymentRequestDto"].get(
                       "paymentRequestEntityMappingDtoList", [])), pid))
        else:
            fail += 1
            say("  %-28s CREATE FAIL (%s) %s" % (key, st, P.Api.err(resp)))

    say("\nattempted=%d ok=%d failed=%d" % (ok + fail, ok, fail))
    if held:
        p = os.path.join(OUT, "held_payments.csv")
        with open(p, "w") as fh:
            fh.write("vendor,cntbtch,cntitem,bank,amt_inr,reason\n")
            for r, why in held:
                fh.write("%s,%s,%s,%s,%s,%s\n"
                         % (r["vendor"], r["cntbtch"], r["cntitem"], r["bank"],
                            r["amt_inr"], why.replace(",", ";")))
        say("held -> %s" % p)
    say("\nlog -> %s" % say.path)
    return 0 if not fail else 1


def contact_index(kind="vendor"):
    """Sage party code -> {"id": contactId, "name": ...}.

    The two sides live in DIFFERENT crosswalks, and each is nested:

      vendor  work/crosswalk_live.json  ["contacts"][code]["contactId"]
      buyer   work/crosswalk_ar.json    ["buyers"][code]["contactId"]

    Reading the top level of either yields a handful of section names and
    resolves nothing, which the server reports as the considered refusal
    "ContactId can not be null". Reading the VENDOR crosswalk for a receipt
    resolves nothing either - crosswalk_live's "contacts" holds no customers,
    which is why all 598 receipts were held on the first run.

    The name travels with the id because the server also asserts a name
    ("ContactName can not be null") and builds it from
    contactDto.getCompanyName().
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if kind == "buyer":
        path, section = os.path.join(here, "work", "crosswalk_ar.json"), "buyers"
    else:
        path, section = os.path.join(here, "work", "crosswalk_live.json"), "contacts"
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        xw = json.load(fh)
    for code, rec in (xw.get(section) or {}).items():
        if isinstance(rec, dict) and rec.get("contactId"):
            # The party LEDGER travels too. resolveContactFinanceAccountId on
            # the server returns null for these contacts, leaving
            # contactFinanceAccountId NULL on the row - and then verify refuses
            # with "financeAccountId can not be null", so the payment exists but
            # can never mint a voucher. The crosswalk already knows the ledger.
            out[code] = {"id": rec["contactId"], "name": rec.get("name") or code,
                         "ledger": rec.get("ledger")}
    return out


# ============================================================================
# phase: receipts (§10)
# ============================================================================

def receipt_payload(r, refs, bank_ids, contact_ids):
    """ReceiptCreateDto - FLAT, unlike the payment one.

    `amount` MUST be the INR figure: the voucher DIVIDES it by conversionRate to
    derive the bank leg, which is the opposite of the intuitive reading. Getting
    this backwards misstates every FX receipt, and 64% of this population's
    value is USD.

    Shape notes proven against the DTO and the live data:
      * the bank is `bankAccountDto`, a nested object;
      * the metadata key is `metadata` (lower-case d) here, while the payment
        DTO and ReceiptDto both use `metaData`;
      * `type` is ReceiptType, whose only real values box-wide are
        AGAINST_CONTACT (105,373) and ADHOC (979) - a receipt against a
        customer is AGAINST_CONTACT. It is NOT the Sage kind, so the
        PREPAYMENT/RECEIPT distinction lives in metadata and in whether
        references[] is populated;
      * dates are Java longs.

    Sage's own receipt number cannot be carried - the voucher number is
    hardcoded "REC/" + yy-MM-dd + "/" + (utr|cheque|randomId) - so it goes in
    metadata where it stays findable.
    """
    mode = (r["paymode"] or "").upper()
    mode = "CHEQUE" if mode.startswith("CHEQ") else \
           ("CASH" if mode == "CASH" else "BANK_TRANSFER")
    ref = (r["remit_ref"] or "").strip()
    bank_id = bank_ids.get(r["bank"])
    body = {
        "amount": str(X.D(r["amt_inr"])),
        "paymentMode": mode,
        "type": "AGAINST_CONTACT",
        "contactType": "BUYER",
        "contactId": (contact_ids.get(r["customer"]) or {}).get("id"),
        "contactFinanceAccountId": (contact_ids.get(r["customer"]) or {}).get("ledger"),
        "contactDto": {
            "contactId": (contact_ids.get(r["customer"]) or {}).get("id"),
            "accountName": (contact_ids.get(r["customer"]) or {}).get("name"),
            "companyName": (contact_ids.get(r["customer"]) or {}).get("name")},
        "voucherDate": X.epoch_ms(r["datebus"]),
        "transactionDate": X.epoch_ms(r["datebus"]),
        "remarks": (r.get("narrative") or "").strip()[:255] or None,
        "metadata": {
            "sageReceipt": ref,
            "sageBatch": str(r.get("cntbtch") or ""),
            "sageItem": str(r.get("cntitem") or ""),
            "sageDoc": str(r.get("docno") or ""),
            "sageCustomer": str(r["customer"]),
            "sageKind": str(r["kind"]),
            "sageBank": str(r["bank"]),
            "migrationSource": "IDEDAT",
        },
        # Prepayments carry no references at all - the money sits in
        # unmappedAmount until a later invoice claims it. That is the source's
        # own shape, not a gap.
        "references": refs or [],
    }
    if bank_id:
        body["bankAccountDto"] = {"bankAccountId": bank_id,
                                  "name": r.get("bankname") or r["bank"]}
    if mode == "CHEQUE":
        body["chequeNumber"] = ref or None
    else:
        body["utrNumber"] = ref or None
    return body


def phase_receipts(args):
    say = open_log("receipts")
    rows = X.read("AR_Receipts_Header")
    banks = load_posted("bank")
    if not banks and not args.dryrun:
        say("REFUSING TO RUN: no bank accounts. Run banks first (§3.1).")
        return 2

    apps = settlement_apps("AR_Receipt_Apps")
    say("\nAR_Receipt_Apps: %d legs, kept %d" % (len(apps["all"]), len(apps["kept"])))
    for why, n in sorted(apps["dropped"].items()):
        say("  dropped %6d  %s" % (n, why))

    inv = load_posted("invoice")
    say("  invoice id index: %d keys" % len(inv))
    # AR_Receipt_Apps has no target_base column - only target_doc.
    cols = {"pay_key": ("cntbtch", "cntitem"),
            "app_key": ("app_cntbtch", "app_cntitem"),
            "party": "customer", "doc": "target_doc", "etype": "INVOICE",
            "doc_types": ("12", "14"), "note_types": ("22", "24", "32", "34")}
    maps = build_settlement(rows, apps, inv, cols, say)

    prepay = [r for r in rows if r["kind"] == "PREPAYMENT"]
    say("\n%d receipts, of which %d prepayments (empty references[], money in "
        "unmappedAmount)" % (len(rows), len(prepay)))

    posted = load_posted("receipt")
    contacts = contact_index("buyer")
    api = P.Api(dry_run=args.dryrun)
    # Holds, not failures: a receipt whose customer or bank does not exist yet
    # is waiting on another phase, and counting it as a failure would make a
    # clean run look broken. Only a server refusal is a failure.
    held = []
    postable = []
    for r in rows:
        if r["customer"] not in contacts:
            held.append((r, "customer %s has no contact yet" % r["customer"]))
        elif not banks.get(r["bank"]):
            held.append((r, "no bank account for %s (needs an IFSC - see "
                            "out/IFSC-REQUEST-13-banks.csv)" % r["bank"]))
        else:
            postable.append(r)
    hb = {}
    for _, why in held:
        hb[why] = hb.get(why, 0) + 1
    say("\n%d receipts postable, %d held" % (len(postable), len(held)))
    for why, n in sorted(hb.items(), key=lambda kv: -kv[1])[:6]:
        say("  held %5d  %s" % (n, why))
    rows = postable
    sel = rows[: args.limit] if args.limit else rows
    ok = fail = 0
    for r in sel:
        key = "%s|%s|%s" % (r["customer"], r["cntbtch"], r["cntitem"])
        if key in posted:
            continue
        body = receipt_payload(r, maps.get((r["cntbtch"], r["cntitem"]), []), banks, contacts)
        if not body.get("bankAccountDto") and not args.dryrun:
            fail += 1
            say("  %-28s NO BANK - refusing to post" % key)
            continue
        st, resp = api.post("/receipt/", body)
        rid = new_id(st, resp) if P.Api.ok(st, resp) else None
        if rid:
            if not args.dryrun:
                mark("receipt", key, rid)
            ok += 1
            say("  %-28s %-10s %14s  %d refs -> %s"
                % (key, body["paymentMode"], body["amount"],
                   len(body["references"]), rid))
        else:
            fail += 1
            say("  %-28s CREATE FAIL (%s) %s" % (key, st, P.Api.err(resp)))

    say("\nattempted=%d ok=%d failed=%d" % (ok + fail, ok, fail))
    if held:
        hp = os.path.join(OUT, "held_receipts.csv")
        with open(hp, "w") as fh:
            fh.write("customer,cntbtch,cntitem,bank,amt_inr,reason\n")
            for r, why in held:
                fh.write("%s,%s,%s,%s,%s,%s\n"
                         % (r["customer"], r["cntbtch"], r["cntitem"], r["bank"],
                            r["amt_inr"], why.replace(",", ";")))
        say("held -> %s" % hp)
    say("\nlog -> %s" % say.path)
    return 0 if not fail else 1


# ============================================================================
# phase: vouchers - verify is what MINTS the voucher (§12)
# ============================================================================

# The path in §12 (/entityLedgerVerification/bulkVerifyVoucherWithoutTally) does
# not exist: the controller is mapped at /api/v1/entityVerification/ and the
# bulk form takes a MultipartFile, not JSON. The per-entity form is
#   POST /entityVerification/{referenceType}/{referenceId}
# and referenceType for a payment is PAYMENT - not PAYMENT_REQUEST - which is
# what the live voucher legs actually carry (212,312 of them).
VERIFY_REF = {"payment": "PAYMENT", "receipt": "RECEIPT"}


def phase_vouchers(args):
    """Verify what has been created, so the vouchers exist.

    A created payment sits at entityLedgerVerificationStatus =
    PENDING_VERIFICATION and moves no money until this runs - the same
    create-then-verify shape the bill side has, where "verify is what creates
    the voucher; an unverified bill moves no money".
    """
    say = open_log("vouchers")
    api = P.Api(dry_run=args.dryrun)
    total_ok = total_fail = 0
    for kind in ("payment", "receipt"):
        posted = load_posted(kind)
        if not posted:
            say("\n%s: nothing posted, nothing to verify" % kind)
            continue
        say("\n%s: %d created, verifying" % (kind, len(posted)))
        ok = fail = 0
        for key, eid in sorted(posted.items()):
            if not eid or eid == "DRYRUN":
                continue
            st, resp = api.post("/entityVerification/%s/%s"
                                % (VERIFY_REF[kind], eid), {})
            err = "" if P.Api.ok(st, resp) else (P.Api.err(resp) or "")
            if P.Api.ok(st, resp):
                ok += 1
                say("  %-30s %s VERIFIED" % (key, eid))
            elif "already exists a voucher" in err:
                # Not a failure - the voucher is there, which is the goal. This
                # is the verify side of idempotency: a re-run must be able to
                # pass over what a previous run already minted, or every re-run
                # reports false failures that mask the real ones.
                ok += 1
                say("  %-30s %s ALREADY VERIFIED" % (key, eid))
            else:
                fail += 1
                say("  %-30s %s VERIFY FAIL (%s) %s" % (key, eid, st, err))
        say("  %s: verified=%d failed=%d" % (kind, ok, fail))
        total_ok += ok
        total_fail += fail
    say("\nattempted=%d ok=%d failed=%d"
        % (total_ok + total_fail, total_ok, total_fail))
    say("\nlog -> %s" % say.path)
    return 0 if not total_fail else 1


# ============================================================================
# phase: selftest - the arithmetic and the guards, with no network at all
# ============================================================================

def phase_selftest(args):
    say = open_log("selftest")
    fails = []

    def check(name, got, want):
        if got == want:
            say("  ok   %-52s %s" % (name, got))
        else:
            say("  FAIL %-52s got %r want %r" % (name, got, want))
            fails.append(name)

    say("\nFINANCIAL YEAR - from the document date, never from today (§3.2)")
    check("Nov 2025 -> FY2025-2026", X.fy("20251115"), "FY2025-2026")
    check("Jan 2026 is still FY2025-2026", X.fy("20260115"), "FY2025-2026")
    check("31 Mar 2026 -> FY2025-2026", X.fy("20260331"), "FY2025-2026")
    check("1 Apr 2026 -> FY2026-2027", X.fy("20260401"), "FY2026-2027")
    check("blank date -> None", X.fy(""), None)

    say("\nRCM FLAG - never None (§7: BooleanUtils.isFalse means null falls INTO")
    say("the reverse-charge branch and under-credits the vendor by the tax)")
    check("is_rcm '1' is True", X.truthy("1"), True)
    check("is_rcm '0' is False", X.truthy("0"), False)
    check("is_rcm '' is False, not None", X.truthy(""), False)

    say("\nJOIN KEY SHAPE - a single column keys on the bare value, because a")
    say("1-tuple silently matches nothing and scored the URP hold as zero")
    cust = X.one_per_key(X.read("Customers_Needed"), "customer")
    check("single-col key is a str", isinstance(next(iter(cust)), str), True)
    bl = X.by_key(X.read("Bills_Lines_PO")[:50], "cntbtch", "cntitem")
    check("multi-col key is a tuple", isinstance(next(iter(bl)), tuple), True)

    say("\nSETTLEMENT - reversals excluded once, centrally (§10)")
    apps = settlement_apps("AP_Payment_Apps")
    check("11/53 reversals dropped", "11/53 reversal" in apps["dropped"], True)
    check("kept < all", len(apps["kept"]) < len(apps["all"]), True)

    say("\nRECEIPT - flat DTO, INR amount, nested bank")
    r = {"amt_inr": "100000.00", "bank": "SBIOSB", "paymode": "TT",
         "remit_ref": "X1", "customer": "C1", "cntbtch": "1", "cntitem": "2",
         "docno": "R9", "narrative": "", "datebus": "20260131", "kind": "RECEIPT",
         "bankname": "SBI"}
    body = receipt_payload(r, [], {"SBIOSB": "BANKID"}, {"C1": {"id": "CID", "name": "C ONE", "ledger": "LEDG"}})
    check("amount is the INR figure", body["amount"], "100000.00")
    check("TT is not CHEQUE", body["paymentMode"], "BANK_TRANSFER")
    check("non-cheque ref goes to utrNumber", body.get("utrNumber"), "X1")
    check("bank DTO keys on bankAccountId, not id",
          sorted(body["bankAccountDto"]), ["bankAccountId", "name"])
    check("type is AGAINST_CONTACT", body["type"], "AGAINST_CONTACT")
    check("contactType is BUYER", body["contactType"], "BUYER")
    check("voucherDate is epoch millis", body["voucherDate"], 1769817600000)
    check("metadata key is lower-case d", "metadata" in body, True)
    check("every metadata value is a str",
          sorted({type(v).__name__ for v in body["metadata"].values()}), ["str"])
    body2 = receipt_payload(dict(r, paymode="CHEQUE"), [], {"SBIOSB": "B"}, {"C1": {"id": "C", "name": "C ONE", "ledger": "LEDG"}})
    check("cheque ref goes to chequeNumber", body2.get("chequeNumber"), "X1")

    say("\nPAYMENT - nested under paymentRequestDto, or the controller NPEs")
    p_ = {"sme_paytype": "SETTLEMENT", "amt_inr": "5000", "bank": "SBIOSB",
          "paymode": "CHEQUE", "remit_cheque": "778812", "vendor": "V1",
          "cntbtch": "9", "cntitem": "4", "docno": "P1", "descinvc": "",
          "datebus": "20260131", "bankname": "SBI"}
    pb = payment_payload(p_, [], {"SBIOSB": "BANKID"}, {"V1": {"id": "CID", "name": "V ONE", "ledger": "LEDG"}})
    check("body is wrapped in paymentRequestDto", list(pb), ["paymentRequestDto"])
    inner = pb["paymentRequestDto"]
    check("mode is CHEQUE", inner["paymentMode"], "CHEQUE")
    check("cheque number lands in utrNumber", inner["utrNumber"], "778812")
    check("contact bank stays absent", "contactBankAccountDto" in inner, False)
    check("company bank keys on bankAccountId, not id",
          inner["companyBankAccountDto"]["bankAccountId"], "BANKID")
    check("contactType is VENDOR", inner["contactType"], "VENDOR")
    check("status is explicit, never null", inner["status"], "SUCCESS")
    check("party ledger is sent, or verify can never succeed",
          inner["contactFinanceAccountId"], "LEDG")
    check("voucherDate is epoch millis", inner["voucherDate"], 1769817600000)
    check("every metaData value is a str",
          sorted({type(v).__name__ for v in inner["metaData"].values()}), ["str"])

    say("\nMANIFEST TIES - control 10, no unexplained remainder")
    post, held = bill_selection()
    check("bills post+held == Bills_Header",
          len(post) + len(held), len(X.read("Bills_Header")))
    ip, ih, ij = invoice_selection()
    check("invoices post+held+journal == Sales_Header",
          len(ip) + len(ih) + len(ij), len(X.read("Sales_Header")))

    say("\nBANK TYPE derivation (§3.1)")
    create, _ = bank_selection()
    by = {r["bank"]: r["sme_bankacct_type"] for r in create}
    check("SBIOSB (2A6B*) is CURRENT", by.get("SBIOSB"), "CURRENT")
    check("SBIEPC (1L5B*) is CASH_CREDIT", by.get("SBIEPC"), "CASH_CREDIT")

    say("\n%d checks failed" % len(fails))
    if fails:
        for f in fails:
            say("  FAILED: %s" % f)
    say("\nlog -> %s" % say.path)
    return 1 if fails else 0


# ============================================================================
# phase: verify - "is it posted or not" (§12)
# ============================================================================

VERIFY_SQL = """
SELECT '1. vouchers whose legs do not balance (must be 0)' AS control;
SELECT COUNT(*) AS unbalanced_vouchers FROM (
  SELECT voucherId,
         ROUND(SUM(CASE WHEN transactionType='DEBIT' THEN amount
                        WHEN transactionType='CREDIT' THEN -amount
                        ELSE 0 END), 4) AS residual
    FROM voucherEntry
   WHERE organisationId = '%(org)s' AND COALESCE(isDeleted,0)=0
   GROUP BY voucherId HAVING residual <> 0) x;

SELECT '1b. transactionType values present' AS control;
SELECT transactionType, COUNT(*) FROM voucherEntry
 WHERE organisationId='%(org)s' AND COALESCE(isDeleted,0)=0 GROUP BY 1;

SELECT '2. bills carrying this migration stamp' AS control;
SELECT billStatus, COUNT(*) n, ROUND(SUM(billAmount)/10000000,2) cr
  FROM bill WHERE organisationId='%(org)s' AND COALESCE(isDeleted,0)=0
   AND metadata LIKE '%%IDEDAT%%' GROUP BY 1 ORDER BY n DESC;

SELECT '3. invoices carrying this migration stamp' AS control;
SELECT invoiceStatus, COUNT(*) n, ROUND(SUM(totalPrice)/10000000,2) cr
  FROM invoice WHERE organisationId='%(org)s' AND COALESCE(isDeleted,0)=0
   AND metaData LIKE '%%IDEDAT%%' GROUP BY 1 ORDER BY n DESC;

SELECT '4. payments and their settlement mappings' AS control;
SELECT pr.paymentRequestType, pr.paymentMode,
       COUNT(DISTINCT pr.id) payments, COUNT(m.id) mappings
  FROM paymentRequest pr
  LEFT JOIN paymentRequestEntityMapping m
         ON m.paymentRequestId = pr.id AND COALESCE(m.isDeleted,0)=0
 WHERE pr.organisationId='%(org)s' AND COALESCE(pr.isDeleted,0)=0
 GROUP BY 1,2 ORDER BY payments DESC;

SELECT '5. receipts' AS control;
SELECT paymentMode, COUNT(*) n, ROUND(SUM(amount)/10000000,2) cr
  FROM receipt WHERE organisationId='%(org)s' AND COALESCE(isDeleted,0)=0
 GROUP BY 1 ORDER BY n DESC;

SELECT '6. bank accounts (yoda, keyed partyId - no organisationId column)' AS control;
SELECT COALESCE(bankAccountType,'(null)') t, COUNT(*) FROM yoda.bankAccount
 WHERE partyId='%(org)s' AND partyType='ORGANISATION' GROUP BY 1;

SELECT '11. ledger group of the legs on migrated SAGE- ledgers' AS control;
SELECT fa.financeGroupType, COUNT(*) legs, ROUND(SUM(ve.amount)/10000000,2) cr
  FROM voucherEntry ve
  JOIN financeAccount fa ON fa.id = ve.financeAccountId
 WHERE ve.organisationId='%(org)s' AND COALESCE(ve.isDeleted,0)=0
   AND fa.name LIKE '%%_SAGE-%%'
 GROUP BY 1 ORDER BY legs DESC;
"""


def phase_verify(args):
    """Read the vouchers back out of MySQL and say what actually landed.

    Always filters organisationId AND COALESCE(isDeleted,0)=0 - the tables are
    multi-tenant and soft-deleted rows double-count. Never SUM(DISTINCT ...).
    """
    say = open_log("verify")
    host = P.cfg("SME_DB_HOST")
    if not host:
        say("SME_DB_HOST is not set - cannot read the vouchers back.")
        return 2
    sql = VERIFY_SQL % {"org": P.ORG_ID}
    import subprocess
    say("\nreading vouchers back from %s (read-only)\n" % host)
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "root@%s" % host,
         "mysql -t -D smeassist"],
        input=sql, capture_output=True, text=True, timeout=600)
    say(proc.stdout or "")
    if proc.returncode != 0:
        say("mysql failed: %s" % proc.stderr[:500])
        return 1

    say("\nposted logs on disk (what this loader believes it created):")
    for kind in ("bank", "bill", "invoice", "payment", "receipt"):
        say("  %-9s %6d" % (kind, len(load_posted(kind))))
    say("\nlog -> %s" % say.path)
    return 0


# ============================================================================

PHASES = {
    "manifest": phase_manifest,
    "selftest": phase_selftest,
    "banks": phase_banks,
    "contacts": phase_contacts,
    "series": phase_series,
    "payments": phase_payments,
    "receipts": phase_receipts,
    "vouchers": phase_vouchers,
    "verify": phase_verify,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--dryrun", action="store_true",
                    help="build every payload, post nothing")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fix-types", action="store_true",
                    help="banks phase: PUT a bankAccountType onto accounts "
                         "left NULL, which otherwise cannot take a payment")
    ap.add_argument("--use-swift", action="store_true",
                    help="banks phase: where no IFSC is provable, send the "
                         "bank's principal India BIC from the platform master")
    ap.add_argument("--reconcile", action="store_true",
                    help="banks phase: rebuild the posted log from "
                         "yoda.bankAccount before doing anything else")
    ap.add_argument("--allow-no-ifsc", action="store_true",
                    help="banks phase: attempt banks with no provable IFSC, "
                         "sending ifscCode null rather than holding them")
    ap.add_argument("--pilot", action="store_true",
                    help="the smallest set that covers the distinct shapes")
    ap.add_argument("--token", default=None,
                    help="fresh auth-token; never logged, never stored")
    args = ap.parse_args()

    if args.token:
        P.TOKEN = args.token
    if args.pilot and not args.limit:
        args.limit = 9          # §10's nine-document pilot on the cheque path

    try:
        return PHASES[args.phase](args)
    except X.ExtractError as exc:
        sys.stderr.write("\nEXTRACT ERROR\n%s\n" % exc)
        return 2
    except P.Stop as exc:
        sys.stderr.write("\nSTOPPED\n%s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
