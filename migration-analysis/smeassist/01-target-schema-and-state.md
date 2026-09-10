# SMEAssist Target: Schema and Current Migration State

**Agent 4 — SMEAssist Database Analyst.** Read-only audit. Every statement below is a `SELECT`;
no row in any database was created, altered or deleted, and no migration script was executed.

| | |
|---|---|
| **Target organisation** | `1029113552088076445` — WONDERBLUES APPARELS PRIVATE LIMITED |
| **Org status / PAN** | `UNVERIFIED` / `AADCW2868E`, org row created 2023-11-28, `orgSource=SA_USER` |
| **Org GSTIN used on bills** | `29XXXC*********` (Karnataka) |
| **Target schema** | `smeassist`, MySQL 8.0.46 on `$SME_DB_HOST` (285 tables) |
| **Tenant column** | `organisationId` — present on 273 of 285 tables |
| **Audited** | 2026-09-05 |

**Evidence labels** used throughout: **DATABASE VERIFIED** (I ran the SQL), **BACKEND VERIFIED**
(read from the JPA entity/service source at `/home/namansharma/Desktop/PROJECTS/smeassist`),
**SCRIPT VERIFIED** (read from the loader source), **INFERRED** (reasoned, not directly observed).
Confidence: **VERIFIED / HIGH / MEDIUM / LOW / UNKNOWN**.

---

## 0. Headline numbers

| Question | Answer | Label |
|---|---|---|
| Bills written by the migration | **10,769** (9,376 ACTIVE, 1,393 REVOKED) | DATABASE VERIFIED |
| Value of ACTIVE migrated bills | **₹498,776,137.99** | DATABASE VERIFIED |
| Sage AP-direct invoices in scope (Jan–Apr 2026) | **12,781**, worth **₹1,763,950,260.97** | DATABASE VERIFIED |
| Coverage | **72.3% by count, 28.0% by value** | DATABASE VERIFIED |
| Products written | **17,875** (of 17,886 in the org) | DATABASE VERIFIED |
| Contacts written | **537** (100% of the org's contacts) | DATABASE VERIFIED |
| Finance accounts (ledgers) written | **19,560** (of 19,794) | DATABASE VERIFIED |
| Voucher legs written | **66,108**; every voucher balances | DATABASE VERIFIED |
| **Declared foreign keys in `smeassist`** | **23, across 285 tables** — and none on `bill`, `product`, `contact`, `voucherEntry` or `financeAccount` | DATABASE VERIFIED |
| **Products carrying the `9999` placeholder HSN** | **1,419 — exactly the figure in the project notes** | DATABASE VERIFIED |

---

## 1. Schema overview by domain

### 1.1 Scale and tenancy

```sql
SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='smeassist';   -- 285
SELECT COUNT(*) FROM information_schema.columns
 WHERE table_schema='smeassist' AND column_name='organisationId';               -- 273
SELECT COUNT(*) FROM yoda.organisation;                                          -- 1,691,436
SELECT COUNT(DISTINCT organisationId) FROM smeassist.bill;                       -- 58
```

**DATABASE VERIFIED / VERIFIED.**

- `smeassist` has **285** tables; **273** carry `organisationId`, **12** do not.
- The `organisation` and `employee` tables **do not live in `smeassist`** — they are in the
  **`yoda`** schema, and the FKs that reference them are **cross-schema**
  (`referential_constraints.unique_constraint_schema='yoda'`). This matters: any tooling that
  dumps or restores `smeassist` alone loses the tenant and user dimension.
- `yoda.organisation` holds **1,691,436** rows. That is a marketplace-scale party table, not an
  ERP tenant list. Only **58** organisations actually hold bills.
- **The target org is the 9th largest by bill count** of those 58, holding 10,769 of 335,761
  bills across all orgs (**3.2%**).

### 1.2 In use vs unused — the distinction that matters

Exact `COUNT(*)` was run against all 285 tables, together with
`SUM(organisationId = <ORG>)`:

| Bucket | Tables | Meaning |
|---|---:|---|
| **In use for the target org** (org rows > 0) | **39** | the real working set |
| Populated globally but **zero rows for this org** | **214** | other tenants use them; this org does not |
| Globally empty (0 rows anywhere) | 20 | dead or not-yet-used features |
| No `organisationId` column | 12 | global reference/system tables |

**DATABASE VERIFIED / VERIFIED.** The working set is **39 tables of 285 — 14%.** Everything else
is noise for this migration.

### 1.3 The 39 in-use tables, by domain

| Domain | Table | Rows (all orgs) | Rows (target org) | Purpose |
|---|---|---:|---:|---|
| **Bills** | `bill` | 335,761 | **10,769** | purchase/expense bill header |
| | `billLineItem` | 699,216 | **36,459** | bill lines |
| | `billEntityMapping` | 364,667 | **10,769** | links a bill to its source document (PO/GRN/adhoc) + `mappedAmount` |
| **Ledger / accounting** | `voucherEntry` | 5,184,964 | **66,108** | the journal — one row per Dr/Cr leg |
| | `financeAccount` | 542,545 | **19,794** | **the chart of accounts** (tree, with live balances) |
| | `financeAccountReferenceMapping` | 701,886 | **19,676** | business-key → ledger lookup (productId → item ledger, GSTIN → party ledger) |
| | `voucherSearch` | 1,416,847 | **10,862** | denormalised read model, one row per voucher (listener-built) |
| | `voucherEntryMapping` | 697,155 | **5** | settlement/knock-off: which credit settled which debit |
| **Products / inventory** | `product` | 278,491 | **17,886** | item master |
| | `catalogCategory` | 108,993 | **29** | product categories |
| | `catalogCategoryAttribute` | 302,661 | **29** | category attributes |
| | `catalogGroup` | 33,627 | **1** | category grouping ("Sage Migration") |
| | `inventoryLineItem` | 104,808 | 1 | stock movement lines — effectively unused |
| | `batch`, `batchEntityMapping`, `batchPricingDetails`, `batchTransaction` | large | **1 each** | batch/lot tracking — effectively unused |
| | `zone`, `zoneProductMapping`, `plant` | — | **1 each** | warehouse topology — a single default |
| **Contacts** | `contact` | 116,096 | **537** | vendor/party master |
| | `contactInfo` | 134,919 | **537** | per-contact type/status/POC |
| | `contactBusinessInfo` | 115,163 | **537** | CIN, MSME, profile type |
| | `contactCategory` | 4,011 | 6 | contact categories |
| **Tax** | `taxation` | 163,225 | **515** | per-vendor TDS/TCS configuration |
| **Notes** | `creditDebitNote` | 53,332 | **96** | credit/debit note header — **all revoked** |
| | `noteLineItem` | 189,605 | **189** | note lines |
| **Config / numbering** | `counter` | 4,232 | **4** | document-number allocator |
| | `keyValue` | 3,482,387 | **1,082** | generic KV; allocates `financeAccount.code` |
| | `organisationProperty` / `organisationConfig` / `organisationFeature` | small | 16 / 1 / 1 | org settings |
| | `label` | 11,725 | 39 | UI labels |
| **Workflow / audit** | `entityStatusTracker` | 8,687,083 | **59,476** | append-only status history for any entity |
| | `approvalProcess` | 1,338 | 2 | approval config |
| **System** | `document` | 2,408,779 | 636 | attachments |
| | `role`, `role_backup`, `roleUserMapping` | — | 31 / 28 / 10 | RBAC |

**DATABASE VERIFIED / VERIFIED.**

### 1.4 Notable tables that are correctly empty for this org

- **`accountingLedger`** — 1,129,480 rows globally, **0 for the target org.** This is **not** a
  migration gap. **BACKEND VERIFIED / HIGH:** `AccountingLedger`
  (`core/.../accountingIntegration/ledgers/domain/AccountingLedger.java`) is a **read-only mirror
  of Tally's chart of accounts**, upserted nightly by
  `integration/saTally/.../TallyService.java` from an ODBC query
  (`SELECT $GUID, $Name, $Parent, $OpeningBalance, $_ClosingBalance`); `referenceId` is the Tally
  `$GUID`. Nothing posts to it. **The Sage migration should leave it empty.** The real chart of
  accounts is `financeAccount`.
- **`purchaseOrder` / GRN / receipt / payment tables** — the migration touches none of them
  (§2.9). No purchase-order or goods-receipt row was created for this org.

---

## 2. Table dossiers

### 2.1 `bill`

**Key / indexes.** PK `id` `char(20)` (application-generated, not auto-increment).
Indexes: `index_orgId(organisationId)`, `index_orgId_billType`, plus createdBy/lastModifiedBy.
**No unique constraint of any kind. No foreign key.**

**Notable columns**

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | char(20) | NO | — | app-generated id |
| `organisationId` | char(20) | **YES** | NULL | tenant key — *nullable*, see §5 |
| `billNumber` | varchar(255) | YES | NULL | **the vendor's own invoice number** (BACKEND VERIFIED) |
| `billSeriesPrefix/Value/Suffix/Number` | varchar(255) | YES | NULL | SMEAssist's *internal* document number from `counter` |
| `billType` | varchar(255) | NO | — | enum `BillType` |
| `billStatus` | varchar(255) | NO | — | enum `BillStatus` |
| `contactId` | varchar(255) | NO | — | → `contact.id` (undeclared) |
| `contactFinanceAccountId` | varchar(255) | YES | NULL | → `financeAccount.id`, the party credit leg |
| `companyBillingAddressId` / `contactBillingAddressId` | varchar(255) | NO | — | → `yoda.address.id` (undeclared, **cross-schema**) |
| `billAmount` / `taxableAmount` / `gstAmount` | decimal(19,6) | YES | NULL | money |
| `billDate` / `dueDate` | bigint | NO | — | epoch millis |
| `currency` | varchar(255) | YES | NULL | enum `Currency` (192 values) |
| `conversionRate` | decimal(19,6) | YES | NULL | FX |
| `purchaseType` | varchar(255) | YES | NULL | enum `CommerceType` — `DOMESTIC`/`INTERNATIONAL` |
| `entityLedgerVerificationStatus` | varchar(255) | YES | NULL | **the posting gate** — must be `VERIFIED` for vouchers to be written (BACKEND VERIFIED) |
| `metadata` | json | YES | NULL | free-form; the migration's provenance marker lands here |
| `isInterState` | bit(1) | NO | — | drives IGST vs CGST+SGST |

**Real relationships, proved by value-matching (DATABASE VERIFIED / VERIFIED):**

```sql
-- bill -> contact : 0 orphans
SELECT COUNT(*) FROM smeassist.bill b LEFT JOIN smeassist.contact c ON c.id=b.contactId
 WHERE b.organisationId=<ORG> AND c.id IS NULL;                                    -- 0
-- bill -> yoda.address (contact billing) : 0 orphans
SELECT COUNT(*) FROM smeassist.bill b
  LEFT JOIN yoda.address a ON a.id=b.contactBillingAddressId COLLATE utf8mb4_unicode_ci
 WHERE b.organisationId=<ORG> AND a.id IS NULL;                                    -- 0
-- bill -> billLineItem : every bill has lines
SELECT COUNT(*) FROM smeassist.bill b WHERE b.organisationId=<ORG>
 AND NOT EXISTS (SELECT 1 FROM smeassist.billLineItem li WHERE li.billId=b.id);    -- 0
```

Match rates: `bill.contactId` → `contact.id` **100.0%**; `bill.contactBillingAddressId` →
`yoda.address.id` **100.0%**; `bill.companyBillingAddressId` → `yoda.address.id` **100.0%**
(10,769/10,769); `billLineItem.billId` → `bill.id` **100.0%** (10,769 distinct billIds).

> **A cross-schema join needs an explicit `COLLATE`.** `smeassist` tables are
> `utf8mb4_0900_ai_ci`; `yoda.address` and `idedat_staging` are `utf8mb4_unicode_ci`. Joining
> them without a `COLLATE` clause raises `ERROR 1267 Illegal mix of collations`. Any reconciliation
> tooling must handle this.

**Migration has written into it: YES — 100% of the org's bills.** See §4.

---

### 2.2 `billLineItem`

PK `id`. Indexes `index_billId(billId)`, `idx_org_ref_stat_isdel`. **No FK, no unique constraint.**

Key columns: `billId` → `bill.id`; `productId` (NOT NULL) → `product.id`;
`financeAccountId` → `financeAccount.id` (the expense/purchase ledger for the line);
`referenceId` → the originating PO/delivery line (BACKEND VERIFIED); `skuCode` (NOT NULL),
`displayUnit` (NOT NULL), `lineItemType` (NOT NULL, enum `LineItemType`),
`status` (NOT NULL, enum `LineItemStatus`), `metaData` json, `hsn`, `quantity`, `unitPrice`,
`taxableAmount`, `totalPrice`, `gstPercentage`, `isRcmEnabled`.

**Integrity (DATABASE VERIFIED / VERIFIED):**

```sql
-- line sum vs header taxableAmount
SELECT COUNT(*) FROM (
 SELECT b.id, b.taxableAmount h, ROUND(SUM(li.taxableAmount),2) l
 FROM smeassist.bill b JOIN smeassist.billLineItem li ON li.billId=b.id AND li.isDeleted=0
 WHERE b.organisationId=<ORG> AND b.isDeleted=0 GROUP BY b.id, b.taxableAmount
 HAVING ABS(h-l)>0.05) x;                                                          -- 0
-- lines pointing at a non-existent product
SELECT COUNT(*) FROM smeassist.billLineItem li LEFT JOIN smeassist.product p ON p.id=li.productId
 WHERE li.organisationId=<ORG> AND p.id IS NULL;                                   -- 0
-- lines with no ledger
SELECT COUNT(*) FROM smeassist.billLineItem WHERE organisationId=<ORG>
 AND financeAccountId IS NULL;                                                     -- 0
```

**This is the cleanest part of the load.** Header totals reconcile to line sums on every one of
the 10,769 bills; every line resolves to a product and a ledger.

**Composition (DATABASE VERIFIED):** 36,459 lines — `CHARGE` **35,753 (98.1%)**, `GOODS` **706 (1.9%)**.
Status: `ACTIVE` 29,602, `INACTIVE` 6,857.

**`displayUnit` is `OTH` on 35,765 of 36,459 lines (98.1%)** — see §5.

**Migration has written into it: YES — 100%** (all 36,459 lines carry
`metaData.$.sageDoc`).

---

### 2.3 `product`

PK `id`. **`UNIQUE KEY orgSku (organisationId, skuCode)`** — the constraint the migration must not
violate. Two CHECK constraints:

```sql
CONSTRAINT `chk_unit_upper` CHECK ((`unitOfMeasurement` = upper(`unitOfMeasurement`)))
CONSTRAINT `inventoryProduct_hsnCode_check`
  CHECK (((`typeOfStock` in ('RESOURCE','CHARGE')) or (`hsnCode` is not null)))
```

NOT NULL with no default: `productName`, `unitOfMeasurement`, `dateCreated`, `isDeleted`,
`lastModified`. `categoryId` → `catalogCategory.id` (undeclared, BACKEND VERIFIED).

**Org profile (DATABASE VERIFIED / VERIFIED):**

```sql
SELECT COUNT(*) products, SUM(isDeleted) del, COUNT(DISTINCT skuCode) d_sku,
       SUM(skuCode LIKE 'SAGE-%') sage_sku, SUM(hsnCode='9999') hsn9999,
       SUM(hsnCode IS NULL) hsn_null, SUM(categoryId IS NULL) no_cat
FROM smeassist.product WHERE organisationId=<ORG>;
-- 17886 | 436 | 17886 | 17452 | 1419 | 2 | 247
```

- 17,886 products; **17,875 created by the migration**, 11 pre-existing (10 `SYSTEM`, 1 org owner).
- 17,452 carry the `SAGE-` SKU prefix; the other 434 migration products are **adopted** rows whose
  SKU predates the loader.
- `typeOfStock`: `RAW_MATERIAL` 11,912, `PACKAGING_ITEM` 5,152, `STORES_AND_SPARES` 336,
  `CHARGE` 173, `CONSUMABLES` 159, `SERVICE` 75, `RESOURCE` 73, `ASSET` 3, `PRODUCT` 2,
  `WORK_IN_PROGRESS` 1.
- `status` is `ACTIVE` on all 17,886.
- `unitOfMeasurement`: NOS 8,953 / BOX 2,449 / YDS 2,375 / MTR 2,065 / GRS 797 / ROL 534 /
  **OTH 245** / SET 160 / …

**Migration has written into it: YES — 17,875 of 17,886 (99.9%).**

---

### 2.4 `contact` and its satellites

`contact` PK `id`; indexes on `(organisationId)`,
`(organisationId, registrationType, registrationNumber)`, `(organisationId, companyName)`.
**No unique constraint** — identity uniqueness is enforced in application code only.
NOT NULL with no default: `companyName`, `registrationType`, `createdBy`, `lastModifiedBy`,
`organisationId`.

`contactInfo` (1:1, `contactId`), `contactBusinessInfo` (1:1, `contactId`),
`taxation` (`entityId` → `contact.id`, `entityType='VENDOR'`),
addresses live in **`yoda.address`** keyed by `partyId` → `contact.id`.

**Org profile (DATABASE VERIFIED / VERIFIED):** 537 contacts, all created by the migration,
88 soft-deleted.

| `registrationType` | count | deleted |
|---|---:|---:|
| `GST` | 297 | 2 |
| `WITHOUT_PAN_OR_GST` | 118 | 86 |
| `INTERNATIONAL` | 64 | 0 |
| `PAN` | 58 | 0 |

Satellites: `contactInfo` 537/537, `contactBusinessInfo` 537/537 (75 with a CIN),
`taxation` **515** — **22 contacts have no taxation row** (§5.6).
Addresses: **1,056 rows for 537 contacts** — 516 contacts have **two** (§5.5).

**Migration has written into it: YES — 100%.**

---

### 2.5 `financeAccount` — the chart of accounts

**BACKEND VERIFIED / HIGH:** this, not `accountingLedger`, is SMEAssist's real chart of accounts.
Self-referencing tree via `parentFinanceId` + `path`, rooted at name `FAROOT` / code `R00001`,
partitioned by `financeGroupType`. It carries **live running balances** (`creditAmount`,
`debitAmount`, `netBalance`, all NOT NULL) mutated in the same transaction as each voucher post,
and a `@Version` optimistic lock.

**Unique constraints the migration must not violate (DATABASE VERIFIED):**

```sql
UNIQUE KEY `index_organisationId_code` (`organisationId`,`code`)
UNIQUE KEY `index_organisationId_name` (`organisationId`,`name`)
```

`financeAccount.name` being unique per org is why the loader appends the Sage account code to
ledger names (`Freight Charges-Export_SAGE-4E4SD04 Expense`).

**Org profile (DATABASE VERIFIED / VERIFIED):** 19,794 rows, **19,560 created by the migration**,
234 soft-deleted.

| `financeGroupType` | rows | leaf |
|---|---:|---:|
| `EXPENSE` | 18,047 | 18,033 |
| `LIABILITIES` | 1,251 | 677 |
| `INCOME` | 421 | 415 |
| `ASSET` | 60 | 47 |
| `CAPITAL` | 14 | 1 |
| `OPENING_BALANCE` | 1 | 1 |

`partyType`: `SELF` 18,718, `VENDOR` 1,076. **Every one of the 538 vendor partyIds has exactly
2 VENDOR ledgers** — worth a look, though the backend may intend one per registration.

**Migration has written into it: YES — 98.8%.**

---

### 2.6 `voucherEntry` — the journal

PK `id`; 13 secondary indexes; **no FK, no unique constraint.**
`voucherId` groups the legs of one voucher (**not** an FK to any table — BACKEND VERIFIED).
`referenceType` + `referenceId` is a polymorphic pointer (`BILL` → `bill.id`).
`transactionType` stores `CREDIT`/`DEBIT` (**not** `Cr`/`Dr`).
NOT NULL with no default: `amount`, `financeAccountId`, `narration`, `remainingAmount`,
`transactionType`, `voucherDate`, `voucherId`, `voucherNumber`, `voucherType`, `partyType`,
`currency`, `conversionRate`.

**BACKEND VERIFIED / HIGH — how these rows come to exist:** the loader never writes a voucher.
`BillServiceImpl.publishVoucherCreateRevokeEvent` fires only when
`billStatus == ACTIVE && entityLedgerVerificationStatus == VERIFIED`;
`VoucherCreationListener` → `EntityVoucherEntryCreateHelperService.getVoucherCreateDtoOnBillCreation`
computes the Dr/Cr breakage → `VoucherEntryServiceImpl.createVoucherEntry` saves the legs,
updates `financeAccount` balances in the same transaction, and publishes
`VoucherSearchIndexEvent` which builds the `voucherSearch` row.

**Org profile (DATABASE VERIFIED / VERIFIED):**

| `voucherType` | legs | vouchers |
|---|---:|---:|
| `PURCHASE` | 46,903 | 7,615 |
| `INDIRECT_EXPENSE` | 16,398 | 2,626 |
| `EXPENSE` | 2,515 | 525 |
| `CREDIT_NOTE` | 155 | 46 |
| `DEBIT_NOTE` | 137 | 50 |

`referenceType`: `BILL` 65,816 legs / 10,766 distinct bills; `NOTE` 292 legs / 96 notes.

**The books balance. (DATABASE VERIFIED / VERIFIED)**

```sql
-- per-voucher imbalance
SELECT COUNT(*) FROM (
 SELECT referenceId, ROUND(SUM(CASE WHEN transactionType='DEBIT' THEN amount ELSE -amount END),2) d
 FROM smeassist.voucherEntry WHERE organisationId=<ORG> AND isDeleted=0
 GROUP BY referenceId HAVING ABS(d)>0.01) x;                                       -- 0
SELECT ROUND(SUM(amount),2) FROM smeassist.voucherEntry
 WHERE organisationId=<ORG> AND isDeleted=0 AND transactionType='DEBIT';   -- 498,772,451.61
SELECT ROUND(SUM(amount),2) FROM smeassist.voucherEntry
 WHERE organisationId=<ORG> AND isDeleted=0 AND transactionType='CREDIT';  -- 498,772,451.61
```

Live debit = live credit = **₹498,772,451.61**, exactly. Zero unbalanced vouchers.

**Migration has written into it: YES — indirectly, via `POST /bill/{id}/verify`.**

---

### 2.7 `counter` — document numbering

**BACKEND VERIFIED / HIGH:** a per-org, per-`counterType`, per-series, per-FY, per-registration
next-number allocator. `value` is a **zero-padded string** whose width is preserved on increment;
`@Version` makes concurrent allocation safe. The declared unique index
`index_counterName_organisationId (counterType, series, value, suffix)` **includes `value`, so it
does not enforce one row per series** — which is precisely the defect observed below.

**Org state (DATABASE VERIFIED / VERIFIED): 4 rows where there should be 2.**

| id | series | value | FY | isDeleted |
|---|---|---:|---|---:|
| 1543832975367503872 | `SAGE27` | 330 | 2026-2027 | **1** |
| 1543833233581441024 | `SAGE` | 1194 | 2025-2026 | **1** |
| 1544554119959707648 | `SAGE` | 7966 | 2025-2026 | 0 |
| 1544554120421081088 | `SAGE27` | 1359 | 2026-2027 | 0 |

The soft-deleted pair issued numbers 1…1194 (`SAGE`) and 1…330 (`SAGE27`); the live pair
restarted from 1 and re-issued the same values. **That is the direct cause of the duplicate
document numbers in §5.1.**

---

### 2.8 `taxation`, `creditDebitNote`, `noteLineItem`

- **`taxation`** — 515 rows, all `taxType='NONE'`, `entityType='VENDOR'`, 0 deleted.
  22 contacts have none (§5.6).
- **`creditDebitNote`** — 96 rows for the org, **every one `noteStatus='REVOKED'` and
  `isDeleted=1`**: `CREDIT_NOTE` 46 (₹9,357,923.58), `DEBIT_NOTE` 50 (₹3,390,121.73).
  The note population was loaded and then rolled back in full.
- **`noteLineItem`** — 189 rows across the same 96 notes.

**SCRIPT VERIFIED / HIGH:** `extract.sql` filters bills to `IDTRXTYPE = 12`, so credit/debit notes
(`22`/`32`) are structurally out of the *bill* scope; the 96 notes came from the separate
`notes_header`/`notes_lines` queries and were later revoked.

---

### 2.9 Purchase order, GRN/receipt, payment — **not written**

**SCRIPT VERIFIED + DATABASE VERIFIED / HIGH.** The loader calls no purchase-order, goods-receipt,
delivery or payment endpoint. Sage POs are *read* (`POPORH1`/`POPORL`) only to enrich bill lines
with quantity and unit price. Correspondingly, `billEntityMapping.entityType` is
**`ADHOC_PURCHASE_BILL` on all 10,769 rows** — no bill is linked to a PO or a GRN.

> **BACKEND VERIFIED / HIGH — latent risk:** `EntityVoucherEntryCreateHelperService.getVoucherTypeByBillDto`
> performs an unguarded `billDto.getBillEntityMappingDtos().get(0)`. A bill with **zero**
> `billEntityMapping` rows throws on posting. Every migrated bill has exactly one, so this is
> currently safe — but it is a hard dependency nothing declares.

---

## 3. Constraints, uniqueness and enum value sets

### 3.1 Declared foreign keys — the plain answer

```sql
SELECT COUNT(*) FROM information_schema.referential_constraints
 WHERE constraint_schema='smeassist';                                              -- 23
SELECT constraint_type, COUNT(*) FROM information_schema.table_constraints
 WHERE table_schema='smeassist' GROUP BY constraint_type;
-- PRIMARY KEY 282 | UNIQUE 62 | FOREIGN KEY 23 | CHECK 16
```

**DATABASE VERIFIED / VERIFIED. There are 23 declared foreign keys across 285 tables, and they
cover only 7 tables:** `batch`, `approvalProcess`, `approvalProcessEntityMapping`,
`approvalStatusInfo`, `measurementUnit`, `bankIfscDetails`, `misGroup`/`misGroupLedgerMapping`.
Every one is `ON UPDATE NO ACTION / ON DELETE NO ACTION`. Six of them point **across schemas** into
`yoda.organisation` / `yoda.employee`.

**Not one FK exists on `bill`, `billLineItem`, `billEntityMapping`, `product`, `contact`,
`contactInfo`, `contactBusinessInfo`, `taxation`, `financeAccount`,
`financeAccountReferenceMapping`, `voucherEntry`, `voucherEntryMapping`, `creditDebitNote`,
`noteLineItem` or `counter`** — i.e. on none of the tables this migration writes.

**BACKEND VERIFIED / HIGH — why:** there is not a single `@ManyToOne`/`@JoinColumn` in any of the
27 entities examined. Every relationship is a bare `String` id resolved in service code.

**What this means for the migration.** Referential integrity is *entirely* a property of the
loader's correctness. Nothing in the database will refuse a bill line pointing at a deleted
product, a bill pointing at another org's contact, or a voucher leg pointing at a ledger that no
longer exists. Equally, nothing cascades: deleting a contact leaves its bills, ledgers and vouchers
in place. **The good news, measured, is that the loader got this right** — every orphan check in
§2 returned 0. But that is an observed property of one run, not a guarantee.

Also: **3 of 285 tables have no PRIMARY KEY** (282 PKs). And note `bill.organisationId` is
**nullable** — the tenant key on the single most important table is not enforced NOT NULL.

### 3.2 Unique constraints the migration must respect

| Table | Constraint | Status for this org |
|---|---|---|
| `product` | `UNIQUE (organisationId, skuCode)` | **holds** — 17,886 rows, 17,886 distinct SKUs |
| `financeAccount` | `UNIQUE (organisationId, code)` | holds |
| `financeAccount` | `UNIQUE (organisationId, name)` | holds |
| `counter` | `UNIQUE (counterType, series, value, suffix)` | **does not prevent duplicate series** — 4 rows for 2 series |
| `keyValue` | `UNIQUE (organisationId, keyType, kv_key, kv_value, isDeleted)` | holds |
| `bill` | **none** | `billNumber` is **not** unique; uniqueness is enforced in code per `(organisationId, contactId, financialYear)` only |
| `contact` | **none** | identity uniqueness is code-only |

`(contactId, billNumber)` is de-facto unique for this org (10,769 distinct pairs / 10,769 rows) but
**`billNumber` alone is not** — 9,759 distinct values across 10,769 bills.

### 3.3 CHECK constraints

16 in the schema. The two that bind this migration are both on `product` (§2.3):
`chk_unit_upper` and `inventoryProduct_hsnCode_check`. The latter is why the loader must invent an
HSN rather than leave it null — and therefore why `9999` exists at all.

### 3.4 Enum value sets actually present, checked against the Java enums

**BACKEND VERIFIED / HIGH:** every `@Enumerated` in all 27 entities is `EnumType.STRING`. There is
no ORDINAL mapping anywhere. A misspelled value therefore fails loudly on read, not silently.

**DATABASE VERIFIED — the values actually present for this org, all valid:**

| Column | Distinct values present | Valid? |
|---|---|---|
| `bill.billStatus` | `ACTIVE` 9,376 · `REVOKED` 1,393 | ✅ of `ACTIVE, BLOCKED, APPROVAL_PENDING, APPROVAL_REJECTED, REVOKED` |
| `bill.billType` | `PURCHASE` 7,618 · `IN_DIRECT_EXPENSE` 2,626 · `DIRECT_EXPENSE` 525 | ✅ of 7 |
| `bill.purchaseType` | `DOMESTIC` 10,769 | ✅ of `DOMESTIC, INTERNATIONAL` — but see §5.3 |
| `bill.currency` | `INR` 10,769 | ✅ — but see §5.3 |
| `bill.billProcurementType` | `SERVICE` 7,195 · `MATERIAL` 423 · NULL 3,151 | ✅ of `MATERIAL, SERVICE, ASSET` |
| `bill.billReferenceType` | NULL (all) | — |
| `billLineItem.lineItemType` | `CHARGE` 35,753 · `GOODS` 706 | ✅ of 5 |
| `billLineItem.status` | `ACTIVE` 29,602 · `INACTIVE` 6,857 | ✅ of 2 |
| `billEntityMapping.entityType` | `ADHOC_PURCHASE_BILL` 10,769 | ✅ of 18 |
| `billEntityMapping.status` | `ACTIVE` 9,376 · `INACTIVE` 1,393 | ✅ of 2 |
| `product.status` | `ACTIVE` 17,886 | ✅ of 4 |
| `product.typeOfStock` | 10 values (§2.3) | ✅ all 10 valid |
| `contact.registrationType` | `GST`, `WITHOUT_PAN_OR_GST`, `INTERNATIONAL`, `PAN` | ✅ — all 4 constants used |
| `contactInfo.contactType` / `.status` | `VENDOR` / `ACTIVE` | ✅ |
| `financeAccount.financeGroupType` | 6 values | ✅ all 6 |
| `financeAccount.partyType` | `SELF`, `VENDOR` | ✅ of 9 |
| `voucherEntry.voucherType` | `PURCHASE`, `INDIRECT_EXPENSE`, `EXPENSE`, `CREDIT_NOTE`, `DEBIT_NOTE` | ✅ of 41 |
| `voucherEntry.referenceType` | `BILL`, `NOTE` | ✅ of 46 |
| `voucherEntry.transactionType` | `DEBIT` 51,305 · `CREDIT` 14,803 | ✅ of 2 |
| `voucherEntry.voucherEntryStatus` | `ACTIVE` | ✅ of 2 |
| `taxation.taxType` / `.entityType` | `NONE` / `VENDOR` | ✅ |
| `creditDebitNote.noteType` / `.noteStatus` | `CREDIT_NOTE`,`DEBIT_NOTE` / `REVOKED` | ✅ |
| `counter.counterType` / `.counterStatus` / `.associatedEntityType` | `BILL` / `ENABLED` / `PAN` | ✅ |
| `entityStatusTracker.entityType` | `ITEM_MASTER`, `BILL`, `CONTACT`, `NOTE` | ✅ (plain String column) |
| `entityStatusTracker.entityStatus` | `CREATED`, `ACTIVE`, `VERIFIED`, `REVOKED` | ✅ |
| `keyValue.keyType` | `FINANCE_ACCOUNT_CODE` 6 · `SHORT_ID` 1,076 | ✅ of 9 |

**No invalid enum value was found anywhere in the migrated data.** (DATABASE VERIFIED / VERIFIED)

The one non-enum free-text field that *is* inconsistent is `bill.metadata.$.sageRcm` — see §5.8.

### 3.5 NOT NULL with no default that a migrated row must supply

- **`bill`**: `dateCreated`, `isDeleted`, `lastModified`, `billDate`, `dueDate`, `billType`,
  `isInterState`, `contactId`, `billStatus`, `contactType`, `companyBillingAddressId`,
  `contactBillingAddressId`, `contactName`.
- **`billLineItem`**: `dateCreated`, `isDeleted`, `lastModified`, `createdBy`, `lastModifiedBy`,
  `organisationId`, `billId`, `displayUnit`, `lineItemType`, `productId`, `skuCode`, `status`.
- **`product`**: `dateCreated`, `isDeleted`, `lastModified`, `productName`, `unitOfMeasurement`.
- **`contact`**: `createdBy`, `lastModifiedBy`, `organisationId`, `companyName`, `registrationType`.
- **`financeAccount`**: `creditAmount`, `debitAmount`, `netBalance`, `financeGroupType`, `leaf`,
  `code`, `openingDate`, `partyId`, `partyType`, `path`, `currency`, `status`.
- **`voucherEntry`**: `amount`, `financeAccountId`, `narration`, `remainingAmount`,
  `transactionType`, `voucherDate`, `voucherId`, `voucherNumber`, `voucherType`, `partyType`,
  `currency`, `conversionRate`.
- **`counter`**: `counterType`, `series`, `value`, `counterStatus`, `associatedEntityType`.

**BACKEND VERIFIED / MEDIUM:** `@Version` columns exist on `financeAccount`, `voucherEntry`,
`voucherEntryMapping` and `counter`. Any *direct-SQL* insert must set them to `0` or every
subsequent update throws `OptimisticLockException`. This migration goes through the REST API, so
it is not exposed — but a future bulk-SQL repair would be.

---

## 4. Current migration state — what is already loaded

### 4.1 How to tell migration rows apart

**DATABASE VERIFIED / VERIFIED.** Four independent markers, all confirmed populated:

```sql
SELECT createdBy, COUNT(*), FROM_UNIXTIME(MIN(dateCreated)/1000), FROM_UNIXTIME(MAX(dateCreated)/1000)
FROM smeassist.bill WHERE organisationId=<ORG> GROUP BY createdBy;
-- 6099416406309215896 | 10769 | 2026-08-31 10:02:14 | 2026-09-04 09:57:21
```

1. **`createdBy = '6099416406309215896'`** — a single employee (`Vivek sethia`,
   `vivek.sethia@ofbusiness.in`, `yoda.employee`), the identity behind the loader's `SME_TOKEN`.
   **Every one of the org's 10,769 bills, 537 contacts and 19,560 finance accounts was created by
   this id**, between **2026-08-29 17:59** and **2026-09-04 15:51**.
2. **`bill.billSeriesPrefix IN ('SAGE','SAGE27')`** — `SAGE` 9,125, `SAGE27` 1,644; **10,769/10,769.**
3. **`bill.metadata.$.migrationSource = 'IDEDAT'`** — **10,769/10,769, zero NULL.**
   Same on `contact.metaData` — **537/537.**
4. **`billLineItem.metaData.$.sageDoc IS NOT NULL`** — **36,459/36,459.**

> **The org had no pre-existing bills at all.** 100% of the bill, contact and line-item population
> for this organisation is migration output. Only 11 of 17,886 products and 234 of 19,794 finance
> accounts predate it.

> **Finding — product provenance was silently dropped.** The loader sends a `metaData` object on
> `POST /product/` (sageItem, sageItemFmt, hsnSource, hsnIsDefault, migrationSource), but the
> `product` table's JSON column is named **`meta`**, and it is **NULL on all 17,875 migrated
> products** while 12,765 products in *other* orgs have it populated:
> ```sql
> SELECT meta, COUNT(*) FROM smeassist.product WHERE organisationId=<ORG> GROUP BY meta;
> -- NULL | 17875      {} | 11
> SELECT COUNT(*) FROM smeassist.product WHERE meta IS NOT NULL AND meta<>CAST('{}' AS JSON); -- 12765
> ```
> **DATABASE VERIFIED / VERIFIED.** Consequence: the `9999` placeholders and the HSN-resolution
> source are **not flagged in the database**. The only way to find them is `hsnCode='9999'` itself,
> and there is no way at all to tell a `line`-sourced HSN from an `ICITEMO`-sourced one after the
> fact. Products are also the one entity with **no `migrationSource` marker** — `skuCode LIKE 'SAGE-%'`
> catches 17,452 of 17,875 and misses the 434 adopted rows.

### 4.2 The real Sage source is `sage_ap_obl`, not `sage_bill_hdr`

**DATABASE VERIFIED / VERIFIED — this corrects a premise worth flagging.** The brief suggested
`idedat_staging.sage_bill_hdr` as the Sage-side proxy. It is the wrong population.

Matching the 9,378 `(vendor, invoice)` keys in `work/posted.log` against staging:

| Staging table | matched | rate |
|---|---:|---:|
| `sage_ap_obl (vendor_code, inv_number_raw)` | 9,370 / 9,378 | **99.9%** |
| `sage_bill_hdr (vendor_code, inv_number_base)` | 128 / 9,378 | 1.4% |
| `sage_bill_hdr (vendor_code, inv_number_raw)` | 121 / 9,378 | 1.3% |

Exactly one posted key matches nothing: `('OTHX003','ROADMAP EURO60')`.

**SCRIPT VERIFIED / HIGH — why.** `extract.sql` defines two disjoint populations:

```sql
-- AP-DIRECT (ADHOC) BILLS ... APOBL + APIBD, SRCEAPPL='AP'
WHERE IDTRXTYPE = 12          -- 12=invoice
  AND SRCEAPPL  = 'AP'        -- AP-direct only; 'PO' is the PO-matched population
  AND DATEINVC BETWEEN 20260101 AND 20260430
```

`sage_bill_hdr` is the **PO-matched (`SRCEAPPL='PO'`)** population — 17,914 of its 18,047 rows are
`srce_appl='PO'`. `sage_ap_obl` is the AP subledger and holds both.

### 4.3 Bills loaded, by status and value

```sql
SELECT billStatus, isDeleted+0 del, COUNT(*), ROUND(SUM(billAmount),2), ROUND(SUM(taxableAmount),2),
       ROUND(SUM(gstAmount),2)
FROM smeassist.bill WHERE organisationId=<ORG> GROUP BY billStatus, isDeleted;
```

| billStatus | isDeleted | count | billAmount | taxableAmount | gstAmount |
|---|---:|---:|---:|---:|---:|
| `ACTIVE` | 0 | **9,376** | **498,776,137.99** | 463,713,330.65 | 35,062,807.54 |
| `REVOKED` | 1 | 1,392 | 38,136,844.38 | 35,358,895.62 | 2,777,949.88 |
| `REVOKED` | 0 | **1** | 13,285.86 | 11,415.10 | 1,870.76 |

`billDate` spans exactly **2026-01-01 → 2026-04-30** — the whole population is inside the
migration window, none outside. (DATABASE VERIFIED / VERIFIED)

**`posted.log` reconciles perfectly to the database** (DATABASE VERIFIED / VERIFIED): 9,371 numeric
bill ids, **all 9,371 present in the org's bill table, all ACTIVE, none revoked**, and the invoice
number in the log equals `bill.billNumber` in **every** case. 5 ACTIVE bills are *not* in
`posted.log` (pilot/smoke-test residue); 7 entries are recorded `preexisting` with no bill id.

### 4.4 Coverage against Sage — the central number

```sql
SELECT COUNT(*), ROUND(SUM(amt_invc_hc),2) FROM idedat_staging.sage_ap_obl
 WHERE trx_type=12 AND srce_appl='AP' AND inv_date BETWEEN '20260101' AND '20260430';
-- 12781 | 1763950260.97
```

Joining that set to `posted.log` → ACTIVE bills:

| | invoices | Sage value (`amt_invc_hc`, ₹) |
|---|---:|---:|
| **Sage AP-direct, Jan–Apr 2026** | **12,781** | **1,763,950,260.97** |
| …with an ACTIVE SMEAssist bill | **9,243 (72.3%)** | **493,627,206.15 (28.0%)** |
| …**not loaded** | **3,538 (27.7%)** | **1,270,323,054.82 (72.0%)** |
| ACTIVE bills whose key is *not* in that set | 128 | — |

**DATABASE VERIFIED / VERIFIED. This is the single most important finding in this document.**
The migration has loaded **72% of the documents but only 28% of the money.** Mean loaded invoice
₹53,405; mean *unloaded* invoice **₹359,051** — the invoices that failed are on average **6.7×
larger** than the ones that succeeded. The gap is not a long tail of small items; it is concentrated
in the largest documents.

The project's own `work/reconcile-report.json` agrees independently:
`sage_documents_in_window: 27384` (12,781 AP-direct + 14,603 goods),
`smeassist_bills_active: 9376`, `in_sage_not_posted: 18006`, `extra_in_smeassist: 0`.

### 4.5 The PO-matched (goods) population is essentially unmigrated

```sql
SELECT SUM(has_item>0) bills_with_goods_lines, SUM(has_item=0) ap_only FROM (
 SELECT li.billId, SUM(JSON_EXTRACT(li.metaData,'$.sageItem') IS NOT NULL) has_item
 FROM smeassist.billLineItem li WHERE li.organisationId=<ORG> GROUP BY li.billId) x;
-- 266 | 10503
```

**DATABASE VERIFIED / VERIFIED.** Only **266 of 10,769 bills (2.5%)** carry any goods line, and
only **706 of 36,459 lines (1.9%)** are `lineItemType='GOODS'`. Against a Sage goods population of
**14,603 documents**, that is a coverage of roughly **1.8%**.

**Yet the item masters for that load were fully built**: 17,220 item products in the crosswalk,
17,886 products in the database — and **17,126 of them (95.7%) have never appeared on a bill line**:

```sql
SELECT COUNT(*) FROM smeassist.product p WHERE p.organisationId=<ORG> AND p.isDeleted=0
 AND NOT EXISTS (SELECT 1 FROM smeassist.billLineItem li
                  WHERE li.organisationId=<ORG> AND li.productId=p.id);            -- 17126
SELECT COUNT(*) FROM smeassist.financeAccount f
 WHERE f.organisationId=<ORG> AND f.leaf=1 AND f.isDeleted=0
 AND NOT EXISTS (SELECT 1 FROM smeassist.voucherEntry v
                  WHERE v.organisationId=<ORG> AND v.financeAccountId=f.id AND v.isDeleted=0);  -- 18347
```

**18,347 leaf ledgers have no voucher activity at all.** The masters phase completed; the goods
posting phase did not.

### 4.6 Ledger and journal rows produced

**DATABASE VERIFIED / VERIFIED.**

- **10,766** distinct bills have voucher legs, of **10,769**.
- **Only 2 ACTIVE bills have no live voucher legs** — `KA-B1-157425684`
  (ATRIA CONVERGNCE TECHNOLOGIES LIMITED, ₹1,709.82) and `103326913568`
  (APM TERMINALS INDIA PVT LTD - CHENNAI, ₹1,978.01). These are the `UNVERIFIED` bills: created,
  never verified, so the backend never wrote the voucher. **They show a payable with zero
  accounting impact.** `work/posted.log` carries exactly 7 `UNVERIFIED` markers; 5 have since been
  repaired.
- `entityStatusTracker` corroborates: `BILL` `CREATED` 10,769 vs `VERIFIED` **10,767**.
- **Zero unbalanced vouchers.** Live Dr = live Cr = **₹498,772,451.61**, which is
  ₹498,776,137.99 (ACTIVE bill total) minus the two unposted bills — reconciled.

### 4.7 Placeholders and degraded rows

| Placeholder | Literal | Count | Where | Label |
|---|---|---:|---|---|
| **Goods HSN** | `9999` | **1,419** | `product.hsnCode` | DATABASE VERIFIED / VERIFIED |
| Expense SAC | `996719` | on every `SAGE-<glaccount>` product | `product.hsnCode` | SCRIPT VERIFIED / HIGH |
| **UOM** | `OTH` | **245** products; **35,765 of 36,459 bill lines (98.1%)** | `product.unitOfMeasurement`, `billLineItem.displayUnit` | DATABASE VERIFIED / VERIFIED |
| Mobile | `9999999999` | **336 of 537 contacts (62.6%)** | `contactInfo.pocMobileNumbers` | DATABASE VERIFIED / VERIFIED |
| Foreign pincode | `999077` | **99 addresses** | `yoda.address.pin_code` | DATABASE VERIFIED / VERIFIED |
| No category | — | **247 products (231 live)** | `product.categoryId IS NULL` | DATABASE VERIFIED / VERIFIED |

**The `9999` figure in the project notes is confirmed exactly:**

```sql
SELECT COUNT(*) FROM smeassist.product WHERE organisationId=<ORG> AND hsnCode='9999';  -- 1419
```

**SCRIPT VERIFIED / HIGH — provenance of the number.** `run_all.sh` records the cause verbatim:

> *"This used to print a warning and carry on. Carrying on is what wrote 1,419 products with the
> 9999 placeholder HSN: with Sage unreachable item_hsn_map() returns {}, resolve_item_hsn()
> silently skips its ICITEMO tier, and the run 'succeeds' while stamping a permanent
> misclassification that later runs skip."*

Only ~572 items genuinely lack an HSN anywhere in Sage. **The excess — roughly 850 products — is
an artefact of one run made while the Sage box was unreachable, not a property of the source
data.** Because later runs skip anything already in the crosswalk, this does not self-heal.

The most common real HSNs, for contrast: `54011000` 2,248 · `55081000` 1,196 · `48191010` 1,154 ·
`96062100` 796 · `5208` 676. `9999` is the **second most common HSN value in the org**.

---

## 5. Data problems in the target (reported, not fixed)

### 5.1 Duplicate document series numbers — **304 live bills**

```sql
SELECT COUNT(*) FROM smeassist.bill b WHERE b.organisationId=<ORG> AND b.billStatus='ACTIVE'
 AND b.billSeriesNumber IN (SELECT billSeriesNumber FROM smeassist.bill
   WHERE organisationId=<ORG> AND billStatus='ACTIVE' GROUP BY billSeriesNumber HAVING COUNT(*)>1);
-- 304
```

**DATABASE VERIFIED / VERIFIED. Severity: HIGH — this is a statutory numbering defect.**

- **1,481 duplicate `billSeriesNumber` groups covering 3,025 bills** overall.
- **152 groups / 304 bills are duplicated among *ACTIVE* bills** — i.e. two live, posted bills
  share one internal document number.
- **63 duplicate `voucherNumber` groups** among live vouchers.

Example: `SAGE2745`, `SAGE2746`, `SAGE2747`, `SAGE2748`, `SAGE2749` each appear on **3** bills
(2 ACTIVE + 1 REVOKED).

**Cause (DATABASE VERIFIED / HIGH):** the duplicate `counter` rows in §2.7. The soft-deleted
counters issued 1…1194 / 1…330 and the live ones restarted from 1. The declared unique index on
`counter` includes `value`, so the database could not prevent it. **SCRIPT VERIFIED:** the loader
detects duplicate counters but records that they *"are not deletable through the API."*

### 5.2 138 orphaned ledgers holding ₹8.73 crore, still receiving postings

```sql
-- 138 ids = crosswalk priorLedger values that are no longer any product's current ledger
SELECT COUNT(*), SUM(isDeleted), ROUND(SUM(netBalance),2), ROUND(SUM(debitAmount),2)
 FROM smeassist.financeAccount WHERE organisationId=<ORG> AND id IN (<138 ids>);
-- 138 | 0 | -87293633.44 | 87293633.44
SELECT COUNT(*) FROM smeassist.voucherEntry
 WHERE organisationId=<ORG> AND isDeleted=0 AND financeAccountId IN (<138 ids>);   -- 16827
```

**DATABASE VERIFIED / VERIFIED. Severity: HIGH.**

`work/crosswalk_live.json` holds **191** `priorLedger` ids; **53** are also some product's current
ledger, leaving **138 genuinely orphaned**. All 138 are **live** (`isDeleted=0`) and together hold
**₹87,293,633.44** of debit balance. **50 of them carry a non-zero balance**, and **16,827 live
voucher legs still post to them.**

**SCRIPT VERIFIED / HIGH — mechanism:** `financeAccountReferenceMapping/item/getOrCreate` *mints a
new ledger* per reference type; it does not re-head an existing one. When a product was re-mapped
(e.g. `ITEM_DIRECT_EXPENSE` → `ITEM_PURCHASE`), the old ledger stayed live with its balance and the
bills already posted against it. **The same Sage GL account's expense is therefore split across two
SMEAssist ledgers.**

Largest (masked to head only): `Freight Charges-Export_SAGE-4E4SD04 Expense` −₹21,651,747.35 ·
`Custom Clearance & Forwarding Charges_SAGE-4E…` −₹17,703,293.92 ·
`Professional Charges_SAGE-4E5O014 Expense` −₹16,549,052.00 ·
`Carriage Outwards_SAGE-4E4SD03 Expense` −₹9,626,522.05.

### 5.3 Every foreign-currency bill is stamped INR / DOMESTIC

```sql
SELECT currency, COUNT(*) FROM smeassist.bill WHERE organisationId=<ORG> GROUP BY currency;
-- INR | 10769
SELECT purchaseType, COUNT(*) FROM smeassist.bill WHERE organisationId=<ORG> GROUP BY purchaseType;
-- DOMESTIC | 10769
SELECT currency, COUNT(*), ROUND(SUM(amt_invc_hc),2) FROM idedat_staging.sage_ap_obl
 WHERE trx_type=12 AND srce_appl='AP' AND inv_date BETWEEN '20260101' AND '20260430'
 GROUP BY currency;
-- INR 12738 (1,509,705,386.89) | USD 39 (251,043,281.12) | GBP 2 (1,916,143.20) | EUR 2 (1,285,449.76)
```

**DATABASE VERIFIED / VERIFIED. Severity: MEDIUM–HIGH.**

Sage's in-scope AP-direct population contains **43 non-INR invoices worth ₹254m in home currency
(14.4% of the scope's value)**. SMEAssist holds **zero** non-INR bills and **zero** bills marked
`INTERNATIONAL`, despite 64 contacts carrying `registrationType='INTERNATIONAL'`.

**SCRIPT VERIFIED / HIGH:** the loader always sends `currencyDto.currency = "INR"` and
`conversionRate = 1.0`, posting Sage's *home-currency* figure. That is arithmetically defensible —
the rupee amount is right — but **the document loses its original currency and its
`purchaseType`**, and `originCountry`/`destinationCountry` are never set. Any GST/customs or FX
reporting off this data will be wrong even though the totals are right. The wider Sage AP ledger
holds 3,242 non-INR obligations across USD, RMB, CNY, EUR, GBP, YEN, AED and HKD.

### 5.4 902 bills carry GST that Sage did not record — **RCM, and correct**

**DATABASE VERIFIED / VERIFIED. Severity: INFORMATIONAL — verified benign.**

Comparing 9,363 matched ACTIVE bills to `sage_ap_obl.amt_invc_hc`:

| | |
|---|---:|
| SMEAssist `billAmount` sum | **496,680,591.18** |
| Sage `amt_invc_hc` sum | **495,148,076.23** |
| difference | **+1,532,514.95 (+0.31%)** |
| bills differing by > ₹0.05 | **902** |

Every one of those 902 has `metadata.$.sageRcm = 'True'` and Sage `amt_tax_hc = 0`, and in each
case **SMEAssist's `taxableAmount` equals Sage's `amt_invc_hc` exactly**, with GST added on top:

| bill | Sage `amt_invc_hc` | SME `taxableAmount` | SME `gstAmount` | SME `billAmount` |
|---|---:|---:|---:|---:|
| `010/2025-26` | 129,150.00 | **129,150.00** | 23,247.00 | 152,397.00 |
| `1/31.01.26` | 79,656.00 | **79,656.00** | 14,338.08 | 93,994.08 |

This is **reverse-charge GST**, booked correctly: 1,994 bills carry `sageRcm='True'`, 902 of them
ACTIVE. The project's own reconciler classifies these as `amount_ok_via_rcm_taxable: 901` and
isolates **375 genuine mismatches** totalling ₹1,457,962 — of which two account for ₹1,457,957
(₹805,684.01 and ₹652,273.65) and the **median mismatch is ₹0.01**. So: two large real breaks, and
373 rounding artefacts.

### 5.5 Duplicate contact addresses — 516 of 537 contacts

```sql
SELECT n_addr, COUNT(*) FROM (
 SELECT c.id, COUNT(a.id) n_addr FROM smeassist.contact c
  LEFT JOIN yoda.address a ON a.partyId=c.id COLLATE utf8mb4_unicode_ci
 WHERE c.organisationId=<ORG> GROUP BY c.id) x GROUP BY n_addr;
-- 1 addr: 20 contacts | 2 addr: 516 contacts | 4 addr: 1 contact
```

**DATABASE VERIFIED / VERIFIED. Severity: MEDIUM.**

1,056 address rows for 537 contacts. The loader assumed the `addressDtoList` sent with
`POST /contact/` was *validated but not persisted*, and followed it with an explicit
`POST /contact/address/create`. **Both were persisted.**

Worse, the *primary* address is the un-resolved first one. Example (same contact):

| id | addressLine1 | city | pin_code | primaryAddress |
|---|---|---|---|---:|
| 1543849116148203520 | ROBERTSONPET | ROBERTSONPET | *(blank)* | **1** |
| 1543849120527056896 | ROBERTSONPET | Bengaluru | 560059 | 0 |

Bills are safe — **10,764 of 10,769 reference the non-primary, resolved address, and 0 bills point
at an address with a blank pincode** — but every UI screen and any future document will show the
degraded primary.

### 5.6 22 contacts with no taxation row

```sql
SELECT COUNT(*) FROM smeassist.contact c WHERE c.organisationId=<ORG>
 AND NOT EXISTS (SELECT 1 FROM smeassist.taxation t
   WHERE t.organisationId=<ORG> AND t.entityId=c.id AND t.entityType='VENDOR');    -- 22
```

**DATABASE VERIFIED / VERIFIED. Severity: MEDIUM.** 515 taxation rows for 537 contacts. The
missing 22 are predominantly `INTERNATIONAL` (e.g. `OCTANE5 INTERNATIONAL, LLC`, `P&H CORP`,
`STANCO WORLDWIDE LTD`) plus at least one GST vendor (`MAATHA INDUSTRIES`).
**SCRIPT VERIFIED:** the loader notes that *"Nothing else creates this row and its absence surfaces
much later, from the BILL module."*

### 5.7 One REVOKED bill that is not soft-deleted

```sql
SELECT billStatus, isDeleted+0, COUNT(*) FROM smeassist.bill WHERE organisationId=<ORG>
 GROUP BY billStatus, isDeleted;
-- REVOKED,1 -> 1392 | REVOKED,0 -> 1 | ACTIVE,0 -> 9377
```

**DATABASE VERIFIED / VERIFIED. Severity: LOW.** 1,393 bills are `REVOKED` but only 1,392 are
`isDeleted=1`. One revoked bill (₹13,285.86) remains visible to the application's
`@Where(clause="isDeleted=0")` filter. Note also the ACTIVE count reads 9,377 by this cut vs 9,376
by `billStatus` alone — the same row seen from the other side.

### 5.8 Inconsistent boolean encoding in `bill.metadata`

```sql
SELECT JSON_UNQUOTE(JSON_EXTRACT(metadata,'$.sageRcm')), COUNT(*)
 FROM smeassist.bill WHERE organisationId=<ORG> GROUP BY 1;
-- 'False' 8474 | 'True' 1994 | NULL 296 | 'NO' 5
```

**DATABASE VERIFIED / VERIFIED. Severity: LOW.** Four encodings for one boolean:
Python-stringified `True`/`False`, a literal `NO`, and NULL. Any consumer filtering on this must
handle all four.

### 5.9 Other counts, all clean

| Check | Result |
|---|---|
| Bills without line items | **0** |
| Line sums vs header `taxableAmount` | **0** mismatches |
| Bill lines pointing at a missing product | **0** |
| Bill lines with NULL `financeAccountId` | **0** |
| Bills pointing at a missing contact | **0** |
| Bills with an unresolvable billing address | **0** |
| Contacts with no address | **0** |
| Bills dated outside 2026-01-01…2026-04-30 | **0** |
| Rows that could not have come from Sage (`extra_in_smeassist`) | **0** |
| Duplicate GSTIN groups | 2 (shared-GSTIN vendors, deliberate) |
| Duplicate `companyName` groups | 66 |
| Products created but never activated | 12 |
| Invalid enum values anywhere | **0** |

---

## 6. The `idedat_staging` mirror and the crosswalk

### 6.1 Contents — exact counts

`information_schema.table_rows` is an InnoDB *estimate* and understates several of these badly.
Exact `COUNT(*)`:

| Table | **Exact** | (estimate) | Holds |
|---|---:|---:|---|
| `sage_ap_obl` | **53,296** | 36,187 | AP obligations — **the migration's real bill source**; all trx types, 2016-03-31→2026-09-30 |
| `sage_ap_obp` | **109,464** | 113,347 | AP payments applied to obligations |
| `sage_ap_dist` | **69,969** | 70,902 | AP distribution lines (GL account + amount) — the AP-direct bill lines |
| `sage_ap_dist_tax` | **69,969** | 58,493 | tax detail per distribution line (1:1) |
| `sage_ap_pomatch` | **29,748** | 29,045 | invoice ↔ PO match |
| `sage_ap_bank` | 217 | 217 | AP bank/payment codes |
| `sage_bill_hdr` | **18,047** | 17,701 | **PO-matched** invoice headers, Jan–Apr 2026 only |
| `sage_goods_line` | **32,586** | 31,964 | goods lines (item, uom, qty, cost, hsn) |
| `sage_service_line` | 2,563 | 2,563 | additional-cost/service lines |
| `sage_item` | **17,129** | 16,815 | item master |
| `sage_vendor` | 357 | 357 | vendor master |
| `sage_gl_acct` | 1,610 | 1,610 | GL chart of accounts |
| `sage_gl_post` | **846,098** | 1,005,114 | GL postings |
| `sage_gl_afs` | 3,222 | 3,222 | GL account fiscal-set balances |
| `sage_gl_hier` | 865 | 865 | GL parent/child hierarchy |
| `sage_gl_srce` | 64 | 64 | GL source-ledger codes |
| `crosswalk` | **0** | 0 | *(see §6.3)* |
| `sku_ledger` | **0** | 0 | *(see §6.3)* |
| `post_attempt` | 6 | 6 | *(see §6.3)* |
| `run_log` | 4 | 4 | *(see §6.3)* |
| `preflight_result` | 29 | 29 | 29 named preflight checks, run 2026-08-31 |

**DATABASE VERIFIED / VERIFIED.** Note the brief's stated figures were the estimates; the exact
counts differ by up to +47% (`sage_ap_obl`) and −16% (`sage_gl_post`).

### 6.2 Is it a faithful extract of Sage, or a transformed one?

**INFERRED / HIGH — it is a *broader, older* raw mirror, and it is NOT the artefact that
`extract.sql` produces.** Four independent pieces of evidence:

1. **Scope.** `extract.sql` filters `IDTRXTYPE = 12 AND SRCEAPPL = 'AP' AND DATEINVC BETWEEN
   20260101 AND 20260430`. `sage_ap_obl` in staging contains **all five trx types**
   (12: 44,666 · 32: 3,412 · 51: 2,673 · 50: 2,293 · 22: 252), **both** `srce_appl` values
   (AP 26,801 / PO 26,495), and spans **2016-03-31 → 2026-09-30**. Staging is *wider* than the
   extract, so it was not produced by these queries.
2. **Row-count disagreement with the extract's own documented expectations.** `extract.sql` states
   *"Expected: 11,256 header rows / 46,385 line rows."* Staging yields **12,781** AP-direct
   in-window headers (the extract adds an `EXISTS (SELECT 1 FROM APIBD …)` filter staging lacks)
   and **46,202** matching `sage_ap_dist` lines — 183 short of 46,385.
3. **Cleaning that `extract.sql` promises is absent.** The file states *"CR/LF/TAB stripping is
   KEPT."* Staging still contains them:
   ```sql
   SELECT SUM(description REGEXP '[\r\n\t]') FROM idedat_staging.sage_ap_dist;      -- 29
   SELECT SUM(inv_number_raw <> TRIM(inv_number_raw)) FROM idedat_staging.sage_ap_obl; -- 11
   ```
   (Preflight check 29, *"document numbers carrying leading or trailing whitespace"*, flags 154 as
   `BLOCK` — staging retains them.)
4. **It is nonetheless load-bearing.** **SCRIPT VERIFIED:** the loader reads it live —
   `idedat_staging.sage_ap_dist` for AP-direct lines (`post_sage_bills.py:1171`),
   `sage_bill_hdr` + `sage_goods_line` + `sage_item` for the goods path (`:1663`, `:1667`), and
   `phase_verify` joins `idedat_staging.sage_ap_dist` directly (`:3757`). `work/reconcile.py` uses
   `sage_ap_obl` + `sage_bill_hdr` as its Sage side.

**Conclusion: staging is a faithful *raw* mirror of the underlying Sage tables (untrimmed,
unfiltered, wider window) rather than a transformed one — but it is not in sync with the
`extract.sql` in this repository, and the two disagree on both scope and cleaning.** A reader who
assumes `extract.sql` describes what is in staging will get the wrong denominator.

### 6.3 The crosswalk has left the database — a continuity risk

**DATABASE VERIFIED + SCRIPT VERIFIED / VERIFIED. Confirmed exactly as suspected.**

```sql
SELECT COUNT(*) FROM idedat_staging.crosswalk;    -- 0
SELECT COUNT(*) FROM idedat_staging.sku_ledger;   -- 0
```

`idedat_staging.crosswalk` (`entity_type, source_key, target_id, target_code, channel, run_id,
created_at`) and `sku_ledger` are **empty**. The other two control tables are abandoned after a
single dry run:

- `run_log` — **4 rows**, all `run_id=55815593be27`, 2026-08-31 12:46:45, `contacts` 1/1 and
  `bills` 5/5.
- `post_attempt` — **6 rows**, same run, every `target_id` the literal string **`DRY-RUN`**.

Meanwhile `work/crosswalk_live.json` is **4.1 MB** and holds the entire live mapping:

| key | entries | maps |
|---|---:|---|
| `items` | **17,220** | `"<item>\|<UNIT>"` → `{productId, skuCode, ledger, name, unit}` |
| `contacts` | **455** | Sage vendor code → `{contactId, addressId, ledger, gstin, name, state, …}` |
| `products` | **197** | Sage GL account → `{productId, skuCode, ledger, billType, itemMapping, priorLedger}` |
| `burned` | **75** | key → a SKU claimed by a row the loader cannot see |
| `series` | **0** | dead key, never written |

**SCRIPT VERIFIED / HIGH:** the path is a compile-time constant with no environment override and no
database branch — `CROSSWALK = os.path.join(WORK, "crosswalk_live.json")`. A repo-wide grep finds
**zero** SQL references to a `crosswalk` table anywhere.

> **The risk, stated plainly.** The Sage→SMEAssist identity map for 17,220 products, 455 vendors
> and 197 GL accounts exists in **exactly one place: an untracked 4.1 MB JSON file on one
> developer's laptop**, under a directory whose only other copies are ad-hoc `.b4-*` / `.before-*`
> siblings. It is gitignored. If that file is lost, the mapping is only partially reconstructible —
> from `product.skuCode LIKE 'SAGE-%'` (which misses the 434 adopted products), from
> `contact.metaData.$.sageVendor`, and from `billLineItem.metaData.$.sageDoc`. It is **not**
> reconstructible for products at all, because `product.meta` is NULL (§4.1). The `priorLedger`
> values that identify the 138 orphaned ledgers in §5.2 exist **nowhere else**.
>
> Note also that **no bill mapping is stored in the crosswalk at all** — bill-level idempotency
> depends entirely on the append-only `work/posted.log`, a second untracked local file.

---

## 7. What I could NOT prove

1. **Why the unloaded 3,538 invoices failed.** I established *that* 27.7% of documents and 72% of
   value are missing and that the missing ones are 6.7× larger on average. I did not establish the
   cause distribution. `work/contacts_held.json` (185 held vendors, 157 for a missing CIN/LLPIN)
   and the 75 burned SKUs are the obvious candidates, but I did not join the held-vendor list to
   the unloaded invoices to attribute value per reason. **This is the highest-value next query.**
2. **Whether `sage_ap_obl.amt_invc_hc` is the correct comparison basis.** It reconciles to
   SMEAssist `billAmount` within 0.31% on the matched set, which is strong evidence — but I did not
   verify against Sage itself, only against the staging mirror, and §6.2 shows staging and
   `extract.sql` disagree.
3. **Whether the 1,393 REVOKED bills were revoked deliberately or are failed attempts awaiting
   repost.** `remarks='migration cleanup'` on 1,392 suggests deliberate, but I could not confirm
   they were subsequently reposted successfully.
4. **Whether the 138 orphaned ledgers double-count expense.** I proved 16,827 live vouchers post to
   them and they hold ₹8.73 crore. I did **not** prove whether the *same* Sage GL account also has
   postings on its new ledger — which would mean one account's expense is split, versus the old
   ledger simply holding all the pre-re-head history.
5. **Whether 2 VENDOR finance accounts per contact is correct.** 538 partyIds × 2 = 1,076. It may
   be by design (one per registration type) or a duplicate. The entity source did not settle it.
6. **The identity of the 128 ACTIVE bills whose key is not an AP-direct Jan–Apr invoice.** They are
   in Sage somewhere (they match `sage_ap_obl` overall) but fall outside the declared scope.
7. **Whether `product.meta` being NULL is a DTO-field-name mismatch or a backend policy.** I proved
   the column is NULL for all 17,875 migrated products and non-empty for 12,765 products elsewhere.
   I did not read the `POST /product/` request mapping to confirm the mechanism. Agent 3 owns this.
8. **Anything about the other 57 organisations.** By instruction, every content query was filtered
   to the target org.

---

## 8. Open questions for the human

1. **Is a 28%-of-value load the intended state, or is this run incomplete?** 9,376 bills worth
   ₹498.8m against a Sage scope of 12,781 worth ₹1,764m. If this is meant to be the final cutover,
   ₹1.27 billion of payables is missing.
2. **Was the PO-matched (goods) population ever meant to load?** 17,220 item products and ~18,000
   ledgers were built for it, but only 266 bills carry a goods line. 95.7% of the product master is
   unused. Either the goods phase must run, or ~17,000 products and ledgers are dead weight in a
   production chart of accounts.
3. **The 304 ACTIVE bills sharing a document series number must be resolved before any statutory
   filing.** Which of each pair is authoritative? The duplicate `counter` rows cannot be deleted
   through the API — does this need a DBA?
4. **The 138 orphaned ledgers holding ₹8.73 crore with 16,827 live postings.** Are these to be
   merged into their successors, or left as historical heads? This changes the trial balance.
5. **1,419 products carry `9999` as HSN, of which ~850 are an artefact of a blind run, not real
   missing data.** Should those ~850 be re-resolved against Sage before go-live? They will not
   self-heal — later runs skip anything already in the crosswalk.
6. **Should foreign-currency bills keep their currency?** All 43 in-scope non-INR invoices
   (₹254m, 14.4% of scope value) are stored as INR/DOMESTIC. The rupee totals are right; the
   documents are not.
7. **The crosswalk must be moved off one laptop.** `idedat_staging.crosswalk` exists and is empty.
   Either populate it, or commit/back up `work/crosswalk_live.json` and `work/posted.log`. The
   `priorLedger` values in it exist nowhere else in the world.
8. **Which is authoritative — `extract.sql` or `idedat_staging`?** They disagree on scope, on
   cleaning, and on row counts, and the loader reads staging while the documentation describes
   `extract.sql`.
9. **Two bills are posted with a payable and no accounting entry** (`KA-B1-157425684`,
   `103326913568`). Re-verify or revoke?
10. **22 contacts have no `taxation` row**, mostly international. The loader's own comment says the
    absence *"surfaces much later, from the BILL module."* Should these be backfilled now?
11. **`bill.organisationId` is nullable** on a multi-tenant table with no FK. Is that deliberate?

---

*End of report. All figures re-derivable from the SQL inline above. No data was modified.*
