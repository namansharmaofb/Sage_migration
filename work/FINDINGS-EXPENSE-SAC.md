# The expense heads are all carrying one placeholder SAC

Read-only, 2 Sep 2026 17:37, from MySQL (`smeassist.product`) while step 1 was
building masters. **Nothing here is applied.**

## What is in the org

**165 of the 290 live products carry `996719`** — 154 of them `CHARGE` products
this migration created, 11 inherited from the earlier load.

| SAC | type | products | what it is |
|---|---|---:|---|
| **996719** | CHARGE | **154** | *other supporting transport services* — the placeholder |
| 998399 | SERVICE | 26 | other professional/technical services |
| 998819 | SERVICE | 20 | other manufacturing services |
| 996719 | SERVICE | 11 | the same placeholder, from the earlier load |
| 998513 | SERVICE | 8 | staff welfare / labour supply |
| 997119 | SERVICE | 3 | financial services |

So a transport SAC is sitting on Office Expenses, Rent, Printing & Stationery,
Welfare - Staff, Welfare - Workers, Water, Power Charges, Local Conveyance,
Travelling, Vehicle Maintenance and Xerox Machine Hire.

## The org already holds the right answer for almost all of them

Sage keeps a base account and per-unit accounts (`4E2ME02`, `4E2ME02-01`,
`4E2ME02-03`, …). The earlier load created products for the **base** accounts with
real, differentiated SACs. This migration creates the **per-unit** ones and gives
every single one `996719`:

    SAGE-4E2ME02-01   Power Charges, IDEPL - 1     996719  ->  SAGE-4E2ME02  998819
    SAGE-4E2ME01-05   Water, IDEPL - 5             996719  ->  SAGE-4E2ME01  998819
    SAGE-4E1M030-01   Darning Charges - IDEPL 1    996719  ->  SAGE-4E1M030  998819
    SAGE-4E5O042-51   Office Expenses, IDEPL-5 Ph2 996719  ->  SAGE-4E5O042  998399
    SAGE-4E3EB19-14   Welfare - Staff, IDEPL-14    996719  ->  SAGE-4E3EB19  998513

**144 of the 154** have a base sibling already carrying a real SAC. Ten do not.

## The metaData defence does not hold

`post_sage_bills.py:1178` sends `"hsnIsDefault": "true"` and the comment at :1171
says the default is "flagged as such in metaData - never presented as known".

**It is not stored.** `meta` is NULL on every product this migration created:

    SAGE-4E5O042-51   meta=<NULL>  itemAttributes=[]  primaryAttributes=<NULL>
    SAGE-4E5O029-91   meta=<NULL>  itemAttributes=[]  primaryAttributes=<NULL>

    products with a non-null meta: 11   (all from the earlier load)

The create endpoint silently drops `metaData`, exactly as it silently drops
`searchKey`. So `996719` is in the org indistinguishable from a finance-approved
SAC, and the same is true of every `sageAccount`, `sageItem` and `migrationSource`
tag this migration believes it is writing. Nothing downstream can tell a migrated
row from a native one, or a placeholder SAC from a real one.

## The update path exists

`ref/SAGE-TO-SMEASSIST-HANDOVER.txt:2022,2558` records **`POST /product/update`**
used for corrections on the earlier load: **402 applied, 0 failed**, covering
"category, unit price, GST percentage". So products can be corrected in place —
which matters here, because deleting one burns its SKU permanently (below).
`hsnCode` is not in that list of corrected fields and is still untested.

## Proposed fix — not applied

Derive the SAC from the base-account sibling, which is a lookup the run already has
the data for:

1. In `ensure_products`, before create: strip the unit suffix from the account
   code, look up that product, and use its `hsnCode` when it is not `996719`.
   Covers 144 of 154.
2. The remaining 10 have no better sibling — hold them for finance rather than
   guess, the way `classify()` already holds the 6 `2A7TX0*` documents.
3. Since `metaData` is dropped, the only durable place to record "this SAC is a
   default" is `description`, or a file on our side. Worth deciding which, because
   right now the provenance is simply lost.

This is presentation-and-GST-reporting, not arithmetic: bill totals, taxable
amounts and tax are unaffected. But it is 154 products and it is the GST code the
bills report under, so it should be settled before the remaining ~11,200 AP bills
post against them.

## Do not fix products by deleting them

`work/fix_misnamed_products.py` deleted two products so `ensure_products` could
recreate them correctly. That is what burned them:

    id=1544577130968416256  SAGE-4E2ME02-14  status=ACTIVE  isDeleted=1
    id=1544577911201234944  SAGE-4E5O024-01  status=ACTIVE  isDeleted=1

`DELETE /product/delete/{id}` is a **soft** delete. The row keeps its SKU and name,
the uniqueness check still sees it, and every read path filters it out — so the SKU
can never be recreated or adopted. Both are still sitting there. That is the whole
mechanism behind the two genuine burns in [FINDINGS-BURNED-SKUS.md](FINDINGS-BURNED-SKUS.md),
and it is not a race and not a platform quirk we hit by accident.

**416 of the 706 product rows in this org are soft-deleted** — every one of them
holding a SKU and a name that can never be used again. Any future "delete and
recreate" costs another one permanently.
