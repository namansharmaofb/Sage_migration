# Runbook: load the whole Jan–Apr 2026 window

> **Read `work/CHANGES-2026-09-03.md` first.** On 3 Sep the contact gate that
> stopped this runbook at step 1 was opened: contacts went from 136 to **305**
> and the held list from 283 to **114**. Several things below are now out of
> date — in particular **step 1 no longer needs Sage up** (it falls back to
> `output/vendors.csv` and `output/gl_accounts.csv`), and Sage has been
> unreachable from every host since that morning.

Measured on this box, 2 Sep 2026. Two populations, two pipelines, and they share
one crosswalk file — so they run **one at a time, never in parallel**.

## 0. Before you start

**Wait for the run that is already going.** `pid 78737` is running
`goods-masters --limit 70` right now. Both pipelines write
`work/crosswalk_live.json` and mint SKUs; two at once race on both, and the
`SAGE-4E2ME07-R2` duplicate that is still sitting in the org was born from
exactly that class of race. Check with:

    pgrep -af post_sage_bills.py

**Get a fresh SME_TOKEN.** The whole load is ~12 h of wall clock (below), and
`Api` treats a 403 as rate limiting: it backs off 6 times and then gives up. An
**expired token looks exactly like rate limiting**, so a stale token will not
announce itself — it will just start failing everything. Put the new one in
`.env` (`SME_TOKEN=...`), or pass `--token` per command.

**Check Sage is up** — `<sage-host>:1433`:

    .venv/bin/python -c "import sys;sys.path.insert(0,'.');import post_sage_bills as m;print(m.sage_query('SELECT 1 x')[0])"

It matters for **step 2 only**. `masters` reads vendor and GL-account names
straight from Sage (`SQL_VENDORS`, `SQL_GL`) and has no staging fallback. The
box is on Wi-Fi DHCP and drops regularly — it went down mid-session today. The
`post` phase does not care (headers fall back to `Bills_JanApr_Header.psv`), and
every goods phase reads staging and never needs Sage at all.

**Run everything under `nohup`, not in a terminal you will close.**

## 1. Time and volume

    API round trip incl. THROTTLE = 0.44 s   (THROTTLE = 0.4, MAX_RETRY = 6)

| step | calls | time |
|---|---:|---:|
| AP masters — 188 GL products + 407 contacts | 2,600 | ~0.3 h |
| AP post — 11,232 bills (create + verify + readback) | 33,700 | ~4.1 h |
| goods masters — up to 17,129 item products | 34,300 | ~4.2 h |
| goods post — 10,378 documents | 31,100 | ~3.8 h |
| | | **~12.5 h** |

Everything **resumes**: both post phases skip any document already in
`work/posted.log`, and both masters phases skip anything already in the
crosswalk. So killing a run and restarting it costs nothing but the in-flight
document. Batch it however suits you.

## 2. AP-direct: masters (needs Sage up)

    nohup .venv/bin/python post_sage_bills.py masters \
      > work/ap_masters.log 2>&1 &

Builds 188 GL-account products and 407 vendor contacts. Watch for:

- `BURNED <sku>` — a SKU is taken by a row adoption cannot see. Two are already
  burned (`SAGE-4E2ME02-14`, `SAGE-4E5O024-01`). Bills on those accounts will
  skip with "no product for GL account" rather than mint a `-R2` duplicate.
- contacts landing in `work/contacts_held.json` — those vendors' bills will skip.

Check it before moving on:

    grep -c "created\|adopted" work/ap_masters.log
    python3 -c "import json;x=json.load(open('work/crosswalk_live.json'));print(len(x['products']),'products',len(x['contacts']),'contacts')"

You want ~188 products and ~407 contacts. **Contacts are the real gate** — the
earlier run skipped 11,170 bills for "no contact built for vendor", and nothing
scales until they exist.

## 3. AP-direct: dry run, then post

Always dry-run first — it shapes all 11,232 and posts nothing:

    .venv/bin/python post_sage_bills.py dryrun 2>&1 | tail -40

Read the grouped skip list at the top. Expect **24 held** (see §6). If the skip
list shows thousands of "no contact built for vendor", step 2 did not finish.

Then post. Start with a batch to confirm the shape is right in the org:

    nohup .venv/bin/python post_sage_bills.py post --limit 50 \
      > work/ap_post_50.log 2>&1 &

Verify those 50 (§5), then let the rest run — it resumes past the 50:

    nohup .venv/bin/python post_sage_bills.py post \
      > work/ap_post_all.log 2>&1 &

## 4. Goods / PO population

Same shape, staging-backed, so Sage may be down:

    nohup .venv/bin/python post_sage_bills.py goods-masters > work/goods_masters.log 2>&1 &
    .venv/bin/python post_sage_bills.py goods-dryrun 2>&1 | tail -40
    nohup .venv/bin/python post_sage_bills.py goods-post --limit 50 > work/goods_post_50.log 2>&1 &
    nohup .venv/bin/python post_sage_bills.py goods-post > work/goods_post_all.log 2>&1 &

`goods-masters` without `--limit` mints up to 17,129 item products — the ~4.2 h
line above, and the biggest single write in the whole exercise. Two things to
decide first, both in `work/RECON-BOTH-SITES.md`:

- **1,563 items have no HSN** and will be created with `EXPENSE_SAC = "996719"`,
  a *service* SAC, on material items. One-line fix; not applied because your
  other session owns the file.
- **every item is created as `RESOURCE`**, not `RAW_MATERIAL`, and the
  docstring's reason for that is now out of date — the org owns 4 catalog
  categories. Untested whether `RAW_MATERIAL` works now.

Add `--all-categories` for every item category rather than raw materials only.

## 5. Verify — after every batch, not just at the end

    .venv/bin/python post_sage_bills.py verify

All seven checks should return clean / 0. Then the two-site reconciliation,
which is the one that compares SMEAssist back against Sage:

    .venv/bin/python /tmp/.../scratchpad/recon.py     # copy it into work/ first

`post` now also reads every bill back and compares it with Sage
(`readback_drift`). Anything that disagrees is printed as `POST-CHECK:` and
lands in `work/failures.json` — **check that file after every batch**; it is the
thing that would have caught `JOBW258|108` sitting a rupee below Sage.

Also expect, and do not chase:

- `RCM TAX VARIANCE` lines, ~795 of them → `work/rcm_variance.json`. Sage rounds
  reverse-charge tax to the rupee on 1,081 of 1,084 documents; the slab figure
  is the exact one. ≤ ₹1.95 each, ₹495 in total.
- a silent ±₹0.01 on 503 forward documents, from Sage truncating each GST
  authority separately. `readback_drift` tolerates it deliberately.

## 6. What will not load, and why

    24 documents held by classify()
        9   stated per-line tax != document tax     ) Sage-side data.
        8   RCM derived rate off every legal slab   ) A person has to
        1   no 4E expense line                      ) look at these in Sage.
        6   2A7TX01/02/03 recoverable, no mapped ledger  <- one-line fix

The 6 need their ledger ids added to `PASSTHRU_GL`; they are worth ₹6,903. The
other 18 are listed with their reasons in `work/ERRORS-JanApr-2026.csv`.

## 7. Loose ends in the org from earlier runs

`post` will not touch these — they need a decision:

- **8 bills sit on `SAGE-4E2ME07-R2`**, a duplicate expense head holding
  ₹1,775,411. The crosswalk now points at the correct product, so nothing new
  will land there, but those 8 need revoking and reposting.
- **`JOBW258|108` is ₹1.00 below Sage** — posted before the round-off fix.
- **10 PO-matched bills book `1L6TA07`**, a balance-sheet clearing account, as an
  expense. Settle that classification before more of that population goes in.
- **`SMOKE/2026/1`** is REVOKED but still live, and the duplicate series counter
  rows are unchanged.

`work/cleanup_pilot.py` removes the first group. It is destructive and has not
been run.

---

## CORRECTION to the timing above — writes are far slower than reads

The table in §1 was built from GET latency (0.44 s). Measured from the
`goods-masters --limit 70` run that was interrupted at 16:25 — **48 item
products in about 10 minutes, i.e. ~12 s each** — writes are roughly 25x
slower. If that rate holds:

| | items/bills | at ~12 s each |
|---|---:|---:|
| AP post | 11,232 | ~37 h |
| goods item master | 17,129 | ~57 h |
| goods post | 10,378 | ~35 h |

That is days, not hours, so **measure it yourself in the first few minutes**
rather than trusting either figure. With a run going:

    watch -n30 'python3 -c "import json;print(len(json.load(open(\"work/crosswalk_live.json\")).get(\"items\",{})))"'

or for bills:

    watch -n30 'wc -l work/posted.log'

If it really is ~12 s per write, the throttle is not the cause (`THROTTLE` is
0.4 s) — it is the platform's own write latency plus, for items,
`find_product_by_sku` paging a fuzzy endpoint up to ten times on any SKU
collision. Worth profiling one create before committing to a multi-day run.

Everything resumes, so the practical approach is to run it in batches with
`--limit` and let it continue across sessions.
