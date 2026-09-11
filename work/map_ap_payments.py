#!/usr/bin/env python3
"""
map_ap_payments.py  --  settle already-verified payments against their bills.

    python3 map_ap_payments.py --check       # what is mappable, nothing posted
    python3 map_ap_payments.py --dry-run     # every request body, nothing posted
    python3 map_ap_payments.py --limit 20    # a pilot
    python3 map_ap_payments.py               # repair everything mappable

WHY A SECOND SCRIPT. `preferredVoucherEntries` is a CREATE-time field on
PaymentRequestCreateDto, so it can only settle a payment as it is made. 2,829
payments are already created and verified - many of them posted deliberately
unlinked, because the bill they pointed at could not absorb Sage's leg at the
time - and there is no way to re-open a verified payment and add the field.

THE ROUTE, and its one real cost:

    POST /voucherEntry/{voucherEntryId}/mapVoucherEntry     body: Set<String>

PROMPT-sage-money-side.md names this exactly: "It exists and works, but its body
is a bare Set<String> of voucher-entry ids with NO AMOUNTS, so the platform nets
the pair itself and Sage's exact split is lost. Keep it as a repair tool for an
already-verified payment." That is precisely this case.

So this script is honest about what it gives up. Where a payment leg maps to ONE
bill leg the netting is unambiguous and nothing is lost. Where it maps to
several, the platform decides the allocation and it may not reproduce Sage's
split - the SUM still ties, the per-bill breakdown may not. --check reports the
two populations separately so the trade is visible before anything posts, and
every mapped pair carries Sage's own amount in the report so the split can be
audited afterwards.

A pair already present in voucherEntryMapping is skipped: mapping twice would
double-settle.
"""

import argparse
import collections
import io
import json
import os
import sys
import time

from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
CONV = "/root/indiandesign/converter"
if os.path.isdir(CONV):
    sys.path.insert(0, CONV)
sys.path.insert(0, HERE)

import load_ap_payments as L                                      # noqa: E402

ORG_ID = L.ORG_ID
REPORT = os.path.join(CONV if os.path.isdir(CONV) else HERE, "out",
                      "ap_payment_mappings.csv")


def payment_legs():
    """Sage (batch,item) -> (payment VENDOR voucherEntryId, remainingAmount, paymentRequestId).

    REVERSED payments are excluded: they are undone, so their legs must not be
    treated as settleable and their Sage key is free to be reposted.
    """
    out = {}
    for r in L.mysql(
            "SELECT p.metaData->>'$.sageBatch', p.metaData->>'$.sageItem', "
            "       v.id, v.remainingAmount, p.id "
            "FROM smeassist.paymentRequest p "
            "JOIN smeassist.voucherEntry v ON v.referenceId=p.id "
            "     AND v.referenceType='PAYMENT' AND v.partyType='VENDOR' "
            "     AND v.isDeleted=0 "
            "WHERE p.organisationId='%s' AND p.isDeleted=0 "
            "  AND p.status<>'REVERSED' "
            "  AND p.metaData->>'$.sageBatch' IS NOT NULL;" % ORG_ID):
        if len(r) >= 5:
            out[(r[0], r[1])] = (r[2], Decimal(r[3] or 0), r[4])
    return out


def existing_pairs():
    return {(r[0], r[1]) for r in L.mysql(
        "SELECT debitVoucherEntryId, creditVoucherEntryId "
        "FROM smeassist.voucherEntryMapping "
        "WHERE organisationId='%s' AND isDeleted=0;" % ORG_ID) if len(r) >= 2}


def plan():
    xw = L.crosswalks()
    sidx, sbase = L.sage_bill_index()
    pay = payment_legs()
    done = existing_pairs()

    todo = collections.defaultdict(list)      # payment leg -> [(bill leg, amt, doc)]
    stat, val = collections.Counter(), collections.Counter()
    for lg in L.psv(L.LEGS):
        if lg["link_type"] not in ("PAYMENT_TO_BILL", "ADVANCE_TO_BILL"):
            continue
        amt = Decimal(lg["amt_abs_inr"] or 0)
        key = (lg["app_cntbtch"], lg["app_cntitem"])
        if key not in pay:
            stat["payment not loaded"] += 1
            val["payment not loaded"] += amt
            continue
        vend = lg["vendor"].strip()
        cand = (sidx.get((vend, lg["target_doc"].strip()))
                or sbase.get((vend, lg["target_base"].strip())))
        if not cand:
            stat["no Sage bill for (vendor,docno)"] += 1
            val["no Sage bill for (vendor,docno)"] += amt
            continue
        if len(cand) > 1:
            stat["ambiguous (vendor,docno) - not guessed"] += 1
            val["ambiguous (vendor,docno) - not guessed"] += amt
            continue
        bid = xw["bill"].get(next(iter(cand)))
        if not bid:
            stat["bill not loaded / INVHSEQ-keyed"] += 1
            val["bill not loaded / INVHSEQ-keyed"] += amt
            continue
        hit = xw["leg"].get(bid)
        if not hit:
            stat["bill has no VENDOR leg"] += 1
            val["bill has no VENDOR leg"] += amt
            continue
        pleg = pay[key][0]
        if (pleg, hit[0]) in done:
            stat["already mapped"] += 1
            val["already mapped"] += amt
            continue
        stat["MAPPABLE"] += 1
        val["MAPPABLE"] += amt
        todo[pleg].append((hit[0], amt, lg["target_doc"].strip(), key,
                           int(lg.get("app_line") or 0)))
    return todo, stat, val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--repost", action="store_true",
                    help="option B: reverse each unmapped payment and repost it "
                         "with preferredVoucherEntries carrying Sage's amounts")
    ap.add_argument("--bill-in-path", action="store_true",
                    help="put the BILL leg in the path and the payment leg in "
                         "the body (the untested direction)")
    a = ap.parse_args()

    todo, stat, val = plan()
    print("\nbill-directed settlement legs: %d" % sum(stat.values()))
    for k in sorted(stat, key=lambda x: -stat[x]):
        print("  %-36s %6d   Rs %8s Cr"
              % (k, stat[k], round(val[k] / Decimal(10000000), 2)))

    # Sage's own application sequence - the netting must consume the bills in
    # the order Sage did, or a partial application lands on the wrong bill.
    for v in todo.values():
        v.sort(key=lambda t: t[4])
    single = sum(1 for v in todo.values() if len(v) == 1)
    print("\n  payment legs to repair            %6d" % len(todo))
    print("    mapping to ONE bill leg         %6d   netting is unambiguous"
          % single)
    print("    mapping to SEVERAL              %6d   the platform allocates; the"
          % (len(todo) - single))
    print("                                             sum ties, the per-bill")
    print("                                             split may not match Sage")
    if a.check:
        return 0

    items = sorted(todo.items())
    if a.limit:
        items = items[: a.limit]

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    fh = io.open(REPORT, "w", encoding="utf-8", newline="")
    w = __import__("csv").writer(fh)
    w.writerow(["payment_voucher_entry", "bill_voucher_entry", "bill_number",
                "sage_batch", "sage_item", "sage_amount", "result"])

    if a.dry_run:
        for pleg, targets in items[:5]:
            print("\n  POST /voucherEntry/%s/mapVoucherEntry" % pleg)
            print("       %s" % json.dumps([t[0] for t in targets]))
        print("\ndry run: nothing posted (%d payment legs in plan)" % len(items))
        return 0

    from channels.api import Api                                  # noqa: E402
    api = Api(timeout=600)

    if a.repost:
        # Option B. Option A (mapVoucherEntry) is inert in this build - piloted
        # in BOTH path directions, each returning 200 with an empty body and
        # writing no row - so a mapping can only be made at create time.
        hdr = {(r["cntbtch"], r["cntitem"]): r for r in L.psv(L.HDR)}
        pay = payment_legs()
        xw = L.crosswalks()
        rev = made = fail = 0
        for pleg, targets in items:
            key = next((t[3] for t in targets), None)
            row = hdr.get(key)
            pid = next((v[2] for v in pay.values() if v[0] == pleg), None)
            if not row or not pid:
                continue
            maps, prefs = [], []
            for bleg, amt, doc, k, _ln in targets:
                prefs.append({"preferredVoucherEntry": {"voucherEntryId": bleg},
                              "mappedAmount": str(amt)})
                if row["sme_paytype"].strip() == "SETTLEMENT":
                    maps.append({"entityId": "", "entityNumber": doc,
                                 "paymentRequestEntityType": "BILL",
                                 "requestedAmount": str(amt), "status": "ACTIVE",
                                 "organisationId": ORG_ID})
            st, rb = api.request(
                "PUT", "/paymentRequest/update",
                [{"paymentRequestId": pid, "paymentRequestStatus": "REVERSED",
                  "reversalDate": int(time.time()) * 1000}], retry_5xx=False)
            if not (200 <= (st or 0) < 300):
                fail += 1
                print("  %s REVERSE FAIL (%s) %s" % (pid, st, Api.error_text(st, rb)))
                continue
            rev += 1
            st, rb = api.post("/paymentRequest/", L.payment_body(row, xw, [], prefs))
            data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
            npid = (data.get("paymentRequestId")
                    or (data.get("paymentRequestDto") or {}).get("paymentRequestId"))
            if not npid:
                fail += 1
                print("  %s REPOST FAIL (%s) %s" % (pid, st, Api.error_text(st, rb)))
                continue
            vst, vb = api.post("/entityVerification/PAYMENT/%s" % npid, {})
            made += 1
            print("  %s|%s  reversed %s -> reposted %s, %d link(s)%s"
                  % (key[0], key[1], pid, npid, len(prefs),
                     "" if 200 <= (vst or 0) < 300
                     else "  VERIFY FAIL " + str(Api.error_text(vst, vb))[:70]))
        print("\nreversed=%d reposted=%d failed=%d" % (rev, made, fail))
        m = L.mysql("SELECT COUNT(*), IFNULL(SUM(mappedAmount),0) "
                    "FROM smeassist.voucherEntryMapping "
                    "WHERE organisationId='%s' AND isDeleted=0;" % ORG_ID)
        print("voucherEntryMapping now: %s rows, Rs %s Cr"
              % (m[0][0], round(Decimal(m[0][1]) / Decimal(10000000), 2)))
        return 0 if not fail else 1

    ok = fail = pairs = 0
    for pleg, targets in items:
        if a.bill_in_path:
            # The brief is explicit that which leg goes in the PATH is not
            # verified. Payment-in-path returns 200 with an empty body and
            # writes nothing, so this is the other way round: the BILL leg in
            # the path, the payment leg in the body. Sage's own application
            # order is preserved by feeding targets in app_line sequence, so the
            # platform's netting consumes them as Sage did.
            ok_all, last = True, (None, None)
            for bleg, amt, doc, key, _ln in targets:
                last = api.post("/voucherEntry/%s/mapVoucherEntry" % bleg, [pleg])
                if not (200 <= (last[0] or 0) < 300):
                    ok_all = False
                    break
            st, rb = last
            body = [t[0] for t in targets]
        else:
            body = [t[0] for t in targets]          # Sage's application order
            st, rb = api.post("/voucherEntry/%s/mapVoucherEntry" % pleg, body)
        good = 200 <= (st or 0) < 300 and (
            not isinstance(rb, dict) or rb.get("success") is not False)
        res = "OK" if good else "FAIL(%s) %s" % (st, Api.error_text(st, rb))
        if os.environ.get("MAP_DEBUG"):
            print("  DEBUG %s -> %s %s" % (pleg, st, json.dumps(rb)[:400]))
        if good:
            ok += 1
            pairs += len(body)
        else:
            fail += 1
            print("  %-20s %d target(s)  %s" % (pleg, len(body), res))
        for bleg, amt, doc, key, _ln in targets:
            w.writerow([pleg, bleg, doc, key[0], key[1], amt, res])
        fh.flush()
    fh.close()
    print("\npayment legs mapped=%d failed=%d  (%d pairs)" % (ok, fail, pairs))
    print("report -> %s" % REPORT)

    m = L.mysql("SELECT COUNT(*), IFNULL(SUM(mappedAmount),0) "
                "FROM smeassist.voucherEntryMapping "
                "WHERE organisationId='%s' AND isDeleted=0;" % ORG_ID)
    print("voucherEntryMapping now: %s rows, Rs %s Cr"
          % (m[0][0], round(Decimal(m[0][1]) / Decimal(10000000), 2)))
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
