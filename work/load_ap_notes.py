#!/usr/bin/env python3
"""
load_ap_notes.py  --  the AP credit/debit notes, TDS first.

    python3 load_ap_notes.py --check      # the plan, nothing posted
    python3 load_ap_notes.py --dry-run    # every JSON body, nothing posted
    python3 load_ap_notes.py --tds-only --limit 5    # pilot
    python3 load_ap_notes.py --tds-only   # the 1,352 TDS notes
    python3 load_ap_notes.py              # all 2,203

WHY THESE COME BEFORE PAYMENTS, and why loading them late is a real cost:
Sage nets its TDS credit notes INSIDE the payment run, so a payment loaded
without its notes cannot reconcile - 1,878 PAYMENT_TO_NOTE legs (Rs 11.62 Cr)
currently point at notes that do not exist. The payments went first here, so
this is catching up.

TDS IS NOT A FIELD ON THE PAYMENT IN THIS DATA. DTTDS - Sage's TDS module -
stops at FY 2017-18; its payment batches run 13..21,000 against this window's
32,901..34,845, and every DTTDS row that touches a window document carries
TOTATDS 0.00. The deduction is booked as NOTES on the 1L8TD* accounts instead:
1,352 of the window's 2,203 AP notes, Rs 1.03 Cr, already carrying tds_glacct
and tds_section from Sage.

    194C contractors   944    194J professional  102
    194Q goods         259    194I rent           43    194H commission  4

SHAPES TAKEN FROM THE DEPLOYED JAR, not guessed:
  * route is POST /note then POST /note/{id}/verify
  * NoteType    CREDIT_NOTE / DEBIT_NOTE (also COMMERCIAL_*, RETURN_ORDER)
  * NoteSubType PURCHASE / SALES - ours is PURCHASE
  * NoteEntityType ADHOC / BILL / INVOICE / COMMERCIAL_INVOICE
  * the line carries financeAccountDto (MinFinanceAccountDto), so each Sage GL
    account maps to its own SAGE- ledger exactly as the bill lines do
  * txsSection/txsAmount carry the TDS section and amount

TWO TRAPS THE MAPPING FILE NAMES:
  * `entityType` is BILL only where parent_doc really resolves to a bill
    (IDTRXTYPE=12). parent_doc is set on ~27% of AP notes and many IDMEMOXREF
    values point at OTHER NOTES. Check the type, do not assume.
  * carrying Sage's own note number needs a blank series, and the duplicate
    check lives INSIDE the series branch - so a blank-series load gets NO
    duplicate guard at all. Dedupe upstream on (cntbtch, cntitem), and check the
    platform as well as the local log.
"""

import argparse
import collections
import csv
import io
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
CONV = "/root/indiandesign/converter"
if os.path.isdir(CONV):
    sys.path.insert(0, CONV)
sys.path.insert(0, HERE)

import load_ap_payments as L                                      # noqa: E402

ORG_ID = L.ORG_ID
REF = L.REF

# The org's own billing address, id included. Taken from the bill loader so the
# notes and the bills agree; a note is refused outright without it.
# The org's own GSTIN. Read from the environment rather than hardcoded: the
# PAN/GSTIN belongs to WONDERBLUES, not to Indian Designs, and that mismatch is
# a live caveat in this migration.
ORG_GSTIN = os.environ.get("SME_ORG_GSTIN") or "29AADCW2868E1Z1"

COMPANY_ADDR = {
    "addressId": "1029168384857610867", "partyId": ORG_ID,
    "partyType": "ORGANISATION",
    "addressLine1": "48/1/2/3, Wonderblues Appare",
    "addressLine2": "Mylsandra Village, Bengaluru",
    "city": "Bengaluru", "state": "KARNATAKA", "pinCode": "560059",
    "country": "INDIA", "addressTypes": ["BILLING_ADDRESS"],
    "primaryAddress": True, "status": "ACTIVE", "organisationId": ORG_ID,
}
HDR = os.path.join(REF, "janapr-20260910/AP_Notes_Header.psv")
LINES = os.path.join(REF, "janapr-20260910/AP_Notes_Lines.psv")
CLASS = os.path.join(REF, "janapr-20260910/AP_Notes_Classification.psv")

STATE = os.path.join(CONV if os.path.isdir(CONV) else HERE, "state")
POSTED = os.path.join(STATE, "ap_notes_posted.log")
HELD = os.path.join(CONV if os.path.isdir(CONV) else HERE, "out",
                    "held_ap_notes.csv")


def ledgers():
    """SAGE-<acct> ledger name -> id, for the note lines."""
    out = {}
    for r in L.mysql(
            "SELECT name, id FROM smeassist.financeAccount "
            "WHERE organisationId='%s' AND isDeleted=0 AND leaf=1 "
            "  AND name LIKE '%%_SAGE-%%';" % ORG_ID):
        if len(r) >= 2 and "_SAGE-" in r[0]:
            out[r[0].rsplit("_SAGE-", 1)[1].strip()] = (r[1], r[0])
    return out


def on_platform():
    """Sage keys the platform already holds - the second idempotency layer.

    Load-bearing here: a note carrying Sage's own number posts with a BLANK
    series, and the platform's duplicate check lives inside the series branch,
    so the server will happily create the same note twice.
    """
    keys = set()
    for r in L.mysql(
            "SELECT CONCAT_WS('|', metadata->>'$.sageBatch', "
            "                     metadata->>'$.sageItem') "
            "FROM smeassist.creditDebitNote "
            "WHERE organisationId='%s' AND isDeleted=0 "
            "  AND metadata->>'$.sageBatch' IS NOT NULL;" % ORG_ID):
        if r and r[0] and r[0] != "|":
            keys.add(r[0])
    return keys


def contact_addresses():
    """Sage vendor code -> the contact's billing address, as the converter needs.

    NoteCreateToNoteDomainConverter.convert does
    getContactBillingAddress().getAddressId() with NO null guard, so a note
    without the vendor's address is a bare NullPointerException 80 lines in.
    The addresses already exist - 1,615 of them, created alongside the contacts.
    """
    out = {}
    for r in L.mysql(
            "SELECT c.metaData->>'$.sageVendor', a.id, a.addressLine1, "
            "       a.city, a.state, a.pin_code, a.country "
            "FROM yoda.address a "
            "JOIN smeassist.contact c ON c.id=a.partyId "
            "WHERE c.organisationId='%s' AND c.isDeleted=0 AND a.isDeleted=0 "
            "  AND c.metaData->>'$.sageVendor' IS NOT NULL;" % ORG_ID):
        if len(r) >= 7 and r[0] not in out:
            out[r[0]] = {"addressId": r[1], "partyType": "CONTACT",
                         "addressLine1": r[2] or "", "city": r[3] or "",
                         "state": r[4] or "", "pinCode": r[5] or "",
                         "country": r[6] or "INDIA",
                         "addressTypes": ["BILLING_ADDRESS"],
                         "status": "ACTIVE", "organisationId": ORG_ID}
    return out


def ensure_note_products(api, led, accounts, say=print):
    """One product per GL account the notes post to, as typeOfStock=RESOURCE.

    The note line demands a productId ("productId cannot be null!") where a bill
    line is happy with a ledger alone. These notes have no item at all - the
    extract's note lines carry a GL account and nothing else - and a CHARGE
    product would need an HSN/SAC that does not exist. Stamping the org's
    existing 996719 (clearing & forwarding) onto them would be false, and on the
    1,352 TDS memos it would be false on a statutory document.

    RESOURCE is the honest slot: MAPPING.json constants.hsn.exemptTypeOfStock
    lists it as the ONE type exempt from the mandatory-HSN rule, so the product
    asserts no GST classification at all - which is exactly right here. Nothing
    is invented and no SAC is guessed.

    Named and SKU'd off the Sage TDS account, so each section reports separately.
    """
    want = {a: led[a][1] for a in accounts if a in led}
    have = {}
    for r in L.mysql(
            "SELECT skuCode, id FROM smeassist.product "
            "WHERE organisationId='%s' AND isDeleted=0 "
            "  AND skuCode LIKE 'SAGE-%%';" % ORG_ID):
        if len(r) >= 2:
            have[r[0]] = r[1]
    out = {}
    for acct in sorted(want):
        sku = "SAGE-%s" % acct
        if sku in have:
            out[acct] = have[sku]
            continue
        name = want[acct].rsplit(" _SAGE-", 1)[0][:100]
        st, rb = api.post("/product/", {
            "productName": name, "skuCode": sku,
            "unit": "OTH", "unitOfMeasurement": "OTH",
            "typeOfStock": "RESOURCE",          # the HSN-exempt type
            "isManageInventory": False,
            "itemStatus": "ACTIVE", "isBulkUpload": True,
            "organisationId": ORG_ID,
            "metaData": {"sageAccount": acct, "migrationSource": "IDEDAT",
                         "isNoteLedgerProduct": "true",
                         "noHsnReason": "the note line is a LEDGER movement - "
                                        "the extract carries no item for a note "
                                        "- and RESOURCE is the one HSN-exempt "
                                        "typeOfStock, so no SAC is invented"},
        })
        data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
        pid = data.get("productId") or data.get("id")
        if pid:
            out[acct] = pid
            say("  product %-12s -> %s  %s" % (acct, pid, name[:38]))
        else:
            say("  product %-12s FAIL (%s) %s"
                % (acct, st, __import__("channels.api", fromlist=["Api"]).Api
                   .error_text(st, rb)))
    return out


def note_body(h, lines, cls, led, contacts, bills, addrs=None, prods=None):
    who = contacts.get(h["vendor"].strip())
    cid, cname = who if who else (None, None)
    key = (h["cntbtch"], h["cntitem"])
    meta = {"sageBatch": str(h["cntbtch"]), "sageItem": str(h["cntitem"]),
            "sageVendor": h["vendor"].strip(), "sageDoc": h["noteno"].strip(),
            "sageType": str(h["sagetype"]), "migrationSource": "IDEDAT"}
    if cls:
        if cls.get("tds_section"):
            meta["sageTdsSection"] = cls["tds_section"]
        if cls.get("tds_glacct"):
            meta["sageTdsAccount"] = cls["tds_glacct"]
        if cls.get("reason_code"):
            meta["sageReason"] = cls["reason_code"]

    # entityType BILL only where the parent really IS a bill; otherwise ADHOC.
    entity_id, entity_type = None, "ADHOC"
    parent = (h.get("parent_doc") or "").strip()
    if parent:
        hit = bills.get((h["vendor"].strip(), parent))
        if hit:
            entity_id, entity_type = hit, "BILL"
            meta["sageParentDoc"] = parent
        else:
            meta["sageParentUnresolved"] = parent

    items = []
    for ln in lines:
        acct = ln["glacct_fmt"].strip()
        fa = led.get(acct)
        amt = abs(Decimal(ln["amt_inr"] or 0))
        items.append({
            "organisationId": ORG_ID,
            "description": (ln["linedesc"] or "").strip()[:255] or acct,
            "quantity": "1", "unitPrice": str(amt), "itemPrice": str(amt),
            "taxableAmount": str(amt), "totalPrice": str(amt),
            "gstPercentage": str(Decimal(ln["gst_pct"] or 0)),
            # MAPPING.json constants.line: cessType is MANDATORY and is
            # dereferenced with no null guard, so omitting it is a bare NPE.
            # Carried over from the bill-line contract - the note line shares
            # the same tax-recalculation path.
            "cessType": "IN_RUPEES", "cessAmount": "0", "cessPercentage": "0",
            # The unit must MATCH the product's primary unit or create fails
            # with "Primary Unit for product does not match with lineItem".
            # The TDS products are created as OTH, same as the bill loader's
            # CHARGE products.
            "unit": "OTH", "displayUnit": "OTH", "displayQuantity": "1",
            "skuCode": "SAGE-%s" % acct,
            "productName": (fa[1].rsplit(" _SAGE-", 1)[0] if fa else acct),
            "discount": "0", "taxableOtherCharge": "0",
            # Same contract as the bill lines: every line names its own Sage
            # ledger, which is what control 3 reconciles on.
            "financeAccountDto": ({"financeAccountId": fa[0], "name": fa[1]}
                                  if fa else None),
            "productId": (prods or {}).get(acct),
        })

    total = abs(Decimal(h["amt_inr"] or 0))
    body = {
        "organisationId": ORG_ID,
        "noteType": h["sme_notetype"].strip(),        # inversion already applied
        "noteSubType": "PURCHASE",
        "entityType": entity_type,
        "entityId": entity_id,
        "contactType": "VENDOR",
        # MinContactDto also carries contactType and status, and the server
        # reads them: ContactServiceImpl.getStatusByContactTypeAndContactId
        # NPEs at line 1404 when the nested dto names only the id, because it
        # resolves the contact by (contactType, contactId) off THIS object, not
        # off the sibling field.
        "contactDto": {"contactId": cid, "accountName": cname,
                       "companyName": cname, "contactType": "VENDOR",
                       "status": "ACTIVE", "organisationId": ORG_ID},
        "contactFinanceAccountId": (L.crosswalks_cache or {}).get(
            h["vendor"].strip()),
        "issueDate": L.epoch_ms(h["datebus"]),
        "voucherDate": L.epoch_ms(h["datebus"]),
        # Sage's own note number travels as the document number with a BLANK
        # series - the platform would otherwise mint one from a counter series,
        # and this migration must keep Sage's identifiers. MAPPING.json warns
        # the duplicate check lives inside the series branch, so a blank-series
        # load gets NO server-side guard: on_platform() above is the only one.
        "creditDebitNoteNumber": {"series": "", "suffix": "",
                                  "value": h["noteno"].strip(),
                                  "documentNumber": h["noteno"].strip()},
        "issuedNoteNumber": h["noteno"].strip() or None,
        "issuedNoteDate": L.epoch_ms(h["dateinvc"] or h["datebus"]),
        "totalAmount": str(total),
        "totalGst": str(abs(Decimal(h["tax_doc"] or 0))),
        "noteStatus": "ACTIVE",
        # NOT NULL on the table, and the DTO does not default it. DOMESTIC vs
        # INTERNATIONAL is read off the note's own currency, not assumed:
        # production runs 28,517 DOMESTIC / 40 INTERNATIONAL purchase notes.
        "commerceType": ("DOMESTIC" if (h.get("curn") or "INR").strip().upper()
                         == "INR" else "INTERNATIONAL"),
        # Also NOT NULL. It is the GSTIN the note is raised UNDER - i.e. ours,
        # the org's own registration - which is what production holds (each
        # value there is the loading org's own GSTIN, not the counterparty's).
        "gstUsedForCreditDebitNote": ORG_GSTIN,
        "noteReason": (h.get("descinvc") or "").strip()[:255] or None,
        # "Company Address can not be null". Reused verbatim from the proven
        # bill loader (post_sage_bills.COMPANY_ADDR) rather than re-typed, so
        # the notes carry the same org billing address the 14,649 bills do.
        "companyBillingAddress": COMPANY_ADDR,
        "contactBillingAddress": (addrs or {}).get(h["vendor"].strip()),
        "lineItemDtoList": items,
        "metadata": meta,
    }
    if cls and cls.get("tds_section"):
        # Sage's own section, off the TDS GL account - not derived from a rate.
        body["txsSection"] = {"section": cls["tds_section"]}
        body["txsAmount"] = str(total)
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tds-only", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    hdr = L.psv(HDR)
    lines = collections.defaultdict(list)
    for ln in L.psv(LINES):
        lines[(ln["cntbtch"], ln["cntitem"])].append(ln)
    cls = {(c["cntbtch"], c["cntitem"]): c for c in L.psv(CLASS)}

    xw = L.crosswalks()
    L.crosswalks_cache = xw["ledger"]
    led = ledgers()
    addrs = contact_addresses()
    done = on_platform()
    posted = set()
    if os.path.exists(POSTED):
        posted = {l.split("||")[0] for l in io.open(POSTED) if "||" in l}

    # (vendor, docno) -> billId, for the parent linkage
    sidx, _ = L.sage_bill_index()
    bills = {}
    for r in L.psv(L.BILLS):
        k = xw["bill"].get((r["cntbtch"], r["cntitem"]))
        if k:
            bills[(r["vendor"].strip(), r["invno"].strip())] = k

    plan, held = [], []
    for h in hdr:
        key = "%s|%s" % (h["cntbtch"], h["cntitem"])
        c = cls.get((h["cntbtch"], h["cntitem"]))
        if a.tds_only and not (c and c.get("n_tds") == "1"):
            continue
        if key in done or key in posted:
            continue
        if c and c.get("route") and c["route"] != "NOTE":
            held.append((h, "routed %s, not a note" % c["route"]))
            continue
        if h["vendor"].strip() not in xw["contact"]:
            held.append((h, "vendor has no contact yet"))
            continue
        if h["vendor"].strip() not in addrs:
            held.append((h, "vendor has no billing address - the converter NPEs"))
            continue
        ls = lines.get((h["cntbtch"], h["cntitem"]))
        if not ls:
            held.append((h, "no line rows in the extract"))
            continue
        miss = [l["glacct_fmt"].strip() for l in ls
                if l["glacct_fmt"].strip() not in led]
        if miss:
            held.append((h, "no ledger for %s" % ", ".join(sorted(set(miss))[:3])))
            continue
        plan.append((h, ls, c))

    tot = lambda rs: round(sum(abs(Decimal(r["amt_inr"] or 0)) for r in rs)
                           / Decimal(10000000), 2)
    print("\nAP notes: %d in the window, %d ledgers available" % (len(hdr), len(led)))
    print("  postable %d (Rs %s Cr)   held %d (Rs %s Cr)   already loaded %d"
          % (len(plan), tot([p[0] for p in plan]), len(held),
             tot([h for h, _ in held]), len(done)))
    for why, n in collections.Counter(w for _h, w in held).most_common(6):
        print("     %-46s %5d" % (why, n))
    types = collections.Counter(h["sme_notetype"].strip() for h, _l, _c in plan)
    print("  by type: %s" % dict(types))

    os.makedirs(os.path.dirname(HELD), exist_ok=True)
    with io.open(HELD, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cntbtch", "cntitem", "vendor", "noteno", "amt_inr", "why"])
        for h, why in held:
            w.writerow([h["cntbtch"], h["cntitem"], h["vendor"], h["noteno"],
                        h["amt_inr"], why])
    print("  exclusion manifest -> %s" % HELD)
    if a.check:
        return 0

    if a.limit:
        plan = plan[: a.limit]
    prods = {}
    if a.dry_run:
        for h, ls, c in plan[:3]:
            print("\n--- %s|%s %s" % (h["cntbtch"], h["cntitem"], h["noteno"]))
            print(json.dumps(note_body(h, ls, c, led, xw["contact"], bills, addrs, prods),
                             indent=1, default=str)[:1800])
        print("\ndry run: nothing posted (%d notes in plan)" % len(plan))
        return 0

    from channels.api import Api                                  # noqa: E402
    api = Api(timeout=600)
    need_accts = sorted({l['glacct_fmt'].strip()
                         for _h, ls, _c in plan for l in ls})
    prods = ensure_note_products(api, led, need_accts)
    os.makedirs(STATE, exist_ok=True)
    ok = fail = ver = vfail = 0
    for h, ls, c in plan:
        key = "%s|%s" % (h["cntbtch"], h["cntitem"])
        st, rb = api.post("/note", note_body(h, ls, c, led, xw["contact"], bills, addrs, prods))
        data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
        nid = data.get("creditDebitNoteId") or data.get("id")
        if not nid:
            fail += 1
            print("  %-16s CREATE FAIL (%s) %s" % (key, st, Api.error_text(st, rb)))
            continue
        with io.open(POSTED, "a") as fh:
            fh.write("%s||%s\n" % (key, nid))
            fh.flush()
            os.fsync(fh.fileno())
        ok += 1
        vst, vb = api.post("/note/%s/verify" % nid, {})
        if 200 <= (vst or 0) < 300:
            ver += 1
        else:
            vfail += 1
            print("  %-16s %s VERIFY FAIL (%s) %s"
                  % (key, nid, vst, Api.error_text(vst, vb)))
    print("\ncreated=%d failed=%d verified=%d verify_failed=%d"
          % (ok, fail, ver, vfail))
    return 0 if not fail else 1


if __name__ == "__main__":
    L.crosswalks_cache = {}
    sys.exit(main())
