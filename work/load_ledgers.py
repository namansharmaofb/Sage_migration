#!/usr/bin/env python3
"""
load_ledgers.py  --  create Sage chart accounts as SMEAssist ledgers.

    python3 load_ledgers.py --prefix 1L8TD --parent-name "Statutory payables" --check
    python3 load_ledgers.py --prefix 1L8TD --parent-name "Statutory payables"

Reads extract-v2/out/Chart_Of_Accounts.psv (1,610 accounts, 0 malformed).

NAMING IS NOT COSMETIC. Every leaf is created with the chart's own
`sme_ledger_name`, which is "<ACCTDESC> _SAGE-<ACCTFMTTD>". Control 3 - the only
control that catches a misfiled line - reconciles per bill x Sage GL account by
reading that suffix back, and it passed 10,227 of 10,227 on the existing load.
Renaming a leaf breaks that silently.

WHY THIS EXISTS AT ALL: the org has 18,048 EXPENSE ledgers but they are mostly
auto-minted per-SKU purchase accounts, not the Sage chart. The accounts a
document actually needs get created on demand by the bill loader, so anything
never touched by a loaded bill is simply absent - which is why all 1,352 TDS
notes were held: 1L8TD01 and its siblings are in the chart extract as
LIABILITIES but were never created.

`acct_bal` ships in the extract and is NEVER loaded as openingBalance. This
migration carries transaction history; an opening balance on top double-counts.
"""

import argparse
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONV = "/root/indiandesign/converter"
if os.path.isdir(CONV):
    sys.path.insert(0, CONV)
sys.path.insert(0, HERE)

import load_ap_payments as L                                      # noqa: E402

ORG_ID = L.ORG_ID
CHART = os.path.join(L.REF, "extract-v2/out/Chart_Of_Accounts.psv")


def existing():
    """SAGE code -> (id, name) for every ledger already carrying a _SAGE- tag."""
    out = {}
    for r in L.mysql(
            "SELECT name, id FROM smeassist.financeAccount "
            "WHERE organisationId='%s' AND isDeleted=0 AND name LIKE '%%\\_SAGE-%%';"
            % ORG_ID):
        if len(r) >= 2 and "_SAGE-" in r[0]:
            out[r[0].rsplit("_SAGE-", 1)[1].strip()] = (r[1], r[0])
    return out


# financeGroupType -> an EXISTING non-leaf ledger to hang the Sage chart under.
# These are the platform's own seeded groups, not something invented: a leaf
# must sit under a parent of the SAME group or the account is misfiled.
#
# EXPENSE splits by the brief's own billTypeMap: 4E1M is DIRECT_EXPENSE,
# everything else indirect. Getting that wrong misfiles the P&L, which is why
# it is read off the account code rather than defaulted.
PARENTS = {
    "ASSET":       ("1202966497556471808", "Current Assets"),
    "LIABILITIES": ("1202966499833978880", "Current Liabilities"),
    "INCOME":      ("1202966516607000576", "Other Income"),
    "EXPENSE":     ("1202966502790963200", "Indirect Expenses"),
}
EXPENSE_DIRECT = ("1202966504326078464", "Direct Expenses")


def parent_for(row):
    g = row["sme_financegroup"].strip()
    if g == "EXPENSE" and row["acct_fmt"].strip().startswith("4E1M"):
        return EXPENSE_DIRECT
    return PARENTS.get(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=False, default="",
                    help="Sage account-code prefix to create, e.g. 1L8TD")
    ap.add_argument("--parent-name", default=None,
                    help="override the per-group parent (single-prefix use)")
    ap.add_argument("--all", action="store_true",
                    help="create every missing chart account, parented by its "
                         "own financeGroupType")
    ap.add_argument("--accounts", help="comma-separated account codes only")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    chart = [r for r in L.psv(CHART)
             if r["acct_fmt"].strip().startswith(a.prefix)]
    if a.accounts:
        want = {c.strip() for c in a.accounts.split(",") if c.strip()}
        chart = [r for r in chart if r["acct_fmt"].strip() in want]
    have = existing()
    todo = [r for r in chart if r["acct_fmt"].strip() not in have]

    if a.all or a.accounts or not a.parent_name:
        print("\nchart accounts matched: %d   already ledgers: %d   to create: %d"
              % (len(chart), len(chart) - len(todo), len(todo)))
        import collections as _c
        byg = _c.Counter(r["sme_financegroup"].strip() for r in todo)
        for g, n in byg.most_common():
            tgt = PARENTS.get(g)
            print("    %-12s %5d -> %s" % (g, n, tgt[1] if tgt else
                                           "NO PARENT - skipped, needs a decision"))
        if a.check:
            return 0
        from channels.api import Api                              # noqa: E402
        api = Api(timeout=600)
        ok = fail = skip = 0
        for r in todo:
            # MAPPING.json rateLimit.perItemThrottleMs = 400. Without it a
            # 1,601-row run trips the limiter, and the converter's Api treats
            # 403 as fatal ("stop and ask for a fresh cookie") - which is right
            # for a real 403 but wrong for throttling. The tell, per the same
            # section: the FIRST refusal says "Rate limit exceeded" and every
            # call after it returns 403 with an EMPTY body. Throttling here is
            # cheaper than teaching the shared helper to tell them apart.
            time.sleep(0.4)
            tgt = parent_for(r)
            if not tgt:
                skip += 1
                continue
            name = r["sme_ledger_name"].strip()
            body = {
                "name": name, "accountingName": name,
                "financeGroupType": r["sme_financegroup"].strip(),
                "parentFinanceId": tgt[0],
                "partyId": ORG_ID, "partyType": "SELF",
                "leaf": True, "organisationId": ORG_ID,
            }
            # MAPPING.json rateLimit: retry while the status is 403 or the body
            # says "Rate limit", exponential, capped ~30s, 6 attempts. The
            # shared Api RAISES Stop on any 403, so the retry has to wrap it -
            # a rate limit is a pause, not a rejection, and treating it as one
            # loses the rest of the run.
            st, rb = None, None
            for attempt in range(6):
                try:
                    st, rb = api.post("/financeAccount/", body)
                    break
                except Exception as exc:                          # noqa: BLE001
                    if "403" not in str(exc) and "Rate limit" not in str(exc):
                        raise
                    wait = min(30, 2 ** attempt * 2)
                    print("  rate limited, waiting %ds (attempt %d/6)"
                          % (wait, attempt + 1))
                    time.sleep(wait)
            if st is None:
                fail += 1
                print("  %-12s gave up after 6 rate-limited attempts"
                      % r["acct_fmt"])
                continue
            data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
            if data.get("financeAccountId") or data.get("id"):
                ok += 1
            else:
                fail += 1
                if fail <= 8:
                    print("  %-12s FAIL (%s) %s"
                          % (r["acct_fmt"], st, Api.error_text(st, rb)))
        print("\ncreated=%d failed=%d skipped_no_parent=%d" % (ok, fail, skip))
        return 0 if not fail else 1

    par = L.mysql(
        "SELECT id, financeGroupType, leaf FROM smeassist.financeAccount "
        "WHERE organisationId='%s' AND isDeleted=0 AND name=%s LIMIT 1;"
        % (ORG_ID, "'" + a.parent_name.replace("'", "''") + "'"))
    if not par:
        raise SystemExit("parent ledger %r not found in this org" % a.parent_name)
    pid, pgroup, pleaf = par[0][0], par[0][1], par[0][2]
    if pleaf == "1":
        raise SystemExit("parent %r is a LEAF - a leaf cannot take children, and "
                         "a voucher cannot post to a non-leaf" % a.parent_name)

    print("\nchart accounts matching %r: %d   already present: %d   to create: %d"
          % (a.prefix, len(chart), len(chart) - len(todo), len(todo)))
    print("  parent: %s (%s, id %s)" % (a.parent_name, pgroup, pid))
    groups = sorted({r["sme_financegroup"].strip() for r in todo})
    print("  financeGroupType in the chart: %s" % ", ".join(groups))
    if groups and groups != [pgroup]:
        print("  NOTE the parent is %s; a child of a different group would misfile "
              "the account, so those are skipped." % pgroup)
    for r in todo[:8]:
        print("    %-12s %-40.40s %s" % (r["acct_fmt"], r["sme_ledger_name"],
                                         r["sme_financegroup"]))
    if a.check:
        return 0

    from channels.api import Api                                  # noqa: E402
    api = Api(timeout=600)
    ok = fail = skip = 0
    for r in (todo[: a.limit] if a.limit else todo):
        if r["sme_financegroup"].strip() != pgroup:
            skip += 1
            continue
        name = r["sme_ledger_name"].strip()
        st, rb = api.post("/financeAccount/", {
            "name": name,
            "accountingName": name,
            "financeGroupType": r["sme_financegroup"].strip(),
            "parentFinanceId": pid,
            "partyId": ORG_ID, "partyType": "SELF",
            "leaf": True, "organisationId": ORG_ID,
            # acct_bal is deliberately NOT sent - see the module docstring.
        })
        data = (rb or {}).get("data") or {} if isinstance(rb, dict) else {}
        fid = data.get("financeAccountId") or data.get("id")
        if fid:
            ok += 1
            print("  %-12s created -> %s  %s" % (r["acct_fmt"], fid, name[:46]))
        else:
            fail += 1
            print("  %-12s FAIL (%s) %s"
                  % (r["acct_fmt"], st, Api.error_text(st, rb)))
    print("\ncreated=%d failed=%d skipped_wrong_group=%d" % (ok, fail, skip))
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
