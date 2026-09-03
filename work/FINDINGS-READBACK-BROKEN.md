# `readback_drift` fails every bill, and it will halt step 3

Found 2 Sep 2026 17:45, while step 1 was still building contacts. Read-only.
**Nothing applied.**

## The bug

`readback_drift` (`post_sage_bills.py:1758`) reads the bill back with
`GET /bill/{id}`. That endpoint returns **`data: []`** — an empty list, with
HTTP 200 and `success: true`:

    GET /bill/1544674748683812864 -> 200  {"data": [], "success": true, ...}

`Api.data` returns `body.get("data")`, so `d` is a `list`, `isinstance(d, dict)`
is False, and the function returns `["unexpected read-back shape"]`. Every time,
for every bill. Tested on 6 posted bills: **6 of 6 fail.**

`Api.ok` passes it (200 + `success: true`), so the earlier guard does not catch it.
Alternate routes do not exist — `/bill/get/{id}`, `/bill/{id}/details`,
`/bill/details/{id}`, `/bill/view/{id}` all 404, and `/bill/bills` returns an empty
list too. The bills are unquestionably there; MySQL has all 39.

It has never actually run to completion: only the 7 AP-direct bills ever reached it,
and all 7 failed at `verify` first, which `continue`s before the read-back. That is
why `work/failures.json` does not exist and why this has stayed invisible.

## Why it stops the load

`post` records the result as `failures.append((tag, "readback: %s" % ...))`, and the
step-3 gate in `work/run_janapr.sh` does:

    drift = [r for r in rows if "readback" in r.get("why", "")]
    ...
    sys.exit(1 if drift else 0)

So step 3 posts its 50 bills, all 50 report `POST-CHECK: unexpected read-back
shape`, the gate finds 50 "readback" failures and exits 1, and `run()` calls
`die "step 3 exited 1"`. **Step 4 never starts.** The 50 bills are posted and fine;
the pipeline just stops on a check that cannot pass.

## The fix: read it back from MySQL

`mysql()` already exists at :1744 and its own docstring says "the definition of done
is verified in MySQL, not from this script's own output" — which is exactly what
this function wants. The `bill` table carries every field it needs:

    id, billNumber, taxableAmount, gstAmount, billAmount, roundOffAmount, isDeleted

Replacing the three API reads with one `mysql()` SELECT keeps every comparison and
tolerance in the function unchanged.

## Validated against Sage

Running that comparison by hand over the 18 posted bills that match a Sage header
reproduces both documented behaviours and finds the one known defect:

| | result |
|---|---|
| bills where stored totals reconcile to Sage | **17 of 18** |
| `JOBW258\|108` | stored 308,406.00 + 15,420.30, Sage gross 323,827.00 → **₹1.00 low** |
| forward bills off by exactly ₹0.01 | 4 of 18 (16,922.05 vs .06, etc.) |

The ₹1.00 is RUNBOOK §7's known `JOBW258|108`, independently confirmed. The ₹0.01s
are the documented per-authority GST truncation that `readback_drift` already
tolerates deliberately. So a MySQL-backed read-back behaves exactly as the function
was designed to — it would have caught the rupee, and it stays quiet about the paise.

One caution on interpreting `output/bills_header.csv`: `gross` is the **bill total**
for the `JOBW*` population (`header_tax` non-zero) but the **taxable amount** for the
`SELD*` population (`header_tax` = 0, tax derived from the slab). Comparing stored
`billAmount` against `gross` for the second group shows a spurious +5%. It is not
drift.

---

## Applied, 18:05 — and a second bug it uncovered

Two changes to `post_sage_bills.py` (backup:
`post_sage_bills.py.before-readback-fix-20260902-175*`):

**1. `mysql()` now reuses one ssh connection.** `ControlMaster=auto`,
`ControlPath=/tmp/.sage-cm-%r`, `ControlPersist=600`. Measured 1.09 s per call
cold, ~0.43 s multiplexed, so the per-bill read-back costs ~1.3 h over 11,232
bills rather than 3.4 h. `verify`'s queries get the same speedup. **ControlPath
must stay short** — it is a unix socket, capped near 104 chars, and a long path
fails with rc=255 and no error message at all.

**2. `readback_drift` reads from `GET /bill/detail/{billId}`**, keeping every
comparison and tolerance.

That route is documented at `ref/SAGE-TO-SMEASSIST-HANDOVER.txt:2027` — it exists
and works, which I missed on the first pass; I went to MySQL instead. It truncates
`lineItemDtoList` to one line, which does not matter here because this function
compares totals (`/bill/lineItems/{billId}` serves lines). Verified to return the
same taxable/gst/bill as the MySQL row, and re-validated against all 18 posted
bills with an identical result.

It is the better of the two: it is the documented path, needs no ssh, and costs
what the original create+verify+readback model already budgeted, whereas a MySQL
read adds ~0.43 s per bill (~1.3 h over 11,232) on top. The `mysql()` multiplexing
in change 1 is kept anyway — `verify` issues many queries and benefits from it.

### I relaxed the taxable assertion, then reverted it — the premise was wrong

First pass: stored taxable was a whole rupee where we sent pence, so I concluded
the platform rounds it and widened the check to accept a HALF_UP rupee rounding.
**That was wrong.** The platform stores exactly `SUM(line taxableAmount)` and does
not round: of 39 posted bills, **9 store pence, every one matching its lines to the
paisa**, and each of those has `roundOffAmount = 0.00`.

What actually produced the whole rupees: Sage books its round-off as a **4E expense
line** on `4E1M016` "Round Off Value on Purchases", and `taxable_all` counts it.
The bills posted *before* the round-off fix never sent it as a line, so their stored
taxable is short by exactly that amount:

    JOBW258|105   Sage 4E2ME07 338441.00 + 4E1M016 -0.06 -> taxable_all 338440.94
                  posted lines 176896.00 + 161545.00     -> stored       338441.00
    JOBW258|108   Sage 4E2ME07 308406.00 + 4E1M016 +0.70 -> taxable_all 308406.70
                  stored 308406.00, and the missing 0.70 is the rupee

`classify` now emits the round-off as a line (`post_sage_bills.py:808-819`), so for
newly posted bills stored taxable equals `taxable_all` exactly. The assertion is
back to strict, and it correctly reports the 9 legacy bills.

**Untested:** *zero* of the 39 posted bills carry a `4E1M016` line, so the
round-off-as-a-line path has never actually run. 655 of the 32,045 documents in the
window carry one (~2%, so ~1 in any batch of 50). Whether the server adds its own
`roundOffAmount` on top of the line, and whether it accepts the 188 negative ones,
is unknown until a batch posts.

### The RCM branch omitted roundOffAmount — fixed

The RCM branch asserted `billAmount == taxable + gst` within 0.01. The server's own
identity is `billAmount = taxableAmount + gstAmount + roundOffAmount`, and it
substitutes its own round-off figure. On the 39 posted bills, **10 carry a non-zero
roundOffAmount and all 10 break the identity** by up to 0.40; **all 39 satisfy it
once roundOff is included.** The branch now reads `roundOffAmount` back and includes
it, so it is correct by construction rather than passing by accident.

### Other edge cases checked

- **Unknown or malformed bill id** — handled cleanly: `"Bill with id X not found"`
  in 0.14 s, reported as a read-back failure. No retry storm.
- **Bills storing pence** (9 of 39) — pass, correctly.
- **Rounding mode at .50** — moot after the revert; no rounding is involved.

### Result

    checked=17  clean=8  flagged=9

The 9 are exactly the pre-round-off-fix AP bills carrying a Sage `4E1M016` line, and
`JOBW258|108` additionally fails on billAmount. Correct legacy findings, not false
positives. Newly posted bills should be clean.
