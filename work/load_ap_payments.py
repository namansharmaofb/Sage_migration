#!/usr/bin/env python3
"""
load_ap_payments.py  (11 Sep 2026)  --  load the AP payments AND settle the bills.

    header  extract-v2/out/AP_Payments_Header.psv    5,003 paying documents
    legs    extract-v2/out/AP_Payment_Apps.psv      80,388 settlement legs
    bills   janapr-20260910/Bills_Header.psv        29,745 Sage bill headers

    python3 load_ap_payments.py --dry-run          # plan + every JSON body, no auth
    python3 load_ap_payments.py --pilot            # 1 SETTLEMENT + 1 ADVANCE, then stop
    python3 load_ap_payments.py --bank SBIOSB      # load one bank
    python3 load_ap_payments.py --check            # crosswalk coverage only

WHAT SETTLES A BILL - two links, doing different jobs, and only one moves the ledger:

    paymentRequestEntityMappingDtoList   the DOCUMENT link. Shows the payment as
                                         settling bill X in the UI / open-item list.
    preferredVoucherEntries              the LEDGER link, and the one carrying the
                                         AMOUNT. Passed on CREATE, lands PENDING in
                                         preferredEntityVoucherMapping, and VERIFY
                                         turns it into a real voucherEntryMapping row.

The org has 12 payments and ZERO mappings of either kind, so the bills and the payments
both sit fully open on the vendor ledger. Closing that is the whole point of this script,
and a run that creates payments without mappings has not done the job.

ROUTES THAT OLDER NOTES HAVE WRONG, all re-checked against the deployed jar:

  * POST /voucherEntryMapping/ DOES NOT EXIST - VoucherEntryMappingController is
    GET-only. Anything routing advance application through it 405s on Rs 143.99 Cr.
    Advances apply at LEDGER level, via preferredVoucherEntries, with no entity mapping;
    2,077 production advances carry no entity mapping at all and 1,515 are VERIFIED.
  * There is no POST /paymentRequest/{id}/verify. Verify is
    POST /entityVerification/PAYMENT/{id}, and an EMPTY body works - the server derives
    voucherType/dates. updateFinanceAccountingDetailsDtos stays empty: it remaps a
    finance account, which a migration must not do at verify time.
  * bulkVerifyVoucherWithoutTally takes (String, MultipartFile). Not a JSON route.

TWO FIELDS THE BRIEF'S FIELD TABLE DOES NOT MENTION, both proven here today:

  * approvedAmount. The VOUCHER leg is built from it, not from requestedAmount. Null
    creates the payment happily and then fails verify with a bare NPE in
    VoucherSortUtils$PartyFirstComparator.compare:189 - javap shows offset 96 is
    a.getAmount().compareTo(b.getAmount()), so the null is an AMOUNT, not a finance
    account. Non-null on all 105,761 verified payments box-wide.
  * contactFinanceAccountId. resolveContactFinanceAccountId returns null for migrated
    contacts, and verify then refuses "financeAccountId can not be null". A contact can
    own SEVERAL vendor ledgers, so the one its existing BILL legs actually use is
    preferred over an arbitrary pick - otherwise the payment credits one ledger and the
    bill debits another and the vendor never nets to zero.

NEVER RESOLVE A BILL BY ITS NUMBER. 1,535 invoice numbers repeat across vendors and that
has already returned a different vendor's bill. A leg names its target by document
number, so it is resolved (vendor, invno) -> Sage (CNTBTCH, CNTITEM) through Sage's OWN
header file, and only then to a billId. Measured: (vendor, invno) is unique on 29,744 of
29,745 Sage rows, and the 1 ambiguous pair is skipped rather than guessed.

AMOUNTS COME FROM SAGE. mappedAmount is the leg's own amt_abs_inr (APOBP.AMTPAYMHC),
never a computed remainder - partial application is the norm: 29,011 legs run 4,877
payments against 28,372 bills.

HOLDS, written to a manifest and never silently dropped:
  * banks with no IFSC (8) and no account number (CANBCC/HDFCEPC/HDFCCC)
  * CASH (26 docs) - not a bank account; needs cashReceiptFinanceAccountId
  * REVERSAL legs (3,702, Rs 28.53 Cr) - transtype/trxtype 11/53, excluded outright
  * PAYMENT_TO_NOTE legs (1,878) - the target is a NOTE and no notes are loaded
  * legs whose bill is one of the 5,400 INVHSEQ-keyed ones, which carry both a bogus
    key AND wrong amounts and must be revoked and reposted first
"""

import argparse
import calendar
import collections
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
CONV = "/root/indiandesign/converter"
REF = "/root/indiandesign/reference"
if os.path.isdir(CONV):
    sys.path.insert(0, CONV)

ORG_ID = os.environ.get("SME_ORG_ID", "1029113552088076445")

HDR = os.path.join(REF, "extract-v2/out/AP_Payments_Header.psv")
LEGS = os.path.join(REF, "extract-v2/out/AP_Payment_Apps.psv")
BILLS = os.path.join(REF, "janapr-20260910/Bills_Header.psv")

# Sage paymode -> PaymentMode. BANK_TRANSFER is NOT loadable: its guard hard-fails
# without an active contactBankAccountId and Sage holds no vendor bank details anywhere
# (full-schema sweep). CHEQUE and CASH guards never ask for the vendor's bank, and
# getPaymentModeFinanceAccountName routes CHEQUE and BANK_TRANSFER to the SAME case, so
# the bank ledger resolves identically off our own account. TT and LC have no PaymentMode
# equivalent at all, so they load as CHEQUE with the real Sage mode kept in metaData -
# we lose the mode NAME, not the payment. 438 documents.
# Sage paymode -> PaymentMode. Cheques load as PAYMENT_GATEWAY, not CHEQUE, and
# the real instrument is stated in the narration and in metaData.
#
# WHY, since PAYMENT_GATEWAY is not how the money actually moved: CHEQUE is not
# offered on the payment side of the UI at all. app/containers/Payment/
# constants.js declares PAYMENT_MODES = {BANK_TRANSFER, CASH, PAYMENT_GATEWAY} -
# every CHEQUE in the frontend belongs to the RECEIPT containers - which is why
# there were ZERO cheque payments box-wide before this load. A mode the screen
# cannot render is a mode nobody can work with.
#
# It is accounting-safe, checked in the jar rather than assumed:
# FinanceAccountUtils.getPaymentModeFinanceAccountName switches on the mode, and
# the PAYMENT_GATEWAY branch builds the SAME "%s (%s) %s" bank-ledger name from
# the same BankAccountDto as the CHEQUE branch. Only CASH diverges, to
# "Cash & Receipts". So the voucher's credit leg is identical.
#
# ONE REAL CONSEQUENCE: the org-wide UTR duplicate check lives inside the
# BANK_TRANSFER/PAYMENT_GATEWAY branch, which CHEQUE bypassed. utrNumber carries
# the cheque number and the source holds 3 duplicate cheque numbers, so those
# will now be refused rather than silently accepted. That is a gain, not a loss.
PAYMODE = {"CHEQUE": "PAYMENT_GATEWAY", "CASH": "CASH", "TT": "PAYMENT_GATEWAY",
           "LC": "PAYMENT_GATEWAY", "ONLINE": "PAYMENT_GATEWAY"}

# What Sage actually said, for the narration.
SAGE_INSTRUMENT = {"CHEQUE": "Cheque", "TT": "Telegraphic transfer",
                   "LC": "Letter of credit", "ONLINE": "Online transfer",
                   "CASH": "Cash"}

# PaymentRequestDto.currency is a CurrencyDto, NOT a String - sending "INR" is
# a 500 JSON parse error, "Cannot construct instance of CurrencyDto ... from
# String value ('INR')". The DTO's field is the Currency enum, so the body is
# {"currency": "<code>"}.
#
# AND THE CODE IS ALWAYS INR, whatever Sage's CODECURN says. The brief's field
# table maps currency to `curn`, and following that is a 90x misstatement:
#
#   requestedAmount comes from amt_inr = ABS(APOBL.AMTINVCHC), the HOME currency
#   figure. Sage keeps the document-currency amount in a separate column,
#   AMTINVCTC, which this extract does not carry. Measured on PP060190:
#   484,733.03 USD x 90.9475 = 44,085,257.25 INR. Labelling that INR figure
#   "USD" would claim a USD 44 million payment.
#
# It is also what the platform requires: every one of the 809 vendor party
# ledgers in this org is INR, and verify refuses a mismatch outright with
# "Finance Account Currency and voucher currency does not match" - measured, 0
# of 51 foreign-currency payments verified, against 699 of 701 INR ones.
#
# So the amount is INR, the currency is INR, conversionRate is 1, and Sage's own
# currency and rate are preserved in metaData rather than thrown away. The
# document-currency amount is NOT loadable until the extract carries AMTINVCTC.
LEDGER_CURRENCY = "INR"

STATE = os.path.join(CONV if os.path.isdir(CONV) else HERE, "state")
POSTED = os.path.join(STATE, "ap_payments_posted.log")
HELD_CSV = os.path.join(CONV if os.path.isdir(CONV) else HERE, "out",
                        "held_ap_payments.csv")


def psv(path):
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh, delimiter="~"))


def epoch_ms(yyyymmdd):
    s = (yyyymmdd or "").strip().split(".")[0]
    if len(s) != 8 or not s.isdigit():
        return None
    return int(calendar.timegm((int(s[:4]), int(s[4:6]), int(s[6:]),
                                0, 0, 0, 0, 0, 0))) * 1000


def mysql(sql):
    """Read-only. Runs on the API host over the local socket."""
    host = os.environ.get("SME_DB_HOST", "10.22.0.165")
    if os.path.exists("/data/smeassist"):          # already on that host
        cmd = ["mysql", "-N", "--batch", "-e", sql]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "root@%s" % host,
               "mysql -N --batch -e %s" % json.dumps(sql)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode:
        raise SystemExit("mysql failed: %s" % p.stderr[:400])
    return [ln.split("\t") for ln in p.stdout.strip().splitlines() if ln.strip()]


# --------------------------------------------------------------- crosswalks

def crosswalks():
    """Sage code -> live id, all read from the platform, none assumed."""
    xw = {"contact": {}, "ledger": {}, "bank": {}, "bill": {}, "leg": {},
          "miskeyed": set(), "onplatform": set()}

    for r in mysql(
            "SELECT metaData->>'$.sageVendor', id, accountName FROM smeassist.contact "
            "WHERE organisationId='%s' AND isDeleted=0 "
            "AND metaData->>'$.sageVendor' IS NOT NULL;" % ORG_ID):
        if len(r) >= 3:
            xw["contact"][r[0]] = (r[1], r[2])

    # A contact can own several vendor ledgers, and only a LEAF one can take a
    # voucher: verify refuses a parent with "Voucher Entry is not possible for
    # non leaf ledger - Pan_ANKPD4671M_HE9QHJ". Measured in this org, the split
    # is exactly even - 1,618 vendor ledgers, 809 leaf and 809 parents - and
    # every contact has at least one leaf, so filtering on leaf=1 loses nobody.
    #
    # Among the leaves, prefer the one the contact's existing VENDOR voucher
    # legs already use, so the payment and the bill land on the SAME ledger and
    # the vendor can actually net to zero.
    best = {}
    for r in mysql(
            "SELECT c.metaData->>'$.sageVendor', f.id, IFNULL(u.n,0) "
            "FROM smeassist.financeAccount f "
            "JOIN smeassist.contact c ON c.id=f.partyId AND c.isDeleted=0 "
            "LEFT JOIN (SELECT financeAccountId, COUNT(*) n FROM smeassist.voucherEntry "
            "  WHERE organisationId='%s' AND isDeleted=0 AND partyType='VENDOR' "
            "  GROUP BY financeAccountId) u ON u.financeAccountId=f.id "
            "WHERE f.organisationId='%s' AND f.isDeleted=0 AND f.partyType='VENDOR' "
            "AND f.leaf=1 "
            "AND c.metaData->>'$.sageVendor' IS NOT NULL;" % (ORG_ID, ORG_ID)):
        if len(r) < 3:
            continue
        code, fid, used = r[0], r[1], int(r[2] or 0)
        if code not in best or used > best[code][1]:
            best[code] = (fid, used)
    xw["ledger"] = {k: v[0] for k, v in best.items()}

    # SECOND IDEMPOTENCY LAYER, and the one that matters: the keys the PLATFORM
    # already holds. A REVERSED payment is excluded - it has been undone, so its
    # Sage key is free again and must be repostable, or a reverse-and-repost
    # cycle would strand the document with no replacement. The posted log alone is not enough - it is per-script, and
    # post_janapr.py keeps a separate one, so the converter could not see the 7
    # payments that loader had already created for batch 33396 and posted every
    # one of them a second time. Both copies verified, double-counting Rs 4.94 L.
    #
    # MAPPING.json's idempotency.layers says exactly this: "a posted log
    # appended per success" AND "a pre-flight existence check matching on the
    # metadata key, because the log can be lost". Matching is on the Sage key in
    # metadata, never on the document number.
    for r in mysql(
            "SELECT CONCAT_WS('|', metaData->>'$.sageBatch', "
            "                     metaData->>'$.sageItem') "
            "FROM smeassist.paymentRequest "
            "WHERE organisationId='%s' AND isDeleted=0 "
            "  AND status<>'REVERSED' "
            "  AND metaData->>'$.sageBatch' IS NOT NULL;" % ORG_ID):
        if r and r[0] and r[0] != "|":
            xw.setdefault("onplatform", set()).add(r[0])

    for r in mysql("SELECT accountingName, id, bankName FROM yoda.bankAccount "
                   "WHERE partyId='%s' AND isDeleted=0x00;" % ORG_ID):
        if len(r) >= 3 and r[0].startswith("SAGE-"):
            xw["bank"][r[0][5:]] = (r[1], r[2])

    # Bill by Sage key, plus its VENDOR voucher leg. The INVHSEQ-keyed 5,400 are
    # recorded as mis-keyed rather than resolved: they carry a bogus sageBatch
    # (an APIBH.DRILLDWNLK, 215,062,515..224,622,091 against a real cntbtch
    # range of 17,103..18,243) AND wrong amounts, so mapping to one would settle
    # against a figure we know to be wrong.
    for r in mysql(
            "SELECT b.metadata->>'$.sageBatch', b.metadata->>'$.sageItem', b.id, "
            "       b.billStatus, IFNULL(v.id,''), IFNULL(v.remainingAmount,0) "
            "FROM smeassist.bill b "
            "LEFT JOIN smeassist.voucherEntry v ON v.referenceId=b.id "
            "     AND v.referenceType='BILL' AND v.partyType='VENDOR' "
            "     AND v.isDeleted=0 AND v.organisationId='%s' "
            "WHERE b.organisationId='%s' AND b.isDeleted=0;" % (ORG_ID, ORG_ID)):
        if len(r) < 6:
            continue
        bt, it, bid, status, leg, rem = r[0], r[1], r[2], r[3], r[4], r[5]
        try:
            batch = int(bt)
        except (TypeError, ValueError):
            continue
        if batch > 200000000 or it == "0":
            xw["miskeyed"].add(bid)
            continue
        xw["bill"][(bt, it)] = bid
        if leg:
            # The leg id AND how much it can still absorb. Verify refuses
            # "Can not map more than the entity amount", so a bill that posted
            # short of Sage cannot take Sage's full payment leg.
            xw["leg"][bid] = (leg, Decimal(rem or 0))
    return xw


def sage_bill_index():
    """(vendor, invno) -> the Sage key, from Sage's OWN header file.

    This is what makes leg resolution safe. A leg names its target by DOCUMENT
    NUMBER; resolving that against SMEAssist's billNumber would be the exact
    mistake the brief forbids, because 1,535 numbers repeat across vendors.
    Going through Sage first turns a number into a KEY.
    """
    idx, base = collections.defaultdict(set), collections.defaultdict(set)
    for r in psv(BILLS):
        k = (r["cntbtch"], r["cntitem"])
        idx[(r["vendor"].strip(), r["invno"].strip())].add(k)
        if r["invbase"].strip():
            base[(r["vendor"].strip(), r["invbase"].strip())].add(k)
    return idx, base


# --------------------------------------------------------------- the payload

def payment_body(p, xw, maps, prefs):
    mode = PAYMODE.get((p["paymode"] or "").strip().upper())
    bank = xw["bank"].get(p["bank"].strip())
    meta = {
        "sageDoc": p["docno"].strip(), "sageBatch": str(p["cntbtch"]),
        "sageItem": str(p["cntitem"]), "sageBank": p["bank"].strip(),
        "sageCheque": (p["remit_cheque"] or "").strip(),
        "sageVendor": p["vendor"].strip(), "sagePayType": p["sme_paytype"].strip(),
        "migrationSource": "IDEDAT",
    }
    cur_raw = (p["curn"] or "INR").strip().upper()
    if cur_raw != LEDGER_CURRENCY:
        meta["sageCurrency"] = cur_raw
        meta["sageExchangeRate"] = str(Decimal(p["rate"] or 1))
        meta["amountIsHomeCurrency"] = ("requestedAmount is APOBL.AMTINVCHC in "
                                        "INR; the %s document amount "
                                        "(AMTINVCTC) is not in the extract"
                                        % cur_raw)
    raw = (p["paymode"] or "").strip().upper()
    if raw and PAYMODE.get(raw) != raw:
        meta["sageInstrument"] = SAGE_INSTRUMENT.get(raw, raw)
        # The substitution is on the record, not hidden: TT/LC/ONLINE have no
        # PaymentMode equivalent and load as CHEQUE.
        meta["sagePaymode"] = raw
    # The narration carries the instrument, because paymentMode no longer does.
    instrument = SAGE_INSTRUMENT.get(raw)
    note = ""
    if instrument and mode == "PAYMENT_GATEWAY":
        note = "%s payment" % instrument
        chq = (p["remit_cheque"] or "").strip()
        if chq:
            note += " no. %s" % chq
        note += " (Sage paymode %s; loaded as PAYMENT_GATEWAY because the " \
                "payment screen offers no cheque mode)" % raw
    desc = (p["descinvc"] or "").strip()
    remarks = ("%s. %s" % (note, desc) if note and desc else note or desc)[:255] or None
    cid, cname = xw["contact"].get(p["vendor"].strip(), (None, None))
    dto = {
        "organisationId": ORG_ID,
        "contactId": cid,
        # contactId alone is refused with "ContactName can not be null", and the
        # message names neither field it reads, so the name travels three ways:
        # the scalar plus both name fields on the nested MinContactDto.
        "contactName": cname,
        "contactDto": {"contactId": cid, "accountName": cname,
                       "companyName": cname},
        "contactFinanceAccountId": xw["ledger"].get(p["vendor"].strip()),
        "contactType": "VENDOR",
        "paymentRequestType": p["sme_paytype"].strip(),
        "paymentMode": mode,
        "requestedAmount": str(Decimal(p["amt_inr"] or 0)),
        # see the module docstring - the voucher leg is built from THIS
        "approvedAmount": str(Decimal(p["amt_inr"] or 0)),
        # The brief's field table calls this companyBankAccountId, and that is
        # wrong: PaymentRequestDto declares companyBankAccountDto, a
        # BankAccountDto. Sending the scalar is accepted with HTTP 200 and
        # SILENTLY leaves the bank null - the response echoes
        # companyBankAccountDto: null - so the payment has no credit side and
        # verify cannot build a voucher. BankAccountDto's getter is
        # getBankAccountId(), so the key is bankAccountId, NOT id.
        "companyBankAccountDto": ({"bankAccountId": bank[0], "name": bank[1]}
                                  if bank else None),
        "companyBankName": bank[1] if bank else None,
        # Sage holds no vendor bank details anywhere - verified by full-schema
        # sweep. Null is the fact, not a gap.
        "contactBankAccountId": None,
        "utrNumber": (p["remit_cheque"] or "").strip() or None,
        "voucherDate": epoch_ms(p["datebus"]),
        "dueDate": epoch_ms(p["datebus"]),
        "currency": {"currency": LEDGER_CURRENCY},
        # 1, not Sage's rate: the amount is already converted.
        "conversionRate": "1",
        "status": "SUCCESS",
        "tdsAmount": "0",
        "remarks": remarks,
        "metaData": meta,
    }
    if maps:
        dto["paymentRequestEntityMappingDtoList"] = maps
    body = {"paymentRequestDto": dto, "metaData": meta}
    if prefs:
        body["preferredVoucherEntries"] = prefs
    if maps:
        # The SINGULAR pair on the wrapper, which the brief says to leave null.
        # Measured: 52,773 of 52,865 production payments carry exactly ONE
        # paymentRequestEntityMapping, so the singular pair - not the list - is
        # how the row actually gets made. The list alone comes back [] and
        # writes nothing, even with status ACTIVE.
        #
        # Our payments settle many bills each (4,877 payments -> 28,372 bills),
        # so the list still travels for the rest; the wrapper carries the first.
        body["entityId"] = maps[0]["entityId"]
        body["paymentRequestEntityType"] = maps[0]["paymentRequestEntityType"]
    return body


def build_links(p, legs_by_payer, xw, sidx, sbase, deferred, capacity_used):
    """The two links for one payment, plus what had to be deferred."""
    maps, prefs = [], []
    key = (p["cntbtch"], p["cntitem"])
    for lg in legs_by_payer.get(key, ()):
        lt = lg["link_type"]
        if lt not in ("PAYMENT_TO_BILL", "ADVANCE_TO_BILL"):
            if lt in ("PAYMENT_TO_NOTE",):
                deferred.append((p, lg, "target is a NOTE and no notes are loaded"))
            continue
        vend = lg["vendor"].strip()
        cand = (sidx.get((vend, lg["target_doc"].strip()))
                or sbase.get((vend, lg["target_base"].strip())))
        if not cand:
            deferred.append((p, lg, "no Sage bill for (vendor, docno)"))
            continue
        if len(cand) > 1:
            deferred.append((p, lg, "ambiguous (vendor, docno) in Sage - not guessed"))
            continue
        bid = xw["bill"].get(next(iter(cand)))
        if not bid:
            deferred.append((p, lg, "bill not loaded, or INVHSEQ-keyed and awaiting repost"))
            continue
        hit = xw["leg"].get(bid)
        if not hit:
            deferred.append((p, lg, "bill has no VENDOR voucher leg - not verified"))
            continue
        leg_id, remaining = hit
        want = Decimal(lg["amt_abs_inr"] or 0)
        # Capacity is per bill leg and is consumed across the whole run, because
        # one bill is routinely covered by several payments (29,011 legs run
        # 4,877 payments against 28,372 bills).
        free = remaining - capacity_used.get(leg_id, Decimal(0))
        if want > free:
            # NOT capped. The brief is explicit that the amount comes from Sage
            # and never from a computed remainder, so a leg the bill cannot
            # absorb is deferred and reported - capping it would silently
            # restate what Sage says was paid. The usual cause is a bill that
            # posted short of Sage and needs reposting, not a bad leg.
            deferred.append((p, lg, "bill cannot absorb the Sage leg "
                                    "(short-posted bill, or already fully mapped)"))
            continue
        capacity_used[leg_id] = capacity_used.get(leg_id, Decimal(0)) + want
        amt = str(want)
        # The document link. ADVANCE_TO_BILL gets NO entity mapping - advances
        # apply at ledger level only, which is how the 2,077 production advances
        # with no entity mapping (1,515 VERIFIED) are shaped.
        if lt == "PAYMENT_TO_BILL":
            maps.append({"entityId": bid,
                         "entityNumber": lg["target_doc"].strip(),
                         "paymentRequestEntityType": "BILL",
                         "requestedAmount": amt,
                         # EntityMappingStatus, and the list is dropped without
                         # it: the enum holds only ACTIVE/INACTIVE and
                         # production is 49,916 ACTIVE to 3,117 INACTIVE.
                         "status": "ACTIVE",
                         "organisationId": ORG_ID})
        # The ledger link, carrying Sage's own split.
        prefs.append({"preferredVoucherEntry": {"voucherEntryId": leg_id},
                      "mappedAmount": amt})
    return maps, prefs


def revoke_duplicates(a):
    """Remove the second and later copy of any payment posted twice.

    Cause, for the record: two loaders wrote into the same org with SEPARATE
    posted logs - post_janapr.py for the cheque-mode proving run and this one
    for the bulk - so this script could not see the 7 payments already created
    for batch 33396 and made them again. Both copies verified, so each pair
    minted two vouchers and double-counted the payment.

    The EARLIEST copy is kept, because it is the one whose id the other loader
    recorded; the later ones are soft-deleted. Keying is on the Sage
    (sageBatch, sageItem) pair in metadata, never on the document number - 1,535
    invoice numbers repeat across vendors.
    """
    rows = mysql(
        "SELECT metaData->>'$.sageBatch', metaData->>'$.sageItem', COUNT(*), "
        "       GROUP_CONCAT(id ORDER BY dateCreated), "
        "       GROUP_CONCAT(DISTINCT requestedAmount) "
        "FROM smeassist.paymentRequest "
        "WHERE organisationId='%s' AND isDeleted=0 "
        "  AND metaData->>'$.sageBatch' IS NOT NULL "
        "GROUP BY 1,2 HAVING COUNT(*)>1;" % ORG_ID)
    if not rows:
        print("no duplicate (sageBatch, sageItem) in the org")
        return 0
    extra, dv = [], Decimal(0)
    for r in rows:
        ids = r[3].split(",")
        keep, drop = ids[0], ids[1:]
        amt = Decimal((r[4] or "0").split(",")[0])
        print("  %s|%s  x%s  keep %s  revoke %s  (Rs %s each)"
              % (r[0], r[1], r[2], keep, ",".join(drop), amt))
        extra.extend(drop)
        dv += amt * len(drop)
    print("%d duplicate copy(ies), Rs %s double-counted" % (len(extra), dv))
    if a.dry_run:
        print("dry run: nothing revoked")
        return 0
    from channels.api import Api                                  # noqa: E402
    api = Api()
    ok = fail = 0
    for pid in extra:
        if a.reverse_not_delete:
            # DELETE /paymentRequest/{id} is softDelete and it refuses a
            # VERIFIED payment - it answers NotFoundException "Payment request
            # not found for organisationId X and id X" (the message template
            # prints the org id in both slots, which is a backend bug, not a
            # clue). A verified payment has a voucher, so the correct treatment
            # is not deletion but REVERSAL, which PaymentRequestStatus declares
            # and PaymentRequestUpdateDto carries alongside reversalDate.
            st, rb = api.request(
                "PUT", "/paymentRequest/update",
                [{"paymentRequestId": pid,
                  "paymentRequestStatus": "REVERSED",
                  "reversalDate": int(time.time()) * 1000}],
                retry_5xx=False)
        else:
            st, rb = api.request("DELETE", "/paymentRequest/%s" % pid, retry_5xx=False)
        if 200 <= (st or 0) < 300:
            ok += 1
        else:
            fail += 1
            print("   REVOKE FAIL %s (%s) %s" % (pid, st, Api.error_text(st, rb)))
    print("revoked=%d failed=%d" % (ok, fail))
    return 0 if not fail else 1


def verify_pending(a):
    """Verify every PENDING payment that CAN verify.

    A created-but-unverified payment is the worst of the three states: it looks
    loaded, mints no voucher and settles nothing. The create and verify steps are
    separate calls, so any interruption between them leaves one behind - this
    sweeps them up, and is safe to re-run because an already-verified payment
    answers "there already exists a voucher against it", which is success.
    """
    rows = mysql(
        "SELECT id, IFNULL(metaData->>'$.sageBatch',''), IFNULL(metaData->>'$.sageItem','') "
        "FROM smeassist.paymentRequest "
        "WHERE organisationId='%s' AND isDeleted=0 "
        "  AND entityLedgerVerificationStatus='PENDING_VERIFICATION' "
        "  AND companyBankAccountId IS NOT NULL AND approvedAmount IS NOT NULL;" % ORG_ID)
    if not rows:
        print("no sound PENDING payment - nothing to verify")
        return 0
    print("%d PENDING payment(s) to verify" % len(rows))
    if a.dry_run:
        return 0
    from channels.api import Api                                  # noqa: E402
    api = Api()
    ok = fail = 0
    for r in rows:
        st, rb = api.post("/entityVerification/PAYMENT/%s" % r[0], {})
        msg = Api.error_text(st, rb) or ""
        if 200 <= (st or 0) < 300 or "already exists a voucher" in msg:
            ok += 1
        else:
            fail += 1
            print("   VERIFY FAIL %s %s|%s (%s) %s" % (r[0], r[1], r[2], st, msg))
    print("verified=%d failed=%d" % (ok, fail))
    return 0 if not fail else 1


def repost_unmapped(a, plan_fn):
    """Option B: reverse a verified-but-unmapped payment and repost it WITH its
    settlement links.

    Option A - repairing in place with POST /voucherEntry/{id}/mapVoucherEntry -
    is not available in this build. Piloted both directions (payment leg in the
    path with bill legs in the body, and the reverse): both answer 200 with an
    EMPTY body and write nothing. voucherEntryMapping stayed at 536 rows and the
    legs' remainingAmount did not move. So the only route to a mapping is
    preferredVoucherEntries, which is create-time.

    Reversal, not deletion: softDelete refuses a verified payment, but
    PaymentRequestStatus declares REVERSED and PUT /paymentRequest/update
    accepts it - proven on the 7 duplicates. The original stays as a reversed
    record with its reversal voucher, which is the correct accounting treatment
    and leaves an audit trail; a fresh payment is then created carrying Sage's
    exact per-bill amounts.
    """
    from channels.api import Api                                  # noqa: E402
    api = Api(timeout=600)
    todo = plan_fn()
    if a.limit:
        todo = todo[: a.limit]
    print("%d verified-but-unmapped payment(s) to reverse and repost" % len(todo))
    rev = made = fail = 0
    for pid, p, m, pr in todo:
        st, rb = api.request(
            "PUT", "/paymentRequest/update",
            [{"paymentRequestId": pid, "paymentRequestStatus": "REVERSED",
              "reversalDate": int(time.time()) * 1000}], retry_5xx=False)
        if not (200 <= (st or 0) < 300):
            fail += 1
            print("  %s REVERSE FAIL (%s) %s" % (pid, st, Api.error_text(st, rb)))
            continue
        rev += 1
        st, rb = api.post("/paymentRequest/", payment_body(p, a.xw, m, pr))
        data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
        npid = (data.get("paymentRequestId")
                or (data.get("paymentRequestDto") or {}).get("paymentRequestId"))
        if not npid:
            fail += 1
            print("  %s REPOST FAIL (%s) %s" % (pid, st, Api.error_text(st, rb)))
            continue
        vst, vb = api.post("/entityVerification/PAYMENT/%s" % npid, {})
        made += 1
        print("  %s -> reversed, reposted as %s with %d link(s)%s"
              % (pid, npid, len(pr),
                 "" if 200 <= (vst or 0) < 300 else "  VERIFY FAILED"))
    print("reversed=%d reposted=%d failed=%d" % (rev, made, fail))
    return 0 if not fail else 1


def revoke_stranded(a):
    """Clear the payments that were created wrong and can never verify.

    Three populations, all created before the defects above were understood:
    approvedAmount null (verify NPEs on a null leg amount), companyBankAccountDto
    null (no credit side at all, because the loader sent the scalar
    companyBankAccountId the brief names), and a party ledger that is not a LEAF
    (verify refuses "Voucher Entry is not possible for non leaf ledger").

    None is repairable in place - PaymentRequestUpdateDto carries no
    contactFinanceAccountId and no bank DTO - so they are soft-deleted and
    un-logged, which is the brief's "verify or revoke each one; do not leave
    them".

    DELETE /paymentRequest/{id} is softDelete, read off the controller's own
    @DeleteMapping("/{paymentRequestId}").
    """
    rows = mysql(
        "SELECT p.id, IFNULL(p.metaData->>'$.sageBatch',''), "
        "       IFNULL(p.metaData->>'$.sageItem',''), "
        "       IF(p.companyBankAccountId IS NULL,'no bank',''), "
        "       IF(p.approvedAmount IS NULL,'no approvedAmount',''), "
        "       IF(f.id IS NOT NULL AND f.leaf=0,'party ledger is not a leaf',''), "
        "       IF(p.currency<>'INR','currency is not INR but the ledger is','') "
        "FROM smeassist.paymentRequest p "
        "LEFT JOIN smeassist.financeAccount f ON f.id=p.contactFinanceAccountId "
        "WHERE p.organisationId='%s' AND p.isDeleted=0 "
        "  AND p.entityLedgerVerificationStatus='PENDING_VERIFICATION' "
        "  %s;" % (ORG_ID, "" if a.revoke_all_pending else
                       "AND (p.companyBankAccountId IS NULL "
                       "OR p.approvedAmount IS NULL "
                       "OR (f.id IS NOT NULL AND f.leaf=0) "
                       "OR p.currency<>'INR')"))
    if not rows:
        print("nothing stranded - no PENDING payment is missing a bank or an amount")
        return 0
    print("%d stranded payment(s) to revoke:" % len(rows))
    for r in rows:
        print("   %-20s %s|%s   %s"
              % (r[0], r[1], r[2], ", ".join(x for x in r[3:] if x)))
    if a.dry_run:
        print("dry run: nothing revoked")
        return 0

    from channels.api import Api                                  # noqa: E402
    api = Api()
    keys, ok, fail = set(), 0, 0
    for r in rows:
        st, rb = api.request("DELETE", "/paymentRequest/%s" % r[0], retry_5xx=False)
        if 200 <= (st or 0) < 300:
            ok += 1
            if r[1]:
                keys.add("%s|%s" % (r[1], r[2]))
            print("   revoked %s" % r[0])
        else:
            fail += 1
            print("   REVOKE FAIL %s (%s) %s" % (r[0], st, Api.error_text(st, rb)))

    # Un-log them, or the repost is skipped as already done.
    if keys and os.path.exists(POSTED):
        kept = [ln for ln in io.open(POSTED) if ln.split("||")[0] not in keys]
        with io.open(POSTED, "w") as fh:
            fh.writelines(kept)
        print("   un-logged %d key(s) so they repost" % len(keys))
    print("revoked=%d failed=%d" % (ok, fail))
    return 0 if not fail else 1


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="crosswalk coverage only")
    ap.add_argument("--pilot", action="store_true",
                    help="one SETTLEMENT and one ADVANCE that both map, then stop")
    ap.add_argument("--bank", help="only this Sage bank code")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--reverse-not-delete", action="store_true",
                    help="with --revoke-duplicates: mark the duplicate REVERSED "
                         "via PUT /paymentRequest/update instead of deleting it, "
                         "which softDelete refuses once a payment is verified")
    ap.add_argument("--revoke-duplicates", action="store_true",
                    help="soft-delete the second and later copy of any payment "
                         "posted twice, keeping the earliest")
    ap.add_argument("--verify-pending", action="store_true",
                    help="verify every sound PENDING payment - one left "
                         "unverified has no voucher and moves no money")
    ap.add_argument("--revoke-all-pending", action="store_true",
                    help="with --revoke-stranded: revoke EVERY unverified "
                         "payment, not only the three known-broken shapes. A "
                         "sound payment verifies in the same loop that creates "
                         "it, so any row left PENDING is a failed one - "
                         "including those whose stored preferredVoucherEntries "
                         "exceed what the bill can absorb, which cannot be "
                         "re-verified because the amount is already committed.")
    ap.add_argument("--revoke-stranded", action="store_true",
                    help="soft-delete PENDING payments that can never verify "
                         "(no bank, or no approvedAmount) and un-log them so a "
                         "later run reposts them correctly")
    a = ap.parse_args()

    if a.revoke_stranded:
        return revoke_stranded(a)
    if a.verify_pending:
        return verify_pending(a)
    if a.revoke_duplicates:
        return revoke_duplicates(a)

    hdr, legs = psv(HDR), psv(LEGS)
    sidx, sbase = sage_bill_index()
    xw = crosswalks()

    print("crosswalks: %d contacts, %d party ledgers, %d banks, %d bills "
          "(%d with a VENDOR leg, %d INVHSEQ-keyed and excluded)"
          % (len(xw["contact"]), len(xw["ledger"]), len(xw["bank"]),
             len(xw["bill"]), len(xw["leg"]), len(xw["miskeyed"])))

    legs_by_payer = collections.defaultdict(list)
    excluded = collections.Counter()
    for lg in legs:
        if lg["link_type"] in ("REVERSAL",):
            excluded["REVERSAL (11/53) - would double-count the settlement"] += 1
            continue
        if lg["link_type"] in ("PAYER_LEG", "NOTE_TO_BILL", "NOTE_UNAPPLIED", "ORPHAN"):
            excluded[lg["link_type"]] += 1
            continue
        legs_by_payer[(lg["app_cntbtch"], lg["app_cntitem"])].append(lg)

    posted = {}
    if os.path.exists(POSTED):
        for ln in io.open(POSTED):
            if "||" in ln:
                posted[ln.split("||")[0]] = ln.strip().split("||")[1]

    plan, held, deferred = [], [], []
    capacity_used = {}        # bill leg id -> already mapped this run
    for p in hdr:
        code = p["bank"].strip()
        key = "%s|%s" % (p["cntbtch"], p["cntitem"])
        if a.bank and code != a.bank:
            continue
        # The PLATFORM is authoritative, not the local log. The log cannot know
        # that a payment was later REVERSED, and a reversed document must be
        # repostable or a reverse-and-repost cycle strands it with no
        # replacement. The log is kept only as a fallback for the case where the
        # platform query returned nothing at all (a failed read, not an empty
        # org), which would otherwise repost everything.
        onplat = xw.get("onplatform") or set()
        if key in onplat or (not onplat and key in posted):
            continue
        if code == "CASH":
            held.append((p, "CASH is not a bank account - needs cashReceiptFinanceAccountId"))
            continue
        if (p["hold_payments"] or "0").strip() not in ("", "0"):
            held.append((p, "bank held: no usable account number in Sage"))
            continue
        if code not in xw["bank"]:
            held.append((p, "bank not created in SMEAssist (no IFSC/SWIFT) - %s" % code))
            continue
        if not PAYMODE.get((p["paymode"] or "").strip().upper()):
            held.append((p, "unmappable paymode %r" % p["paymode"]))
            continue

        if p["vendor"].strip() not in xw["contact"]:
            held.append((p, "vendor has no contact yet - %s" % p["vendor"].strip()))
            continue
        if not xw["ledger"].get(p["vendor"].strip()):
            held.append((p, "vendor has no party ledger - verify would refuse"))
            continue
        maps, prefs = build_links(p, legs_by_payer, xw, sidx, sbase, deferred,
                                  capacity_used)
        plan.append((p, maps, prefs))

    tot = lambda rs: round(sum(Decimal(r["amt_inr"] or 0) for r in rs) / 10000000, 2)
    print("\nplan: %d payments postable (Rs %s Cr), %d held (Rs %s Cr)"
          % (len(plan), tot([x[0] for x in plan]), len(held), tot([x[0] for x in held])))
    withmap = sum(1 for _p, m, pr in plan if m or pr)
    print("  of the postable, %d carry at least one bill link, %d carry none yet"
          % (withmap, len(plan) - withmap))
    print("  mapped legs: %d document links, %d ledger links"
          % (sum(len(m) for _p, m, _pr in plan),
             sum(len(pr) for _p, _m, pr in plan)))
    print("  deferred legs: %d" % len(deferred))
    for why, n in collections.Counter(w for _p, _l, w in deferred).most_common():
        print("      %-58s %6d" % (why, n))
    print("  excluded legs:")
    for why, n in excluded.most_common():
        print("      %-58s %6d" % (why, n))
    print("  held reasons:")
    for why, n in collections.Counter(
            w.split(" - ")[0] for _p, w in held).most_common():
        print("      %-58s %6d" % (why, n))

    os.makedirs(os.path.dirname(HELD_CSV), exist_ok=True)
    with io.open(HELD_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cntbtch", "cntitem", "vendor", "docno", "bank",
                    "amt_inr", "sme_paytype", "why_held"])
        for p, why in held:
            w.writerow([p["cntbtch"], p["cntitem"], p["vendor"], p["docno"],
                        p["bank"], p["amt_inr"], p["sme_paytype"], why])
    print("  exclusion manifest -> %s" % HELD_CSV)

    if a.check:
        return 0

    if a.pilot:
        # One of each that actually maps - a pilot that maps nothing proves nothing.
        pick = []
        for want in ("SETTLEMENT", "ADVANCE"):
            for p, m, pr in plan:
                if p["sme_paytype"].strip() == want and pr:
                    pick.append((p, m, pr))
                    break
        if len(pick) < 2:
            print("\nCannot pilot: no mappable %s found"
                  % ("SETTLEMENT" if not any(
                      p["sme_paytype"].strip() == "SETTLEMENT" for p, _, _ in pick)
                     else "ADVANCE"))
            return 1
        plan = pick
        print("\npilot: %s" % ", ".join(
            "%s %s|%s (%d links)" % (p["sme_paytype"], p["cntbtch"], p["cntitem"], len(pr))
            for p, _m, pr in plan))
    elif a.limit:
        plan = plan[: a.limit]

    if a.dry_run:
        for p, m, pr in plan[:5]:
            print("\n--- %s|%s %s %s" % (p["cntbtch"], p["cntitem"],
                                         p["vendor"], p["sme_paytype"]))
            print(json.dumps(payment_body(p, xw, m, pr), indent=1, default=str))
        print("\ndry run: nothing posted (%d payments in plan)" % len(plan))
        return 0

    from channels.api import Api                                  # noqa: E402
    # The Api default is a 60s read timeout, which one payment cannot fit:
    # 34325|3 carries 1,331 PAYMENT_TO_BILL legs (the next largest is 688), and
    # the server needs longer than a minute to write that many
    # preferredEntityVoucherMapping rows. It timed out twice at 60s.
    #
    # A read timeout on a create is the worst failure shape available - the
    # request may have committed and the client cannot tell - so the answer is
    # to wait long enough, not to retry into an unknown state. (Checked: it had
    # NOT committed either time, so nothing was orphaned.)
    api = Api(timeout=600)
    os.makedirs(STATE, exist_ok=True)
    ok = fail = ver_ok = ver_fail = unlinked = 0
    for p, m, pr in plan:
        key = "%s|%s" % (p["cntbtch"], p["cntitem"])
        body = payment_body(p, xw, m, pr)
        # Api.post returns a (status, body) TUPLE, not a Result. Treating it as
        # an object makes every success read as a failure, so the id never
        # reaches the posted log and the next run posts the row again - which is
        # exactly what happened to 33436|8 and 33973|45 before this was fixed.
        st, rb = api.post("/paymentRequest/", body)
        data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
        # data.paymentRequestId, NOT data.id.
        pid = (data.get("paymentRequestId")
               or (data.get("paymentRequestDto") or {}).get("paymentRequestId"))
        _err = Api.error_text(st, rb) or ""
        if not pid and ("map more than the entity amount" in _err
                        or "remaining amount can not be less than zero" in _err):
            # Two different server guards, same meaning: the bill cannot absorb
            # this leg. "Can not map more than the entity amount" is the entity
            # check; "The voucher remaining amount can not be less than zero for
            # voucher <bill>" is the voucher-level one. Either can fire where the
            # local capacity check passed, because that reads the leg's
            # remainingAmount and the server also counts capacity consumed by
            # payments this loader did not create - the 7 duplicates, for
            # instance. The payment itself is fine and the create rolls back
            # wholly, so retry it BARE.
            #
            # This is the brief's own instruction for a bill we cannot settle
            # correctly: "load those payments WITHOUT the bill mapping rather
            # than mapping to a bill you know is wrong, and record which ones
            # you deferred". Losing the payment entirely would be worse than
            # losing its link, and the link can be added later; a missing
            # payment cannot be found later.
            # Drop ONLY the offending leg, not every link on the payment.
            #
            # This used to retry bare, and that cost real settlement: 384 SBIOSB
            # payments lost ALL their links because ONE leg was over capacity,
            # and they cannot be repaired afterwards - a verified payment can
            # neither be revoked (softDelete refuses it) nor re-linked
            # (/voucherEntry/{id}/mapVoucherEntry returns 200 with an empty body
            # and writes nothing in this build). preferredVoucherEntries is
            # create-time only, so the links have to be right on the way in.
            #
            # The server names the amount it refused - "Can not map more than the
            # entity amount 72662.600000" - so the leg carrying that amount is
            # dropped and the rest are retried. Repeats while it keeps naming a
            # new amount, capped so a pathological payment cannot loop.
            keep_m, keep_p = list(m), list(pr)
            for _ in range(12):
                bad = re.search(r"([0-9]+\.[0-9]+)", _err)
                if not bad:
                    break
                tgt = bad.group(1)
                keep_m = [x for x in keep_m if str(x.get("requestedAmount")) != tgt]
                keep_p = [x for x in keep_p if str(x.get("mappedAmount")) != tgt]
                st, rb = api.post("/paymentRequest/",
                                  payment_body(p, xw, keep_m, keep_p))
                data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
                pid = (data.get("paymentRequestId")
                       or (data.get("paymentRequestDto") or {}).get("paymentRequestId"))
                if pid:
                    break
                _err = Api.error_text(st, rb) or ""
                if ("map more than the entity amount" not in _err
                        and "remaining amount can not be less than zero" not in _err):
                    break
            if not pid:
                # Last resort: the payment matters more than its links.
                st, rb = api.post("/paymentRequest/", payment_body(p, xw, [], []))
                data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
                pid = (data.get("paymentRequestId")
                       or (data.get("paymentRequestDto") or {}).get("paymentRequestId"))
                keep_p = []
            if pid:
                dropped = len(pr) - len(keep_p)
                if dropped:
                    unlinked += 1
                    for lg in legs_by_payer.get((p["cntbtch"], p["cntitem"]), ()):
                        if lg["link_type"] in ("PAYMENT_TO_BILL", "ADVANCE_TO_BILL"):
                            deferred.append((p, lg, "bill could not absorb the Sage "
                                                    "leg - %d of %d link(s) dropped"
                                                    % (dropped, len(pr))))
        if not pid:
            fail += 1
            print("  %-16s CREATE FAIL (%s) %s"
                  % (key, st, Api.error_text(st, rb)))
            continue
        with io.open(POSTED, "a") as fh:
            fh.write("%s||%s\n" % (key, pid))
            fh.flush()
            os.fsync(fh.fileno())
        ok += 1
        vst, vb = api.post("/entityVerification/PAYMENT/%s" % pid, {})
        vok = 200 <= (vst or 0) < 300 and (
            not isinstance(vb, dict) or vb.get("success") is not False)
        if vok:
            ver_ok += 1
            print("  %-16s %-10s Rs %14s  %2d links  -> %s VERIFIED"
                  % (key, p["sme_paytype"], p["amt_inr"], len(pr), pid))
        else:
            ver_fail += 1
            print("  %-16s %-10s -> %s CREATED, VERIFY FAIL (%s) %s"
                  % (key, p["sme_paytype"], pid, vst, Api.error_text(vst, vb)))

    print("\ncreated=%d failed=%d verified=%d verify_failed=%d "
          "posted_unlinked=%d" % (ok, fail, ver_ok, ver_fail, unlinked))

    if ok:
        rows = mysql(
            "SELECT COUNT(*), IFNULL(SUM(mappedAmount),0) FROM smeassist.voucherEntryMapping "
            "WHERE organisationId='%s' AND isDeleted=0;" % ORG_ID)
        print("voucherEntryMapping now: %s rows, mappedAmount %s"
              % (rows[0][0], rows[0][1]) if rows else "voucherEntryMapping: unreadable")
        print("  ^ THE gate: if this is 0 the payments settle nothing.")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
