#!/usr/bin/env python
"""Master / reference-data reconciliation: SAGE vs SMEASSIST.

Read-only on both sides. Writes work/master-mismatches.json.

  .venv/bin/python work/master_recon.py                     # every check
  .venv/bin/python work/master_recon.py --detail            # + print first 40
  .venv/bin/python work/master_recon.py --check ledger_head # one or more
  .venv/bin/python work/master_recon.py --limit 500         # sample

WHY THIS EXISTS, given reconcile.py and value_recon.py already run

    Those two compare TRANSACTIONS - documents, lines, vouchers. Between them
    they run 18 checks and not one of them looks at whether the MASTER data the
    transactions point at agrees across the two systems.

    The gap that motivated this file: value_recon's only ledger check is
    `null_ledger`, which asks whether a line's finance account is MISSING. It
    never asks whether the account is under the RIGHT HEAD. So when 138 of the
    197 SAGE pseudo-items were re-headed, the old Direct-Expenses ledgers were
    left live in SMEAssist's chart of accounts - item_ledger_for() mints beside
    the old ledger rather than remapping it - and every existing check reported
    clean. item_master_recon.py counts "how many carry a ledger" and
    failure_report.py reports items_without_ledger: 0. Both true, both blind.

THE RULE THIS FILE IS BUILT AROUND

    A check whose data is UNAVAILABLE reports `unavailable`, never `clean`.

    This is not defensiveness for its own sake. The migration has already been
    bitten once by exactly this shape: with the Sage box unreachable,
    item_hsn_map() returns {} and resolve_item_hsn() silently skips its
    ICITEMO tier and falls through to the 9999 placeholder - a "successful" run
    that stamps a permanent wrong classification. A reconciler that answers
    "no differences found" when it could not read one side is worse than no
    reconciler, because it launders a blind spot into a clean bill of health.

    So every check records which sources it needed, whether it got them, and
    how many rows it actually compared. `summary.checks_unavailable` being
    non-empty means the run is PARTIAL, and the exit code says so.

INDEPENDENCE

    Both sides are read raw. Sage from ICITEM / ICITEMO / APVEN (or the
    idedat_staging mirror when the box is down, which is recorded as a
    DIFFERENT source, not silently substituted); SMEAssist from its own MySQL
    and from the finance-account API. Nothing is re-derived through
    classify() / build_payload() / ensure_products(), so a defect in the
    posting logic cannot hide by having been applied consistently to both
    sides. The only things borrowed from the loader are the join keys and the
    ITEM_MAPPING_BY_BILLTYPE table this is meant to audit against.

EXIT CODES
    0  ran every requested check, no mismatches
    1  ran, found mismatches
    2  partial - at least one check could not read a side it needed
    3  could not run at all
"""
import argparse
import collections
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import post_sage_bills as P                                     # noqa: E402
from decimal import Decimal as D                                # noqa: E402

OUT = os.path.join(P.WORK, "master-mismatches.json")

# The head each item mapping is expected to be minted under.
#
# Matching is EXACT on the normalised name, deliberately not a substring test.
# "Indirect Expenses" CONTAINS the substring "direct expense", so a substring
# matcher passes a ledger that is wrongly under Indirect Expenses while its
# mapping says ITEM_DIRECT_EXPENSE - a false negative in the one check this
# file exists to perform. Measured against the org's live chart of accounts,
# the stored values are already canonical:
#
#     ITEM_IN_DIRECT_EXPENSE -> "Indirect Expenses"    (133 ledgers)
#     ITEM_DIRECT_EXPENSE    -> "Direct Expenses"      (55)
#     ITEM_PURCHASE          -> "Purchase Accounts"    (7)
#
# so exactness costs nothing and removes the ambiguity. The extra names are
# the other heads item_ledger_for()'s docstring records as valid for each
# mapping; add to these rather than loosening to a substring.
EXPECTED_GROUP = {
    "ITEM_PURCHASE": {"purchase accounts", "raw material - purchase",
                      "packing material - indigenous"},
    "ITEM_DIRECT_EXPENSE": {"direct expenses", "repairs & maintenance",
                            "repairs and maintenance", "factory maintenance"},
    "ITEM_IN_DIRECT_EXPENSE": {"indirect expenses", "in direct expenses"},
}


def norm_group(v):
    """Collapse spacing/case so exact matching is not defeated by whitespace."""
    return " ".join(P.s(v).lower().split())

ALL_CHECKS = ["ledger_head", "ledger_orphan", "ledger_missing",
              "product_missing", "product_extra", "product_unit",
              "product_duplicate_sku",
              "contact_missing", "contact_gstin", "contact_state",
              "hsn_missing", "hsn_default", "hsn_mismatch", "gst_rate"]

# Which sources each check cannot run without. Checked BEFORE the check runs so
# an unreadable side is reported as unavailable rather than as agreement.
NEEDS = {
    "ledger_head":           ("crosswalk", "api"),
    "ledger_orphan":         ("crosswalk", "api"),
    "ledger_missing":        ("crosswalk", "api"),
    "product_missing":       ("sage_items", "sme_products"),
    "product_extra":         ("sage_items", "sme_products"),
    "product_unit":          ("sage_items", "sme_products"),
    "product_duplicate_sku": ("sme_products",),
    "contact_missing":       ("sage_vendors", "sme_contacts"),
    "contact_gstin":         ("sage_vendors", "sme_contacts"),
    "contact_state":         ("sage_vendors", "sme_contacts"),
    "hsn_missing":           ("sage_items", "sme_products"),
    "hsn_default":           ("sme_products",),
    "hsn_mismatch":          ("sage_hsn", "sme_products"),
    "gst_rate":              ("sage_items", "sme_products"),
}

SEVERITY = {
    # A ledger under the wrong head misstates the P&L, and every bill already
    # posted against it carries the error. Nothing here is cosmetic.
    "ledger_head": "high",
    "ledger_orphan": "medium",
    "ledger_missing": "high",
    "product_missing": "medium",
    "product_extra": "low",
    "product_unit": "medium",
    "product_duplicate_sku": "high",
    "contact_missing": "medium",
    "contact_gstin": "high",
    "contact_state": "high",
    "hsn_missing": "medium",
    "hsn_default": "low",
    "hsn_mismatch": "high",
    "gst_rate": "high",
}


# ---------------------------------------------------------------- plumbing
def q(sql, db="smeassist"):
    """Read-only MySQL over a multiplexed ssh connection.

    --batch WITHOUT --raw, exactly as work/value_recon.py: descriptions carry
    newlines and tabs, and --raw passes them through verbatim, splitting one
    row across several lines and silently corrupting every column after it.
    """
    p = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "-o", "ControlMaster=auto",
                        "-o", "ControlPath=/tmp/.sage-cm-%r",
                        "-o", "ControlPersist=600",
                        P.db_host(), "mysql %s -N --batch -e %s"
                        % (db, json.dumps(sql))],
                       capture_output=True, text=True, timeout=900)
    if p.returncode:
        raise Unavailable("%s query failed: %s" % (db, p.stderr.strip()[:300]))
    return [ln.split("\t") for ln in p.stdout.splitlines() if ln.strip()]


class Unavailable(Exception):
    """A side could not be read. Never caught into a 'clean' result."""


def norm_unit(v):
    """Units are compared case- and space-insensitively.

    Sage writes 'NOS', 'Nos', 'nos ' for the same unit and SMEAssist
    title-cases on create, so a literal comparison reports thousands of
    mismatches that are not differences.
    """
    return P.s(v).strip().upper().replace(".", "")


def norm_gstin(v):
    return P.s(v).strip().upper().replace(" ", "")


def norm_state(v):
    """STATE_ENUM spells states with underscores ('TAMIL_NADU') while Sage
    writes them with spaces ('TAMIL NADU'). Comparing the two raw reported
    every single vendor as a mismatch."""
    return " ".join(P.s(v).upper().replace("_", " ").split())


def sku_of(item, unit):
    """The SKU the loader posts under, so both sides join on the same string."""
    return "SAGE-" + P.s(item).replace("-", "").replace("/", "") \
           + "-" + norm_unit(unit)


# ------------------------------------------------------------------ sources
class Sources:
    """Lazily loads each side, and REMEMBERS what it could not read.

    Every loader either populates its slot or records why it is missing. No
    loader substitutes a different source silently: when the Sage box is down
    and the staging mirror stands in, that is recorded as source
    'idedat_staging' so the report says which data actually answered.
    """

    def __init__(self, org, limit=None):
        self.org = org
        self.limit = limit
        self.have = {}
        self.missing = {}
        self.origin = {}

    def _try(self, name, fn, origin):
        if name in self.have or name in self.missing:
            return self.have.get(name)
        try:
            v = fn()
            self.have[name] = v
            # A loader that chose between sources records the one it actually
            # used; do not overwrite it with the generic label. Reporting
            # "Sage APVEN / staging" when the staging mirror answered would
            # hide which data backed the result.
            self.origin.setdefault(name, origin)
            print("  %-14s %s  (%d)"
                  % (name, self.origin[name], len(v)), flush=True)
            return v
        except Unavailable as exc:
            self.missing[name] = str(exc)
            print("  %-14s UNAVAILABLE: %s" % (name, exc), flush=True)
        except Exception as exc:                                # noqa: BLE001
            self.missing[name] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            print("  %-14s UNAVAILABLE: %s" % (name, self.missing[name]),
                  flush=True)
        return None

    # ---- Sage -----------------------------------------------------------
    def sage_items(self):
        """{sku: {item, unit, name, category}} from the goods book.

        load_goods_book() reads idedat_staging, not the Sage box, so this one
        survives the box being down - but it is the STAGING mirror of the item
        master, not ICITEM itself, and is labelled as such.
        """
        def load():
            book = P.load_goods_book()
            if not book:
                raise Unavailable("load_goods_book() returned nothing")
            # Field names verified by dumping a real line, NOT guessed: the
            # goods book uses um / descr / rate / item_raw. An earlier version
            # read ln['unit'] and ln['uom'], both of which are absent, so
            # every SKU came out as 'SAGE-<item>-' with an empty unit and
            # matched nothing - product_missing and product_extra then each
            # reported every row on their side.
            out = {}
            for bill in book.values():
                for ln in bill.get("lines", []):
                    item = P.s(ln.get("item"))
                    if not item:
                        continue
                    unit = norm_unit(ln.get("um") or ln.get("stock_um"))
                    if not unit:
                        continue
                    # item_raw is Sage's own unformatted number, which is what
                    # the SKU is built from. Prefer it over re-stripping the
                    # formatted one.
                    raw = P.s(ln.get("item_raw")) or item
                    sku = "SAGE-%s-%s" % (raw, unit)
                    rec = out.setdefault(sku, {
                        "item": item, "raw": raw, "unit": unit,
                        "name": P.s(ln.get("descr")),
                        "category": P.s(ln.get("category")),
                        "hsn": P.s(ln.get("hsn")),
                        "rate": ln.get("rate"),
                        # EVERY line HSN this item was billed under, not just
                        # the last one. The same item legitimately appears on
                        # many lines, and hsn_mismatch has to know the whole
                        # set: an item billed under 48211010 on one document
                        # and 48211020 on another is not in disagreement with
                        # itself, and keying on whichever line happened to be
                        # read last reported both orderings as defects.
                        "line_hsns": set(),
                        # Same reasoning as line_hsns: an item billed at 5%
                        # on one line and 18% on another has no single "Sage
                        # rate". Keeping a set makes the comparison stable -
                        # keying on one arbitrary line made the result depend
                        # on dict-write order, and flipping setdefault/assign
                        # moved this count between 4 and 76.
                        "line_rates": set(),
                    })
                    lh = P.normalise_hsn(ln.get("hsn"))
                    if lh:
                        rec["line_hsns"].add(lh)
                    if ln.get("rate") is not None:
                        try:
                            rec["line_rates"].add(P.q2(D(str(ln["rate"]))))
                        except Exception:                       # noqa: BLE001
                            pass
            return out
        return self._try("sage_items", load, "idedat_staging.sage_item")

    # ICITEMO holds 258,770 non-empty HSNCODE rows. A load that returns
    # dramatically fewer did not read the tier, whatever it returned.
    HSN_FLOOR = 1000

    def sage_hsn(self):
        """The ICITEMO optional-field tier. Sage box only - staging never
        pulled HSNCODE, which is the whole reason resolve_item_hsn has an
        ICITEMO tier. If the box is down this is UNAVAILABLE, not empty.

        Deliberately NOT via P.item_hsn_map(): that helper CATCHES the
        connection failure, prints 'Sage unreachable', and returns its
        near-empty module-level cache. Called from here that is indistinguish-
        able from 'Sage has almost no HSNs', and this check would then compare
        nothing and report clean - laundering a dead source into a pass. Going
        straight at sage_query lets the OperationalError propagate, and the
        floor catches a connection that opens but returns a truncated result.
        """
        def load():
            rows = P.sage_query(P.SQL_ITEM_HSN)
            m = {P.s(r["itemno"]): P.s(r["hsn"]) for r in rows
                 if P.s(r.get("hsn"))}
            if len(m) < self.HSN_FLOOR:
                raise Unavailable(
                    "ICITEMO returned only %d HSN rows (expected >%d); "
                    "treating the tier as unread rather than as agreement"
                    % (len(m), self.HSN_FLOOR))
            return m
        return self._try("sage_hsn", load, "Sage ICITEMO")

    def sage_vendors(self):
        """{vendor code: row}. Prefers APVEN; falls back to the staging
        mirror, recorded under its own name."""
        def load():
            try:
                rows = P.sage_query(P.SQL_VENDORS)
                origin = "Sage APVEN"
            except Exception as exc:                            # noqa: BLE001
                rows = None
                origin = None
                first = str(exc)[:120]
            if rows is None:
                try:
                    raw = P.staging_query(P.SQL_STG_VENDORS)
                except Exception as exc2:                       # noqa: BLE001
                    raise Unavailable(
                        "APVEN unreadable (%s) and staging mirror also "
                        "unreadable (%s)" % (first, str(exc2)[:120]))
                rows = [dict(zip(P.STG_VENDOR_COLS, r)) for r in raw]
                origin = "idedat_staging.sage_vendor"
            self.origin["sage_vendors"] = origin
            return {P.s(r["vendor"]): r for r in rows if P.s(r.get("vendor"))}
        v = self._try("sage_vendors", load, "Sage APVEN / staging")
        return v

    # ---- SMEAssist ------------------------------------------------------
    def sme_products(self):
        """{skuCode: row} for the org's live products, with the finance
        account the product's item ledger resolves to."""
        def load():
            # unitOfMeasurement, not primaryUnit - verified against
            # information_schema; product has no primaryUnit column.
            rows = q("SELECT p.skuCode, p.productName, "
                     "COALESCE(p.unitOfMeasurement,''), COALESCE(p.hsnCode,''), "
                     "COALESCE(p.gstPercentage,''), p.id, "
                     "COALESCE(p.typeOfStock,'') "
                     "FROM product p WHERE p.organisationId='%s' "
                     "AND p.isDeleted+0=0" % self.org)
            out = {}
            for r in rows:
                if len(r) != 7 or not r[0]:
                    continue
                out[r[0]] = {"sku": r[0], "name": r[1],
                             "unit": norm_unit(r[2]), "hsn": P.s(r[3]),
                             "rate": r[4], "id": r[5], "stock": r[6]}
            if not out:
                raise Unavailable("product returned no rows for org %s"
                                  % self.org)
            return out
        return self._try("sme_products", load, "SMEAssist product")

    def sme_contacts(self):
        """{contactId: row} for the org's contacts.

        The name is accountName: contact has no `name` column, companyName
        mirrors accountName, and labelName is NULL on every row sampled.
        There is no contactType column either - registrationType ('GST' /
        'WITHOUT_PAN_OR_GST') is what distinguishes them.
        """
        def load():
            rows = q("SELECT c.id, COALESCE(c.registrationNumber,''), "
                     "COALESCE(c.accountName,''), "
                     "COALESCE(c.registrationType,'') "
                     "FROM contact c WHERE c.organisationId='%s' "
                     "AND c.isDeleted+0=0" % self.org)
            out = {}
            for r in rows:
                if len(r) != 4:
                    continue
                # MySQL --batch renders NULL as the four characters 'NULL'.
                gst = norm_gstin(r[1])
                out[r[0]] = {"id": r[0],
                             "gstin": "" if gst == "NULL" else gst,
                             "name": P.s(r[2]), "regtype": r[3]}
            if not out:
                raise Unavailable("contact returned no rows for org %s"
                                  % self.org)
            return out
        return self._try("sme_contacts", load, "SMEAssist contact")

    def sme_finance_accounts(self):
        """{financeAccountId: {name, group}} for the whole org.

        Read from MySQL rather than per-product over the API: the ledger-head
        check needs the group of ~6,200 accounts and the API would be ~6,200
        round trips against a service that rate-limits at 63 writes/min.
        """
        def load():
            # `leaf`, not `isLeaf` - verified against information_schema.
            # netBalance is read because an orphaned ledger's EXISTENCE is
            # cosmetic but its BALANCE is not: measured, 50 of 138 orphans
            # hold Rs 8.73 crore between them. A check that only proves the
            # row exists rates that medium and buries it.
            rows = q("SELECT f.id, COALESCE(f.name,''), "
                     "COALESCE(f.accountingGroupName,''), "
                     "COALESCE(f.leaf+0,0), COALESCE(f.netBalance,0) "
                     "FROM financeAccount f WHERE f.organisationId='%s' "
                     "AND f.isDeleted+0=0" % self.org)
            out = {}
            for r in rows:
                if len(r) != 5:
                    continue
                try:
                    bal = D(str(r[4] or 0))
                except Exception:                               # noqa: BLE001
                    bal = D(0)
                out[r[0]] = {"name": r[1], "group": r[2], "leaf": r[3],
                             "balance": bal}
            if not out:
                raise Unavailable("financeAccount returned no rows for org %s"
                                  % self.org)
            return out
        return self._try("sme_finance_accounts", load,
                         "SMEAssist financeAccount")

    def crosswalk(self):
        def load():
            if not os.path.exists(P.CROSSWALK):
                raise Unavailable("%s does not exist" % P.CROSSWALK)
            with open(P.CROSSWALK, encoding="utf-8-sig") as fh:
                d = json.load(fh)
            if not d.get("products") and not d.get("items"):
                raise Unavailable("crosswalk carries no products or items")
            return d
        return self._try("crosswalk", load, os.path.basename(P.CROSSWALK))

    def api(self):
        """Present iff the finance-account table could be read - that is the
        SMEAssist side the ledger checks actually consult."""
        return self.sme_finance_accounts()


# ------------------------------------------------------------------- checks
class Report:
    def __init__(self):
        self.rows = []
        self.ran = []
        self.unavailable = {}
        self.compared = collections.Counter()

    def add(self, check, key, field, sage, sme, note=None, severity=None):
        """`severity` overrides the per-check default for one row.

        Needed because a check can find two different classes of thing: a
        cross-system state disagreement drives IGST vs CGST/SGST and is high,
        while a Sage-internal spelling variance the loader already overrides
        via the GSTIN prefix is not. Reporting both as high is how a report
        stops being read.
        """
        self.rows.append({
            "check": check,
            "severity": severity or SEVERITY.get(check, "medium"),
            "key": key, "field": field,
            "sage": sage, "smeassist": sme,
            **({"note": note} if note else {})})

    def skip(self, check, why):
        self.unavailable[check] = why
        print("  SKIP %-22s %s" % (check, why), flush=True)


def check_ledgers(src, rep, checks):
    """The gap this file was written for.

    Three distinct questions, deliberately separate checks:

      ledger_head     the ledger the crosswalk points at is minted under a
                      group that does not match its itemMapping
      ledger_orphan   a priorLedger from a re-heading is still live, so the
                      chart of accounts carries a head with no Sage counterpart
      ledger_missing  the crosswalk names a ledger the org does not have
    """
    xw = src.crosswalk()
    fa = src.api()
    for c in ("ledger_head", "ledger_orphan", "ledger_missing"):
        if c not in checks:
            continue
        if xw is None:
            rep.skip(c, "crosswalk: " + src.missing.get("crosswalk", "?"))
        elif fa is None:
            rep.skip(c, "financeAccount: "
                     + src.missing.get("sme_finance_accounts", "?"))
    if xw is None or fa is None:
        return

    records = []
    for key, rec in (xw.get("products") or {}).items():
        records.append(("pseudo-item", key, rec))
    for key, rec in (xw.get("items") or {}).items():
        records.append(("item", key, rec))

    for kind, key, rec in records:
        if not isinstance(rec, dict):
            continue
        led = P.s(rec.get("ledger"))
        mapping = P.s(rec.get("itemMapping"))
        prior = P.s(rec.get("priorLedger"))
        name = P.s(rec.get("name"))

        if "ledger_missing" in checks and led:
            rep.compared["ledger_missing"] += 1
            if led not in fa:
                rep.add("ledger_missing", key, "ledger", None, led,
                        "%s %s: crosswalk names a finance account the org "
                        "does not have" % (kind, name[:40]))

        # An item record carries no itemMapping - only the pseudo-items do -
        # so the head test can only speak to those. Saying so beats guessing
        # a mapping and reporting a mismatch against the guess.
        if "ledger_head" in checks and led and mapping and led in fa:
            rep.compared["ledger_head"] += 1
            want = EXPECTED_GROUP.get(mapping)
            got = P.s(fa[led].get("group"))
            if want and got and norm_group(got) not in want:
                rep.add("ledger_head", key, "accountingGroupName",
                        "expected one of %s (for %s)" % (sorted(want), mapping),
                        got,
                        "%s %s: ledger %s is minted under a group that does "
                        "not match its mapping" % (kind, name[:40], led))
            elif want and not got:
                rep.add("ledger_head", key, "accountingGroupName",
                        "expected one of %s (for %s)" % (list(want), mapping),
                        "", "%s %s: ledger %s carries no accountingGroupName, "
                        "so its head cannot be confirmed"
                        % (kind, name[:40], led))

        if "ledger_orphan" in checks and prior and prior != led:
            rep.compared["ledger_orphan"] += 1
            if prior in fa:
                bal = fa[prior].get("balance") or D(0)
                grp = fa[prior].get("group") or "?"
                if bal:
                    # Money is sitting under the superseded head. The P&L
                    # total is right but the group split is not, and it stays
                    # wrong until the bills that booked it are revoked and
                    # reposted - item_ledger_for mints beside the old ledger
                    # rather than moving its balance.
                    rep.add("ledger_orphan", key, "priorLedger balance",
                            None, "%s on '%s'" % (bal, grp),
                            "%s %s: re-headed to %s, but %s is still booked "
                            "on the superseded ledger %s - the Direct/Indirect "
                            "split is misstated by this amount until those "
                            "bills are revoked and reposted"
                            % (kind, name[:40], led, abs(bal), prior),
                            severity="high")
                else:
                    rep.add("ledger_orphan", key, "priorLedger", None, prior,
                            "%s %s: re-headed to %s but the old ledger is "
                            "still live under group '%s' - carries no "
                            "balance, so this is a stale head only"
                            % (kind, name[:40], led, grp),
                            severity="low")


def check_products(src, rep, checks):
    want = {"product_missing", "product_extra", "product_unit",
            "product_duplicate_sku", "hsn_missing", "hsn_default",
            "gst_rate"} & set(checks)
    if not want:
        return
    sme = src.sme_products()
    sage = src.sage_items() if want - {"product_duplicate_sku", "hsn_default"} \
        else True
    for c in sorted(want):
        if sme is None:
            rep.skip(c, "product: " + src.missing.get("sme_products", "?"))
        elif sage is None and c not in ("product_duplicate_sku", "hsn_default"):
            rep.skip(c, "sage items: " + src.missing.get("sage_items", "?"))
    if sme is None:
        return

    if "product_duplicate_sku" in checks:
        # SKUs are the join key for the whole migration; two products on one
        # SKU means adoption picked one arbitrarily and the other holds
        # stranded balances.
        seen = collections.Counter(k for k in sme)
        rep.compared["product_duplicate_sku"] = len(sme)
        for k, n in seen.items():
            if n > 1:
                rep.add("product_duplicate_sku", k, "skuCode", None, n,
                        "%d products share this SKU" % n)

    if "hsn_default" in checks:
        rep.compared["hsn_default"] = len(sme)
        for k, r in sme.items():
            if P.s(r["hsn"]) == P.GOODS_HSN_DEFAULT:
                rep.add("hsn_default", k, "hsnCode", None, r["hsn"],
                        "on the %s placeholder - needs a real HSN before "
                        "filing" % P.GOODS_HSN_DEFAULT)

    if sage is None or sage is True:
        return

    limit = src.limit
    keys = sorted(sage)
    if limit:
        keys = keys[:limit]

    for k in keys:
        srow = sage[k]
        mrow = sme.get(k)
        if mrow is None:
            if "product_missing" in checks:
                rep.compared["product_missing"] += 1
                rep.add("product_missing", k, "product", srow["item"], None,
                        "Sage bills this item; SMEAssist has no product for it")
            continue
        if "product_unit" in checks:
            rep.compared["product_unit"] += 1
            if srow["unit"] and mrow["unit"] and srow["unit"] != mrow["unit"]:
                rep.add("product_unit", k, "primaryUnit",
                        srow["unit"], mrow["unit"])
        if "hsn_missing" in checks:
            rep.compared["hsn_missing"] += 1
            if not P.s(mrow["hsn"]):
                rep.add("hsn_missing", k, "hsnCode", srow.get("hsn") or None,
                        "", "SMEAssist product carries no HSN at all")
        rates = srow.get("line_rates") or set()
        if "gst_rate" in checks and rates:
            rep.compared["gst_rate"] += 1
            mr = P.q2(D(str(mrow["rate"] or 0)))
            # Flag only when the product's default matches NONE of the rates
            # the item was actually billed at. An item billed at several rates
            # is normal, and a default equal to any of them is defensible.
            if mr != 0 and mr not in rates:
                # HONEST ABOUT THE FIELDS: this compares Sage's LINE rates
                # against SMEAssist's PRODUCT-MASTER default. They are not the
                # same field, and a product legitimately billed at a rate other
                # than its default is not an error. What this flags is a
                # candidate to inspect, not a proven tax mismatch - the
                # per-line rate comparison is value_recon.py's gst_rate check,
                # which reads the line on both sides.
                shown = ", ".join(str(x) for x in sorted(rates))
                rep.add("gst_rate", k, "gstPercentage", shown, float(mr),
                        "product default matches none of the %d rate(s) this "
                        "item was billed at. Sage LINE rate vs SMEAssist "
                        "PRODUCT-MASTER default are different fields; confirm "
                        "against value_recon's per-line gst_rate before "
                        "treating as a tax error" % len(rates))

    if "product_extra" in checks and not limit:
        rep.compared["product_extra"] = len(sme)
        for k in sme:
            if k.startswith("SAGE-") and k not in sage:
                rep.add("product_extra", k, "product", None, k,
                        "SMEAssist has a SAGE-* product Sage does not bill in "
                        "this window")


def check_hsn_mismatch(src, rep, checks):
    if "hsn_mismatch" not in checks:
        return
    sme = src.sme_products()
    hsn = src.sage_hsn()
    if sme is None:
        return rep.skip("hsn_mismatch",
                        "product: " + src.missing.get("sme_products", "?"))
    if hsn is None:
        # THE important skip. Silently treating an unreadable ICITEMO as "no
        # HSN differences" is the failure mode this whole file guards against.
        return rep.skip("hsn_mismatch",
                        "ICITEMO: " + src.missing.get("sage_hsn", "?"))
    items = src.sage_items() or {}
    for k, srow in items.items():
        mrow = sme.get(k)
        if not mrow:
            continue
        raw = P.s(srow.get("item"))
        want = P.normalise_hsn(hsn.get(raw.replace("-", "")) or hsn.get(raw))
        got = P.s(mrow["hsn"])
        if not want or not got:
            continue
        rep.compared["hsn_mismatch"] += 1
        if want == got:
            continue

        # RESPECT THE RESOLVER'S PRECEDENCE. resolve_item_hsn() is
        # line > sibling > ICITEMO > placeholder, deliberately: the HSN Sage
        # states on the line it actually billed outranks the item master. So a
        # product carrying a line HSN that differs from ICITEMO is CORRECT, not
        # a defect. Comparing raw against ICITEMO reported 387 of 396 rows as
        # mismatches when the resolver had done exactly the right thing - and
        # buried the 9 that mattered.
        lines = srow.get("line_hsns") or set()
        if got in lines:
            continue                     # the line won, as designed

        if not lines:
            # No line stated an HSN, so ICITEMO was the top live tier and
            # should have supplied it. Landing on the placeholder instead
            # means the tier was unreadable when this product was created.
            note = ("no line HSN, so ICITEMO %s should have been used; "
                    "product carries %s" % (want, got))
            if got == P.GOODS_HSN_DEFAULT:
                note += (" - the placeholder, i.e. created while Sage was "
                         "unreachable; Sage can supply the real code now")
            rep.add("hsn_mismatch", k, "hsnCode", want, got, note)
        else:
            # Matches neither the item master nor any line it was billed
            # under - the value came from somewhere else entirely.
            rep.add("hsn_mismatch", k, "hsnCode", want, got,
                    "matches neither ICITEMO (%s) nor any line HSN (%s)"
                    % (want, ", ".join(sorted(lines))))


def check_contacts(src, rep, checks):
    want = {"contact_missing", "contact_gstin", "contact_state"} & set(checks)
    if not want:
        return
    sage = src.sage_vendors()
    sme = src.sme_contacts()
    for c in sorted(want):
        if sage is None:
            rep.skip(c, "vendors: " + src.missing.get("sage_vendors", "?"))
        elif sme is None:
            rep.skip(c, "contact: " + src.missing.get("sme_contacts", "?"))
    if sage is None or sme is None:
        return

    by_gstin = {}
    for r in sme.values():
        if r["gstin"]:
            by_gstin.setdefault(r["gstin"], r)
    by_name = {}
    for r in sme.values():
        if r["name"]:
            by_name.setdefault(r["name"].strip().upper(), r)

    for code, v in sorted(sage.items()):
        # APVEN.BRN is free text: "GST - 29XXXPX0001X1ZN", "29XXXPX0005 X1ZV".
        # A raw string compare reports those as disagreeing with the very same
        # GSTIN stored cleanly in SMEAssist - measured: 1 of 1 "genuine"
        # mismatches was this artefact. extract_gstin is borrowed as a PARSER
        # of Sage's own text (it repairs and infers nothing), not as a
        # derivation of the answer being checked.
        gstin = norm_gstin(P.extract_gstin(P.s(v.get("brn_raw")).upper()) or "")
        name = P.s(v.get("name")).strip().upper()
        hit = by_gstin.get(gstin) if gstin else None
        if hit is None:
            hit = by_name.get(name)

        if "contact_missing" in checks:
            rep.compared["contact_missing"] += 1
            if hit is None:
                rep.add("contact_missing", code, "contact",
                        P.s(v.get("name"))[:60], None,
                        "Sage vendor has no SMEAssist contact - this is the "
                        "no_vendor_contact backlog, by vendor")
                continue
        if hit is None:
            continue

        if "contact_gstin" in checks and gstin:
            rep.compared["contact_gstin"] += 1
            if hit["gstin"] and hit["gstin"] != gstin:
                rep.add("contact_gstin", code, "registrationNumber",
                        gstin, hit["gstin"],
                        "matched by name; the GSTINs disagree")
            elif not hit["gstin"] and hit["regtype"] == "GST":
                # Only a contact that CLAIMS to be GST-registered and carries
                # no number is a defect. A WITHOUT_PAN_OR_GST contact having
                # no GSTIN is the correct state, not a mismatch - measured, 67
                # of 68 findings here were that, which buried the 1 real one.
                rep.add("contact_gstin", code, "registrationNumber",
                        gstin, "",
                        "SMEAssist contact is registrationType=GST but holds "
                        "no number, while Sage states one")

        if "contact_state" in checks:
            # There is NO contact address table carrying a state on this
            # platform - contact has no state column and nothing matching
            # '%address%' exists in the schema. So state is compared where it
            # is actually authoritative on both sides: the GSTIN's first two
            # digits. That is also what decides IGST vs CGST/SGST on every
            # bill for the vendor, which is why this is high severity.
            #
            # Deliberately NOT routed through P.resolve_state(): that is the
            # loader's own derivation, and testing it against itself would
            # confirm nothing. Only GST_STATE_CODES - the published code
            # table, reference data rather than logic - is borrowed.
            sgst, mgst = gstin, hit["gstin"]
            if len(sgst) >= 2 and sgst[:2].isdigit() \
                    and len(mgst) >= 2 and mgst[:2].isdigit():
                rep.compared["contact_state"] += 1
                if sgst[:2] != mgst[:2]:
                    rep.add("contact_state", code, "state (GSTIN prefix)",
                            "%s (%s)" % (sgst[:2],
                                         P.GST_STATE_CODES.get(sgst[:2], "?")),
                            "%s (%s)" % (mgst[:2],
                                         P.GST_STATE_CODES.get(mgst[:2], "?")),
                            "drives IGST vs CGST/SGST on every bill for this "
                            "vendor")
            # Sage's own CODESTTE is free text and holds city names, so a
            # disagreement with its OWN GSTIN is a Sage-side data-quality
            # finding, not a migration difference. Reported separately so it
            # is not mistaken for SMEAssist drift.
            # 'NULL' is the four characters MySQL --batch writes for a null,
            # and an ABSENT CODESTTE is not an inconsistency - it is why
            # resolve_state prefers the GSTIN in the first place. Measured, 52
            # of 52 findings here were an absent or underscore-spelled state.
            sraw = norm_state(v.get("state_raw"))
            if sraw and sraw != "NULL" and len(sgst) >= 2 \
                    and sgst[:2].isdigit():
                want = norm_state(P.GST_STATE_CODES.get(sgst[:2]))
                if want and want != sraw:
                    rep.add("contact_state", code, "state (Sage internal)",
                            "%s says %s; its GSTIN %s says %s"
                            % ("CODESTTE", sraw, sgst[:2], want),
                            None,
                            "Sage-side inconsistency, not SMEAssist drift - "
                            "resolve_state() prefers the GSTIN, so the "
                            "migration is unaffected",
                            severity="low")


# --------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Master/reference-data reconciliation: Sage vs SMEAssist")
    ap.add_argument("--check", help="comma-separated subset of: "
                    + ",".join(ALL_CHECKS))
    ap.add_argument("--detail", action="store_true",
                    help="print the first 40 mismatches")
    ap.add_argument("--limit", type=int,
                    help="compare at most N Sage items (sampling)")
    a = ap.parse_args(argv)

    checks = ALL_CHECKS
    if a.check:
        checks = [c.strip() for c in a.check.split(",") if c.strip()]
        bad = [c for c in checks if c not in ALL_CHECKS]
        if bad:
            sys.exit("unknown check(s): %s\nknown: %s"
                     % (", ".join(bad), ", ".join(ALL_CHECKS)))

    org = P.ORG_ID
    if not org:
        sys.exit("SME_ORG_ID is not set")

    print("MASTER RECON  org=%s  window=%s-%s" % (org, P.DATE_FROM, P.DATE_TO))
    print("checks: %s\n" % ", ".join(checks))
    print("SOURCES", flush=True)
    src = Sources(org, limit=a.limit)
    rep = Report()

    print("\nCHECKS", flush=True)
    check_ledgers(src, rep, checks)
    check_products(src, rep, checks)
    check_hsn_mismatch(src, rep, checks)
    check_contacts(src, rep, checks)

    ran = [c for c in checks if c not in rep.unavailable]
    by_check = collections.Counter(r["check"] for r in rep.rows)
    by_sev = collections.Counter(r["severity"] for r in rep.rows)

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "organisationId": org,
        "window": {"from": P.DATE_FROM, "to": P.DATE_TO},
        "checks_requested": checks,
        "checks_run": ran,
        "checks_unavailable": rep.unavailable,
        "partial": bool(rep.unavailable),
        "sources": {"read": src.origin,
                    "unreadable": src.missing},
        "summary": {
            "rows_compared": dict(rep.compared),
            "mismatch_rows": len(rep.rows),
            "by_check": dict(by_check),
            "by_severity": dict(by_sev),
        },
        # Ordered highest-severity-first BEFORE the cap. Truncating in
        # insertion order dropped high-severity rows off the end of a 19,234
        # row run while keeping thousands of low ones - the consumer then
        # cannot see the findings that matter. repost_stranded_parts.py reads
        # a sibling report to decide what to revoke, so a report that loses
        # its own worst rows is actively dangerous.
        "mismatches": sorted(
            rep.rows,
            key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(
                r["severity"], 9))[:5000],
        "mismatches_total": len(rep.rows),
        "mismatches_truncated": max(0, len(rep.rows) - 5000),
    }
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True, default=str)

    print("\nSUMMARY")
    print("  compared      : %s" % (dict(rep.compared) or "nothing"))
    print("  mismatch rows : %d" % len(rep.rows))
    # Report the severities the ROWS actually carry. Printing
    # SEVERITY[check] here labelled 6 rows that add() had downgraded to low as
    # 'high', which is the report lying about its own contents.
    sev_of = collections.defaultdict(collections.Counter)
    for r in rep.rows:
        sev_of[r["check"]][r["severity"]] += 1
    for c, n in by_check.most_common():
        mix = sev_of[c]
        label = mix.most_common(1)[0][0] if len(mix) == 1 else \
            ", ".join("%d %s" % (v, k) for k, v in mix.most_common())
        print("      %-22s %d  (%s)" % (c, n, label))

    if a.detail and rep.rows:
        # Highest severity first: a run with 138 medium rows and 2 high ones
        # must not bury the 2.
        order = {"high": 0, "medium": 1, "low": 2}
        rows = sorted(rep.rows, key=lambda r: order.get(r["severity"], 9))
        print("\nDETAIL (first 40 of %d, highest severity first)"
              % len(rep.rows))
        for r in rows[:40]:
            print("  [%-6s] %-22s %s" % (r["severity"], r["check"], r["key"]))
            print("           %s" % r["field"])
            print("           sage      : %s" % r["sage"])
            print("           smeassist : %s" % r["smeassist"])
            if r.get("note"):
                print("           %s" % r["note"])
    if rep.unavailable:
        print("\n  PARTIAL RUN - these checks could not read a side they need:")
        for c, why in sorted(rep.unavailable.items()):
            print("      %-22s %s" % (c, why))
        print("\n  These are NOT reported as clean. Re-run when the source is"
              "\n  reachable before treating this as a full reconciliation.")
    print("\nwrote %s" % OUT)

    if rep.unavailable:
        return 2
    return 1 if rep.rows else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:                                    # noqa: BLE001
        print("could not run: %s: %s" % (type(exc).__name__, exc))
        sys.exit(3)
