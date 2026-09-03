#!/usr/bin/env python
"""Revoke the bills that posted WITHOUT their Sage item detail, so the next
`post` run rebuilds them with it.

WHY
    load_po_items() had no source while <sage-host> was on its old DHCP lease, so
    po_detail() reported "no PO linked" and these bills posted as a single
    distribution line instead of the items Sage ordered. The amounts were never
    wrong - po_detail() only ever admits single-GL documents, so the ledger and
    the taxable are identical either way - but the item-level narrative was
    lost. Sage is reachable again and the detail is cached in
    work/po_items_cache.json, so they can be rebuilt properly.

    One of them is JOBW258|108, RUNBOOK section 7's known Rs 1.00 discrepancy:
    it posted before round-off became a line, so its stored taxable is short by
    exactly the 4E1M016 amount. Reposting corrects that too.

WHAT IT DOES, PER BILL
    1. PUT /bill/updateStatus/{id}/REVOKED
    2. DELETE /bill/{id}          (soft delete - this is what cleanup_pilot.py
                                   does, and it is what lets the same
                                   billNumber be used again)
    3. drops the line from work/posted.log

    Then re-run `post` and they come back with their item lines.

SAFETY
    Targets ONLY the keys in work/repost_needed_po_items.txt, which is generated
    by intersecting work/posted.log with the PO cache - nothing is matched by
    pattern and it cannot reach past what it names. It REFUSES to run while a
    post run is in progress, because both write work/posted.log.

    --dry-run (the default) prints the plan and changes nothing.
    --apply   actually revokes.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

TARGETS = os.path.join(HERE, "work", "repost_needed_po_items.txt")


def another_run_in_progress():
    out = subprocess.run(["pgrep", "-af", r"post_sage_bills\.py post"],
                         capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if "pgrep" not in ln]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually revoke; without it nothing is changed")
    args = ap.parse_args()

    busy = another_run_in_progress()
    if busy:
        print("REFUSING: a post run is in progress - it writes work/posted.log too.")
        for ln in busy:
            print("   " + ln)
        return 1

    if not os.path.exists(TARGETS):
        print("nothing to do: %s does not exist" % TARGETS)
        return 0
    keys = [ln.strip() for ln in open(TARGETS) if ln.strip()]

    posted = {}
    for ln in open(m.POSTED_LOG):
        ln = ln.strip()
        if ln:
            posted[ln.split("||")[0]] = ln.split("||")[-1]

    plan = [(k, posted[k]) for k in keys if k in posted]
    missing = [k for k in keys if k not in posted]
    print("bills to revoke and repost: %d" % len(plan))
    for k, bid in plan:
        print("   %-34s %s" % (k, bid))
    if missing:
        print("not in posted.log (already handled): %s" % ", ".join(missing))

    if not args.apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply, then re-run "
              "`post` to rebuild them with their item lines.")
        return 0

    api = m.Api()
    done = set()
    for k, bid in plan:
        st, b = api.call("PUT", "/bill/updateStatus/%s/REVOKED"
                                "?remarks=repost+with+Sage+item+detail" % bid)
        rv = "ok" if api.ok(st, b) else api.err(b)[:70]
        st2, b2 = api.call("DELETE", "/bill/%s" % bid)
        dl = "ok" if api.ok(st2, b2) else api.err(b2)[:70]
        print("   %-34s revoke=%-8s delete=%s" % (k, rv, dl))
        if api.ok(st, b):
            done.add(k)

    # Rewrite posted.log without the revoked keys, so `post` rebuilds them.
    kept = [ln for ln in open(m.POSTED_LOG)
            if ln.strip() and ln.split("||")[0] not in done]
    tmp = m.POSTED_LOG + ".tmp"
    with open(tmp, "w") as fh:
        fh.writelines(kept)
    os.replace(tmp, m.POSTED_LOG)
    print("\nremoved %d keys from work/posted.log - now re-run `post`." % len(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
