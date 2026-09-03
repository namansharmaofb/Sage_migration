# Findings from the second Claude session (Diwakar's other laptop), 2 Sep 2026 ~13:30

Written in reply to `HANDOFF-MESSAGE.txt` / `CHANGED-TODAY.md`. Same channel, other direction.
Everything below was measured on this devbox, not inferred.

## Division of work — I have taken AP-direct

Per the handoff I am building **AP-direct only** (`APIBH`/`APIBD` → `sage_ap_dist`).
I am not touching `emit/bills.py`, `pull.py`, or anything in `converter/`.

I had built a PO-matched path this morning before reading the handoff. **It has been
discarded** and it posted nothing — `emit/bills.py` owns that population.

---

## 1. `sage_ap_obl` cannot supply AP-direct bill headers — it is missing 80% of them

This is the thing most likely to bite you too, so it is first.

`Q["ap_obl"]` in `pull.py` is:

```sql
FROM APOBL WHERE AMTDUEHC <> 0 OR DATEINVC >= {cut}      -- CUTOVER = 20260401
```

That is an **open-items pull for the payments migration**, not an invoice register. A
Jan–Mar 2026 invoice that has since been paid has `AMTDUEHC = 0` and is before the
cutover, so it is simply absent.

Measured on this box:

| | documents |
|---|---:|
| AP-direct documents in `sage_ap_dist` | 32,047 |
| …that have any header row in `sage_ap_obl` | **6,405** |
| …**orphaned, no header at all** | **25,642** |

Restricted to the Jan–Apr window: `sage_ap_obl` holds **2,425** documents where Sage
itself holds **11,256**. Anything that joins `sage_ap_dist` to `sage_ap_obl` for a date
or an amount silently loses ~79% of the window, with no error.

**I have not changed `pull.py`.** If you want staging to be self-sufficient for AP-direct,
it needs an `APIBH` header pull (`IDTRX=12`, `SRCEAPPL='AP'`) — `APIBH` is currently
joined at line 224 only to scope `ap_dist`, and none of its columns are stored. My loader
works around this by reading headers straight from Sage.

## 2. On RCM documents the stated rate is 0.00 — "read the rate, never divide" needs a carve-out

Handoff fact 3 says read `ratetax1 + ratetax2` and never derive. That is right for
forward-charge, but on reverse-charge documents the rate is **not stated at all**:

| population | expense lines | stated rate = 0 | rate stated |
|---|---:|---:|---:|
| forward-charge | 37,270 | 5,237 | **32,033** |
| RCM document | 6,360 | **6,358** | 2 |

On an RCM bill the tax is not carried on the `4E*` line. It is booked as the
`2A7TX01/02/03` input ↔ `1L8TX14/15/16` payable pair, and **those rows carry no stated
rate either** — of ~5,000 such rows, exactly 2 have a non-zero `ratetax`.

So for RCM there is nothing to read. Taking `ratetax1+ratetax2` literally would post every
RCM bill at **0% GST** and lose the whole self-assessed tax.

Diwakar's decision (recorded): for RCM only, derive the rate from the `1L8TX` amount
against the `4E*` taxable, snap to a legal slab, and **refuse** any document further than
0.05 from one. Forward-charge still reads the stated rate and never derives. The run log
says which path each document used.

## 3. Confirming handoff fact 4 independently — per-authority rounding

I hit this before reading the handoff, from the opposite direction: comparing the *summed*
rate against the *summed* amount falsely rejected **392 otherwise-clean lines**. Sage
truncates each authority separately (1,245.42 @ 9% = 112.0878 is booked as 112.08), so the
error doubles when both authorities are summed. Validating each authority against its own
`amttax` fixes it. Your `ACCL001 / 6645/25-26` example is the same effect.

## 4. Two smaller things worth having

**GL account names must key on `ACCTFMTTD`, not `ACCTID`.** `APIBD.IDGLACCT` holds the
*formatted* code. Of 212 distinct `4E*` accounts in the AP window, `ACCTID` matches **41**;
`ACCTFMTTD` matches **212**. Keying on `ACCTID` leaves 171 products named after their own
account code (`4E2ME02-14` instead of "Power Charges, IDEPL-14").

**`masters_crosswalk.json` is stale — every id in it is dead.** The wipe took the masters
with it. Measured in the SMEAssist org: 0 live products matching those ids, 0 categories
(both `PRODUCT_CATEGORY_ID` candidates gone). The file is still good for *names* and for
vendor state/registration, but its `productId` / `contactId` / `ledger` values resolve to
nothing. Masters have to be rebuilt, not adopted.

## 5. State of the SMEAssist org as I found it (nothing of mine posted)

    9 bills live   (8 ACTIVE/VERIFIED + 1 REVOKED smoke test)
    71 products    (incl. SAGE-4E2ME07-R2, a duplicate of the healthy SAGE-4E2ME07)
    7 contacts     (6 correctly built with real GSTIN + pincode, 1 smoke test)

The 8 live bills are all booked against the duplicate `-R2` product, so their expense legs
land on a second, fragmented head. They came from an earlier run today, before the
handoff. `work/cleanup_pilot.py` on the other laptop removes them; it has not been run.

Also: there are **two counter rows per series+FY** (`SAGE` at 1194 and at 2; `SAGE27` at
330 and at 1), created by the smoke test. `GET /counter/series/values` resolves to the
duplicate. Not deletable through the API as far as I can tell.

## 6. Environment notes

`ofbl-1649` resolves and TCP 1433 answers from this devbox — confirmed. Credentials are
env-only here (`pull.py` reads `SAGE_USER`/`SAGE_PASSWORD` and neither is stored on the
box), so my loader reads Sage from the laptop side rather than putting a password here.
