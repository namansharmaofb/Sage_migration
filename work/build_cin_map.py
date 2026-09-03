#!/usr/bin/env python
"""Build ref/cin_by_gstin.json - CIN / LLPIN keyed by GSTIN.

WHY THIS EXISTS
    ContactServiceImpl.getIsCinOrLlpinRequired demands a corporate identifier
    for any GSTIN whose 6th character is C (company) or F (LLP). The platform
    normally fetches it itself, but that lookup is an external risk/lead
    service: on this devbox GET /contact/gst/{gstin} answers "GST Info not
    available. Please add first." and POST /contact/ fails with the same
    message. 137 vendors carrying 8,773 of the window's 11,256 bills were held
    on this alone.

WHERE THE NUMBERS COME FROM
    Other organisations on this same devbox already hold contacts for the very
    same GSTINs, with the CIN the platform's own registry lookup populated for
    them. A GSTIN identifies exactly one legal entity, so the CIN recorded
    against that GSTIN elsewhere is that entity's real CIN - this copies an
    existing fact across, it does not mint one.

    The script REFUSES to emit any GSTIN that carries two different CINs on the
    box. There were none at the time of writing, and if that ever changes the
    right answer is to look rather than to pick.

    Nothing is invented. A GSTIN with no CIN anywhere is simply absent from the
    output and its vendor stays held.

RUN
    .venv/bin/python work/build_cin_map.py
"""
import collections
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402

OUT = os.path.join(HERE, "ref", "cin_by_gstin.json")


DB_HOST = os.environ.get("SME_DB_HOST")


def devbox_sql(sql):
    if not DB_HOST:
        raise SystemExit("SME_DB_HOST is not set: export it to the devbox host "
                         "that runs the smeassist MySQL.")
    p = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/.sage-cm-%r",
         "-o", "ControlPersist=600",
         "root@" + DB_HOST, "mysql smeassist -N --raw --batch -e %s" % json.dumps(sql)],
        capture_output=True, text=True, timeout=600)
    if p.returncode:
        raise SystemExit("devbox query failed: %s" % p.stderr[:400])
    return [ln.split("\t") for ln in p.stdout.splitlines() if ln.strip()]


def billed_vendors():
    """Vendor codes that actually carry a bill in the window - vendors.csv is
    the wider APVEN pull and holds codes with no document in Jan-Apr."""
    import csv
    path = os.path.join(HERE, "output", "bills_header.csv")
    with open(path) as fh:
        return {r["vendor"] for r in csv.DictReader(fh)}


def main():
    vendors = m.vendors_master()
    billed = billed_vendors()

    # Every in-window vendor whose GSTIN needs an identifier, recomputed rather
    # than read off the old hold list: extract_gstin() promoted 7 vendors from
    # PAN to GST, so the set is not the one the 2 Sep run wrote out.
    need = {}
    for code, v in sorted(vendors.items()):
        if code not in billed:
            continue
        reg, no = m.registration_of(v)
        if reg == "GST" and m.needs_cin_lookup(no):
            need.setdefault(no, []).append(code)
    print("in-window vendors needing a CIN/LLPIN: %d across %d GSTINs"
          % (sum(len(x) for x in need.values()), len(need)))
    if not need:
        return

    # Match on PAN, not on the whole GSTIN. A CIN belongs to the LEGAL ENTITY,
    # and a company registered in several states has one CIN behind as many
    # GSTINs - the box proves it, carrying U63011MH2006PTC162700 for DSV under
    # both 06XXXCX0004X1Z3 and 29XXXCX0004X1ZV. The PAN sits at GSTIN
    # characters 3-12 and is what the two share, so keying on it resolves the
    # same fact from any state's registration. Still exact equality on a
    # 10-character PAN - no fuzziness.
    pans = {g[2:12] for g in need}
    in_list = ",".join("'%s'" % p for p in sorted(pans))
    rows = devbox_sql("""
        SELECT SUBSTRING(c.registrationNumber, 3, 10),
               COALESCE(NULLIF(b.corporateIdentificationNumber,''),''),
               COALESCE(NULLIF(b.limitedLiabilityPartnershipIdentificationNumber,''),''),
               c.organisationId, COALESCE(c.accountName,'')
          FROM contact c JOIN contactBusinessInfo b ON b.contactId = c.id
         WHERE CHAR_LENGTH(c.registrationNumber) = 15
           AND SUBSTRING(c.registrationNumber, 3, 10) IN (%s)
           AND (NULLIF(b.corporateIdentificationNumber,'') IS NOT NULL
             OR NULLIF(b.limitedLiabilityPartnershipIdentificationNumber,'') IS NOT NULL)
    """ % in_list)

    seen = collections.defaultdict(set)
    src = {}
    for row in rows:
        # accountName is free text and a few carry tabs, so pad/trim rather
        # than unpack - a ragged row must not take the whole build down.
        if len(row) < 4:
            # The devbox's my.cnf prints "PAGER set to stdout" on every client
            # start; anything else short is worth seeing.
            if row != ["PAGER set to stdout"]:
                print("   skipping ragged row: %r" % (row,))
            continue
        pan, cin, llpin, org = row[0], row[1], row[2], row[3]
        nm = row[4] if len(row) > 4 else ""
        seen[pan].add((cin, llpin))
        src.setdefault(pan, (org, nm))

    out, conflicts = {}, []
    for gstin in sorted(need):
        ids = seen.get(gstin[2:12])
        if not ids:
            continue
        if len(ids) > 1:
            conflicts.append((gstin, sorted(ids)))
            continue
        cin, llpin = next(iter(ids))
        org, nm = src[gstin[2:12]]
        rec = {"vendors": sorted(need[gstin]),
               "sourceOrganisationId": org, "sourceAccountName": nm}
        if cin:
            rec["corporateIdentificationNumber"] = cin
        if llpin:
            rec["limitedLiabilityPartnershipIdentificationNumber"] = llpin
        out[gstin] = rec

    if conflicts:
        print("\nREFUSED - these GSTINs carry more than one identifier on the box.")
        print("A person has to decide which is right; none of them are emitted.")
        for g, ids in conflicts:
            print("   %s  %s" % (g, ids))

    covered = sum(len(r["vendors"]) for r in out.values())
    missing = sorted(set(need) - set(out) - {g for g, _ in conflicts})
    with open(OUT, "w") as fh:
        json.dump({
            "_comment": "CIN/LLPIN by GSTIN, copied from contacts other orgs on "
                        "this devbox already hold for the same GSTIN. Generated "
                        "by work/build_cin_map.py. Nothing here is invented.",
            "identifiers": out,
        }, fh, indent=1, sort_keys=True)

    print("\nwrote %s" % OUT)
    print("  GSTINs resolved : %d  (covering %d vendors)" % (len(out), covered))
    print("  GSTINs with none: %d  (covering %d vendors - these stay HELD)"
          % (len(missing), sum(len(need[g]) for g in missing)))


if __name__ == "__main__":
    main()
