# Why SKUs "burn" — root cause, 2 Sep 2026, 17:15

Investigated because `burned` went from 2 (RUNBOOK, 16:30) to 41 in half an hour.
All probes were read-only GETs plus staging/Sage SELECTs. **Nothing here is applied.**

## Verdict

**39 of the 41 are not burned.** Their SKUs are free and unused. Only the original
two GL entries (`SAGE-4E2ME02-14`, `SAGE-4E5O024-01`) are genuinely lost.

The racing theory in RUNBOOK §0 is not what caused these. Parallel runs are still
worth avoiding — they do race on the crosswalk file — but they are not the cause.

## Root cause: uniqueness is on `productName`, not `skuCode`

`POST /product/` rejects a duplicate **product name** with the message
`Resource already exists`. The code reads that as a taken SKU, calls
`find_product_by_sku`, correctly finds nothing — the SKU really is free — and
records a false `BURNED`.

| population | name already held by another live product | name free |
|---|---:|---:|
| the 39 "burned" items | **39** | 0 |
| the 63 that built fine | 1 | 62 |

39/39 vs 1/63. The discriminator is the name, and nothing else:

- **Not HSN.** All 39 burned items *have* an HSN; so do all 63 that built. The
  `EXPENSE_SAC` fallback is unrelated — the two problems are independent.
- **Not soft-deleted rows.** All 172 live products are `ACTIVE`; there are no
  hidden non-ACTIVE rows to collide with.
- **Not paging.** The catalogue is 172 rows, fully covered in 2 pages.

Examples — the colliding name and the product already holding it:

    ID40458A-ST02|NOS   "METAL DETECTION"          held by SAGE-ID40457TST02-NOS
    ID40458A-ST03|NOS   "P LIST"                   held by SAGE-ID40457TST03-NOS
    ID40591A-CT01|ROL   "H&M GUM TAPE-3''"         held by SAGE-ID40552AGT01-ROL
    ID39663Y-GT01|ROL   "GUM TAPE 3\" TRANSPARENT" held by SAGE-ID39657XGT01-ROL

The Sage item master simply reuses descriptions across distinct item codes. That is
normal data, not an error in it.

`SAGE-4E2ME07-R2` was most likely minted the same way: the pre-fix AP path saw
"already exists", assumed a taken SKU, and retried as `-R2`. A name collision
produces exactly that symptom.

## This is a 36.5% failure rate on step 5

Measured over the full goods population (staging: 32,398 lines, 16,993 items):

| | count |
|---|---:|
| distinct (item, unit) products wanted | 17,222 |
| distinct product **names** they reduce to | 10,930 |
| **products that would fail to create** | **6,292** |
| names wanted by more than one product | 1,643 |

Worst cases: one name is wanted by 85 different products, and there are ten names
wanted by 48 or more:

    #C9760 - (C) 6280-TKT-100-TEX27-10000MTS   85
    3" RED stop printed gumtape                70
    TAG BULLET 65MM                            67
    TAG BULLET-3" REGULAR                      64
    CARTON STICKER                             61

Every one of those 6,292 would be logged as `BURNED`, and every bill line needing
one of them would then skip for "no product". Run as it stands today, step 5 loses
a third of the item master and takes the goods population down with it.

## Proposed fix — one line, not applied

Make the name unique by carrying the item code, which is what already keeps the GL
pseudo-items distinct ("Power Charges, IDEPL-14"). In `_item_payload`:

    - "productName": name[:200],
    + "productName": ("%s [%s]" % (name, s(it["item"])))[:200],

Measured against the full population: **17,222 distinct names, 0 collisions.**

Two secondary fixes worth making at the same time:

1. **`post_sage_bills.py:1187`** — the AP path tests only
   `"Sku Code already exists"`, so a name collision there skips adoption entirely
   and is not even recorded. The goods path already tests both via
   `PRODUCT_EXISTS_ERRORS`; the AP path should use the same tuple.

2. **`post_sage_bills.py:2073`** — the parallel path records `burned[key]` for
   *every* failure, including `"no ledger"` and any API error. That is what makes
   the burned list unreadable as a diagnostic. Record it only on the genuine
   adoption-failed branch.

Note `burned` is not a blocklist — `todo` at :2012 keys off `ledger`, so these keys
are retried every run. Nothing is permanently excluded; the cost is wasted calls
and a misleading log.

## The two genuine burns

`post_sage_bills.py:1176` explains them: `status` is not a field on
`ProductCreateUpdateDto`, so Jackson dropped it and the row was stored with a null
status — invisible to every later lookup. Confirmed for `SAGE-4E2ME02-14`
(productId `1544577130968416256`, from the other session's probe):

    GET /product/1544577130968416256   -> 200, data: []
    present in the 172-row product list -> no

Unreadable through every read path the API offers, while create still enforces
uniqueness against it. Those two SKUs are permanently consumed. The `itemStatus`
fix means no new ones are being made.

## Secondary finding: `searchKey` does nothing

Every query parameter on `/product/products` is silently discarded:

    (no params)                      total=172   first=SAGE-4E2ME07-R2
    searchKey=SAGE-ID40458AST02-NOS  total=172   first=SAGE-4E2ME07-R2
    searchKey=NONSENSEZZZZ           total=172   first=SAGE-4E2ME07-R2
    itemStatus=INACTIVE              total=172
    includeInactive=true             total=172

Also tried and ignored: `search`, `q`, `skuCode`, `sku`, `keyword`, `name`,
`productName`, `status`, `productStatus`. (`query=` returns 0 rows — recognised but
wants some other syntax. `filter=` 500s.)

So `find_product_by_sku`'s docstring is wrong: the endpoint is not a fuzzy search
that can push a match off the page, it is an **unfiltered list**. The function
works today only because 172 rows fit inside its 10-page cap. At ~17,000 products
it will silently stop finding anything past the first 1,000 and adoption will fail
across the board. Raise the cap or page to `last`.

## `RAW_MATERIAL` — I had this wrong; RUNBOOK §4 is right

My first pass said the org owns zero catalog categories, on this evidence:

    GET /product/categories -> 200, total=0
    GET /product/category   -> 200, total=0

**Those are the wrong endpoints.** `ref/SAGE-TO-SMEASSIST-HANDOVER.txt:2021,2558`
records the earlier load creating categories through **`POST /catalogGroup`** and
**`POST /catalogCategory`** — 4 of them, "Buttons, Hanger, Other Packing
Materials", 0 failures. So categories both exist and are creatable, just not on
the routes `stock_type_for`'s docstring names. RUNBOOK §4's "the org owns 4
catalog categories" is correct and the docstring's reason is the thing that is out
of date.

Whether `RAW_MATERIAL` now takes one of those category ids is still untested — it
needs a create against a real categoryId. `RESOURCE` stays until that is tried.

---

## Addendum, 17:37 — the mechanism behind the two genuine burns

Confirmed in MySQL, not inferred. Both rows still exist:

    id=1544577130968416256  SAGE-4E2ME02-14  status=ACTIVE  isDeleted=1
    id=1544577911201234944  SAGE-4E5O024-01  status=ACTIVE  isDeleted=1

`work/fix_misnamed_products.py` deleted these two so `ensure_products` could
recreate them with the right name. `DELETE /product/delete/{id}` is a **soft**
delete: the row keeps its SKU, the create endpoint's uniqueness check still sees
it, and every read path filters it out. So the SKU is consumed permanently and
adoption can never reach it. The earlier guess in this file — "created with a null
status before the itemStatus fix" — is wrong; `status` is `ACTIVE` on both. The
delete is the cause.

**416 of the 706 product rows in this org are soft-deleted.** Each one holds a SKU
and a product name that can never be reused. Never fix a product by deleting it.

Also checked: of five sampled *false* burns, **zero** rows exist at all, deleted or
otherwise — confirming those SKUs are genuinely free and the name collision is the
only thing that stopped them.
