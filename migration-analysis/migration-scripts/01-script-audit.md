# 01 — Migration Script Audit

**Scope**: every migration-related script in `/home/namansharma/Desktop/sage-pull` — `post_sage_bills.py`
(3,943 lines, per phase), `extract.sql`, `run_all.sh`, and the `work/*.py` probes, repairs and
reconciliations. **Read-only audit. No migration script was executed.**

**Evidence labels** — `SCRIPT VERIFIED` (read in the source), `DATABASE VERIFIED` (queried live Sage
`IDEDAT` and/or `smeassist`/`idedat_staging` MySQL), `BACKEND VERIFIED` (read the Spring source),
`DOCUMENTED` (the project's own notes assert it), `INFERRED`.
**Confidence** — VERIFIED / HIGH / MEDIUM / LOW / UNKNOWN.

GSTINs are masked to state code + 6th character (`29XXXCX0000X0ZX`).

Target org: `SME_ORG_ID = 1029113552088076445`. Window: `DATE_FROM, DATE_TO = 20260101, 20260430`
(`post_sage_bills.py:82`).

---

## A. What the loader actually does, end to end

`SCRIPT VERIFIED / VERIFIED`

There are **two entirely separate populations** with separate readers, classifiers and phases, sharing
one payload builder, one crosswalk and one `posted.log`.

### A.1 AP-direct (`masters` → `dryrun` → `post`)

| step | code | source |
|---|---|---|
| headers | `load_headers` `:950`, `SQL_HEADERS` `:333` | **live Sage `APOBL`**; falls back to `Bills_JanApr_Header.psv` **on the devbox over ssh** (`HEADER_PSV` `:947`) |
| distribution lines | `load_book` `:1159`, `SQL_STAGING_LINES` `:369` | **`idedat_staging.sage_ap_dist` over ssh+mysql** — not Sage, not any CSV. Only this source carries `RATETAX1/2` |
| PO item detail | `load_po_items` `:1263`, `SQL_PO_ITEMS` `:1233` | **live Sage only**, else `work/po_items_cache.json`, else nothing |
| vendor master | `vendors_master` `:1003`, `SQL_VENDORS` `:376` | live Sage `APVEN`, else `output/vendors.csv` |
| GL groups / names | `gl_groups` `:1048`, `gl_names` `:1122`, `SQL_GL` `:405` | live Sage `GLAMF`, else `output/gl_accounts.csv` |
| item HSN | `item_hsn_map` `:3192`, `SQL_ITEM_HSN` `:229` | live Sage `ICITEMO` only |

Documents are keyed `(vendor, base_invoice(IDINVC))` and multi-part `*N` receipts consolidate
(`load_book:1194-1202`). `classify` `:1390` splits the distribution into expense (`4E*` minus
`4E1M016`), round-off (`4E1M016`), RCM payable (`1L8TX14/15/16`) and recoverable input (`2A7T*`),
derives the rate, and refuses anything it cannot tie to `AMTINVCHC` to the paise. `build_payload`
`:2493` emits one `billLineItem` per distribution line (or per PO item where `po_detail` `:1313`
reconciles exactly), plus zero-rated lines for the round-off and the import-IGST pass-through.
`assert_invariants` `:2718` checks the payload against itself, then `POST /bill/` →
`POST /bill/{id}/verify` → `readback_drift` `:2893`.

### A.2 Goods / PO-matched (`goods-masters` → `goods-dryrun` → `goods-post`)

Everything comes from `idedat_staging` (`sage_bill_hdr`, `sage_goods_line`, `sage_item`,
`sage_service_line`, `sage_ap_dist`, `sage_vendor`, `sage_gl_acct`) — `load_goods_book` `:1661`.
No Sage read at all except `item_hsn_map`. `classify_goods` `:1720` ties `Σ ext + Σ svc + tax` to
`doc_total`, refuses a line whose `qty × unit_cost ≠ extended`, and slab-validates every stated rate.
Products are one per `(Sage item, platform unit)` (`item_key` `:3178`), created in parallel
(`ensure_item_products_parallel` `:3303`).

### A.3 State

`work/crosswalk_live.json` (`products` 197 GL pseudo-items, `items` 17,220, `contacts` 455,
`burned` 75) plus the append-only `work/posted.log` (9,378 lines, all tags unique).
`idedat_staging.crosswalk` exists and is **empty** — `DATABASE VERIFIED`.

---

## B. `post_sage_bills.py` — per-phase audit

### B.0 Cross-phase constants (all magic values)

`SCRIPT VERIFIED / VERIFIED`

| value | line | note |
|---|---|---|
| `DATE_FROM/DATE_TO = 20260101/20260430` | `:82` | duplicated verbatim in `work/unit_consistency.py:6,17`, `work/apibh_probe.py:12`, `work/apibh_scope.py:11` |
| `SERIES_BY_FY = {"2025-2026":"SAGE", "2026-2027":"SAGE27"}` | `:86` | **`"SAGE27"` is a prefix-extension of `"SAGE"` — see §E.6** |
| `COMPANY_ADDRESS_ID = "1029168384857610867"` | `:88` | org-specific, hardcoded in a tracked file |
| `CONTACT_CATEGORY = "1029658153383367321"` | `:92` | org-specific, hardcoded |
| `ROUNDOFF_GL = "4E1M016"` | `:93` | |
| `RCM_GL = {1L8TX14:SGST, 1L8TX15:CGST, 1L8TX16:IGST}` | `:94` | |
| `PASSTHRU_GL = {"2A7TX04": ("1541311685238407168", "IGST Input (Import) @ 0.00%")}` | `:116` | one entry only; `2A7TX01/02/03` deliberately unmapped → those documents are refused |
| `BILLTYPE_BY_GROUP` (5 groups) | `:133` | `4E1M` deliberately absent |
| `PURCHASE_ACCOUNTS = {4E1M001,002,003}`, `DIRECT_ACCOUNTS` (5 codes) | `:151`, `:152` | |
| `SETTLED_ACCOUNTS = {4E1M016, 2A7TX04 → DIRECT_EXPENSE}` | `:213` | |
| `GOODS_HSN_DEFAULT = "9999"` | `:240` | **1,419 products carry it — `DATABASE VERIFIED`** |
| `EXPENSE_SAC = "996719"` | `:242` | "DEFAULT awaiting finance sign-off" |
| `PLACEHOLDER_MOBILE = "9999999999"` | `:243` | |
| `MAX_RETRY, THROTTLE = 8, 0.4`; `API_RATE = 4.5` | `:247`, `:258` | |
| `LEGAL_SLABS = {0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28}` | `:294` | includes 6 and 7.5, which are *half*-slabs for 12% and 15%; see §E.1 |
| `GST_STATE_CODES` (38), `STATE_ALIASES` (10), `PIN2_STATES` (~60), `CITY_STATE` (15) | `:424`–`:503` | |
| `STATE_HEAD_PINCODE` (36 head-post-office pincodes) | `:515` | used as a placeholder when Sage has none |
| `COUNTRY_ENUM` (12), `FOREIGN_PIN_DEFAULT = "999077"` | `:725`, `:744` | |
| `UNIT_MAP` (26 entries) → `"OTH"` fallback | `:1575` | |
| `RAW_MATERIAL_CATEGORIES` (14 Sage ICCATG codes) | `:1600` | |
| `INDIRECT_ITEM_MAPPING = "ITEM_IN_DIRECT_EXPENSE"`, `ITEM_LEDGER_MAPPING = "ITEM_PURCHASE"` | `:202`, `:3122` | |
| `COMPANY_ADDR` — full street address, city, state, pincode | `:2468` | contradicts `README.md:31-35`, which claims the org's identity was removed from tracked files |

---

### B.1 PHASE `cleanup`

- **PURPOSE** Remove smoke-test artefacts and report duplicate series counters. `phase_cleanup:2824`.
- **SOURCE** `smeassist` MySQL over ssh (`mysql()` `:2871`) + the SMEAssist REST API.
- **TARGET** `PUT /bill/updateStatus/{id}/REVOKED`, `DELETE /bill/{id}`, `DELETE /contact/{id}`.
- **SOURCE TABLES / FIELDS** `bill(id, billNumber, billStatus)`, `contact(id, accountName)`,
  `counter(id, series, value, associatedFinancialYear)`, all `organisationId=ORG AND isDeleted+0=0`.
- **FILTERS** Bills: only `billNumber` starting `SMOKE` are touched (`:2834`). **Contacts: every
  contact in the org that is not in the crosswalk is DELETED** (`:2843-2852`) — no name filter, no
  confirmation, no `--apply`.
- **HARDCODED** the literal `"SMOKE"` prefix; `remarks=migration+cleanup`.
- **DUPLICATE HANDLING** Duplicate `counter` rows are detected and **warned about only** (`:2862-2870`)
  — "they are not deletable through the API". This is the mechanism behind the duplicated series
  numbers in §E.6.
- **TRANSACTION HANDLING** None. Each revoke/delete is independent; a mid-run stop leaves a partial
  cleanup with no record.
- **POTENTIAL BUGS** The contact sweep is unbounded relative to the crosswalk. If `crosswalk_live.json`
  is missing or truncated, `state.xw["contacts"]` is empty and the guard at `:2845-2848` matches
  nothing — the code then tries to delete **every contact in the org** (537, of which 353 carry bills).
  **It would fail.** `BACKEND VERIFIED / VERIFIED`: `ContactController.java` (520 lines) contains **no
  `@DeleteMapping` at all**, so `DELETE /contact/{id}` is not a route. The sweep is inert and always
  has been. `DELETE /bill/{id}` likewise always fails after a revoke —
  `BillServiceImpl.java:1647-1649` refuses to delete anything not in `APPROVAL_REJECTED`. So `cleanup`
  revokes SMOKE bills and otherwise does nothing it believes it is doing, while reporting success.
- **VALIDATION MISSING** no dry-run, no count confirmation, no crosswalk-non-empty assertion.

---

### B.2 PHASE `masters`

- **PURPOSE** Create the GL pseudo-item products and the vendor contacts the selected bills need.
  `main():3893-3925` → `ensure_products:1968` + `ensure_contacts:2172`.
- **SOURCE** `load_book()` for the selection; Sage `GLAMF` (or `output/gl_accounts.csv`) for names and
  groups; Sage `APVEN` (or `output/vendors.csv`) for vendors; `ref/cin_by_gstin.json` for CIN/LLPIN.
- **TARGET** `POST /product/`, `PATCH /product/{id}/ACTIVE`,
  `POST /financeAccountReferenceMapping/item/getOrCreate/{mapping}?referenceIds={pid}`,
  `POST /contact/`, `POST /contact/address/create`, `POST /taxation`,
  `GET /financeAccount/minDetails/VENDOR/{cid}`, `DELETE /contact/{cid}` (rollback),
  `GET /address/pincode/{pin}`, `GET /contact/search`.
- **TRANSFORMATIONS**
  - product: `sku = "SAGE-" + acct`, `name = GLAMF.ACCTDESC`, `unit/unitOfMeasurement = "OTH"`,
    `typeOfStock = "CHARGE"`, `hsnCode = EXPENSE_SAC (996719)`, `itemStatus = "ACTIVE"`.
  - contact: `registration_of` `:570` → GST / INTERNATIONAL / PAN / WITHOUT_PAN_OR_GST;
    `resolve_state` `:590` → state enum; `normalise_pincode` `:705` or
    `normalise_foreign_pincode` `:760`; `GSTIN_ENTITY_TO_PROFILE[reg_no[5]]` (or `[3]` for a bare PAN).
- **FILTERS / SILENT EXCLUSIONS** Masters are built only for the *shaped* selection (`:3897-3921`),
  so a document `classify()` refuses never gets its vendor or GL account built — and therefore stays
  unpostable even if the refusal is later fixed, until `masters` is re-run.
- **LOOKUPS** `pincode_state` `:2141` asks `GET /address/pincode/{pin}` and holds the vendor on
  disagreement. **Fail-open**: any non-2xx (including a throttled 403) yields `None` and the guard is
  skipped silently (`:2141-2152`).
- **ID GENERATION** `productId`, `contactId`, `addressId`, `financeAccountId` all come from the server
  and are stored in the crosswalk. `sku = "SAGE-" + acct` is the only client-generated identity.
- **ERROR HANDLING** `LookupFailed` `:302` distinguishes a throttled read from an absent record —
  the fix for the burned-SKU epidemic. `Api.call:834` retries 403/rate-limit and content-free 5xx,
  never a 5xx that carries an `errorMessage`.
- **MISSING-DATA HANDLING** Deliberate and good: a state it cannot prove, a malformed GSTIN, a
  missing CIN, an unmapped country — all held to `work/contacts_held.json`, never guessed.
- **DUPLICATE HANDLING** Adopt-before-create by SKU (`:2039-2059`); on `PRODUCT_EXISTS_ERRORS` with
  no adoptable row the account is recorded **BURNED** and never retried as `-R2` (`:2100-2106`).
  Vendors sharing one GSTIN are mapped onto the existing contact (`:2404-2434`).
- **TRANSACTION HANDLING** Per-object, with one genuine rollback: a contact whose address create is
  **definitely** refused is `DELETE`d (`:2400-2418`, uncommitted). A transient failure keeps the
  contact and says so. There is no rollback for a contact created but whose `taxation` or party
  ledger fails — those hold the vendor while leaving the contact live.
- **ORDERING** products → contacts. `phase_goods_masters:3520` deliberately does contacts **first**
  and recomputes eligibility, because `eligible_goods` filters on contacts.
- **POTENTIAL BUGS**
  - `api.post("/taxation", …)` `:2431` — **return value discarded**. A failed taxation row records the
    contact as complete and surfaces much later from the BILL module.
  - `api.call("PATCH", "/product/%s/ACTIVE" % pid)` `:2115`, `:3374`, `:3508` — return discarded.
  - The goods path feeds `ensure_contacts` from `SQL_STG_VENDORS:3101`, which selects **only
    `street1`** and no email — while `resolve_state`'s city-corroboration loop reads
    `("city","street4","street3","street2","street1")` `:637`. The goods path therefore resolves
    strictly fewer states than the AP path, from a mirror that is missing 469 of the window's vendors.
    Whichever phase runs first writes the contact and the other silently reuses it (`:2183-2184`).
- **VALIDATION PRESENT** state must be provable; `pincode_state` cross-check; CIN never invented;
  bill type must be derivable before a ledger is minted (`:1988-1997`).
- **VALIDATION MISSING** no verification that the taxation row exists; no re-validation of a contact
  already in the crosswalk (a contact created by an earlier, buggier run is never revisited).

---

### B.3 PHASE `dryrun`

`phase_run(..., do_post=False)` `:2972`. Builds every payload and posts nothing
(`Api.call:836` short-circuits every non-GET). **It still performs GETs** — `series_number:2478`
issues `GET /counter/series/values` per bill and `ensure_*` are not called. Writes
`work/skipped.json` (grouped counts only, not per document — see §G, regression R1).

- **VALIDATION PRESENT** full `classify` + `build_payload` + `assert_invariants` path.
- **VALIDATION MISSING** it does not exercise `readback_drift`, and it cannot detect the
  `KeyError` in §D-BUG-3 because that fires inside `build_payload` and would abort the dry run too.

---

### B.4 PHASE `post`

- **PURPOSE** Create + verify + read back, resumably. `phase_run:2972`.
- **SOURCE** as §A.1. **TARGET** `POST /bill/`, `POST /bill/{id}/verify`, `GET /bill/detail/{id}`.
- **TARGET FIELDS** (`build_payload:2662-2706`) `organisationId, billType, billNumber,
  billSeriesNumber{series,value,suffix,associatedEntityType,associatedEntityId,associatedFinancialYear},
  billDate, dueDate, voucherDate, billStatus, billAmount, taxableAmount, gstAmount, contactDto,
  contactType, contactFinanceAccountId, gstUsedForBill, companyBillingAddressDto,
  contactBillingAddressDto, purchaseType, originCountry, destinationCountry, billProcurementType,
  billEntityMappingDtos, lineItemDtoList, conversionRate, currencyDto, autoMapRemainingVoucher,
  roundOffAmount(0.0), hasRoundOff(false), expenditureDtoList, metadata`.
  Per line: `productId, skuCode, productName, hsn, unit, displayUnit, quantity, displayQuantity,
  unitPrice, itemPrice, taxableAmount, totalPrice, gstPercentage, cessType, cessAmount,
  cessPercentage, lineItemType, isGstClaimable, isRcmEnabled, discount, itemDiscount,
  taxableOtherCharge, description, financeAccountDto, metaData`.
- **FILTERS** `eligible:2757` — must shape, must have a contact **with an addressId**, must not be a
  URP vendor carrying forward-charge GST, must have a product for every `exp` GL account.
- **ID GENERATION** `billSeriesNumber.value = GET counter value + 1` (`:2478-2491`).
- **ERROR HANDLING** `"Bill number already exists"` → `state.mark(tag, "preexisting")` and **move on
  without verifying** (`:3053-3057`) — see BUG-1 in file 02. Verify failure → `tag||{bid}||UNVERIFIED`,
  which a later run re-verifies (`:3000-3013`). Readback drift → recorded as a failure but the bill
  is still marked posted (`:3081-3086`).
- **DUPLICATE HANDLING** `state.posted` skip on the tag; the server's own bill-number uniqueness is
  the second line of defence. `DATABASE VERIFIED`: 0 duplicate `(contactId, billNumber)` pairs in any
  status; `posted.log` has 0 duplicate tags.
- **TRANSACTION HANDLING** **A bill can survive half-posted.** `POST /bill/` succeeds → process dies
  before `state.mark` → the bill exists, unlogged, and the next run re-attempts it (protected only by
  the server's uniqueness check, which has **no DB unique index** behind it — `DATABASE VERIFIED`,
  `SHOW INDEX FROM bill` carries only PRIMARY and org indexes). Create succeeds → verify fails →
  the bill is ACTIVE with **zero voucher entries**, i.e. a payable with no accounting impact.
- **ORDERING** must follow `masters`. `run_all.sh:154-157` enforces `masters → post → goods-masters →
  goods-post`.
- **DATA LOSS / FINANCIAL RISKS** see file 02.

---

### B.5 PHASE `goods-masters`

`phase_goods_masters:3520`. Contacts first, then eligibility recomputed, then item products.

- **SOURCE** `idedat_staging` exclusively (`SQL_GOODS_HDR:1605`, `SQL_GOODS_LINES:1612`,
  `SQL_GOODS_GL:1622`, `SQL_GOODS_SERVICE:1628`, `SQL_GOODS_SERVICE_LINES:1637`,
  `SQL_STG_VENDORS:3101`, `SQL_STG_GL:3111`) **plus live Sage `ICITEMO`** for item HSN.
- **TRANSFORMATIONS** `item_key = "{item}|{platform_unit(um or stock_um)}"` `:3178`;
  `sku = "SAGE-{item_raw}-{unit}"` `:3339`; `item_product_name` `:3237` appends `" [item|unit]"` to a
  200-char-truncated description to defeat the name-uniqueness constraint;
  `stock_type_for` `:3147` → `(typeOfStock, categoryId)` from `ref/item_categories.json`, else
  `("RESOURCE", None)`; `resolve_item_hsn` `:3211` → line → sibling → `ICITEMO` → `9999`.
- **FILTERS** `--all-categories` / `--all-items`; without `--all-items` the scope is
  `shaped[:max(limit or 10,1)*8]` `:3543` — an arbitrary slice.
- **HARDCODED** `workers=6` default; flush every 50 products `:3404`; `unitPrice` from the **latest**
  bill date only under `--all-items` `:3591`, otherwise `setdefault` (arbitrary first-seen price)
  `:3596-3598`.
- **POTENTIAL BUGS**
  - `ensure_products` is called with `{gl for exp lines}` only `:3546` — **`ROUNDOFF_GL` is never
    included**, so the goods path never creates `SAGE-4E1M016`. See BUG-3.
  - The serial `ensure_item_products:3414` calls `resolve_item_hsn(it)` **without `by_item`**
    (`:3489`), losing the sibling tier, and its `metaData` (`:3495-3499`) **omits `hsnSource` and
    `hsnIsDefault`** — so a `9999` product created by the serial path is unfindable by the flag the
    parallel path stamps. See BUG-7.
  - `ensure_item_products_parallel:3303` relies on `locals().get("deferred")` `:3387` to distinguish a
    throttled lookup from a genuine burn. It works, but only because `deferred` is bound in exactly
    one branch; any future edit silently reintroduces false burns.
- **TRANSACTION HANDLING** crosswalk flushed every 50 (`:3404`) and once at the end. A crash loses up
  to 49 mappings; those products are then re-created, collide, and are adopted — recoverable.

---

### B.6 PHASE `goods-dryrun` / `goods-post`

`phase_goods:3626`.

- **FILTERS** `eligible_goods:3611` checks **only** that the vendor has a contact. `phase_goods` then
  checks products (`:3662`) and item **ledgers** (`:3670-3675`).
- **MISSING CASES relative to the AP path** — all `SCRIPT VERIFIED / VERIFIED`:
  1. **No URP guard.** `eligible:2773-2780` holds a `WITHOUT_PAN_OR_GST` vendor carrying
     forward-charge GST; `eligible_goods` has no such test. `DATABASE VERIFIED`: **18** goods documents
     (₹1,550,109.78, carrying ₹73,814.78 of GST) come from vendors the crosswalk records as
     `WITHOUT_PAN_OR_GST`. **0 posted so far** — the next `goods-post` claims that input credit against
     a URP ledger.
  2. **No `readback_drift`.** `phase_goods:3720-3722` goes from `VERIFIED` straight to `state.mark`.
     The check that exists *because the server overrides `gstAmount` and `roundOffAmount`* is applied
     to one of the two posting paths only.
  3. **No RCM detection.** `classify_goods:1825` hardcodes `"is_rcm": False`; `:1733` merely *filters
     out* `1L8TX*` heads rather than reading them. `DATABASE VERIFIED`: **0** goods documents in this
     window carry a `1L8TX` distribution, so there is no exposure today — a latent hole, not a live one.
  4. **No currency filter and no FX.** See §E.7 — this is the single most serious finding in the audit.
- **DUPLICATE HANDLING** same as `post`.

---

### B.7 PHASE `verify`

`phase_verify:3778` runs the seven `DOD` queries `:3736` against `smeassist` MySQL. Read-only.

- **VALIDATION PRESENT** billStatus/verification spread; distinct `gstPercentage` (catches a
  re-derived rate); ledger heads minted at a fake rate; NULL `financeAccountId`; `TEXTDESC` loss
  joined through `metaData`; voucher balance; `voucherNumber == billNumber`.
- **POTENTIAL BUGS** the 4.2 query `:3754-3763` joins
  `d.inv_number_raw = metaData.sageDoc`, but `sageDoc` holds the **base** invoice after the `*N` strip
  (`:2590`, `:2698`). Multi-part documents never match and are silently excluded from the count.
- **VALIDATION MISSING** `run_all.sh:154-163` **never invokes this phase**. The only automated check
  that defects 4.1 / 4.2 / 4.6 have not returned runs solely if a human types it.

---

### B.8 PHASE `legs`

`phase_legs:3792`. Diagnostic side-by-side of Sage's `APIBD` distribution against SMEAssist's
`voucherEntry` legs.

- **POTENTIAL BUG** `:3800-3801`: `rec = state.posted.get(tag, "")` returns the **whole log line**
  (`"VEND|INV||1544…"`), then `bid = rec.split("||")[0] if rec and rec[0].isdigit() else None`.
  `rec[0]` is the first character of the vendor code, never a digit, so **`bid` is always `None`** and
  the SMEAssist half of the comparison never prints. `SCRIPT VERIFIED / VERIFIED`. Diagnostic only.

---

## C. `extract.sql`

`DATABASE VERIFIED / VERIFIED` (populations re-run against live Sage today; zero drift).

- **PURPOSE** Seven `@@name` blocks → `output/<name>.csv`: `bills_header` (11,256), `bills_lines`
  (45,179), `notes_header` (200), `notes_lines` (337), `vendors` (669), `gl_accounts` (1,029),
  `control_counts` (1).
- **"Every statement is a SELECT"** — **TRUE**. The only non-SELECT statements are 7 × `SET NOCOUNT ON`.
- **PROVENANCE — the file did not produce those CSVs.** `extract.sql` was written 2026-09-03 13:10,
  **27 hours after** `output/*.csv` (2026-09-02 10:15). `work/patch_pull.py:14,40,75-81` shows the real
  `pull.py` lives at `/root/indiandesign/converter/pull.py`, holds its queries as Python
  `(table, cols, sql)` tuples, has no `@@name` concept, and loads **MySQL staging tables**, not CSVs —
  its 14 pull names are disjoint from the seven block names. `pull.py` was never tracked in git.
  The CSVs are an independent, *higher-fidelity* Sage re-read: 796 descriptions contain commas that the
  27-Aug Windows/`sqlcmd` PSV extract had irreversibly replaced with spaces. `extract.sql:4-7` is
  therefore false on every clause, and `README.md:37` understates it.
- **KEY DEFECTS**
  1. `extract.sql:34` states the validation target as **46,385 line rows**. That number is the `wc -l`
     of the corrupt PSV, inflated by 1,206 newline continuation fragments. The truth is **45,179** —
     and `MACHINE-CHANGES.md:190-196` already says so, in the same tree.
  2. `gl_accounts` filters `LEFT(RTRIM(ACCTID),2)='4E'` (`:275`) while the loader filters
     `ACCTFMTTD` **and** `2A7T` (`SQL_GL:405-410`). The CSV is missing all 10 `2A7T` accounts, so the
     Sage-down fallback cannot resolve **4,479 `bills_lines` rows across 7 accounts**.
  3. `bills_lines` orders by `CNTLINE` but never emits it — **6,528 rows across 2,645 groups are
     fully indistinguishable**. Anything that dedupes the file destroys real money.
  4. `notes_lines:212` adds a `4E`-only filter `notes_header` does not: ₹37,918 of `2A7T` lines are
     dropped and the two files miss reconciliation by **₹40,422.03**.
  5. `RTRIM` runs *before* the CR/LF→space `REPLACE` on four blocks (`:115`, `:208`, `:239-245`,
     `:270`), leaving trailing whitespace on **1,317 window rows**.
  6. `vendors` omits `TEXTPHON1`/`EMAIL1`, which `SQL_VENDORS:390` selects — the CSV fallback forfeits
     242 mobiles and 179 emails and stamps `PLACEHOLDER_MOBILE` instead.
  7. Inline exclusion counts have drifted: 1,071→**1,079**, 39→**43**, 114→**126**;
     "154 documents carry leading/trailing whitespace" → **1**.
- **STALENESS** none. Every population matches live Sage today to the row and to the paise, including
  `control_counts` (11,256 / ₹719,631,371.43 / ₹49,763,350.83 / 412 vendors).
- **Note** `post_sage_bills.py` never reads `output/bills_header.csv` or `output/bills_lines.csv` —
  lines come from MySQL staging because only staging carries `RATETAX`. The two largest files in
  `output/` are, for the loader, dead weight.

---

## D. `work/*.py` — audit summary

Full per-script blocks were produced for 30 scripts; the load-bearing findings:

### D.1 Scripts that write to live SMEAssist

| script | writes | idempotent | risk |
|---|---|---|---|
| `work/cleanup_pilot.py:14-21` | `PUT …/REVOKED` + `DELETE /bill/{id}` for **every ACTIVE bill in the org** | terminal | **CATASTROPHIC** — written for 8 pilot bills; the org now holds 9,376. No `--apply`, no dry run |
| `work/repost_stranded_parts.py:179,190` | revoke + delete goods bills that posted short | yes | gated by a full rebuild + `assert_invariants` + Sage-total match — the strongest gate in the tree |
| `work/repost_po_items.py:95-99` | revoke + delete 16 named AP bills | yes | **`DELETE` runs even when the REVOKE failed** — the fix present in its sibling was never back-ported |
| `work/fix_qty_unitprice.py:75,80` | `PUT /bill/{id}` (full payload) + verify | content-idempotent | re-derives `billSeriesNumber` on every PUT without ever advancing the counter |
| `work/fix_misnamed_products.py:22,30` | 2 product deletes + crosswalk overwrite | no | crosswalk keys dropped **regardless of delete success**; non-atomic 4.1 MB write |
| `work/build_item_categories.py:230` | `POST /catalogCategory` | by name | — |
| `work/make_passthru_ledgers.py:88` | `POST /financeAccount/` | by name | ids are only **printed**; a human must paste them into `PASSTHRU_GL:116` |
| `work/cleanup_probes.py:71,75` | 14 mapping deletes + 14 ledger DISABLEs | yes | `KEEP` assertion guards `LEDGERS` but not `MAPPINGS` |
| `work/revive_probe.py:6,14` | 4 mutation attempts against a **real production product id**, no dry run | no | loosest write in the tree after `cleanup_pilot.py` |
| `work/probe_stock_types_all.py` | creates up to 18 products | no | **dies at `:80` (`item_ledger_for` missing its now-required `mapping`) before cleanup — leaks 9 live products** |
| `work/try_one_contact.py:25` | full contact create path | per vendor | **truncates `work/contacts_held.json`** to just the vendors passed in |
| `work/find_sage.py:230-232` | rewrites `.env` | yes | **non-atomic** — a crash between truncate and write loses the SQL password and the auth token |
| `work/patch_pull.py:84,128` | rewrites devbox `pull.py`, appends DDL containing `DROP TABLE IF EXISTS sage_item_master/sage_item_category` | `pull.py` yes, **SQL append no** | mutates infrastructure outside this tree |

### D.2 The reconciliation scripts disagree with each other

`DATABASE VERIFIED / VERIFIED`

- `work/reconcile.py` reads **`idedat_staging.sage_ap_obl`** with *no* currency filter, *no* tax-group
  filter and *no* distribution-head filter, and reports `sage_ap_direct: 12781`.
  `work/value_recon.py` reads `load_headers()` with the full purity filter and reports **11,256**.
  The 1,525-document gap is exactly the exclusion funnel in §E.7. `reconcile.py` is the one wired into
  `run_all.sh:27,161` and `--check`.
- `reconcile.py:110` compares amounts **exactly**, with no tolerance for Sage's per-authority tax
  truncation → 375 "mismatches" of which ~362 are noise, all flagged `<-- INVESTIGATE`.
- `reconcile.py:113` `elif txb == sg: rcmok += 1` counts **any** bill whose taxable equals Sage's gross
  as "RCM ok" — including a forward-charge bill that posted with its GST dropped entirely. 901
  documents land in that bucket unexamined.
- `reconcile.py:99-102` builds the SMEAssist side only from `posted.log`, so
  `in_smeassist_not_in_sage` is structurally 0 while `value_recon` finds 5.
- A `tag||preexisting` line is counted as **posted** by `failure_report.py:55`, as **not posted** by
  `value_recon.py:326` and `reconcile.py:87`, and as **both** by `reconcile.py:123`.
- `master_recon.py:587` — the `ledger_head` check, the reason the file exists, can only run on
  records carrying `itemMapping`: **197 pseudo-items do, 17,220 item products do not**. 98.9% of the
  chart of accounts this migration minted is never head-checked.
- **No script anywhere compares Sage's stated goods-line GST rate to the posted line rate.**
  `value_recon.py:454` keys on `metaData.sageLine ∈ d["rates"]`, but goods lines are stamped
  `"PO:<item>"` / `"SVC:…"` (`post_sage_bills.py:2541`), which never matches a `cntline`.
  14,603 goods documents have no per-line tax-rate reconciliation.

### D.3 Dead code

`work/po_validate.py` and `work/po_holds.py` call `m.load_po_book()`, `m.classify_po()`,
`m.build_po_payload()`, `m.po_pilot_pick()` — **none of which exist** in `post_sage_bills.py` any more.
Both `AttributeError` on first call.

---

## E. Independent validation of the transformations that carry money

Every subsection below was checked **in the code and against the data**. Nothing here is taken on the
script's word.

### E.1 Tax / GST rate — defect 4.1

**Claim** (`README.md:88-90`, `post_sage_bills.py:293`): "Read the tax rate Sage states; never divide
to infer one", validated against the legal slabs.

**Code**: `line_rate:1347` sums `RATETAX1` + `RATETAX2`, rounding each authority to 2 dp first, and
validates each authority against **its own** `AMTTAX` to ±0.01. `classify:1443-1461` refuses any rate
not in `LEGAL_SLABS` and refuses the document if the summed stated tax disagrees with `AMTTAXHC`
beyond `max(0.05, 0.01 × nlines × 2)`. `build_payload:2570` sends that rate verbatim.

**DATABASE VERIFIED / VERIFIED** — joined every posted bill line to `idedat_staging.sage_ap_dist`
through `metaData.sageVendor/sageDoc/sageLine`:

```
lines joined 28,507 | rate differs from Sage's stated RATETAX1+RATETAX2 5,185 | taxable differs 0
```

Every one of the 5,185 is a **reverse-charge** line where Sage states `0.00` and the loader derived
and snapped (5,131 at 5%, 54 at 18%). **On forward charge the stored rate equals Sage's stated rate on
all 23,322 lines, exactly.** Defect 4.1 **HOLDS**.

`DATABASE VERIFIED`: rate distribution on ACTIVE bills is `18.00` (20,231 lines), `5.00` (6,930),
`0.00` (2,441) — nothing else. 767 off-slab lines exist (`5.01`, `5.02`, `4.99`, `4.58`, `13.42`, a tail
of `4.76`–`5.45`, ₹2,467,582.59) and **every one sits on a REVOKED or deleted bill** — the signature of
the old divide-to-infer defect, fully quarantined.

**Where snapping changes the value.** Only reverse charge. `snap_to_slab:1381` picks the nearest legal
slab to `tax/taxable×100` and refuses beyond 0.05. `DATABASE VERIFIED` on the 902 posted RCM
documents: stored `gstAmount` vs Sage's own `1L8TX` total — **636 differ, net −₹184.74, absolute
₹421.76**, worst ±₹1.95. Sage rounds self-assessed RCM tax to the rupee and no legal slab satisfies
both numbers; the loader logs each to `work/rcm_variance.json` and posts anyway. This is a knowing,
bounded, sub-rupee-per-document divergence.

**Residual risks**
- `LEGAL_SLABS` includes **6** and **7.5**, which are single-authority (CGST-only) rates for the 12%
  and 15% slabs, not combined rates. A document where Sage booked only one authority would pass the
  slab test at half the true rate. It is caught in practice by the `stated_tax` vs `AMTTAXHC` gate at
  `:1459` (which then *skips* the document), so it fails safe — but silently. `SCRIPT VERIFIED / HIGH`.
- `line_rate` reads slots 1–2 only (`:1364`) and `SQL_STAGING_LINES:372` pulls only `ratetax1/2`.
  **`DATABASE VERIFIED`: `RATETAX3/4/5` are non-zero on 0 of the window's lines**, so the claim holds
  today.
- **The goods path has no rate reconciliation at all.** `value_recon.py:454` requires
  `metaData.sageLine ∈ d["rates"]`, and goods lines are stamped `PO:<item>` / `SVC:…`
  (`post_sage_bills.py:2541`), which never matches a `cntline`. 14,603 goods documents are unchecked.

### E.2 Quantity / unit price / amount

**What was wrong.** AP-direct distribution lines state no quantity and no unit price anywhere
(`post_sage_bills.py:1209-1211`: `QTYINVC`, `UNITMEAS`, `BILLRATE`, `AMTCOST` are zero or blank on all
46,156 window lines, and `IDITEM` is empty on every one). Bills posted before the PO join existed
therefore carry `quantity=1, unitPrice=<the whole line amount>` (`build_payload:2552`).
`work/fix_qty_unitprice.py` rewrites those lines in place from `POPORL.OQORDERED`/`UNITCOST`, gated by
`assert_invariants` so the totals cannot move, via `PUT /bill/{id}` + re-verify.

**Is the current code right?** `assert_invariants:2726-2731` compares `q2(unitPrice × quantity)`
against `taxableAmount` using **full-precision** `unitPrice` and rounding only the product — correct,
and the comment records why (`33.18 / 64 = 0.5184375`; rounding the rate first is a rupee out).
`po_detail:1330-1343` and `classify_goods:1773-1777` apply the identical rule before adopting any
item detail.

**Did the repair land? DATABASE VERIFIED / VERIFIED.**

```
ACTIVE bill lines 29,602 | quantity <= 0: 0 | abs(qty*unitPrice - taxableAmount) > 1: 0
all lines 36,459        | quantity <= 0: 1 | mismatches: 0   -> discrepancy value Rs 0.00
```

142 lines carry `unitPrice <= 0`; all are the small negative round-off legs (max magnitude under ₹1),
which `billLineItem` accepts by design. **No previously-posted row is left wrong.**

**Two issues in the repair script itself** (`SCRIPT VERIFIED / HIGH`):
- `work/fix_qty_unitprice.py:54` calls `P.build_payload` **without `items=`**, so every line falls back
  to the GL pseudo-item and gets `unit="OTH"` alongside a real quantity — `qty=64 unit=OTH`.
- `build_payload:2660` unconditionally calls `series_number`, which does `GET counter + 1`. Because the
  script only ever `PUT`s, **the counter never advances and every bill in one run is sent the identical
  `billSeriesNumber.value`.** Whether the server honours it on update is unchecked. See §E.6.

### E.3 Bill type derivation

`account_bill_type:161` reads `GLAMF.ACCTGRPCOD` per **formatted** account (`ACCTFMTTD`), routes
`4E2M`→DIRECT, `4E3E/4E4S/4E5O/4E6F`→INDIRECT, and inside the heterogeneous `4E1M` group only matches
an explicit stem list. `bill_type_of:1079` then lets the largest absolute amount win, ties going to
IN_DIRECT_EXPENSE.

**DATABASE VERIFIED / VERIFIED** — re-ran `account_bill_type` over the real `GLAMF` master and the
window's actual distribution:

```
GLAMF 4E/2A7T accounts             1,039   groups: 4E3E 378, 4E5O 256, 4E2M 213, 4E4S 68,
                                                   4E6F 64, 4E1M 49, 2A7T 10, blank 1
accounts with NO billType             35   (24 x 4E1M, 10 x 2A7T, 1 blank)
window 4E lines on a fall-through      1 account -> 4E1M016, 573 lines, Rs 60.86
window 4E accounts absent from GLAMF   0
```

The only account in the window that falls through is the round-off `4E1M016`, and
`SETTLED_ACCOUNTS:213` already resolves it. **The mapping is complete for this population.**
24 unmapped `4E1M` accounts and the 10 `2A7T` accounts remain a latent hold for any future window.

Live spread on ACTIVE bills (`DATABASE VERIFIED`): PURCHASE 6,226, IN_DIRECT_EXPENSE 2,626,
DIRECT_EXPENSE 525 — no bill carries the old blanket `"PURCHASE"` default on the AP-direct side
(the 6,226 are the goods population, which is legitimately PURCHASE).

**But the derivation landed too late.** See §E.10 — 16,829 lines, ₹87.3 M, still book to the
pre-derivation ledger.

### E.4 HSN / SAC

`normalise_hsn:660` takes the leading digits, truncates to 8, then drops to the nearest valid level
(8 → 6 → 4); anything under 4 digits returns `None`. `resolve_item_hsn:3211` tries, in order: the
line's own HSN → the same item's HSN on a sibling line → Sage `ICITEMO` → `GOODS_HSN_DEFAULT = "9999"`.

**The `9999` path. DATABASE VERIFIED / VERIFIED. The project's claim of 1,419 is exact.**

```sql
SELECT COUNT(*) total, SUM(hsnCode='9999') nines, SUM(hsnCode IS NULL) nul
FROM product WHERE organisationId='1029113552088076445';
-- 17,886 total | 1,419 hsnCode='9999' | 2 NULL
```

All 1,419 are live (`isDeleted=0`); `9999` is the second most common HSN in the org behind `54011000`.
**Only 8 of them are referenced by any bill line** (12 lines, ₹1,114,196.10 taxable); the other 1,411
are dormant master-data pollution. `billLineItem.hsn = '9999'` on **0** lines, so the placeholder does
not propagate onto documents.

**Cause chain, confirmed** (`SCRIPT + DOCUMENTED`): `item_hsn_map:3192` caches `{"": ""}` on a Sage
connection failure, so `resolve_item_hsn` silently skips its `ICITEMO` tier for the rest of the
process; `work/logs/gm-20260904-113445.log` shows exactly that during the 11,105-product create. The
uncommitted `run_all.sh` change (§G) is the fix — it now aborts rather than running Sage-blind.

**Two code defects around the placeholder** (`SCRIPT VERIFIED / VERIFIED`):
1. The **serial** `ensure_item_products:3489` calls `resolve_item_hsn(it)` **without `by_item`**,
   losing the sibling tier the parallel path uses — it will produce more `9999`s than the parallel path
   for the same input.
2. That same serial payload's `metaData` (`:3495-3499`) **omits `hsnSource` and `hsnIsDefault`**, so a
   `9999` product created serially is not flagged even in the payload.

**And the flag does not reach the database on any path.** `DATABASE VERIFIED / VERIFIED`:
`product.meta` is populated on only **11 of 17,886** products in this org, and on **0 of the 1,419**
carrying `hsnCode='9999'` — `POST /product/` silently discards `metaData`, while `bill.metadata`
(10,769/10,769), `billLineItem.metaData` (36,459/36,459) and `contact.metaData` (537/537) are all
persisted. So the whole premise of `GOODS_HSN_DEFAULT` — "a visible placeholder can be found and
corrected later… Flagged hsnIsDefault in metaData" (`:236-239`) — rests entirely on the *literal value*
`9999` being implausible, never on the flag. See H-11 in file 02.

**Line-level HSN is not normalised.** `build_payload:2564` sends `s(it["hsn"])` raw for goods lines,
bypassing `normalise_hsn` entirely. `DATABASE VERIFIED`: today all 199 lines carrying an HSN are 4 or
8 digits, so it has not fired — latent, not active.

**`EXPENSE_SAC = "996719"`** is sent unconditionally as the `hsnCode` of **every** AP GL pseudo-item
(`:2076`). `work/FINDINGS-EXPENSE-SAC.md:68-78` proposed deriving it from the base-account sibling
(covering 144 of 154); **that proposal is not implemented** — there is no sibling lookup on that path.

### E.5 GSTIN / state / place of supply — the IGST vs CGST+SGST switch

`extract_gstin:544` digs a GSTIN out of free-text `APVEN.BRN` (whitespace removed, exact 15-char
pattern must still match — nothing repaired). `registration_of:570` lets a well-formed GSTIN beat
`CODECTRY` (which holds city names). `resolve_state:590` takes the GSTIN prefix first, then
`CODESTTE` text, then `CODECTRY`, then **corroboration only** — GSTIN-prefix + pincode circle, or
city + pincode circle — and otherwise returns `UNKNOWN`, which holds the vendor.

**DATABASE VERIFIED / VERIFIED — this is correct in live data.** I re-derived `resolve_state` and
`registration_of` from live `APVEN` for every posted bill and compared against what SMEAssist stored:

```
bills checked against the Sage-derived state      9,330
  isInterState agrees with (state != KARNATAKA)   9,330
  DISAGREE                                            0
registration type (Sage-derived, stored):
  GST/GST 8,828 | WITHOUT_PAN_OR_GST/... 296 | PAN/PAN 206   -- 100% agreement
```

46 bills were skipped because their vendor is outside the AP window vendor master (they are goods
bills). **Not one posted bill has the wrong place of supply.** The IGST/CGST+SGST switch is sound.

`master-mismatches.json` reports **one** genuine state/GSTIN conflict — `OTHI022`, Sage
`33XXXPX0291X1ZY` (Tamil Nadu) against the stored contact `29XXXPX0420X1Z9` (Karnataka) — a vendor
whose contact was **reused by GSTIN match against a pre-existing contact for a different entity**.
That vendor has no posted bill, so no money has moved; it is a live trap for the next run.

**But the state the loader sends is discarded on the address, and the ledger follows the discarded
one.** `BACKEND VERIFIED / VERIFIED`. `POST /contact/address/create` takes `OrgAddressMinUpsertDto`
(`yoda/commons/.../OrgAddressMinUpsertDto.java`), which **has no `city`, no `state` and no
`primaryAddress` field at all** — those three keys the loader sends at `:2385-2392` are silently
dropped. The state is then *derived from the pincode*
(`OrgAddressToAddressUpsertDtoConverter.java:32-73`: `geoService.getGeoDetails(pinCode, country)` then
`.withCity(...).withState(...)`).

The two systems then disagree about which state matters:

| consumer | reads | code |
|---|---|---|
| `bill.isInterState` | the **payload's** address DTO state (GSTIN-derived) | `BillCreateConverter.java:38-40` |
| the **voucher's** CGST/SGST-vs-IGST split | the **stored** address, re-fetched by id (pincode-derived) | `EntityVoucherEntryCreateHelperService.java:3057-3065`, split at `:3078-3104` |

**And the loader's guard against exactly this is dead code.** `pincode_state:2141` calls
`GET /address/pincode/{pin}`; the real route is `GET /api/v1/pincode/{pincode}`
(`yoda/web/.../geo/PincodeController.java:25,34`). The call always 404s, `api.data()` yields nothing,
`pincode_state` always returns `None`, and the hold at `:2255-2258` **has never once fired.** The
loader's later comment (`:2237-2244`) noticed the 404 and drew the opposite conclusion — that the
pincode "decides nothing here". It decides the stored state, and the stored state decides the tax head.

**Realised exposure, measured.** `DATABASE VERIFIED / VERIFIED` — voucher legs against the flag, on
every ACTIVE bill:

```
isInterState = TRUE   5,961 bills : 5,926 carry IGST legs, 0 carry CGST+SGST      -> consistent
isInterState = FALSE  3,413 bills : 2,950 carry CGST+SGST, 129 carry an IGST leg
   of those 129:  125 are the PASSTHRU_GL 0% ledger "IGST Input (Import) @ 0.00%"  -> by design
                    4 are a genuine tax split on ledger "IGST Input @ 18.00 %"
```

Those 4 are one vendor: GSTIN `29XXXPX1133X1ZV` — **state code 29, Karnataka, i.e. intra-state** — whose
Sage pincode is `110065`, so the backend filed the address in **DELHI** and the voucher booked
**₹19,379.70 as IGST** where it should be CGST + SGST. `bill.isInterState` says `false`; the accounting
says otherwise. Sweeping every GST contact for the same shape finds **exactly one such vendor** out of
322 — the defect bites only when Sage holds a *real* pincode belonging to a different state than the
GSTIN, and `STATE_HEAD_PINCODE` round-trips correctly because it is the head office *of the proven
state*. Small in rupees, decisive as a mechanism, and it grows with every vendor added.

**Fail-open, additionally.** Even with the right route, `pincode_state` returns `None` on any non-2xx —
including a throttled 403 — after which `:2380` skips the check with no log line. Under sustained rate
limiting (2,816 backoff events in one run log) it would be absent anyway.

### E.6 Invoice number and series number

**Can two Sage documents collide onto one SMEAssist bill?** `base_invoice:796` strips only a trailing
`*<digits>` and only `rstrip`s (never `lstrip` — Sage holds `' WPL/25-26/07516'` and
`'WPL/25-26/07516'` as two separate obligations).

`DATABASE VERIFIED / VERIFIED`: I searched live Sage for the collision shape — a document whose raw
invoice equals another document's base for the same vendor:

```
base_invoice collisions in the Jan-Apr window: 0
AP book: 11,256 documents -> 11,256 consolidated bills (no merges occurred in this data)
duplicate (contactId, billNumber) in smeassist, any status: 0
duplicate tags in work/posted.log: 0
```

63 bill numbers are shared by more than one ACTIVE bill, but every case is different vendors using a
generic number (`31.03.26` across nine telecom/utility vendors; `001`; `1/31.01.26`). Not a defect.

**`*1`/`*2` markers**: 0 in SMEAssist (`billNumber`, `billSeriesNumber`, `remarks`, `metadata`) — the
loader strips them by design. Upstream, `idedat_staging.sage_bill_hdr` carries them on **6,421 of
18,047** rows, so the merge logic in `load_goods_book:1685-1699` is load-bearing: before it was fixed,
`FABI470|1173/2025-26` posted ₹10,183.95 against Sage's ₹815,867.96.

**A genuine, previously unreported defect: `billSeriesNumber` is not unique.**
`DATABASE VERIFIED / VERIFIED`.

```
ACTIVE bills                                          9,376
distinct billSeriesNumber (the concatenated string)   9,224   -> 152 bills share one
distinct (billSeriesPrefix, billSeriesValue)          9,322   ->  54 bills share one
cross-prefix collisions                                  98
```

Two independent causes:
1. **`SERIES_BY_FY = {"2025-2026": "SAGE", "2026-2027": "SAGE27"}` (`:86`).** `"SAGE27"` is a
   prefix-extension of `"SAGE"`, so the concatenated series numbers collide across the financial-year
   boundary: `SAGE2710` is both `SAGE`+`2710` (FY 2025-26) and `SAGE27`+`10` (FY 2026-27). **98 series
   numbers are shared by two bills this way.** The comment two lines above (`:84-85`) shows the author
   was thinking about exactly this class of collision and still chose a colliding name.
2. **`series_number:2478-2491` re-derives the value by `GET` + 1 with no reservation, and silently
   falls back to `"1"`**: `value = str(int(d["value"]) + 1) if d and d.get("value") is not None else "1"`.
   A failed or throttled counter lookup therefore stamps series value **1**. Combined with the
   duplicate `counter` rows `phase_cleanup:2862-2870` warns about but cannot delete, this yields
   **54 within-`SAGE` duplicate values** (7,964 distinct values across 8,018 bills).

### E.7 Currency and foreign vendors — **the most serious finding in this audit**

**AP-direct** filters `RTRIM(b.CODECURN) = 'INR'` (`SQL_HEADERS:346`) and `SQL_VENDORS:381`.
Non-INR AP documents are excluded outright.

**The goods path has no currency filter and applies no exchange rate.** `SQL_GOODS_HDR:1605` selects
`h.doc_total, h.tax_total` from `idedat_staging.sage_bill_hdr` with only an `inv_date` predicate.
`build_payload:2692` always sends `"conversionRate": 1.0, "currencyDto": {"currency": "INR"}`.

`DATABASE VERIFIED / VERIFIED` — `sage_bill_hdr` carries **both** a source-currency total and a
home-currency total, and the loader reads the wrong one:

```
DESCRIBE idedat_staging.sage_bill_hdr
  currency char(3) | fx_rate decimal(20,8)
  doc_total decimal(20,4)      <- SOURCE currency  (what SQL_GOODS_HDR selects)
  tax_total decimal(20,4)      <- SOURCE currency  (what SQL_GOODS_HDR selects)
  ap_amount_hc decimal(20,4)   <- HOME currency, never read
  ap_tax_hc   decimal(20,4)    <- HOME currency, never read

Jan-Apr 2026 window, by currency:
  INR 16,049 docs   doc_total 1,440,377,343.30   home 1,438,183,151.96   fx 1.0000
  USD  1,841 docs   doc_total     2,688,752.54   home   242,392,995.60   fx 90.5064
  RMB     92 docs   doc_total    10,886,465.28   home   138,912,052.52   fx 12.7897
  CNY     65 docs   doc_total     9,592,335.59   home   122,125,197.73   fx 12.7662
```

`sage_goods_line.extended` is also in source currency, so `classify_goods`'s internal identity holds
(`Σ ext + svc + tax == doc_total` on 1,703/1,841 USD, 92/92 RMB, 62/65 CNY) — **the document passes
every gate and posts its foreign face value as rupees.**

**Has it happened? No — but it is armed.** `DATABASE VERIFIED`:

```
non-INR goods documents in the window                                        1,998
  ... already posted (present in work/posted.log)                                0
  ... whose vendor ALREADY has a contact in the crosswalk                    1,468
      would post as INR   Rs   22,071,112.42
      true home currency  Rs  403,866,335.94
      UNDERSTATEMENT      Rs  381,795,223.52
```

The `masters` work that unblocked international vendors (commit `81ba90b`, "Post the international
vendors") is precisely what armed this: 1,468 of the 1,998 are now contact-ready and will be picked up
by the next `./run_all.sh` (`goods-post --all-categories`). **₹38.2 crore understated, on the next run.**

The `README`'s own reasoning at `:2687-2690` — "INR, not the vendor's local currency: every amount this
loader posts is Sage's home-currency figure (doc_total / AMTINVCHC) and the bill declares currencyDto
INR at conversionRate 1.0" — is **true for `AMTINVCHC` on the AP path and false for `doc_total` on the
goods path.** `doc_total` is the source-currency figure; `ap_amount_hc` is the home-currency one.

**Silently excluded population, AP-direct.** `DATABASE VERIFIED / VERIFIED` — the funnel from raw Sage
to the book:

| stage | docs | value (AMTINVCHC) |
|---|---:|---:|
| raw window (`IDTRXTYPE=12, SRCEAPPL='AP'`, Jan–Apr 2026) | 12,781 | ₹1,763,950,260.97 |
| 1. dropped: no `APIBD` distribution row | **1,079** | **₹295,107,559.76** |
| 2. dropped: non-INR currency | **43** | **₹254,244,874.08** |
| 3. dropped: `CODETAXGRP` in VAT/NRVAT/NRST/NRVATST | **118** | **₹5,669,280.00** |
| 4. dropped: purity filter — a line outside `4E`/`2A7T`/`1L8TX14-16` | **285** | **₹489,297,175.70** |
| **IN THE BOOK** | **11,256** | **₹719,631,371.43** |

**1,525 documents worth ₹1,044,318,889.54 — 45% more money than is in the book — are excluded in SQL,
before any Python runs.** None of them is covered by the goods path either (I joined all 1,525 keys
against `sage_bill_hdr`: **0 overlap**). Because the exclusion happens in `SQL_HEADERS`, these
documents never enter `book`, never reach `classify()`, and therefore **cannot appear in
`work/skipped.json` or `work/failures-report.json`** — which `run_all.sh:163` presents as "anything
still outstanding".

**Foreign vendors on the AP path** are handled well (`ensure_contacts:2288-2313`, `:2378-2397`):
`platform_country:747` refuses to guess (`'ISLAND'` is held, not filed as Iceland or Ireland),
`normalise_foreign_pincode:760` stops throwing away real foreign codes, `registrationType
INTERNATIONAL`, `state UNKNOWN`, `profileType OTHERS`, `currencies: ["INR"]`, and
`purchaseType/originCountry/destinationCountry` set on the bill. 43 international contacts exist.

### E.8 Dates and financial year

`epoch_ms:781` returns `calendar.timegm((y, m, d, 0,0,0)) * 1000` — **UTC midnight**.
`financial_year:787` uses the bill's own date, `month >= 4` → `y-(y+1)`.

**DATABASE VERIFIED / VERIFIED.** Read every posted bill's stored `billDate` back as UTC and compared
against Sage `DATEINVC`:

```
bills compared 9,248 | date mismatches 0 | series prefix disagreeing with the bill's own FY 0
stored billDate range: 2026-01-01 .. 2026-04-30 (exactly the window)
```

No off-by-one. **Residual risk**: the value is a UTC-midnight instant, so any consumer rendering it in
a timezone west of UTC shows the previous day. India is UTC+5:30, so it renders correctly here; a
report generated in a US timezone would be a day out on every bill. `INFERRED / MEDIUM`.

`sage_date_parts:774` rejects `'00000000'` and anything not 8 digits; `build_payload:2497` falls back
`due_date → bill_date`. `series_number:2480` raises `Stop` for an FY with no configured series — which
would abort a whole run rather than skip a bill, but `DATE_FROM/DATE_TO` keep every document inside the
two configured years.

### E.9 Readback / verification

`FINDINGS-READBACK-BROKEN.md` is accurate and the defect **is fixed now**. `readback_drift:2893` used
`GET /bill/{id}`, which answers `200 / success:true / data:[]` for every bill, so `isinstance(d, dict)`
was always false and nothing was ever compared while `Api.ok` passed it. It now calls
`GET /bill/detail/{billId}` (`:2911`). `SCRIPT VERIFIED / VERIFIED`.

**The broken window was 2026-09-02 ~16:20 → 18:05 and exactly 10 bills were posted inside it**
(`ACCL005|DE-2220/25-26`, `ACCL005|DE-2232/25-26`, `ACCL003|1694/2025-26`, `ACCL073|4625/25-26`,
`ACCL073|4628/25-26`, `ACCL179|RMD/25-26/305`, `ACCL035|DISHA/25-26/284`, `ACCL005|DE-2223/25-26`,
`ACCL047|3602/25-26`, `ACCL047|3601/25-26`). They were **never re-run through `readback_drift`** —
`posted.log` marks them done — but all 10 fall inside the 9,371 documents `value_recon.py` compared on
2026-09-05 across 18 checks and none appears in its 674 mismatch rows. **Re-verification came from the
reconcilers, not from the loader.** `ARTEFACT VERIFIED / HIGH`.

**Three gaps remain**:
1. `phase_goods:3720-3722` **never calls `readback_drift`.** The whole goods population — the larger of
   the two, and the one with the FX defect in §E.7 — is posted with no server-side read-back at all.
2. On RCM the readback deliberately does **not** compare against Sage (`:2954-2969`); it asserts only
   the server's internal identity. RCM bills are therefore never tied back to a Sage figure by any
   automated check inside the loader.
3. `assert_invariants:2747-2749` compares `payload["billAmount"]` against `shape["bill_amount"]` — but
   `:2663` assigned `payload["billAmount"] = float(shape["bill_amount"])`. **It is `X vs X`.** Its
   comment ("the whole point of the exercise: it has to be Sage's own figure") overstates what it does.
   The real tie is `classify:1522-1525`, and on an RCM document even that is tautological because
   `bill_amount` is *computed* as `taxable + tax + roundoff` rather than read from Sage.
4. **`readback_drift`'s taxable check is vacuous too.** `BACKEND VERIFIED / VERIFIED`: bill-level
   `taxableAmount` is the **one** total the server never recomputes — `BillCreateConverter.java:83`
   stores `source.getTaxableAmount()` verbatim and nothing ever compares it to the lines. `:2933-2935`
   therefore compares the loader's own number to itself. Its comment ("the server stores exactly
   SUM(line taxableAmount) and does not round it") is a misreading. A drift between the sent bill
   taxable and the sum of the server-recomputed line taxables would go entirely undetected — and such a
   drift is possible, because the server recomputes each line's taxable as `qty × unitPrice` at **6 dp**
   (`BillLineItemConvertor.java:208-223`, `BigDecimalUtils` HALF_UP at 6) while
   `assert_invariants:2726-2731` ties only `q2(unitPrice × qty)` at 2 dp.

### E.9b Does the backend trust what the loader sends? — DATABASE VERIFIED

The loader's central architectural claim (`post_sage_bills.py:1463-1474`) is that the server **ignores
the `gstAmount` and `roundOffAmount` in the payload and recomputes both from the lines**, so the only
levers are per-line `taxableAmount` and `gstPercentage`. That claim is the entire justification for
inverting defect 4.5 (round-off as a line, not a bill field) and for `readback_drift` existing at all.

**It is empirically true.** `DATABASE VERIFIED / VERIFIED`, over every ACTIVE bill in the org:

```sql
SELECT COUNT(*) bills,
  SUM(ABS(b.gstAmount - x.derived) <= 0.01)                                        gst_equals_sum_of_lines,
  SUM(ABS(b.billAmount - (b.taxableAmount + b.gstAmount + COALESCE(b.roundOffAmount,0))) <= 0.01) identity_holds
FROM bill b JOIN (SELECT billId, SUM(taxableAmount*gstPercentage/100) derived
                  FROM billLineItem WHERE isDeleted+0=0 GROUP BY billId) x ON x.billId = b.id
WHERE b.organisationId = <org> AND b.isDeleted+0=0 AND b.billStatus='ACTIVE';

-- bills 9,376 | gst_equals_sum_of_lines 9,376 | identity_holds 9,376 | non-zero roundOffAmount 10
```

`gstAmount = SUM(line taxable x line rate / 100)` on **all 9,376**, and
`billAmount = taxableAmount + gstAmount + roundOffAmount` on **all 9,376**. The Sage tax figure the
loader puts in `payload["gstAmount"]` never survives. Only 10 bills carry a non-zero
`roundOffAmount` — the legacy ones posted before `hasRoundOff` was set to `False`.

**Two consequences the code handles and one it does not:**
- On forward charge the recomputed figure matches Sage to within the per-authority truncation, and
  `readback_drift:2938-2952` applies exactly that tolerance. Handled.
- On reverse charge the recomputed figure is the slab result, which *cannot* equal Sage's rupee-rounded
  `1L8TX` total. Logged per document to `work/rcm_variance.json`, posted anyway. Handled, knowingly.
- **The goods path never reads any of it back** (`:3720-3722`). Whatever the server substitutes on a
  goods bill is never compared to anything.

### E.10 RCM accounting — verified correct, and a reconciliation trap

`DATABASE VERIFIED / VERIFIED.` For 901 posted RCM documents, SMEAssist's `billAmount` is
**₹1,532,517.26 higher in total** than Sage's `AMTINVCHC`. That is *not* an error: defect 4.4 grosses
RCM bills up deliberately, and the voucher legs prove the accounting is right. For `OTHL166|010/2025-26`
(Sage ₹129,150.00, stored ₹152,397.00):

```
DEBIT   Rent - others_SAGE-4E5O021-02 Indirect Expense   129,150.00
DEBIT   CGST Input @ 9.00 %                               11,623.50
DEBIT   SGST Input @ 9.00 %                               11,623.50
CREDIT  Pan_XXXPX3707X_<vendor>                          129,150.00   <- vendor payable = taxable only
CREDIT  SGST Payable RCM @ 9.00 %                          11,623.50
CREDIT  CGST Payable RCM @ 9.00 %                          11,623.50
```

The vendor is credited the net invoice; the self-assessed tax goes to the RCM payable heads. Correct.
**The trap**: any total-vs-total reconciliation of the two systems will show a ₹15.3 lakh gap that is
by design, and `work/reconcile.py:113` "resolves" it with `elif txb == sg: rcmok += 1` — a heuristic
that would also silently pass a **forward-charge** bill that posted with its GST dropped entirely.
901 documents sit in that unexamined bucket.

**One forward-charge bill is still ₹1.00 below Sage** — `JOBW258|108`, ₹323,826.00 stored against Sage's
₹323,827.00. That is the pre-fix `hasRoundOff` defect, still present in live data.

### E.11 Ledger heads — ₹8.73 crore misfiled, already in live data

`DATABASE VERIFIED / VERIFIED.` `ensure_products:2015-2034` re-heads a product whose ledger was minted
under the old `ITEM_DIRECT_EXPENSE` default by minting a **new** ledger beside the old one — the
endpoint does not remap. The bills already posted keep pointing at the old ledger. I resolved every
`priorLedger` in the crosswalk against `financeAccount` and `billLineItem`:

```
products carrying a superseded priorLedger                        138
superseded ledgers still carrying a balance                        50   total Rs 87,293,633.44
ACTIVE bill lines still pointing at a superseded ledger        16,829   taxable Rs 87,296,758.72
```

Worst offenders, every one filed under **"Direct Expenses"** while its account group says indirect:

| account | ledger name | group | balance |
|---|---|---|---:|
| `4E4SD04` | Freight Charges-Export | Direct Expenses | −21,651,747.35 |
| `4E4SD01` | Custom Clearance & Forwarding Charges | Direct Expenses | −17,703,293.92 |
| `4E5O014` | Professional Charges | Direct Expenses | −16,549,052.00 |
| `4E4SD03` | Carriage Outwards | Direct Expenses | −9,626,522.05 |
| `4E2ME14` | Freight Charges - Imports | Direct Expenses | −5,427,925.07 |
| `4E5O036` | Donation | Direct Expenses | −2,167,168.00 |

The only fix is to revoke and repost those 16,829 lines. **No script in `work/` does that** —
`repost_stranded_parts.py` targets amount shortfalls only.

---

## F. Resumability and the state machine

### F.1 What holds the state

| store | contents | durability |
|---|---|---|
| `work/crosswalk_live.json` (4.16 MB) | `products` 197, `items` 17,220, `contacts` 455, `burned` 75, `series` {} | **gitignored local file on one laptop** |
| `work/posted.log` (390 KB, 9,378 lines) | `vendor|invoice||billId` (+ `||UNVERIFIED`, or `||preexisting`) | append-only, `fsync`ed per line |
| `idedat_staging.crosswalk` | **0 rows** — `DATABASE VERIFIED` | the durable copy that was never written |
| `state/crosswalk.json`, `state/superseded-2026-09-01/` | earlier generations | superseded |

### F.2 Is the crosswalk written atomically?

`State.save:925` writes `CROSSWALK + ".tmp"` then `os.replace` — **atomic rename, but neither the temp
file nor the directory is `fsync`ed**. On a clean process crash the rename is safe; on a power loss or
kernel panic the rename can be visible while the data is not, leaving a zero-length or truncated
crosswalk. `SCRIPT VERIFIED / VERIFIED`.

That matters because a truncated crosswalk is not merely a resume failure — `phase_cleanup:2843-2852`
deletes every contact in the org that is not in the crosswalk. An empty crosswalk turns `cleanup` into
"delete all 537 contacts".

Four hand-made backups sit beside it (`.pre-unburn-101355` 765 KB, `.before-intl-*` 4.14 MB,
`.b4-intl2-*` 3.76 MB, current 4.16 MB) — the file has been repaired by hand at least twice. The
765 KB → 4.1 MB jump is entirely `items` going 2,690 → 17,220 in the 4 Sep `--all-items` run, not a
repair. The `.pre-unburn` snapshot is a genuine manual edit clearing false-burned entries.

`work/fix_misnamed_products.py:30` and `work/cleanup_pilot.py:44` both write the crosswalk with a
plain `json.dump(xw, open(path,"w"))` — **truncate-then-write, no temp file**. An exception mid-dump
destroys all 17,947 mappings.

### F.3 Can a bill be posted twice?

`SCRIPT VERIFIED / HIGH` — the window exists, and it is narrow:

```
POST /bill/  -> 201, billId returned
   <process dies here>                <-- the bill exists; nothing is in posted.log
POST /bill/{id}/verify
state.mark(tag, bid)                  <-- only now is it recorded
```

On the next run the tag is absent, the bill is rebuilt and re-posted. The only thing that stops a
duplicate is the server answering `"Bill number already exists"` (`:3053`). **`DATABASE VERIFIED`:
there is no unique index behind that check** — `SHOW INDEX FROM bill` returns only `PRIMARY`,
`index_orgId`, `index_orgId_billType` and two FK indexes. The guard is service-level only.

**Measured outcome today: no duplicates.** 0 duplicate `(contactId, billNumber)` pairs in any status,
0 duplicate tags in `posted.log`. The mechanism has held so far.

### F.4 Can a master be created twice?

- GL pseudo-items: adopt-before-create by SKU (`:2039`), so no.
- Item products: **create-first, adopt-on-collision** (`:3358-3372`) — deliberate, because
  `find_product_by_sku` used to page a fuzzy endpoint. Safe because the create is refused on a taken
  SKU. `DATABASE VERIFIED`: 0 duplicate SKUs among the org's 17,886 products.
- Contacts: guarded by `if state.xw["contacts"].get(code): continue` (`:2183`) plus the
  reuse-by-GSTIN branch (`:2404`). `DATABASE VERIFIED`: 537 contacts, 0 duplicate registration numbers
  among the migration's own.
- Ledgers: `getOrCreate/{referenceType}` **mints a new ledger per reference type** rather than
  remapping (`:1944-1946`). Calling it with a second mapping is *additive by design* — which is
  exactly how the 138 superseded ledgers in §E.11 came about.

### F.5 Can a crosswalk entry cause permanent skipping?

Yes, and it did. Three distinct mechanisms, two now fixed:

1. **BURNED on a throttled lookup.** `_find_product_by_sku_paged:1908` used to return `None` for a 403,
   making "rate limited" indistinguishable from "does not exist"; the caller then recorded the SKU
   BURNED permanently. `LookupFailed:302` fixed it. `FINDINGS-BURNED-SKUS.md` measured a ~7% burn rate
   from this and a **43% failure rate** from the related 10-page cap on a fuzzy endpoint that never
   filtered.
2. **BURNED on a name collision.** `POST /product/` enforces uniqueness on `productName`, not
   `skuCode`, and reports it with the same `"Resource already exists"` message. `item_product_name:3237`
   fixed it by appending ` [item|unit]`.
3. **The `burned` dict is now inverted and misleading.** `ARTEFACT VERIFIED / HIGH`: all **75** current
   `burned` entries **also have a live `items` entry carrying the identical `skuCode`** — they are
   fully built, yet `failures-report.json` still reports `burned_skus: 75` as a blocker. Meanwhile the
   **2 genuinely unrecoverable SKUs** — `SAGE-4E2ME02-14` and `SAGE-4E5O024-01`, soft-deleted by
   `work/fix_misnamed_products.py` — have been **dropped from the list** and still block the 17
   documents `skipped.json` counts as "no product for GL account".

### F.6 Half-posted documents

`SCRIPT + DATABASE VERIFIED / VERIFIED`. Three states can survive:

- **created but not verified** → `tag||{bid}||UNVERIFIED`. The bill is ACTIVE with **zero voucher
  entries**: a payable that books nothing. `phase_run:3000-3013` re-verifies these on a later run.
  7 such tags exist in `posted.log`; all 7 are now VERIFIED with legs in the database, so they were
  repaired — but **`posted.log` was never updated**, so the next `post` run will re-issue `verify` on
  seven already-verified bills.
- **created, refused as pre-existing, never verified** → `tag||preexisting`. **This is not repaired by
  anything.** See BUG-1.
- **created and verified but drifted** → `readback_drift` records a failure and `state.mark` runs
  anyway (`:3081-3086`). The bill stays live and wrong.

### F.7 The single point of failure

`DATABASE VERIFIED / VERIFIED`. `idedat_staging.crosswalk` exists with a proper schema
(`entity_type, source_key, target_id, target_code, channel, run_id, created_at`, PK on
`entity_type, source_key`) and holds **zero rows**. Nothing in `post_sage_bills.py` references it —
`grep` finds no write path at all.

Every Sage→SMEAssist identity mapping this migration has built — **17,947 entries: 17,220 item
products, 455 contacts, 197 GL pseudo-items, plus every `financeAccountId`** — exists only as one
gitignored JSON file on one laptop, with hand-made backups beside it. Losing it means:

- every `masters`/`goods-masters` re-run re-creates 17,417 products, every one of which collides,
  and adoption depends on `product_index` reading 17,886 rows through a rate-limited API;
- every contact is re-created and refused on GSTIN, then reused only if the *sibling* crosswalk entry
  that held its `addressId` still exists (`:2419-2426`) — which it would not;
- `phase_cleanup` becomes destructive (§B.1).

`posted.log` is a better citizen (append-only, `fsync` per line) but is likewise the only record of
which of 11,256 documents were posted.

---

## G. The uncommitted diff (+282 / −54 across five files)

`git diff` — `SCRIPT VERIFIED / VERIFIED`. All five changes are bug fixes; four are unambiguous
improvements, one introduces a new risk.

### G.1 `post_sage_bills.py` (+41 / −4)

**(a) `:2389-2422` — `str(None)` is the truthy string `"None"`.**
```python
-  addr_id = str((api.data(b2) or {}).get("addressId")) if api.ok(st2, b2) else None
+  _addr   = (api.data(b2) or {}).get("addressId") if api.ok(st2, b2) else None
+  addr_id = str(_addr) if _addr else None
```
A 2xx whose body carried no `addressId` produced `addr_id == "None"`, which passed `if not addr_id`
and was committed to the crosswalk as a permanent unusable address id. **Real bug, correctly fixed.**
`DATABASE VERIFIED`: 0 contacts in the current crosswalk carry `addressId == "None"`, so it either
never fired or was cleaned.

**(b) `:2400-2418` — orphan-contact rollback.** A contact whose address create is *definitely* refused
is now `DELETE`d so a retry starts clean. The `definite = st2 is not None` test is well reasoned and
correct for this `Api`: `call()` returns `None` only for a connection error or exhausted retries and
has already retried 403 and content-free 5xx, so a `400 <= st < 500` test would never fire against a
service that signals business refusals with a 500-plus-`errorMessage`.

**But the rollback cannot work.** `BACKEND VERIFIED / VERIFIED`: there is **no `DELETE /contact/{id}`
route** — `ContactController.java` has no `@DeleteMapping` anywhere in its 520 lines. Every orphaned
contact is left behind and the code takes its "(orphan contact %s left behind)" branch every time; the
change improves the *message*, not the outcome. Worse, `ContactRepository.java:51-54` filters
`isDeleted=false` but ignores status, and there is no `DELETED` status — so **a contact created against
the wrong GSTIN blocks that GSTIN permanently through the public API.**

**(c) `:2631-2645` — `contactBillingAddressDto.country`.** Was the constant `"INDIA"`; now the
contact's own country, and **only when `registrationType == "INTERNATIONAL"`**. Correct: it stops a
payload declaring `purchaseType=INTERNATIONAL, originCountry=CHINA` while describing the vendor as
Indian, and it does not trust `APVEN.CODECTRY` for domestic vendors (which holds city names).
**Consistent with the committed design.**

### G.2 `run_all.sh` (+41 / −10)

**(a) Sage gate.** Replaces a bare TCP connect to 1433 with `find_sage.py --write --quiet`, retried
once, and **aborts the run (exit 3) unless `--allow-stale-sage`**. This is the fix for the 1,419
placeholder HSNs — an open port is not proof of Sage, and running Sage-blind stamps a permanent
misclassification. **Correct and important.**
**New risk**: the preflight now **writes `.env`** as a side effect (`find_sage.py:230-232`,
non-atomically). A crash there loses the SQL password and the auth token, and there is no backup.

**(b) `--from` parsing.** Was `FROM="${2:-}"; [ "${1:-}" = "--from" ] && FROM="${2:-}"` — `$2` was taken
**unconditionally**, so `--allow-stale-sage --from goods-post` set `FROM="--from"`, matched no phase,
skipped everything and **exited 0 reporting success while doing nothing.** Now scanned positionally,
validated against the four phase names, and `--from` as the last argument is a hard error rather than
silently meaning "run everything against the live ERP". **Two real bugs, both correctly fixed.**

### G.3 `work/find_sage.py` (+45 / −11)

- `sql_port()` unifies the port between the scan and the login. Previously `port_open` hardcoded 1433
  while `is_sage` honoured `SQL_PORT`, so on a non-default port relocation always reported NOT FOUND.
  **Real bug.**
- Interface filtering (`docker|br-|veth|virbr|lo$|tun|tap|gpd`) and a `/20` ceiling (4,096 addresses,
  down from 65,536), with an explicit stderr message when everything was skipped rather than a silent
  "not found". **Correct.**
- **Not fixed, and worth flagging**: `is_sage` opens a `pymssql` connection with the real
  `SQL_USER`/`SQL_PASSWORD` against **every host answering on the SQL port** across up to 4,096
  addresses — and its own docstring notes there is a second, foreign SQL Server on this network. The
  migration credentials are sprayed across the office subnet twice per `run_all.sh` preflight.

### G.4 `work/master_recon.py` (+128 / −26)

Six fixes, all in the "a check that could not run must not report clean" family, which is the file's
entire thesis:
- `sku_of` now routes the unit through `P.platform_unit()`. Joining on the raw unit put the two sides
  in different namespaces: **8,634 phantom `product_missing` and 8,856 phantom `product_extra` rows
  against a true 2 and 230.**
- `product_duplicate_sku` counts from raw rows; it used to `Counter` the keys of a dict, which are
  unique by construction, so it **could never fire while still reporting as run**.
- `check_hsn_mismatch` no longer does `src.sage_items() or {}` — an unreadable Sage side now reaches
  `rep.skip()` instead of iterating an empty dict and landing in `checks_run` with zero mismatches.
- `sage_vendors` refuses an empty result, and claims its `origin` only after success (it used to be
  listed under both `sources.read` and `sources.unreadable`).
- A `NEEDS` pre-gate loads each check's sources up front, and `checks_requested` is kept separate from
  the gated list — previously a partial run serialised `checks_requested == checks_run` and
  **described itself as complete**.
- `check_ledgers` returns early rather than pulling ~19,500 finance accounts for a run that only
  asked for `hsn_default`.

**All six are genuine and consistent with the design. This is the best-audited file in the tree.**

### G.5 `work/repost_stranded_parts.py` (+18 / −5) — the one change that adds risk

```python
-  rv = "ok" if live.ok(st, b) else live.err(b)[:70]
-  st2, b2 = live.call("DELETE", "/bill/%s" % bid)      # ran regardless
+  if not live.ok(st, b):
+      print("   %-24s revoke=FAILED ... not deleting, left intact"); continue
   st2, b2 = live.call("DELETE", "/bill/%s" % bid)
...
-  if live.ok(st, b): done.add(tag)
+  done.add(tag)          # <-- now added even when the DELETE failed
```

The **first half is a correct and important fix**: `DELETE` no longer runs when the `REVOKE` failed, a
sequence that could remove a bill from SMEAssist while its tag stayed in `posted.log`, making
`goods-post` skip it forever.

The **second half** moves `done.add(tag)` outside the success branch, so the tag is dropped even when
the `DELETE` failed. Its comment reasons that "a REVOKED bill books nothing, so dropping the tag is
correct - `goods-post` will recreate it whole". That holds only if the server's bill-number uniqueness
check ignores REVOKED bills - and **it does**. `BACKEND VERIFIED / VERIFIED`:
`smeassist/core/src/main/java/com/assist/core/bill/service/Impl/BillServiceImpl.java:611-633` restricts
the existence check to `billStatuses = [ACTIVE, APPROVAL_PENDING, BLOCKED]`; `REVOKED` is absent, so a
revoked bill does not block a repost under the same number. **The reasoning is sound and the change is
safe.** (Note `isDeleted` is not in that predicate either.)

Two pre-existing weaknesses this diff does not address: the fixed backup name
`posted.log.before-repost` (a second `--apply` overwrites the first backup), and the fact that
`posted.log` is rewritten **once at the end**, so a crash after revoking N bills strands all N.

**The identical revoke-before-delete fix was never back-ported to `work/repost_po_items.py:95-99`**,
where `DELETE` still runs unconditionally.

---

## H. The project's own claims, checked

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| H1 | `README.md:7-9` — a port of `load_janapr_bills.ps1` "with six field-level defects fixed, each annotated inline against its defect number" | **PARTLY WRONG** | See §H.1 below. `SCRIPT VERIFIED / VERIFIED` |
| H2 | `run_all.sh:64` — "1,419 products with the 9999 placeholder HSN" | **HOLDS, exactly** | `SELECT SUM(hsnCode='9999') FROM product WHERE organisationId=…` → **1,419**. Causal chain confirmed. Only 8 are billed. `DATABASE VERIFIED` |
| H3 | `run_all.sh:94-95` — "vendors come from the staging mirror, which is missing **469** of the vendors live APVEN has" | **NUMBER RIGHT, SENTENCE WRONG** | The window needs 541 INR vendors; `idedat_staging.sage_vendor` holds 357, of which **72** are in-window → 541 − 72 = **469** exactly. But (a) against *all* of APVEN (4,752) the gap is 4,395, and (b) the **AP path's fallback is `output/vendors.csv` (669 rows), not the staging mirror** — the mirror feeds only the goods path. `DATABASE VERIFIED` |
| H4 | `README.md:71-72`, `run_all.sh:13` — "12 bills/min concurrent against 63 bills/min alone" | **WRONG — the figures look swapped** | Bill ids are snowflakes (`ts = (id>>22)+1420070400000`), so `posted.log` is timestamped. Solo runs peak at **29 bills/min** (median 27); 63 is never reached alone. The one minute that hit **63** was a *concurrent* minute (09-03 18:43). The AP stream was **unharmed** by the 5 concurrent minutes on record (mean 25.6/min vs 23.9 solo in the same session). Nothing resembling 12/min was ever observed. Overall: 9,371 bills in 363.7 active minutes = **25.8/min**. The serial policy may still be right; the number quoted for it is not supported. `ARTEFACT VERIFIED / HIGH` |
| H5 | `FINDINGS-BURNED-SKUS.md` — 39 of 41 burns were false, root cause name-collision + throttle-as-absence | **DIAGNOSIS HOLDS; the artefact is now inverted** | Both fixes landed (`item_product_name:3237`, `LookupFailed:302`, `product_index:1849`). But all **75** current `burned` entries are fully built, and the **2 genuinely dead SKUs** were dropped from the list. `ARTEFACT VERIFIED / HIGH` |
| H6 | `FINDINGS-READBACK-BROKEN.md` — readback was inert | **HOLDS; fixed** | Route changed `GET /bill/{id}` → `GET /bill/detail/{id}` (`:2911`). Window 09-02 16:20→18:05, **10 bills**; none re-run through readback, all 10 clean under `value_recon`. Goods path still has **no** readback. `SCRIPT + ARTEFACT VERIFIED` |
| H7 | `FINDINGS-EXPENSE-SAC.md:68-78` — derive the SAC from the base-account sibling | **NOT IMPLEMENTED** | `:2076` still sends `EXPENSE_SAC` unconditionally; there is no sibling lookup on the AP product path. `SCRIPT VERIFIED` |
| H8 | `FINDINGS-EXPENSE-SAC.md:39-55` — "`metaData` is not stored; the create endpoint silently drops it" | **HOLDS — and the code still relies on it** | `product.meta` populated on **11 of 17,886** products and **0 of the 1,419** placeholder-HSN rows, while `bill.metadata`, `billLineItem.metaData` and `contact.metaData` are populated on 100% of rows. `:2084`, `:3287-3295` and `work/build_item_categories.py:57-62` all depend on a flag that is never stored. `DATABASE VERIFIED / VERIFIED` |
| H9 | `FINDINGS-ITEM-MASTER.md:120-145` — `ITEMBRKID` is the better type axis | **NOT TAKEN** | `stock_type_for:3147` maps Sage `CATEGORY`; `ITEMBRKID` appears nowhere in the codebase. `SCRIPT VERIFIED` |
| H10 | `README.md:80`, `run_all.sh:163` — `failures-report.json` holds "everything not posted" | **WRONG, twice over** | (a) `failure_report.py:64-76` replays only `classify()` + the contact test, missing three `eligible()` branches — **44 documents** (27 URP, 17 no-product) never appear. (b) Far larger: the **1,525 documents / ₹1,044,318,889.54** excluded in `SQL_HEADERS` never enter the book at all and are structurally invisible to it. `SCRIPT + DATABASE VERIFIED` |
| H11 | `README.md:31-35` — the org's identity was removed from tracked files | **WRONG** | `:88` `COMPANY_ADDRESS_ID`, `:92` `CONTACT_CATEGORY`, `:2468-2475` the full street address, city, state and pincode are all hardcoded in the tracked loader. Only GSTIN/PAN/hosts moved to `.env`. `SCRIPT VERIFIED` |
| H12 | `extract.sql:4-7` — "`pull.py` reads it, splits it on the `@@name` markers, each becomes `output/<name>.csv`" | **FALSE on every clause** | `extract.sql` was written **27 h after** the CSVs. `work/patch_pull.py:14,40,75-81` shows the real `pull.py` is a Sage→**MySQL** loader with Python-literal queries, no `@@name` concept, and 14 pull names disjoint from the 7 block names. `SCRIPT VERIFIED / VERIFIED` |
| H13 | `extract.sql:34` — "Expected: 11,256 header rows / **46,385** line rows" | **WRONG** | 46,385 is the `wc -l` of the corrupt PSV (1,206 newline fragments). Truth: **45,179**, confirmed against live Sage today. `MACHINE-CHANGES.md:190-196` already says so. `DATABASE VERIFIED` |
| H14 | `RUNBOOK:136-138`, `FIX-PROPOSAL:99` — "~795 RCM variance documents, ₹495 in total" | **STALE** | `rcm_variance.json` (09-04): **245 documents, ₹161.58**, max ±₹1.95. The bound holds; the population is a third. My own DB check found 636 differing documents totalling ₹421.76 absolute. `ARTEFACT + DATABASE VERIFIED` |
| H15 | `RECON-BOTH-SITES.md:203` — "₹21,726.98 across 10 bills booked to `1L6TA07` as expense" | **HOLDS, still unrepaired** | `master-mismatches.json` `ledger_orphan` reports the identical figure on the superseded ledger; my own query confirms it inside the ₹87.3 M total. `DATABASE VERIFIED` |
| H16 | `post_sage_bills.py:93` — `ROUNDOFF_GL = "4E1M016"  # 4.5 bill field, never a line` | **STALE COMMENT, contradicted by the code 1,700 lines below** | `:1477-1487` and `:2691-2696` emit it as a 0% **line** and send `hasRoundOff: False`. `SCRIPT VERIFIED` |
| H17 | `README.md:79-80` — presents `reconcile-report.json` and `failures-report.json` side by side | **MISLEADING** | The two reconcilers disagree on the Sage population by **1,525 documents** (12,781 vs 11,256) and therefore on "not posted" by 1,518 (18,006 vs 16,488), because `reconcile.py:57` reads raw `sage_ap_obl` with none of the purity filters. Nothing says so. `SCRIPT + ARTEFACT VERIFIED` |

### H.1 The six PowerShell defects

The README says `post_sage_bills.py` is a port of `ref/load_janapr_bills.ps1` "with the six
field-level defects fixed". The canonical list is `ref/PROMPT-python-bill-poster.md:165-234`
(§§4.1–4.6). Reading the PowerShell in full against the Python:

| defect | present in `load_janapr_bills.ps1`? | Python status |
|---|---|---|
| **4.1** GST rate: read it, never divide | **YES** — `ps1:128` `$gstPct = round(($tax/$taxable)*100, 2)`, one blended rate per **bill**, stamped on every line | **REAL FIX.** `line_rate:1347` + slab gate. The only surviving division is the RCM path (`:1434`), which snaps and refuses beyond 0.05. `DATABASE VERIFIED` clean on all 23,322 forward-charge lines |
| **4.2** description = `APIBD.TEXTDESC` | **NO** — `ps1:158-160` already does `if($e.descr){$e.descr}else{$pr.name}`, with a comment explaining why | inherited, not fixed. The defect belonged to the *earlier* `load_expense_bills.ps1` which `ps1:12` supersedes |
| **4.3** explicit `isRcmEnabled` from Sage's booking | **NO** — `ps1:108,116,156` already decides from the `1L8TX` distribution and sends an explicit bool | inherited. **And it is NOT applied on the goods path**: `classify_goods:1825` hardcodes `"is_rcm": False` and `:1733` merely filters `1L8TX*` heads out |
| **4.4** RCM gross-up + per-line/per-bill assertions | amounts: **NO** (`ps1:117-127` already correct). Assertions: **YES** (none exist) | **PARTIAL.** `assert_invariants:2718` is new and its per-line checks are genuine, but its headline check (`:2747`) is `X vs X` (see §E.9), and the canonical `billAmount == Σ totalPrice` invariant is never asserted |
| **4.5** round-off is a bill field, not a line | **NO** — `ps1:185` implements the canonical prescription exactly | **DELIBERATELY INVERTED.** `:2695-2696` sends `roundOffAmount: 0.0, hasRoundOff: False` and emits the round-off as a 0% CHARGE line — the specific thing `ref/PROMPT-python-bill-poster.md:231-232` forbids. The reasoning is measured and recorded (`:1477-1482`) and the *outcome* is better, but the README calls it "fixed" and `:93` still carries the contradicting old annotation |
| **4.6** send the line ledger explicitly | **NO** — `ps1:161-164` already sends `financeAccountDto`, with the same reasoning | inherited, then hardened (`:2114-2116`, `:2535-2538`). `DATABASE VERIFIED`: 0 ACTIVE lines with a NULL `financeAccountId` |

**Verdict.** Six distinct `4.x` numbers do appear inline, so the count is literally defensible — but
**only 4.1 was a defect of `load_janapr_bills.ps1`.** Four were already correct in the PowerShell and
are inherited; one is implemented backwards on purpose. Separately, **`4.6` is used for two unrelated
things** — `:2585` (the canonical line ledger) and `:2652` (billType derivation, which is not in the
canonical list at all and is documented unnumbered at `:122-125`). The file annotates **seven**
defects, one mislabelled.

An honest rewrite of `README.md:7-9` would read: *a port of `load_janapr_bills.ps1` that fixes the
derived-GST-rate defect (4.1) it still carried, adds the amount assertions (4.4) it lacked,
deliberately reverses the round-off placement (4.5) after measuring server behaviour, adds a
seventh fix the PowerShell never attempted (billType from the account group), and carries forward its
already-correct handling of 4.2, 4.3 and 4.6.*

### H.2 Regressions the port introduced

`SCRIPT VERIFIED`. Behaviours the PowerShell had that the Python lost or weakened:

| # | regression | cite |
|---|---|---|
| R1 | **Per-document skip reasons gone.** `ps1:216` wrote `{doc, why}` per document; `:2983` writes only a `Counter`. Breaks the stated definition-of-done "skipped documents are listed with a reason". Partially recovered by `work/failure_report.py`, which re-derives them — and misses 44 (see H10) | `ps1:216` / `:2983` |
| R2 | **Per-document result file gone.** `ps1:215` wrote taxable/tax/billAmount per document; `phase_run` builds `results` and never writes it | `ps1:215` / `:3096` |
| R3 | `-Only` and `-RcmOnly` selectors dropped; there is no way to post one named invoice | `ps1:4,8,74-80` |
| R4 | **Line ordering is now non-deterministic** — `SQL_STAGING_LINES:369` has no `ORDER BY`, unlike `SQL_HEADERS:358` | `:369` |
| R5 | **Multi-part consolidation takes header scalars from part 1 only.** `:1419-1420` sums `gross`/`header_tax` across parts, but `classify:1393` takes `headers[0]` for the dates, tax group, `CNTBTCH` and `CNTITEM`. The other parts' batch/item numbers are lost from `metadata` | `:1393`, `:2701` |
| R6 | **Raw `IDINVC` is no longer recorded anywhere.** `ps1:165,187` stamped the raw invoice; `:2590,2698` stamp the base. This is what breaks the 4.2 acceptance query in §B.7 | `:2590` |
| R7 | Unhandled `KeyError` on zero-rated GL products (BUG-3 in file 02) | `:2604` |
| R8 | Goods path has no read-back verification | `:3720` |
| R9 | Goods path skips the URP hold that both `ps1:135-138` and `eligible:2773` apply | `:3611` |
| R10 | The `verify` phase is never invoked by `run_all.sh` — the only automated check that 4.1/4.2/4.6 have not regressed runs solely by hand | `run_all.sh:154-163` |

**Improvements over the PowerShell**, for balance: per-line rather than per-bill rates; real vendor
city and pincode instead of a hardcoded Bengaluru/560059 (`ps1:169`); org identity from `.env`;
`*N` consolidation; `UNVERIFIED` bills re-verified rather than skipped forever; a process-wide rate
gate, 401/500 discrimination and `LookupFailed` in the HTTP layer; a bill type derived from Sage's own
account groups; and `taxableOtherCharge` added.

---

## I. The loader's assumptions about the API, checked against the backend

Backend source: `/home/namansharma/Desktop/PROJECTS/smeassist`. `BACKEND VERIFIED` unless noted.

### I.1 Bill-number uniqueness — the loader's only defence against double-posting

`core/src/main/java/com/assist/core/bill/service/Impl/BillServiceImpl.java:611-633`:

```java
List<BillStatus> billStatuses = new ArrayList<>();
billStatuses.add(BillStatus.ACTIVE);
billStatuses.add(BillStatus.APPROVAL_PENDING);
billStatuses.add(BillStatus.BLOCKED);
...
return billRepository.existsByOrganisationIdAndBillNumberAndContactIdAndBillDateBetweenAndBillStatusIn(
        organisationId, billNumber, contactId, startDate, endDate, billStatuses);
```

Called on create at `:703-709` and on update at `:913-919`, both throwing `"Bill number already
exists"`. Three consequences:

1. **The scope really is org + contact + financial year**, exactly as `post_sage_bills.py:3052` claims.
   Two different vendors may share a bill number — which is why 63 ACTIVE bills legitimately share one
   (`31.03.26` across nine telecom vendors).
2. **`REVOKED` is not in the status list**, so a revoked bill does not block a repost under the same
   number. This makes `work/repost_stranded_parts.py`'s revoke-then-recreate strategy sound (§G.5).
3. **`isDeleted` is not in the predicate at all.** A soft-deleted bill still carrying `ACTIVE` status
   would block a legitimate repost. Not observed in the data.

There is **no database unique index** behind this — `DATABASE VERIFIED`, `SHOW INDEX FROM bill`
returns only `PRIMARY`, `index_orgId`, `index_orgId_billType` and two FK indexes. The guard is
service-level only, which is why the crash window in §F.3 is real rather than theoretical.

### I.2 The series counter — the loader opts out of the safe path

`purchaseManagement/src/main/java/com/assist/purchaseManagement/service/impl/CounterServiceImpl.java:234-345`,
reached from `BillServiceImpl.java:716-747`:

```java
if (ObjectUtils.isBlankObject(counterValue)) {
    return updateCounterWithOutValue(...);   // read, increment, SAVE, server-side
} else {
    return updateCounterWithValue(...);      // take the CLIENT's number
}

// updateCounterWithValue, :311-330
if (presentCounterValueInt > counterValueInt) {
    log.info("update should not be possible");
    return ...withValue(counterValue)...;    // returns the client's number, does NOT save
}
counter.setValue(counterValue);              // equal or higher: accepted verbatim
```

Bill creation is wrapped in `@RedisLock(lockKey = BILL_CREATION_ORG_ID + organisationId)`
(`BillServiceImpl.java:635-640`), so `updateCounterWithOutValue` is **collision-free by construction**.

`series_number:2478-2491` sends an explicit `"value"`, taking the client-driven branch — where a stale
or equal number is accepted with nothing more than a log line. **This is the root cause of the 54
within-`SAGE` duplicate series values in §E.6**, and the fix is to send `"value": None` and let the
server allocate. `BACKEND VERIFIED / VERIFIED`.

### I.3 Does the backend trust the tax and totals the loader sends?

**No — it recomputes both.** Proven from the data rather than by reading the calculator, over every
ACTIVE bill in the org (`DATABASE VERIFIED / VERIFIED`, query and results in §E.9b):

```
bills 9,376 | gstAmount == SUM(line taxable x rate / 100)  9,376 / 9,376
            | billAmount == taxable + gst + roundOff       9,376 / 9,376
            | non-zero roundOffAmount                          10 (all pre-fix legacy bills)
```

The `gstAmount` the loader computes from Sage is discarded on arrival. This confirms the reasoning at
`post_sage_bills.py:1463-1474` and vindicates the deliberate inversion of defect 4.5 — with
`hasRoundOff: true` the server substitutes its own nearest-rupee figure, so a round-off can only reach
`billAmount` as a line. The `readback_drift` check exists precisely because of this and is the only
thing that can catch a divergence; **the goods path does not call it.**

### I.4 Endpoints the loader depends on, and what it learned the hard way

The following are recorded in the source as measured facts and are consistent with the backend and the
data. `SCRIPT VERIFIED / DOCUMENTED`:

| assumption | where | status |
|---|---|---|
| `POST /product/` persists `itemStatus`, **not** `status` — Jackson silently drops the latter | `:2078-2081` | consistent with 0 null-status products |
| `POST /bill/` persists `billLineItem.financeAccountId` **only** from `financeAccountDto`; verify never writes it back | `:2585-2589` | `DATABASE VERIFIED`: 0 ACTIVE lines with a NULL ledger |
| `getOrCreate/{referenceType}` **mints** a ledger per reference type, never remaps | `:1944-1946` | `DATABASE VERIFIED`: 138 products carry both a prior and a current ledger (C-2) |
| `/product/products?searchKey=X` does **not** filter — it returns the org's products unfiltered | `:1849-1866` | the cause of the 43% false-burn rate; replaced by a full one-pass index |
| `GET /bill/{id}` answers `200 / success:true / data:[]` for every bill; the real route is `GET /bill/detail/{id}` | `:2900-2910` | fixed; see §E.9 |
| `GET /address/pincode/{pin}` 404s on this build, so the platform derives nothing from the pincode and stores the state verbatim | `:515-530`, `:2141` | this is what makes `STATE_HEAD_PINCODE` safe |
| `GET /contact/address/{id}/CONTACT` does not exist — only `POST` is offered | `:2411-2418` | the reuse branch now falls back to the sibling crosswalk entry |
| an `INTERNATIONAL` contact needs top-level `country` **and** `currencies` (an array), read off `GET /v3/api-docs` | `:2382-2395` | |
| omitting `cessType` is a bare NPE at `BillServiceImpl:2062` | `:2572` | |
| omitting `contactCategory` makes the server call `findById(null)` and every contact create dies | `:89-92` | |

### I.5 One field the backend does silently drop: `metaData` on products

`DATABASE VERIFIED / VERIFIED`. The loader stamps `metaData` on bills, bill lines, contacts and
products. Three of the four are persisted; the product endpoint is not:

| table | rows for this org | metadata populated |
|---|---:|---:|
| `bill.metadata` | 10,769 | 10,769 |
| `billLineItem.metaData` | 36,459 | 36,459 |
| `contact.metaData` | 537 | 537 |
| **`product.meta`** | **17,886** | **11** |

**0 of the 1,419 `hsnCode='9999'` products carry any `meta`.** Every provenance field the loader
believes it is writing — `sageAccount`, `sageItem`, `sageItemFmt`, `sageCategory`, `sageUnit`,
`hsnSource`, `hsnMissing`, `hsnIsDefault`, `migrationSource` — is discarded. This is H-11 in file 02
and it confirms `FINDINGS-EXPENSE-SAC.md:39-55`, reported on 2 Sep and never acted on. It is also why
`work/master_recon.py` has to reconstruct the item mapping by re-reading Sage rather than reading it
back off the products.

**Still not verified here**: a DTO-field-by-DTO-field check of every other payload key, and whether the
backend enforces server-side validation the loader would trip on inputs it has not yet sent.

### I.6 Fields the backend silently drops

`BACKEND VERIFIED / VERIFIED`. Jackson's `FAIL_ON_UNKNOWN_PROPERTIES` is left at the Spring Boot
default of `false` — no `spring.jackson.*` config, no `ObjectMapper` bean, no converter override in
either repo. **Every unrecognised key is dropped silently; never a 400.** Five of the loader's keys
land nowhere:

| sent | endpoint | why it is dropped | consequence |
|---|---|---|---|
| `metaData` | `POST /product/` | the DTO field is **`meta`** (`ProductCreateUpdateDto.java:82`) | all product provenance lost — §I.5, H-11 |
| `isManageInventory` | `POST /product/` | **no such field** on `ProductCreateUpdateDto` (only on the unused `ProductCreateDto:30`) | inventory management not actually disabled |
| `isBulkUpload` | `POST /product/` | field is `private boolean isBulkUpload` → Lombok emits `isBulkUpload()`/`setBulkUpload()`, so the JSON property is **`bulkUpload`** | `ProductServiceImpl.java:209-226` therefore takes the `isFalse(isBulkUpload())` branch and runs the item-approval engine — a product can be saved `APPROVAL_PENDING`, not `ACTIVE` |
| `originCountry`, `destinationCountry` | `POST /bill/` | the fields are **`originCountryDto`/`destinationCountryDto`** (`CountryDto`) at `BillCreateDto.java:145,147` | every international bill stores `Bill.originCountry` and `destinationCountry` as **NULL** |
| `city`, `state`, `primaryAddress` | `POST /contact/address/create` | not fields on `OrgAddressMinUpsertDto` at all | the state is derived from the pincode instead — §E.5 |

Also overwritten rather than dropped: line `skuCode` (from the product master,
`BillLineItemConvertor.java:241,262`), line `taxableAmount`/`itemPrice`/`totalPrice` (recomputed,
`:201-239`), bill `gstAmount`/`billAmount`/`roundOffAmount` (`BillServiceImpl.java:2131,2195,2196`),
and `autoMapRemainingVoucher` (recomputed from an org property, `BillCreateConverter.java:42-45`).

**No bean validation runs on the bill or contact endpoints.** `BillController` and `ContactController`
carry no `@Valid`, and neither service class is `@Validated` with a `MethodValidationPostProcessor`
present — so `@NotNull`, `@NotBlank`, `@Email`, `@Pincode` on `BillCreateDto` and `ContactCreateDto`
are all inert. Every "required" field fails later as an NPE, a `CustomException` or a DB not-null
violation, and **all business rejections come back as HTTP 500** with an `errorMessage`
(`ExceptionHandlingController.java:65-133`) — which is exactly why `Api.call:872-876`'s rule of
retrying a 500 only when the message is empty is not merely a nicety but necessary.
`POST /product/` **does** carry `@Valid` (`ProductController.java:95`).

### I.7 HSN validation — `9999` is accepted, and nothing derives a rate from it

`BACKEND VERIFIED / VERIFIED`. `ProductServiceImpl.preCheckBeforeSaving:551-561` enforces only that the
HSN is present (unless `typeOfStock == RESOURCE`, the sole member of
`TYPE_OF_STOCK_WITH_NON_MANDATORY_HSN`, `TypeOfStock.java:72-73`) and that its **length is exactly 4, 6
or 8**, plus `@Size(min=2,max=8)` on the DTO. There is no master lookup and no numeric check —
`"ABCD"` would pass as readily as `9999`. `GOODS_HSN_DEFAULT = "9999"` (4) and
`EXPENSE_SAC = "996719"` (6) both pass, and `normalise_hsn:660`'s level-snapping to 4/6/8 exists
precisely because of this rule.

Critically, **no code path derives a tax rate from an HSN on the POST route.** The only HSN→rate lookup
(`BillCreateDtoService.getHsnInfoSafely:3096-3140`) serves the UI prefill endpoint. So the loader's
rate-first design survives contact with the server intact: `gstPercentage` is trusted verbatim and the
amount is recomputed from `qty × unitPrice`, which `assert_invariants` already ties to Sage.

### I.8 The six backend tickets were all reverted

`BACKEND VERIFIED / VERIFIED`. HEAD of the backend repo is
**`2785b4c3a3 "#12425324 - Do not merge: revert the Sage migration backend changes"`**, which reverted
all 32 files. `sage-migration-tickets.md` and `sage-migration-backend-changes.patch` are **untracked**.
Every symbol from the tickets greps empty in the live source.

| ticket | asked for | in live source? |
|---|---|---|
| 1 — bulk-upload sheets, `*1`/`*2` parent-bill matching | 20 modified + 6 new files | **ABSENT** |
| 2 — **rejected product create burns the SKU**: validate before `save`, add `GET /product/bySkuCode` | `preCheckCategoryAndSecondaryUnit` | **ABSENT, and not in the saved patch either** |
| 3 — **GST vendors cannot be created**: make the CIN/LLPIN lookup best-effort | `lookUpCinOrLlpinSilently` | **ABSENT, and not in the saved patch either** |
| 4 — purchase-note verify fails with a null error | 4 named errors | ABSENT |
| 5 — migrated purchase debit notes and GST: org flag, off by default, needs finance sign-off | new `EntityFeature` | **ABSENT** — migrated debit notes will take the standalone output-tax path |
| 6 — purchase-note charges always land in Direct Expenses | `getNoteItemReferenceTypes` | ABSENT |

**Tickets 2 and 3 are the two that directly unblock this loader, and they cannot be restored from the
saved patch** — it declares 20+6 files against a 32-file revert, and omits `ContactServiceImpl`,
`ProductServiceImpl`, `ProductController`, `EntityFeature`, `EntityName` and `VoucherCreationListener`.
They exist only in commit `2f70c19da3`.

Ticket 2's absence is the **live root cause of the burned SKUs**: `ProductServiceImpl.java:228` still
saves and *then* validates at `:231-235`, and `ProductRepository.java:73-74`'s SKU lookup **omits
`isDeleted = false`** (unlike every neighbouring query) — so a product that failed validation after
insertion keeps its SKU forever, unreachable through the `@Where(isDeleted=0)` API. That is precisely
the row "adoption cannot see" at `post_sage_bills.py:2100-2106`.

### I.9 Idempotency, per endpoint

`BACKEND VERIFIED / VERIFIED`. **No create endpoint has a database unique constraint** — every guard is
a service-level SELECT-then-INSERT.

| endpoint | guard | DB constraint | repeat POST |
|---|---|---|---|
| `POST /bill/` | `BillServiceImpl.java:701-709` | **none** (`Bill.java:48-49`, no `uniqueConstraints`) | rejected on org + contactId + FY of billDate + status ∈ {ACTIVE, APPROVAL_PENDING, BLOCKED} |
| `POST /product/` | `preCheckBeforeSaving:530-550` | **none** (`Product.java:39`) | rejected on `skuCode`; for `RESOURCE`, on `LOWER(productName)` instead |
| `POST /contact/` | `getExistingContactWithSameGstOrPan:1406-1462` | **none** (`Contact.java:28-34`, non-unique indexes) | rejected org-wide on `(registrationType, registrationNumber)` |
| `POST /taxation` | `TaxationServiceImpl.java:57-70,140` | none | **upsert** |
| `financeAccountReferenceMapping/…/getOrCreate` | `:236-244` early return | n/a | **idempotent per `(referenceType, referenceId)`** |

**Five ways the loader's crash-resume can still double-post:** after a revoke (REVOKED and
APPROVAL_REJECTED are outside the status list, and `@Where(isDeleted=0)` hides soft-deleted rows);
across the FY boundary (the check scopes to the FY of `billDate`, and this window straddles it); on a
Redis outage (the only serialisation is `@RedisLock` on `organisationId`); in parallel product creation
(6 workers, no lock, no index — and `item_key` is built from the *formatted* code while the SKU is
built from `item_raw`, so two distinct keys can produce one SKU); and if the org property
`DUPLICATE_GST_ALLOWED` is set, in which case the loader's reuse-by-GSTIN branch never fires.

**And one silent loss in the other direction.** Several Sage vendor codes legitimately share one GSTIN
and the loader maps them onto one contact (`:2404-2434`). The server's dedup key is
`(org, billNumber, contactId, FY)`, so **two different Sage documents from sibling vendor codes with
the same invoice number in the same FY collapse to one server key** — the second is rejected as
`"Bill number already exists"` and recorded as `"preexisting"` (H-1), silently dropping a real
document. `BACKEND + SCRIPT VERIFIED / HIGH`.

### I.10 Two silent status changes the loader does not observe

`BACKEND VERIFIED / HIGH`.
- `BillServiceImpl.java:804-817` — if an approval flow matches the bill type, `billStatus` is forced to
  **`APPROVAL_PENDING`**. `publishVoucherCreateRevokeEvent` requires `ACTIVE` (`:1390-1391`), and the
  same guard applies to `/verify` — so the loader logs `VERIFIED` for a bill with **zero accounting
  impact**. Only the aggregate `DOD[0]` query would catch it.
- `BillServiceImpl.java:829-843` — if the org lacks `LEDGER_VERIFICATION_ENABLED`, the bill is marked
  VERIFIED and the voucher is created at POST time; the loader's separate `/verify` is then a
  guarded no-op. Both paths work.

**Backdating**: `VoucherEntryServiceImpl.java:208-218` rejects a voucher predating the configured
posting-period start — **at verify, not at create**, so a backdated bill lands in the loader's
`UNVERIFIED` bucket. There is **no financial-year lock on bill create itself**, and no ticket asked for
one, which for a load straddling an FY boundary is itself worth noting.

---

## J. Artefact freshness — read these in this order, and distrust the rest

`ARTEFACT VERIFIED`. Several files in `work/` are stale enough to mislead an operator.

| artefact | mtime | status |
|---|---|---|
| `work/master-mismatches.json` | 09-05 14:41 | **current** — and carries the largest money finding (₹87.3 M, C-2) |
| `work/failures-report.json`, `reconcile-report.json`, `value-mismatches.json`, `po_items_cache.json` | 09-05 11:25 | current, with the caveats in §D.2 |
| `work/crosswalk_live.json`, `contacts_held.json` | 09-04 15:51 | current |
| `work/posted.log`, `rcm_variance.json` | 09-04 09:57 | current for AP; predates the last goods work |
| `work/skipped.json` | 09-03 17:42 | **stale** — predates 43 international contacts and the entire item master |
| `work/goods_failures.json` | 09-03 18:32 | **100% obsolete** — all 3,631 rows say "no item product for X"; every one of those items now exists in the crosswalk |
| `work/ERRORS-JanApr-2026.csv` | 09-02 15:34 | stale — its groups 2 and 4 were closed by the 2 Sep fixes |
| `work/loadable.json` | 09-02 10:12 | stale |
| `held_vendors.csv` (repo root) | 09-01 13:08 | **superseded** by `work/contacts_held.json` (380 rows vs 185) |
| `MACHINE-CHANGES.md` | 09-01 | declares itself a dated log; do not read its numbers as current state |

**Ranked by money, the three reports say different things and only one of them is the headline the
README points at:**

| report | headline | what it actually measures |
|---|---:|---|
| `failures-report.json` | **₹1,399,227,564** blocked | documents not posted — 98.2% of it one cause, "no contact built for vendor" (10,209 documents). Excludes 44 documents `eligible()` skips, and structurally excludes the 1,525 / ₹1,044 M of C-3 |
| `master-mismatches.json` | **₹87,293,633.44** | expense stranded on 50 superseded ledgers (C-2) — **already booked wrongly**, and the only one of the three that is a live misstatement |
| `value-mismatches.json` | ₹1,458,360.79 | per-document value drift, of which **99.97% is two documents** (`FABI470|1173/2025-26` posted ₹10,183.95 against Sage's ₹815,867.96; `FABI470|1177/2025-26` ₹327,021.66 against ₹979,295.31) — the `*N` receipt-part loss that `work/repost_stranded_parts.py` exists to repair and which, as of the 09-05 artefact, **is still unrepaired** because the script defaults to dry run |

**Throughput, measured.** Bill ids are snowflakes (`ts_ms = (id >> 22) + 1420070400000`), so
`posted.log` is timestamped after all. 9,371 bills over 363.7 active minutes = **25.8 bills/min**,
median 27, and **no solo run ever exceeded 29/min**. The single minute that reached 63 was a
*concurrent* one. `run_all.sh` has in fact **never run its own `post` phase** — no
`post-YYYYMMDD-HHMMSS.log` of its naming convention exists; all 9,371 bills came from the hand
wrappers (`run_janapr.sh`, `run_rest.sh`, `wait_then_run*.sh`, `run_goods_chain.sh`).
