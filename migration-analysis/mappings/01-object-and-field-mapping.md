# Sage → SMEAssist mapping

> **Provenance note.** A dedicated mapping agent was dispatched for this and was stopped
> before it reported. What follows is written by the lead from evidence already established
> and verified in the other reports. It is **sound but less exhaustive** than a dedicated
> pass would have been: the object map, the identity and amount rules, and the
> money-carrying fields are covered; a complete field-by-field enumeration of every DTO
> attribute is **not**. Gaps are marked `NOT COVERED`.

---

## 1. The two rules that govern every row below

Before any field mapping means anything, two structural facts constrain all of it. Both are
BACKEND VERIFIED.

**Rule 1 — no Sage identifier can be carried across.**
IDs are snowflake-generated (`EntityUniqueIdGenerator`, `CHAR(20)`); no create DTO exposes
`id`; and **there is no first-class external-reference column** on `bill`, `contact` or
`product`. `entitySourceMapping` exists but its `EntityType` enum is a closed set of internal
sales documents. So every Sage→SMEAssist relationship must be re-established through the
crosswalk, and provenance survives only in three fragile places:

| mechanism | where | weakness |
|---|---|---|
| `bill.metadata` JSON | all 10,769 bills | unindexed, non-unique; the **bulk** path hardcodes `withMetadata(null)` |
| SKU string prefix `SAGE-` | 17,452 products | user-editable |
| finance-account **display name** | 17,589 accounts | user-renameable — rename the ledger and the link is gone |
| `product.meta` | **NULL on all 17,450** | the loader sends `metaData`; the column is `meta`; it is dropped (`contradictions/02`) |

**Rule 2 — no Sage amount can be carried across.**
`BillServiceImpl.reCalculateAndValidateBillCreateDto` recomputes and **unconditionally
overwrites** (`:2195`, outside the `if`). The identity the server enforces:

```
line.itemPrice   := quantity × unitPrice
line.taxable     := itemPrice + taxableOtherCharge − discount
bill.gstAmount   := Σ (line.taxable × line.gstPercentage ÷ 100)
bill.billAmount  := Σ line.taxable + Σ cess + gstAmount + Σ lineTcs − discountAmount
                    + txsAmount   [then round-off]
```

Sage's `AMTINVCHC` / `AMTTAXHC` can only be **reproduced**, by choosing per-line `quantity`,
`unitPrice` and `gstPercentage` so the server's arithmetic lands on Sage's number. The
loader's author knew this (`post_sage_bills.py:1463-1474`: *"the only levers are per-line
taxableAmount and gstPercentage"*).

**Fields the server silently discards:** `billAmount`, `gstAmount`, `roundOffAmount`,
`itemPrice`, `cessAmount` (when `cessType=IN_PERCENTAGE`), `discountAmount` (when
`discountType=IN_PERCENTAGE`), and `metaData` on product. **Fields it trusts verbatim:**
`txsAmount` when `txsType=TDS`, `discountAmount` for any non-`IN_PERCENTAGE` type.

---

## 2. Object map, classified

The brief requires master / transaction / derived / opening-balance kept separate. They are
not interchangeable and the migration treats them very differently.

### MASTER data

| Sage | SMEAssist | Card. | Key on each side | Implemented? | Confidence |
|---|---|---|---|---|---|
| `APVEN` (vendor) | `contact` + `contactInfo` + `contactBusinessInfo` + `taxation` + address | N:1 | `IDVEND` → crosswalk → `contact.id` | **yes** — 537 built | VERIFIED |
| `ICITEM` / `ICITEMO` (item) | `product` (+ `catalogCategory`, + `financeAccountReferenceMapping`) | 1:N per `(item, unit)` | `ITEMNO` → SKU `SAGE-<item>-<unit>` | **yes** — 17,875 | VERIFIED |
| `GLAMF` (chart of accounts) | `financeAccount` | 1:N (**one Sage account → up to 86 ledgers**) | account code ↔ ledger *display name* | **partial** — pseudo-items only; COA never loaded | VERIFIED |
| tax rate (per line) | `financeAccount` per rate | 1:1 per distinct rate | rate string in the name | **yes**, and **unvalidated** — any rate mints an account | VERIFIED |
| UOM | `product.unitOfMeasurement` | N:1 | `UNIT_MAP` translation table | **yes** | SCRIPT VERIFIED |
| HSN/SAC | `product.hsnCode` | 1:1 | `ICITEMO.HSNCODE`, else category modal, else `9999` | **yes** — 1,419 on the placeholder | VERIFIED |
| currency | `bill.currency` | — | — | **no** — hardcoded INR @ 1.0 | VERIFIED |
| location / warehouse (23 in Sage) | — | — | — | **NO TARGET CONCEPT** | VERIFIED |
| 252 payables control accounts by category | one ledger **per party** | N:M | — | **structurally lost** | VERIFIED |

### TRANSACTION data

| Sage | SMEAssist | Card. | Key | Implemented? | Confidence |
|---|---|---|---|---|---|
| `APOBL` (AP-direct invoice, `IDTRXTYPE=12`, `SRCEAPPL='AP'`) | `bill` | 1:1 | `(IDVEND, IDINVC)` → `bill.metadata.sageDoc` | **yes** — 9,250 of 11,256 | VERIFIED |
| `APIBD` (distribution) | `billLineItem` | 1:N | `(CNTBTCH, CNTITEM, CNTLINE)` | **yes** | VERIFIED |
| `POINVH1` (PO invoice) | `bill` | **N:1** — `*N` parts merge | `inv_number_base` after stripping `*N` | **yes, barely** — 128 of 14,603 | VERIFIED |
| `POINVL` (goods line) | `billLineItem` | 1:N | `INVLSEQ` | yes | VERIFIED |
| `POPORH1/L` (purchase order) | purchase order tables | — | — | **NO** — not migrated | VERIFIED |
| `PORQNH1/L` (requisition) | — | — | — | **NO** — and 88% of POs start here | VERIFIED |
| `PORCPH1/L` (receipt / GRN) | GRN tables | — | — | **NO** — and stock moves *here* in Sage | VERIFIED |
| `APOBL` type 32 / 22 (credit / debit note) | `creditDebitNote` | 1:1 | — | **NO** — 0 live rows; 2,066 + 137 in window | VERIFIED |
| `APOBL` type 50 (prepayment) | — | — | — | **NO** — 1,729 in window, ₹1.55 bn | VERIFIED |
| `APOBP` (payment / settlement) | payment + allocation | — | — | **NO** — 0 rows; every bill shows unpaid | VERIFIED |
| `GLJEH` / `GLJED` (journal) | `voucherEntry` (manual JV) | — | — | **NO** — never extracted, mapped or posted | VERIFIED |
| `GLPOST` (posted GL) | — | — | — | **NO** — and loading it would double-count (§5) | DOCUMENTED |

### DERIVED (produced by the target, never mapped)

`voucherEntry` legs (66,108 rows), `voucherSearch`, `financeAccountAggregation`,
`billEntityMapping`, `counter` values, `financeAccount.netBalance`. **These must never be
written directly** — they are the server's output, and the audit confirms they are correct:
every voucher balances, live Dr = Cr = ₹498,772,451.61.

### OPENING BALANCE

| Sage | SMEAssist | Implemented? |
|---|---|---|
| `GLAFS.OPENBAL` + period nets (3,222 rows; trial balance ties — 249 accounts, ₹769.05 Cr, difference 0.0000) | `VIRTUAL_VOUCHER` / `OP_BL_` against the auto-created Opening Balance ledger | **NO — zero rows.** The platform supports it; it was not used. Sage's creditor heads stood at **₹535,122,264.66 Cr on 31 Dec 2025** |

---

## 3. Field-level mapping — the fields that carry money

Format per the brief: `Sage Table · Sage Field · SMEAssist Table · SMEAssist Field ·
Transformation · Evidence · Confidence`.

### 3.1 Bill header (AP-direct)

| Sage | field | SMEAssist | field | transformation | evidence | conf. |
|---|---|---|---|---|---|---|
| `APOBL` | `IDVEND` | `bill` | `contactId` | crosswalk lookup; **never copied** | SCRIPT+DB | VERIFIED |
| `APOBL` | `IDINVC` | `bill` | `billNumber` | `RTRIM` only — **never `LTRIM`**: Sage holds `' WPL/…'` and `'WPL/…'` as two obligations | SCRIPT | VERIFIED |
| `APOBL` | `DATEINVC` | `bill` | `billDate` | `YYYYMMDD` int → epoch ms | SCRIPT | VERIFIED |
| `APOBL` | `DATEINVCDU` | `bill` | `dueDate` | same | SCRIPT | VERIFIED |
| `APOBL` | `AMTINVCHC` | `bill` | `billAmount` | **home currency**; *reproduced*, not carried — server overwrites | BACKEND+DB | VERIFIED |
| `APOBL` | `AMTTAXHC` | `bill` | `gstAmount` | **zero on RCM documents** — never read there | SCRIPT+DB | VERIFIED |
| `APOBL` | `CODETAXGRP` | — | — | `LOCAL`/`INTERSTATE`; **does not drive the target's split** (§4) | DB | VERIFIED |
| `APOBL` | `FISCYR`/`FISCPER` | — | — | **no target field** — period-close unreproducible | DB | VERIFIED |
| — | — | `bill` | `billSeriesNumber` | `SAGE` (FY2025-26) / `SAGE27` (FY2026-27); **collides — see `risks`** | DB | VERIFIED |
| derived | GL account group | `bill` | `billType` | `account_bill_type()` from `GLAMF` groups | SCRIPT | HIGH |

### 3.2 Bill line

| Sage | field | SMEAssist | field | transformation | evidence | conf. |
|---|---|---|---|---|---|---|
| `APIBD` | `IDGLACCT` | `billLineItem` | `productId` | strip unit suffix after `-`; one CHARGE pseudo-product per natural account | SCRIPT | VERIFIED |
| `APIBD` | `AMTDIST` | `billLineItem` | `taxableAmount` | **pre-tax**; source currency (safe only because filtered to INR) | SCRIPT+DB | VERIFIED |
| `APIBD` | `TEXTDESC` | `billLineItem` | description | strip CR **and** LF **and** TAB — stripping only CR split 14 rows | SCRIPT | VERIFIED |
| `APIBD` | *(none)* | `billLineItem` | `quantity`, `unitPrice` | **AP-direct has no item**: `IDITEM` empty on all 178,592 FY2026 lines → qty 1, price = amount | SCRIPT+DB | VERIFIED |
| staging `sage_ap_dist` | `RATETAX1+2` | `billLineItem` | `gstPercentage` | **read, never inferred by division**; snapped to legal slab (max move 0.10pp) | SCRIPT+DB | VERIFIED |
| `APIBD` | `IDGLACCT ∈ 1L8TX14/15/16` | `billLineItem` | `isRcmEnabled` | **the only correct RCM test**; must be explicit true/false, never null | DB+DOC | VERIFIED |

### 3.3 Goods line — **where the currency defect lives**

| Sage / staging | field | SMEAssist | field | transformation | evidence | conf. |
|---|---|---|---|---|---|---|
| `sage_bill_hdr` | **`doc_total`** | `bill` | `billAmount` | **DEFECT — source currency posted as INR** (`risks/04`) | DB+SCRIPT | VERIFIED |
| `sage_bill_hdr` | `ap_amount_hc` | — | — | **the correct column, never read** | DB | VERIFIED |
| `sage_bill_hdr` | `hdr_discount` | `bill` | `discountAmount` | **never read** — hardcoded 0 (`risks/03`) | SCRIPT | VERIFIED |
| `sage_goods_line` | `discount` | `billLineItem` | `discount` | **never read** — hardcoded 0 | SCRIPT | VERIFIED |
| `sage_goods_line` | `qty`, `unit_cost` | `billLineItem` | `quantity`, `unitPrice` | real values (unlike AP-direct); also source currency | SCRIPT | VERIFIED |
| `sage_bill_hdr` | `inv_number_raw` | `bill` | `billNumber` | strip `*N`; **6,421 of 18,047 headers merge to 14,602 logical bills** | DB | VERIFIED |

### 3.4 Vendor → contact

| Sage | field | SMEAssist | field | transformation | evidence | conf. |
|---|---|---|---|---|---|---|
| `APVEN` | **`BRN`** | `contact` | `registrationNumber` (GSTIN) | **non-obvious column**; `TAXNBR`/`IDTAXREGI1` empty on all 4,752 | DB | VERIFIED |
| `APVEN` | `BRN[6]` | `contact` | `profileType` | 6th char = entity type; `C`/`F` require CIN/LLPIN **which is not in Sage** | DB | VERIFIED |
| `APVEN` | `CODESTTE` | address | `state` | **holds city names and foreign postcodes** — state derived from GSTIN prefix first | DOC+DB | VERIFIED |
| `APVEN` | `VENDNAME` | `contact` | `accountName` | RTRIM | SCRIPT | VERIFIED |
| — | — | `contact` | `state` | **accepted and silently discarded**; re-derived from GSTIN on read | BACKEND | VERIFIED |
| — | — | address | — | `addressDtoList` on `POST /contact` is **validated but not persisted** — separate call required | DOC+BACKEND | VERIFIED |
| Sage | *(nothing)* | — | bank details | **absent from Sage entirely** (IFSC value-shape scan: zero hits) | DB | VERIFIED |

---

## 4. Where the two models genuinely disagree

These are not mapping choices; they are places where a faithful mapping is impossible.

| # | Concept | Consequence |
|---|---|---|
| 1 | **Tax head is chosen by billing-address state, not GSTIN.** `OrgAddressMinUpsertDto` has no `state`; the voucher split reads a **pincode-derived** address | 4 bills book IGST against a matching-state GSTIN, ₹19,379.70. Sage's `CODETAXGRP` is ignored |
| 2 | **RCM**: Sage's payable *excludes* the tax; SMEAssist's `billAmount` *includes* it | `billAmount` is not the payable on 901 bills; summing it as "amount owed" overstates by ₹1,532,517.35 |
| 3 | **One Sage account → N ledgers** (up to 86) | Sage GL balances are not recoverable by account without aggregation |
| 4 | **Same account → two P&L groups** (Direct + Indirect) | 48 accounts split, ₹139,577,534.48 (`risks/01`) |
| 5 | **7 contacts absorb 2–14 Sage vendor codes** | per-vendor balances unrecoverable for those; 2 documents silently dropped |
| 6 | **No cost-centre dimension anywhere in SMEAssist** | unit-wise P&L cannot exist unless ledgers are split per unit |
| 7 | **No fiscal-period stamp** on `bill` or `voucherEntry` | period close and locking unreproducible |

---

## 5. `NOT COVERED` by this document

- Complete field enumeration of `BillCreateDto`, `ContactCreateDto`, `ProductCreateDto`
  (every attribute, nullability, validation). The bill DTO is enumerated in
  `backend/01-…§3.3`; contact and product are only partly covered.
- Credit/debit note and payment DTO mappings — the target concepts hold no live rows, so
  there was nothing to verify against.
- The bulk-upload sheet column mappings (the *other* mechanism) — documented in
  `~/Desktop/smeassist-bulk-templates/README.md` and not re-derived here.
- The GL-history double-count decision: per source module, **GL history OR the real entity,
  never both.** `AP IN`, `AP PY`, `AP PP`, `AP CR`, `PO RC` and `PO IN` must be excluded from
  any GL load because those documents migrate as entities. DOCUMENTED, not re-measured.
