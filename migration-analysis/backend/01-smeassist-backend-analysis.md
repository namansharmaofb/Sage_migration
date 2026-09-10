# SMEAssist Backend — Behavioural Analysis for the Sage 300 → SMEAssist Migration

**Agent 3 — SMEAssist Backend Analyst.** Read-only reverse-engineering audit.
**Date:** 2026-09-05
**Backend source:** `/home/namansharma/Desktop/PROJECTS/smeassist` @ `2785b4c3a3` (*"Do not merge: revert the Sage migration backend changes"*)
**Target org:** `1029113552088076445` (namespace `wonderblues`), org GSTIN `29xxxCxxxxxxxx` (Karnataka, state code 29), PAN `AADCW2868E`
**Deployed server:** `http://10.22.0.165:9069/api/v1` → `/mnt/smeassist-web.jar`
**Database:** `smeassist` on `10.22.0.165`, 285 tables

### Evidence labels used throughout

| Label | Meaning |
|---|---|
| **BACKEND VERIFIED** | Read directly in Java source in this checkout, with `file:line` |
| **RUNTIME VERIFIED** | Confirmed against the *deployed jar* or a live actuator endpoint |
| **DATABASE VERIFIED** | Confirmed by `SELECT` against the live `smeassist` MySQL |
| **SCRIPT VERIFIED** | Confirmed by reading `post_sage_bills.py` (the migration loader) |
| **DOCUMENTED** | Asserted by `sage-migration-tickets.md` / repo docs, not independently proven |
| **INFERRED** | Reasoned conclusion, stated as such |

Confidence: **VERIFIED / HIGH / MEDIUM / LOW / UNKNOWN**.
GSTINs are masked: state code + 6th character retained, e.g. `37xxxCxxxxxxxx`.

---

## 0. Executive summary — the six findings that matter

1. **Amounts are recalculated server-side and silently overwritten, never rejected.** `BillServiceImpl.reCalculateAndValidateBillCreateDto` recomputes item price, cess, GST and the bill total from `quantity × unitPrice × rate`, logs a warning to the log and to Google Chat on mismatch, and then **overwrites the caller's value**. Line 2195 sets `billAmount` to the server's total *unconditionally*. Sage's stated totals cannot be carried across except by making the arithmetic agree. **BACKEND VERIFIED / VERIFIED.**

2. **The deployed server does not contain the migration patch.** The running jar was built ~26 minutes *after* the revert commit and contains none of the patch symbols. Every defect in tickets 1–6 is live on the server the migration is posting to. **RUNTIME VERIFIED / VERIFIED.**

3. **The `.patch` file is not a complete record of what was reverted.** The revert touched 32 files; the patch contains 20. **Twelve files of reverted work exist in no patch** — including the two fixes the migration most needs (product SKU, contact CIN lookup) and the entire payment-allocation feature. **BACKEND VERIFIED / VERIFIED.**

4. **Document numbering is already broken in the live data: 3,025 bill rows share a colliding `billSeriesNumber`,** including ACTIVE-vs-ACTIVE pairs. Two independent causes, both proven from code and confirmed in data. **DATABASE VERIFIED + BACKEND VERIFIED / VERIFIED.**

5. **`@NotNull` on the bill and contact DTOs is decorative.** Neither controller passes `@Valid`, and no service class carries `@Validated`. Required-field enforcement is whatever imperative `if` happens to exist — plus several bare NPEs. **BACKEND VERIFIED / VERIFIED.**

6. **SMEAssist does not validate GST rates and auto-creates a ledger account per distinct rate it sees.** The live chart of accounts now contains ~45 illegal GST rates (4.76%, 4.99%, 5.01%, 2.29%, 6.71%, 9.01% …) created by this migration. Revoking the bills did not remove the accounts. **DATABASE VERIFIED / VERIFIED.**

---

## 1. Module & domain map

Multi-module Maven, 34 modules declared in `pom.xml:<modules>`. Java 8 source, Spring Boot 2.7.8, MySQL 8, Hibernate 5.6.14. **BACKEND VERIFIED / VERIFIED.**

### 1.1 Architectural shape

The layering does **not** follow module boundaries. Three rules explain almost everything:

- **`@RestController` lives in `web/`** — 231 controllers under `web/src/main/java/com/assist/web/controller/`, plus 42 admin-only ones in `adminWeb/`. Two exceptions: `approval` (`EmployeeGroupController`) and `integration/saTally` (3 controllers for the standalone Tally agent).
- **Service *interfaces* live in `commons/`, `*Impl` lives elsewhere** — usually in `core/` (214 impls) or the owning domain module. `commons` is therefore **not** a pure DTO jar: it also owns ~29 shared JPA entities.
- **`@Entity` lives in the owning domain module** under `.../domain`, never in `web`.

The practical consequence for this audit: tracing one operation crosses at least four modules. `POST /bill` is `web` → `commons` (interface + DTO) → `core` (impl) → `salesManagement` (entity + repository) → `ledger` (voucher) → `purchaseManagement` (counter).

### 1.2 Module ownership

| Module | Owns | Shape |
|---|---|---|
| `commons` | Shared DTOs, enums, **and** ~29 generic entities (`LineItem`, `EntitySourceMapping`, `ReferenceEntityMapping`, `Charge`, `BulkOperation`, `OrganisationProperty`, `OrganisationConfig`, `SecondaryUnitMapping`) | Hybrid — DTO jar + shared entities |
| `core` | Cross-module orchestration; nearly all bill/ledger/bulk/migration business logic. Few entities of its own | Service layer |
| `salesManagement` | **`Bill` and `BillLineItem` live here** despite `Bill` being the *purchase* invoice, plus `SalesOrder`, `DispatchOrder`, `DeliveryChallan`, `CommercialInvoice` | Entities + services |
| `purchaseManagement` | `PurchaseOrder`, `Delivery` (= GRN), **`Counter`**, `GrnToBillVariance`, `BillOfEntry`, `VendorQuote` | Entities + services |
| `ledger` (`ledger/core`, `ledger/commons`) | `FinanceAccount`, `VoucherEntry`, `VoucherEntryMapping`, `FinanceAccountReferenceMapping`, `VoucherSearch`, `ParkedVoucher` | Entities (core) + interfaces/DTOs (commons) |
| `contact` | `Contact`, `Taxation`, `BusinessPlaces` | Entities + services |
| `inventory` (`inventory-core`, `inventory-commons`) | `Product`, legacy `Category`, `BomItem`, `Plant`, batch/stock | Entities + services |
| `catalog` | `CatalogCategory` — **supersedes** the legacy `inventory` `Category` | Entities + services |
| `payment` | `PaymentRequest`, `PaymentRequestEntityMapping`, `PaymentRequestAdvanceBillMapping`, `Transaction`, `CardTransaction`, `Receipt` | Entities + services |
| `creditDebitNote` | `CreditDebitNote`, `NoteLineItem` | Entities + services |
| `expenditure` | `Expenditure`, `ExpenditureEntityMapping` | Entities + services |
| `approval` | Generic approval engine + 1 controller | Entities + services + controller |
| `financialManagement` | `GstWorking`/`ItcClassification`, `TdsWorking`, `TdsReconciliationSummary`, `SecurityDeposit` | Entities + services |
| `integration` (`ofb`, `yoda`, `saTally`, `eInvoiceAndEwayBill`, `loc`, `bidassist`) | External adapters. `saTally` has **zero JPA entities** — a pure JDBC bridge | Adapters |
| `assetManagement`, `bankReconciliation`, `borrowing`, `investment`, `lease`, `jobMonitor`, `announcement`, `dealer`, `label`, `notification`, `permission`, `analytics`, `materialPlanning`, `riskManagement`, `vehicleManagement`, `ai`, `fileServer`, `scheduler` | Their named domains | Entities + services |
| `web`, `adminWeb` | Controllers only, no entities | Controllers |

### 1.3 Things that are NOT in this repo

Critical for anyone trying to trace a value to its source. **BACKEND VERIFIED / HIGH.**

| Concept | Where it actually lives |
|---|---|
| **The `Organisation` entity itself** | External Maven artifact `com.ofb.assist.org:*`. Only `OrganisationConfig` / `OrganisationProperty` are local. |
| **`StateTax` / org GSTIN registrations** | External "Yoda" service. `integration/yoda/.../StateTaxInfoService.java:15` is a facade returning `com.assist.yoda.common.dto.StateTaxInfoDomainDto`. |
| **HSN/SAC master** | External Yoda service (`commons/.../HsnApiService.java`, `core/.../yoda/api/HsnApi.java`). Locally only a `Product.hsnCode` string. |
| **The snowflake ID generator** | External repo `/home/namansharma/yoda` → `com.assist.yoda.common.mysql.EntityUniqueIdGenerator`. |
| **`AbstractEntityDto`** | `/home/namansharma/yoda/libs/.../AbstractEntityDto.java` — and it has **no `id` field** (see §7). |

### 1.4 Dead / superseded code encountered

- `web/.../controller/inventory/CategoryController.java` — **entire file body commented out**. The live category surface is `CatalogCategoryController`. **BACKEND VERIFIED / HIGH.**
- `BillServiceImpl.java:2202-2208` — an unreachable validation (see §3.6).
- `BillServiceImpl.java:2209-2212` — a no-op check (see §3.6).
- `BillServiceImpl.java:760-763` — the "Bill amount can not be zero" guard is **commented out**, so zero-amount bills are accepted.

---

## 2. Concept index — entity / DTO / service / repository / controller

All paths relative to `/home/namansharma/Desktop/PROJECTS/smeassist`. **BACKEND VERIFIED / VERIFIED.**

| # | Concept | Entity (`file:line`) | Table | Create DTO | Service iface / impl | Repository | Controller |
|---|---|---|---|---|---|---|---|
| 1 | **Contact / Vendor** | `contact/src/main/java/com/assist/contact/domain/Contact.java:55` | `contact` | `commons/.../contacts/dto/ContactCreateDto.java:23` | `commons/.../contacts/service/ContactService.java:40` / `contact/.../service/Impl/ContactServiceImpl.java:127` | `contact/.../repository/ContactRepository.java:14` | `web/.../controller/contact/ContactController.java:94` |
| 2 | **Product / Item** | `inventory/inventory-core/.../domain/Product.java:58` | `product` | `commons/.../inventory/dto/ProductCreateUpdateDto.java:23` | `commons/.../inventory/commons/service/ProductService.java:32` / `inventory/inventory-core/.../service/impl/ProductServiceImpl.java:127` | `inventory/inventory-core/.../repository/ProductRepository.java:18` | `web/.../controller/inventory/ProductController.java:61` |
| 3 | **Bill (purchase invoice)** | `salesManagement/.../invoice/domain/Bill.java:52` | `bill` | `commons/.../invoice/dto/BillCreateDto.java:37` | `commons/.../service/BillService.java:49` / `core/.../bill/service/Impl/BillServiceImpl.java:220` | `salesManagement/.../invoice/repository/BillRepository.java:19` | `web/.../controller/inventory/BillController.java:77` |
| 4 | **Bill line item** | `salesManagement/.../invoice/domain/BillLineItem.java:32` | `billLineItem` | `commons/.../invoice/dto/BillLineItemCreateUpdateDto.java:26` | folded into `BillService` | folded into `BillRepository` | none (nested in bill) |
| 5 | **Purchase Order** | `purchaseManagement/.../domain/PurchaseOrder.java:79` | `purchaseOrder` | `commons/.../purchaseOrder/dtos/domestic/PurchaseOrderCreateDto.java:34` | `commons/.../rfq/service/purchaseOrder/PurchaseOrderBaseService.java:45` / `purchaseManagement/.../purchaseOrder/impl/PurchaseOrderBaseServiceImpl.java:173` | `purchaseManagement/.../repository/PurchaseOrderRepository.java:27` | `web/.../controller/rfq/PurchaseOrderController.java:78` |
| 5b | PO line item | `purchaseManagement/.../domain/PurchaseOrderLineItem.java:33` | `purchaseOrderLineItem` | — | — | — | — |
| 6 | **GRN / Receipt** | `purchaseManagement/.../delivery/domain/Delivery.java:42` — **there is no "GRN" entity**; inward receipt is `Delivery` with `DeliveryType.DELIVERY_IN` | `delivery` | `commons/.../delivery/dtos/DeliveryCreateDto.java:14` | `commons/.../service/DeliveryService.java:42` / `purchaseManagement/.../delivery/service/Impl/DeliveryServiceImpl.java:211` | `purchaseManagement/.../delivery/repository/DeliveryRepository.java:24` | `web/.../controller/delivery/DeliveryController.java:60` |
| 6b | GRN-vs-Bill variance | `purchaseManagement/.../grnToBillVariance/domain/GrnToBillVariance.java` | `grnToBillVariance` | — | — | — | `web/.../inventory/grnToBillVariance/GrnToBillVarianceController.java` |
| 7 | **Credit / Debit Note** | `creditDebitNote/.../domain/CreditDebitNote.java:67` | `creditDebitNote` | `commons/.../creditDebitNote/dto/CreditDebitNoteCreateDto.java:37` | `commons/.../creditDebitNote/service/CreditDebitNoteService.java:45` / `creditDebitNote/.../service/impl/CreditDebitNoteServiceImpl.java:172` | `creditDebitNote/.../repository/CreditDebitNoteRepository.java:25` | `web/.../controller/creditDebitNote/NoteController.java:79` |
| 7b | Note line item | `creditDebitNote/.../domain/NoteLineItem.java:31` | `noteLineItem` | — | — | — | — |
| 8 | **Payment / Payment Request** | `payment/.../domain/PaymentRequest.java:159` | `paymentRequest` | `commons/.../payment/dto/PaymentRequestCreateDto.java:17` | `commons/.../payment/service/PaymentRequestService.java:39` / `payment/.../service/Impl/PaymentRequestServiceImpl.java:179` | `payment/.../repository/PaymentRequestRepository.java:21` | `web/.../controller/payment/PaymentRequestController.java:91` |
| 9 | **Payment allocation** | `payment/.../domain/PaymentRequestEntityMapping.java:30` (generic) and `payment/.../domain/PaymentRequestAdvanceBillMapping.java:21` (advance-vs-bill) | `paymentRequestEntityMapping`, `paymentRequestAdvanceBillMapping` | nested `List<PaymentRequestEntityMappingDto>` on `PaymentRequestDto` | `commons/.../payment/service/PaymentRequestEntityMappingService.java:12` / `payment/.../Impl/PaymentRequestEntityMappingServiceImpl.java:24` | `payment/.../repository/PaymentRequestEntityMappingRepository.java:13` | **none dedicated** — processed inside `PaymentRequestServiceImpl.java:391-434` |
| 10 | **Journal Voucher** | **not a separate entity** — `VoucherType.JOURNAL` on `VoucherEntry` (`commons/.../enums/VoucherType.java:50`) | `voucherEntry` | `ledger/commons/.../voucherEntry/dtos/VoucherCreateDto.java:19` | `ledger/commons/.../voucherEntry/service/VoucherEntryService.java:31` / `ledger/core/.../Impl/VoucherEntryServiceImpl.java:115` | `ledger/core/.../voucherEntry/repository/VoucherEntryRepository.java:17` | `web/.../controller/ledger/VoucherController.java:66` |
| 11 | **Ledger / Finance Account** | `ledger/core/.../financeAccount/domain/FinanceAccount.java:52` | `financeAccount` | — | `ledger/core/.../financeAccount/service/Impl/FinanceAccountServiceImpl.java` | `ledger/core/.../financeAccount/repository/Impl/FinanceAccountRepositoryImpl.java` | `web/.../controller/ledger/LedgerController.java`, `web/.../accountingLedger/AccountingLedgerController.java:27` |
| 11b | Voucher entry | `ledger/core/.../voucherEntry/domain/VoucherEntry.java:49` | `voucherEntry` | — | — | — | — |
| 11c | Account→reference mapping | `ledger/core/.../financeAccountEntityReferenceType/domain/FinanceAccountReferenceMapping.java:23` | `financeAccountReferenceMapping` | — | — | — | — |
| 11d | Settlement mapping | `ledger/core/.../voucherEntryMapping/domain/VoucherEntryMapping.java:18` | `voucherEntryMapping` | — | — | — | — |
| 12 | **Counter** | `purchaseManagement/.../domain/Counter.java:38` | `counter` | none — `@RequestParam`s | `commons/.../rfq/service/CounterService.java:14` / `purchaseManagement/.../service/impl/CounterServiceImpl.java:48` | `purchaseManagement/.../repository/CounterRepository.java:12` | `web/.../controller/counter/CounterController.java:32` |
| 13 | **Organisation** | **external artifact `com.ofb.assist.org`** | — | — | — | — | `web/.../controller/org/OrganisationController.java` |
| 13b | Org properties | `commons/.../organisationProperties/domain/OrganisationProperty.java:25` | `organisationProperty` | — | `commons/.../service/OrganisationPropertyService.java:11` / `.../Impl/OrganisationPropertyServiceImpl.java:38` | `commons/.../repository/OrganisationPropertyRepository.java:11` | `web/.../common/OrganisationPropertyController.java:26` |
| 13c | Org config | `commons/.../organisationConfig/domain/OrganisationConfig.java:21` | `organisationConfig` | `.../dtos/OrganisationConfigCreateUpdateDto.java:11` | `.../service/OrganisationConfigService.java:10` / `.../Impl/OrganisationConfigServiceImpl.java:34` | `.../repository/OrganisationConfigRepository.java:10` | `web/.../common/OrganisationConfigController.java` |
| 14 | **Tax / GST** | **no single entity.** TDS config: `contact/src/main/java/com/assist/taxation/domain/Taxation.java:28` (`taxation`). GST rate = plain `Product.gstPercentage` (`Product.java:92`) and `BillLineItem.gstPercentage`. GST filing: `financialManagement/.../gstWorking/domain/GstWorking.java:39` (`gstWorking`) | — | — | — | — | `web/.../gstWorking/GstWorkingController.java`, `web/.../org/StateTaxController.java` |
| 14b | StateTax (org GSTIN) | **external Yoda service** — `integration/yoda/.../StateTaxInfoService.java:15` | — | — | — | — | — |
| 15 | **HSN / SAC** | **external Yoda service.** Locally only `Product.hsnCode` (`Product.java:89`), a plain string; single field serves both HSN and SAC | — | — | `commons/.../service/HsnApiService.java` / `core/.../yoda/service/impl/HsnApiServiceImpl.java` | — | `web/.../controller/hsnInfo/HsnInfoController.java:21` |
| 16 | **UOM / secondary unit** | **no UOM master.** Units are free-text strings. Only the conversion mapping is an entity: `commons/.../secondaryUnitMapping/domain/SecondaryUnitMapping.java:25` | `secondaryUnitMapping` | `.../domain/SecondaryUnitMappingDto.java` | `.../service/SecondaryUnitMappingService.java:7` / `.../Impl/SecondaryUnitMappingServiceImpl.java:21` | `.../repository/SecondaryUnitMappingRepository.java:8` | **none** |
| 17 | **Category** | *legacy* `inventory/inventory-commons/.../domain/Category.java:24` (`category`) — controller commented out, effectively dead. *Live* `catalog/catalog-core/.../domain/CatalogCategory.java:34` (`catalogCategory`) | | `catalog/catalog-commons/.../dto/CatalogCategoryCreateDto.java:15` | `catalog/catalog-commons/.../service/CatalogCategoryService.java:20` / `catalog/catalog-core/.../impl/CatalogCategoryServiceImpl.java:61` | `catalog/catalog-core/.../repository/CatalogCategoryRepository.java:14` | `web/.../productCatalog/CatalogCategoryController.java:42` |

**Naming traps worth internalising:**
- `Bill` is the **purchase** invoice but lives in `salesManagement`. `Invoice` is the sales document.
- "GRN" is `Delivery` with `DeliveryType.DELIVERY_IN`. `GrnToBillVariance` is a *different, separate* reconciliation feature.
- There is **no `Voucher` entity**. A voucher is the set of `voucherEntry` rows sharing a `voucherId`.
- Two enums both read like "reference type": `commons/.../ledger/enums/ReferenceType` (what document a voucher points at) vs `commons/.../ledger/enums/FinanceAccountReferenceType` (the account-mapping key). They are unrelated.

---

## 3. The bill-creation trace (centrepiece)

### 3.1 The route

```
POST /api/v1/bill/                              <- note the TRAILING SLASH; "/api/v1/bill" 404s
  web/.../controller/inventory/BillController.java:105-112   createBill(@RequestBody BillCreateDto)
    -> @AuthorizationRequired(BILL_CREATE_ID)               BillController.java:97-104
    -> billCreateDto.setOrganisationId(SMEAssistContext.getLoggedInOrganisationId())   :108-109
    -> BillService.create(BillCreateDto)                     commons/.../service/BillService.java:85
       core/.../bill/service/Impl/BillServiceImpl.java:648
```

**RUNTIME VERIFIED / VERIFIED** — `GET /actuator/mappings` on the deployed server lists exactly `POST /api/v1/bill/ → BillController.createBill`.

### 3.2 Cross-cutting guards on `create()`

`BillServiceImpl.java:636-648`:

```java
636  @RedisLock(lockKey = "'" + RedisLockConstants.BILL_CREATION_ORG_ID + "-" + "'+"
640      + "#billCreateDto.organisationId", timeout = RedisLockConstants.DEFAULT_TIMEOUT)
641  @Transactional(rollbackFor = Exception.class)
642  @PreConditionValidator(conditionsOn = {
643      PreConditionValidatorConstant.CONTACT_STATUS_CHECK_WITH_ID + ":"
645      + "#billCreateDto.contactType" + "|"
647      + "(#billCreateDto.contactDto != null && #billCreateDto.contactDto.contactId != null ? ... : null)"})
648  public BillDto create(@Valid @NotNull BillCreateDto billCreateDto) throws Exception {
```

| Guard | Effect | Migration consequence |
|---|---|---|
| `@RedisLock` on **organisationId** | Every bill create for the org is **fully serialised** through a Redisson `tryLock` (`core/.../aspects/RedisLockAspect.java:44-88`). | **The migration cannot be parallelised.** 10,769 bills posted one at a time. Also means counter/number races cannot occur under normal operation. **BACKEND VERIFIED / VERIFIED.** |
| `@Transactional(rollbackFor = Exception.class)` | Any exception, checked or unchecked, rolls the whole method back. | No partially-written bill *within* the create call. See §3.7. **BACKEND VERIFIED / VERIFIED.** |
| `@PreConditionValidator(CONTACT_STATUS_CHECK_WITH_ID)` | `core/.../aspects/PreConditionValidatorAspect.java:82-89` throws `"<name> is not in active state"` unless `ContactStatus == ACTIVE`. | A vendor in `APPROVAL_PENDING`, `APPROVAL_REJECTED`, `DISABLED` or `BLACKLISTED` blocks its bills. **BACKEND VERIFIED / VERIFIED.** |
| `@Valid @NotNull` on the parameter | **DOES NOTHING** — see §3.4. | |

### 3.3 The full request DTO

`commons/src/main/java/com/assist/commons/invoice/dto/BillCreateDto.java:37` — `extends AbstractEntityDto`, which contributes only `dateCreated`, `lastModified`, `createdBy`, `lastModifiedBy`, `isDeleted` and **no `id` field** (`/home/namansharma/yoda/libs/.../AbstractEntityDto.java:5-15`).

| Field | Type | Line | Annotated | **Actually required?** |
|---|---|---|---|---|
| `organisationId` | String | 40 | `@NotBlank` (39) | Overwritten by the controller regardless |
| `billEntityMappingDtos` | `List<BillEntityMappingDto>` | 42 | — | **YES** — `IllegalStateException("Bill Entity Mapping cannot be empty")` at `:654-656` |
| `billType` | `BillType` | 45 | `@NotNull` (44) | **YES** in practice — dereferenced at `:659` |
| `billNumber` | String | 48 | `@NotNull` (47) | **YES** — uniqueness check at `:703`; DB column nullable |
| `billSeriesNumber` | `DocumentNumber` | 50 | — | **YES for this org** — `ENABLE_BILL_SERIES` is ON; `CustomException("Bill series number is required")` at `:717-720` |
| `billDate` | Long (epoch ms) | 54 | `@NotNull` (52) | **YES** — `DateTimeUtils.getFinancialYear()` at `:702` |
| `dueDate` | Long | 56 | — | **YES** — DB column `nullable = false` (`Bill.java:89-90`) |
| `contactDto` | `MinContactDto` | 58 | — | **YES** — dereferenced unguarded at `:705` |
| `contactType` | `ContactType` | 60 | — | **YES** — used by the precondition aspect |
| `contactFinanceAccountId` | String | 62 | — | **YES for the ledger leg** — `EntityVoucherEntryCreateHelperService.java:3153` |
| `billAmount` | BigDecimal | 65 | `@NotNull` (64) | **Accepted then OVERWRITTEN** at `:2195` |
| `taxableAmount` | BigDecimal | 67 | — | DB `nullable = false`; **never recomputed at bill level** — see §3.6 caveat |
| `gstAmount` | BigDecimal | 69 | — | **Accepted then OVERWRITTEN** at `:2131` |
| `gstPercentage` | BigDecimal | 71 | — | Header-level only; ledger uses per-line rates |
| `extraCharges` | BigDecimal | 73 | — | Optional |
| `cessPercentage` | BigDecimal | 75 | — | Optional |
| `isInterState` | Boolean | 77 | — | DB `nullable = false`. **NOT used for the GST decision** — see §6.2 |
| `txsAppliedOnLineItem` | Boolean | 79 | — | **YES** — mutual-exclusion checks at `:2216-2229` |
| `gstTxsAppliedOnLineItem` | Boolean | 81 | — | Optional |
| `txsType` | `TaxType` (NONE/TDS/TCS) | 83 | — | Drives TDS/TCS legs |
| `txsSection` | `TaxSectionDto` | 85 | — | Needed to resolve the TDS/TCS account |
| `txsAmount` / `txsPercentage` / `txsOnAmount` | BigDecimal | 87/89/91 | — | Recomputed **only** when `txsType ∈ {NONE, TCS}` (`:2157-2172`) — **TDS amount is TRUSTED** |
| `discountAmount` | BigDecimal | 93 | — | Recomputed **only** when `discountType == "IN_PERCENTAGE"` (string compare, `:2137`); otherwise trusted |
| `discountType` | String | 95 | — | Free-text string, not an enum |
| `discountPercentage` | BigDecimal | 97 | — | |
| `actualDeliveryDate` / `expectedDeliveryDate` | Long | 99/101 | — | Optional |
| `namedImageList` | `List<NamedImage>` | 103 | — | Optional attachments |
| `isBlockedForPayment` | boolean | 105 | — | Optional |
| `gstUsedForBill` | String | 107 | — | **YES** — the org's own GSTIN for this bill; `stateTaxInfoService.getStateTaxInfoByGst()` at `:2028-2030` |
| `remarks` | String | 109 | — | Optional. Plain `VARCHAR(255)` |
| `lineItemDtoList` | `List<BillLineItemCreateUpdateDto>` | 111 | — | **YES** — three separate checks: `:651-653`, `:2025-2027`, `:856-858` |
| `billDateFrom` / `voucherDateFrom` | Long | 113/115 | — | Optional |
| `voucherDate` | Long | 117 | — | Ledger posting date; passed to `publishVoucherCreateRevokeEvent` at `:849` |
| `billStatus` | `BillStatus` | 119 | — | **YES in effect** — `ACTIVE` triggers approval + ledger; null skips both |
| `roundOffAmount` | BigDecimal | 121 | — | **Always OVERWRITTEN** at `:2196` |
| `hasRoundOff` | Boolean | 123 | — | **NPE if null** — unboxed at `:2176` |
| `billCostDistributionDtos` | `List<...>` | 125 | — | Optional |
| `references` | `List<ReferenceEntityMappingDto>` | 127 | — | Optional — a **legacy-key candidate**, see §7.3 |
| `contactBillingAddressDto` | `AddressDomainDto` | 129 | — | **YES** — DB `nullable = false`; **decides CGST/SGST vs IGST** |
| `companyBillingAddressDto` | `AddressDomainDto` | 131 | — | **YES** — DB `nullable = false`; **decides CGST/SGST vs IGST** |
| `entityLedgerVerificationStatus` | enum | 133 | — | **Overwritten** at `:836-840` |
| `ledgerVerificationRemarks` | String | 135 | — | Optional |
| `autoMapRemainingVoucher` | boolean | 137 | — | DB `nullable = false` |
| `purchaseType` | `CommerceType` | 139 | — | DB `nullable = false`. `INTERNATIONAL` enables conversion-rate round-off |
| `conversionRate` | BigDecimal | 141 | — | Used only when `purchaseType == INTERNATIONAL` |
| `currencyDto`, `destinationCountryDto`, `originCountryDto`, `portOfLoadingDto`, `portOfDischargeDto`, `incoTerms`, `customsCenterDto` | various | 143-155 | — | Import fields |
| `metadata` | `Map<String,String>` | 157 | — | **JSON column. This is where the migration stores Sage keys** — see §7.4 |
| `expenditureDtoList` | `List<ExpenditureInfoDto>` | 159 | — | Optional |
| `poPaymentTermDto`, `dueDateCalculationReference`, `dueDateCalculationInfo`, `billPaymentTerm`, `billCreditDays` | various | 161-171 | — | Due-date derivation |
| `selectiveUserApprovalDto` | `RoleBasedApprovalCreateDto` | 167 | — | Optional |
| `billProcurementType` | `BillProcurementType` | 173 | — | Optional |
| `billNumberFromDelivery`, `billDateFromDelivery` | String/Long | 175/177 | — | Optional |

**Line item DTO** — `commons/.../invoice/dto/BillLineItemCreateUpdateDto.java:26`, **zero validation annotations on any of its 39 fields**:

`lineItemId`(28) `organisationId`(30) `productId`(32) `billId`(34) `productName`(36) `skuCode`(38) `unit`(40) `quantity`(42) `displayUnit`(44) `displayQuantity`(46) `unitPrice`(48) `itemPrice`(50) `taxableOtherCharge`(52) `taxableAmount`(54) `hsn`(56) `gstPercentage`(58) `isRcmEnabled`(60) `totalPrice`(62) `discount`(64) `cessAmount`(66) `cessPercentage`(68) `cessType`(70) `description`(72) `referenceId`(74) `lineItemType`(76) `metaData`(78) `financeAccountDto`(80) `txsType`(82) `txsSection`(84) `txsAmount`(86) `txsPercentage`(88) `txsOnAmount`(90) `chargeDtoList`(92) `isGstClaimable`(94) `secondaryUnitConversion`(96) `gstTxsSection`(98) `gstTxsAmount`(100) `gstTxsPercentage`(102) `gstTxsOnAmount`(104).

**Landmines in this DTO (all BACKEND VERIFIED / VERIFIED):**
- **`cessType` must be non-null or the create dies with a bare NPE** — `BillServiceImpl.java:2087` calls `lineItemDto.getCessType().equals(CessType.IN_PERCENTAGE)` with no null guard. The loader already knows this: `post_sage_bills.py:2571-2572` carries the comment *"Omitting cessType is a bare NPE"* and always sends `"cessType": "IN_RUPEES"`. **SCRIPT VERIFIED.**
- **`isGstClaimable`** decides whether input GST lands in a claimable ITC account or an **ITC-Ineligible expense** account (§5.4). Null/false silently routes GST to expense.
- **`isRcmEnabled`** is taken verbatim from the payload — never derived from vendor registration (§6.3).
- `lineItemId` exists on the *create* DTO, so a caller can in principle pin a line-item id.

### 3.4 Bean validation is not wired — the `@NotNull`s are decorative

**BACKEND VERIFIED / VERIFIED.** Three independent facts:

1. `BillController.createBill` has **no `@Valid`** on the `@RequestBody` (`BillController.java:106-107`). Confirmed: `grep -n 'Valid\|Validated' BillController.java` returns nothing.
2. `BillServiceImpl` is annotated only `@Service` and `@Slf4j` (`BillServiceImpl.java:218-220`) — **not `@Validated`**. Spring's method-level validation requires `@Validated` on the bean.
3. There is **no `MethodValidationPostProcessor`** bean anywhere in the repo, and `org.springframework.validation.annotation.Validated` is imported in exactly one unrelated file.

Therefore the `@Valid @NotNull` on `BillServiceImpl.create(...)` at line 648 and on `BillService.create(...)` at `BillService.java:85` are **inert**. The same is true of `POST /contact` (§4.1). `POST /product` is the exception — it *does* carry `@Valid` (`ProductController.java:95`).

**Migration consequence:** a payload missing `billType`, `billNumber`, `billDate` or `billAmount` is **not rejected by validation**. It fails later — as an NPE, a DB not-null violation, or silently. Error messages will not name the field.

### 3.5 THE CENTRAL QUESTION — are amounts recalculated or trusted?

**Answer: recalculated, then silently overwritten. A mismatch is a warning, never a rejection.**
**BACKEND VERIFIED / VERIFIED.** `BillServiceImpl.reCalculateAndValidateBillCreateDto`, `:2013-2256`, invoked as the very first statement of `create()` at `:649`.

The method uses one pattern, five times: *compute → compare → warn → overwrite*.

**(a) Line item `itemPrice`** — `:2062-2081`
```java
2062  BigDecimal itemPrice = BigDecimalUtils.multiply(lineItemDto.getQuantity(), lineItemDto.getUnitPrice());
2065  taxableAmount = itemPrice;
2067  taxableAmount = BigDecimalUtils.add(taxableAmount, lineItemDto.getTaxableOtherCharge());
2068  if (ObjectUtils.isNotBlankObject(lineItemDto.getDiscount())) {
2070      taxableAmount = BigDecimalUtils.subtract(taxableAmount, lineItemDto.getDiscount());  }
2072  if (!BigDecimalUtils.isEqualR(itemPrice, lineItemDto.getItemPrice(), 6)) {
2077      googleChatService.sendErrorMessageOnGoogleChat(itemAmountWarningMessage, Level.WARNING, ...);
2079      logger.warn(BILL_MARKER, itemAmountWarningMessage);
2080      lineItemDto.setItemPrice(itemPrice);          // <-- SILENT OVERWRITE
2081  }
```

**(b) Line item `cessAmount`** — `:2087-2103`. Recomputed when `cessType == IN_PERCENTAGE`, otherwise taken from the payload; mismatch → warn → `setCessAmount(cessAmount)` at `:2102`.

**(c) Bill `gstAmount`** — `:2124-2132`
```java
2124  if (!BigDecimalUtils.isEqualR(totalGstPrice, billCreateDto.getGstAmount(), 6)) {
2128      googleChatService.sendErrorMessageOnGoogleChat(gstAmountWarningMessage, Level.WARNING, ...);
2131      billCreateDto.setGstAmount(totalGstPrice);    // <-- SILENT OVERWRITE
2132  }
```
where `totalGstPrice = Σ over lines of PercentageAmount(lineTaxableAmount, line.gstPercentage)` (`:2082-2086`). **The header `gstAmount` you send is discarded; the server's per-line slab arithmetic wins.**

**(d) Bill `discountAmount`** — `:2134-2153`. Recomputed **only** if `discountType.equals("IN_PERCENTAGE")` — a raw string comparison at `:2137`. Any other value (including `"IN_RUPEES"`, `null`, a typo) means the payload amount is **trusted verbatim** (`:2151-2153`).

**(e) `txsAmount`** — `:2155-2172`. Recomputed as `PercentageAmount(txsOnAmount, txsPercentage)` **only when `txsType ∈ {NONE, TCS}`**. **For `txsType == TDS` the payload amount is trusted with no check at all.** (The inclusion of `NONE` in a recalculation branch looks like a bug in its own right.)

**(f) Bill total `billAmount`** — `:2174-2196`, the decisive lines:
```java
2110  totalPrice = BigDecimalUtils.add(totalItemPrice, totalCessPrice, totalGstPrice, lineItemTcsAmount);
2156  totalPrice = BigDecimalUtils.subtract(totalPrice, discountAmount);
2173  totalPrice = BigDecimalUtils.add(totalPrice, txsAmount);
2176  if (billCreateDto.getHasRoundOff()) {                          // NPE if hasRoundOff is null
2184      roundOffPair = SAUtils.getRoundOffAmount(totalPrice, conversionRate);  }
2186  totalPrice = roundOffPair.getFirst();
2187  if (!BigDecimalUtils.isEqualR(totalPrice, billCreateDto.getBillAmount())) {
2191      googleChatService.sendErrorMessageOnGoogleChat(totalPriceWarningMessage, Level.WARNING, ...);
2193      logger.warn(BILL_MARKER, totalPriceWarningMessage);
2194  }
2195  billCreateDto.setBillAmount(totalPrice);        // <-- UNCONDITIONAL. Outside the if.
2196  billCreateDto.setRoundOffAmount(roundOffPair.getSecond());   // <-- ALSO UNCONDITIONAL
```

Note carefully: **line 2195 is outside the `if`.** The bill total is replaced with the server's computation *whether or not* it matched. The warning at `:2187-2194` is the only trace, and it goes to the server log and a Google Chat webhook — **neither is visible to the API caller**. The HTTP response returns `200 OK` with the server's numbers.

**The identity the server enforces:**
```
line.itemPrice   := quantity × unitPrice
line.taxable     := itemPrice + taxableOtherCharge − discount        (used for totals; see caveat)
bill.gstAmount   := Σ (line.taxable × line.gstPercentage / 100)
bill.billAmount  := Σ line.taxable + Σ cess + gstAmount + Σ lineTcs − discountAmount + txsAmount   [then round-off]
bill.roundOffAmount := server-computed
```

**Migration consequence (the answer the whole migration depends on):** Sage's stated `AMTINVCHC` / `AMTTAXHC` **cannot be carried across as values**. They can only be *reproduced*, by choosing per-line `quantity`, `unitPrice`, `taxableOtherCharge`, `discount` and `gstPercentage` such that the server's arithmetic lands on Sage's number. Where Sage's effective tax rate is not a clean slab (because Sage stored a rounded tax amount), the loader must either accept a residual difference or invent a fractional `gstPercentage` — which is what produced the illegal GST accounts in §6.5.

**The loader already knows this.** `post_sage_bills.py:1463-1474` states: *"The server ignores the gstAmount and roundOffAmount we [send] … gstAmount = SUM(line taxableAmount × line gstPercentage / 100); billAmount = taxableAmount + gstAmount + roundOffAmount … So the only levers are per-line taxableAmount and gstPercentage."* And `:2938` — *"Tax is NOT exact, and cannot be."* **SCRIPT VERIFIED / VERIFIED** — independent agreement between my code reading and the loader author's.

**Caveat I could not fully close:** the per-line `taxableAmount` computed at `:2065-2071` is used for the totals but is **never written back onto the line-item DTO**. The stored `billLineItem.taxableAmount` row therefore keeps whatever the caller sent. If a caller sends a `taxableAmount` inconsistent with `quantity × unitPrice`, the bill header and the line rows will disagree permanently. See §10.

### 3.6 Two broken validations in the same method

**BACKEND VERIFIED / HIGH** — worth reporting, not fixing.

```java
2202  if (BooleanUtils.isFalse(ledgerVerificationEnabled)
2203      && BillStatus.getNonRejectedStatues().contains(billCreateDto.getBillStatus())
2205      && ObjectUtils.isBlankObject(billCreateDto.getBillStatus())) {
2207      throw new CustomException("Contact finance Account Id can not be null");
2208  }
```
`billStatus` cannot simultaneously be *in* the non-rejected set and *blank*. **The condition is unsatisfiable — this check is unreachable dead code.** It also tests `billStatus` while its message talks about `contactFinanceAccountId`. **There is consequently no server-side check that `contactFinanceAccountId` is present**, even though the vendor CREDIT leg dereferences it (`EntityVoucherEntryCreateHelperService.java:3153`).

```java
2209  if (productIds.contains(chargeIds)) {            // Set<String>.contains(Set<String>)
2210      throw new CustomException("Lumpsump charge and charges per unit can not be same"); }
```
`Set<String>.contains(Set<String>)` is **always false**. Dead check.

### 3.7 Write order, tables and transaction boundary

The full sequence inside the single `@Transactional` boundary, `BillServiceImpl.java:649-880`:

| # | Line | Action | Table written |
|---|---|---|---|
| 1 | 649 | `reCalculateAndValidateBillCreateDto` — recompute + overwrite | — |
| 2 | 650 | `validateDirectExpenditureExpenseBill` | — |
| 3 | 651-656 | line items / entity mappings non-empty | — |
| 4 | 659-696 | PURCHASE: reject if a bill already exists for the DELIVERY; detect transfer-order auto-bill (`skipApproval`) | — |
| 5 | 698-699 | `validateVoucherDateAgainstGrnDate` | — |
| 6 | 701-709 | **bill-number uniqueness** per (org, contact, financial year) → `CustomException("Bill number already exists")` | — |
| 7 | 711-747 | **`ENABLE_BILL_SERIES` → `counterService.updateCounter(...)`** | `counter` |
| 8 | 749 | `billCreateConverter.convert(...)` | — |
| 9 | 750-759 | expense-bill due-date derivation | — |
| 10 | 766-767 | `canHaveAvailableLimitToCreateBill` — vendor credit limit | — |
| 11 | 769-794 | **MSME snapshot injected into `bill.metadata`** (`isMSME`, `msmeSnapshotDate`, `msmeClassificationYear`, `msmeEnterpriseType`, `msmeMajorActivity`) | — |
| 12 | **796** | **`billRepository.save(bill)` — the id is generated here** | **`bill`** |
| 13 | 797-798 | `referenceEntityMappingService.createByEntityIdAndEntityName(...)` | `referenceEntityMapping` |
| 14 | 800-801 | `billEntityMappingService.createOrUpdate(...)` | `billEntityMapping` |
| 15 | 804-817 | approval engine; if started → `billStatus = APPROVAL_PENDING`, **save #2** | `approvalProcess*`, `bill` |
| 16 | 820-828 | `EntityTrackerEvent` (CREATED) | `entityStatusTracker` |
| 17 | 831-843 | `LEDGER_VERIFICATION_ENABLED` → `PENDING_VERIFICATION`, else `VERIFIED`; **save #3** | `bill` |
| 18 | **845-846** | **`saveBillLineItems(...)` — soft-deletes then re-creates all lines** | **`billLineItem`** |
| 19 | 847-848 | documents/attachments | `document` |
| 20 | **849** | **`publishVoucherCreateRevokeEvent(billDto, voucherDate)`** | `voucherEntry`, `financeAccount`, `voucherSearch` — *conditionally*, see §5.1 |
| 21 | 851-854 | `BillCreateOrUpdateEvent` (Tally sync, search index, …) | various |
| 22 | 856-858 | post-hoc: line items must exist, else `CustomException` | — |
| 23 | 859-862 | `createExpenditures(...)` | `expenditure`, `expenditureEntityMapping` |
| 24 | 864-878 | approval-pending tracker event | `entityStatusTracker` |

**Transaction boundary — the answer to "can a half-written bill exist?"**

- **Within `POST /bill`: NO.** One `@Transactional(rollbackFor = Exception.class)` spans all 24 steps. Critically, the voucher listener is a **plain `@EventListener`** (`core/.../ledger/listeners/VoucherCreationListener.java:134`) — *not* `@TransactionalEventListener`, *not* `@Async`, and no custom `applicationEventMulticaster` bean exists. Spring's default multicaster invokes it **synchronously on the calling thread, inside the same transaction**. If voucher construction throws, the bill row rolls back with it. **BACKEND VERIFIED / VERIFIED.**
- **Across the two-call flow: YES.** Because this org has `LEDGER_VERIFICATION_ENABLED`, step 17 sets `PENDING_VERIFICATION`, so step 20's guard (`billStatus == ACTIVE && verificationStatus == VERIFIED`, `:1390-1391`) is false and **no ledger rows are written**. The bill exists, ACTIVE, with zero vouchers, until a separate `POST /bill/{billId}/verify` runs in its **own transaction**. A crash between the two leaves exactly that state.

**DATABASE VERIFIED / VERIFIED** — that state exists right now in the target org:

| billStatus | ledgerVerificationStatus | bills | with voucher | **without voucher** |
|---|---|---|---|---|
| ACTIVE | VERIFIED | 9,374 | 9,374 | 0 |
| REVOKED | VERIFIED | 1,392 | 0 | 1,392 |
| **ACTIVE** | **PENDING_VERIFICATION** | **2** | **0** | **2** |
| REVOKED | PENDING_VERIFICATION | 1 | 0 | 1 |

Two ACTIVE bills carry no ledger entries at all. They count toward AP outstanding but are invisible to the trial balance.

The loader does call the second step — `post_sage_bills.py:3007`: `api.post("/bill/%s/verify" % bid)`. **SCRIPT VERIFIED.**

### 3.8 Document numbering — the counter, and how it is already producing duplicates

**Two independent numbers exist on a bill and they are not the same thing:**

| Field | Source | Uniqueness |
|---|---|---|
| `bill.billNumber` | **Always taken verbatim from the payload.** `BillCreateConverter.java:75` — `.withBillNumber(source.getBillNumber())`. SMEAssist never generates it. | Application-level: unique per **(organisationId, contactId, financial year)** among non-rejected bills — `isBillExistWithNumber`, `BillServiceImpl.java:611-633,703-709`. **No DB constraint.** |
| `bill.billSeriesNumber` (+ `billSeriesPrefix`/`Value`/`Suffix`) | The **counter**, only when the org property `ENABLE_BILL_SERIES` is set for `EntityName.BILL` | **NONE. No uniqueness enforcement anywhere.** |

**This org has `ENABLE_BILL_SERIES` ON.** **DATABASE VERIFIED / VERIFIED** — `organisationProperty` row `1539859495601078272`: `[{"entityName": "BILL", "entityFeatures": ["ENABLE_BILL_SERIES"]}]`.

So for every one of the 10,769 bills, `create()` took the branch at `:716-747` and called `counterService.updateCounter(...)`. **An externally-supplied Sage bill number does NOT bypass the counter** — it lands in `billNumber`, while the counter separately mints a `billSeriesNumber`.

**Counter mechanics** — `purchaseManagement/.../domain/Counter.java:34-58`:
- Unique index on `(counterType, series, value, suffix)` — note it includes `value`, so it does **not** prevent two counter rows for the same series.
- Scoped in practice by `organisationId` + `associatedEntityType` (PAN/GST) + `associatedEntityId` + `associatedFinancialYear`.
- `@Version` optimistic locking (`Counter.java:52-54`).
- `updateCounter` is itself `@RedisLock`-ed per org and `@Transactional` (`CounterServiceImpl.java:230-234`), and joins the bill's transaction (propagation REQUIRED) — **so a rolled-back bill create rolls back its counter increment too; no gap from that path.**

**The document number is `prefix ‖ value` — a bare concatenation with no separator.** `BillServiceImpl.java:733-746` builds `DocumentNumber` from the returned `CounterSeries`.

#### Two proven collision mechanisms

**Mechanism 1 — caller-supplied value below the stored value is silently accepted but not reserved.**
`CounterServiceImpl.updateCounter` (`:234-250`) branches: blank value → `updateCounterWithOutValue` (increment by 1); non-blank → `updateCounterWithValue`. And `updateCounterWithValue`, `:311-330`:
```java
311  if (presentCounterValueInt > counterValueInt) {
312      log.info("update should not be possible");
313      return CounterSeries.Builder.counterSeries()
              ...
322          .withValue(counterValue)        // <-- the CALLER'S requested value
              ...
329          .build();                       // <-- returns WITHOUT EVER CALLING save()
330  }
331  counter.setValue(counterValue);
333  counter = counterRepository.save(counter);
```
The method **returns a `CounterSeries` claiming the caller's number, having reserved nothing.** `create()` then builds the bill's `billSeriesNumber` from that value (`:733-746`) and saves the bill. Two bills can therefore be issued the same series number. **BACKEND VERIFIED / VERIFIED.**

**Mechanism 2 — ambiguous concatenation across two series.** Series `SAGE27` with value `2` and series `SAGE` with value `272` both render as **`SAGE272`**.

#### The damage, measured

**DATABASE VERIFIED / VERIFIED.**

```
10,769 bills   →   only 9,225 distinct billSeriesNumber values
3,025 bill rows share a billSeriesNumber with at least one other row
```

| billSeriesPrefix | bills | value range | distinct values |
|---|---|---|---|
| `SAGE` | 9,125 | 2 – 7,966 | 7,965 |
| `SAGE27` | 1,644 | 2 – 1,359 | 1,358 |

A worked example, both mechanisms visible at once:

| billSeriesNumber | prefix | value | billNumber (Sage doc) | status |
|---|---|---|---|---|
| `SAGE2745` | `SAGE27` | 45 | `103327970947` | **ACTIVE** |
| `SAGE2745` | `SAGE27` | 45 | `114/26-27` | REVOKED |
| `SAGE2745` | `SAGE` | 2745 | `103326934657` | **ACTIVE** |

Two **ACTIVE** bills carrying the identical document number `SAGE2745`. Also note the value range starts at **2**, not 1 — number 1 is missing from both series.

The `counter` table further shows two *soft-deleted* counter rows (`SAGE`→1194, `SAGE27`→330) alongside the live ones (`SAGE`→7966, `SAGE27`→1359). Deleting and re-creating a counter restarts numbering from scratch while the bills bearing the old numbers remain — which is the bulk of Mechanism 1's damage.

**`bill.billNumber` itself is clean:** 0 duplicates per (contact, billNumber) among non-revoked bills. The application-level check works. **DATABASE VERIFIED / VERIFIED.**

---

## 4. Master-data traces

### 4.1 `POST /api/v1/contact/` — vendor create

`web/.../controller/contact/ContactController.java:143-151` → `contact/.../service/Impl/ContactServiceImpl.java:191` (`@Transactional(rollbackFor = Exception.class)` at `:190`).
**No `@Valid` on the `@RequestBody` (`:144-145`)**, and `ContactServiceImpl` is a plain `@Service` with no `@Validated` (`:124,127`) — so, exactly as with the bill, **every `@NotNull`/`@NotBlank`/`@Email`/`@PhoneNumber` on `ContactCreateDto` and its nested DTOs is inert.** `ContactCreateDto` also lacks `@Valid` cascade on its list fields (`:52-56`), so nested validation would not fire even if method validation were on. **BACKEND VERIFIED / VERIFIED.**

**Code-enforced requirements** (`ContactServiceImpl.createContact`):

| Rule | Line | Error |
|---|---|---|
| addresses present | `:219-221` | `"Address must be present."` |
| companyName present | `:222-224` | (re-check of the inert `@NotNull`) |
| `registrationType` present | `:1410-1412` | `"Registration Type can not be blank"` |
| `country` required if `INTERNATIONAL` | `:225-228` | |
| `currencies` required if `INTERNATIONAL` | `:229-232` | |
| taxation blocked for `INTERNATIONAL` unless `taxType == NONE` | `:233-238` | |

**GSTIN validation** — `commons/.../utils/GstUtils.java:12-17`:
```java
GST_REGEX = "^[0-9]{2}[A-Z]{4}[0-9A-Z]{1}[0-9]{4}[A-Z]{1}[1-9A-Z]{2}[0-9A-Z]{1}$";
```
Applied only when `registrationType == GST` (`ContactServiceImpl.java:1413-1423`): blank → `"Gst Number Can not be null"`; malformed → `"Gst Number is not valid"`. **No checksum validation, no government lookup — format only.**

**PAN validation** — `/home/namansharma/yoda/libs/.../PanUtils.java:6,26-32`, `PAN_REGEX = "^[a-zA-Z]{3}[abcfghljpt...][a-zA-Z][0-9]{4}[a-zA-Z]$"`, applied only when `registrationType == PAN` (`:1424-1432`).

**State / place-of-supply derivation — a critical subtlety.**
`Contact` has **no `state` column at all.** `ContactCreateDto.state` is accepted, copied into `ContactDto` (`ContactConvertor.java:79`), and then **silently dropped** — the persist-direction converter (`ContactConvertor.java:179-199`) never maps it. On read it is *derived*, `ContactConvertor.java:133-135`:
```java
.withState(contact.getRegistrationType() == RegistrationType.GST
    ? SAUtils.getStateByGst(contact.getRegistrationNumber()) : null)
```
i.e. **first two digits of the GSTIN** (`SAUtils.java:556-566` → `GstUtils.getGstStateCode`, `substring(0,2)`), and **`null` for any non-GST-registered contact.**

**But the GST decision on a bill does not use this.** It uses the *address* state (§6.1). The contact's GSTIN-derived state and the contact's address state are two independent values that nothing reconciles. This is the root of the misclassification proven in §6.2. **BACKEND VERIFIED / VERIFIED.**

**Duplicate detection** — `getExistingContactWithSameGstOrPan`, `:1407-1464`, all queries org-scoped:
1. If org has `EntityFeature.DUPLICATE_GST_ALLOWED` **or** `registrationType == WITHOUT_PAN_OR_GST` → uniqueness is on **(companyName, labelName)** (`:1435-1446`, `ContactRepository.java:59-62`) → `"Contact already exists with this name & label name"`. **For unregistered vendors, the name is the identity.**
2. Otherwise uniqueness is on **(registrationType, registrationNumber)** (`ContactRepository.java:51-54`); a hit → `"Contact Already exists with registration Number - …"` (`:205-216`) **unless** the existing contact's only type is `EMPLOYEE`, which takes a merge path.
3. Cross-type PAN guard: `validatePanNotUsedByOtherContactType` (`:353-376`).
4. **No uniqueness on mobile or email anywhere.**

**"Held" contacts.** There is **no `HOLD`/`HELD` state** — the string does not appear in the contact module. `ContactStatus` (`commons/.../contacts/enums/ContactStatus.java:6-11`) is `ACTIVE, APPROVAL_PENDING, APPROVAL_REJECTED, DISABLED, BLACKLISTED`. A new contact-info row defaults `ACTIVE` but flips to `APPROVAL_PENDING` if an approval workflow matches (`ContactHelperServiceImpl.java:159-171`). **Anything other than `ACTIVE` blocks bill creation** via the precondition aspect (§3.2). `APPROVAL_PENDING` is what the migration's `held_vendors.csv` is describing. **BACKEND VERIFIED / HIGH.**

**Ticket 3 — CONFIRMED PRESENT AND UNFIXED.** `ContactServiceImpl.java:288-301`:
```java
288  ContactBusinessInfoCreateDto contactBusinessInfoCreateDto = contactCreateDto.getContactBusinessInfo();
291  Boolean isCinOrLlpinRequired = getIsCinOrLlpinRequired(contactDto,
292      contactBusinessInfoCreateDto.getCorporateIdentificationNumber(),      // <-- NPE if section omitted
293      contactBusinessInfoCreateDto.getLimitedLiabilityPartnershipIdentificationNumber());
294  if (isCinOrLlpinRequired) {
295      LeadSearchDto leadSearchDto = getContactCinOrLlpin(contactDto.getCompanyName(), contactDto.getRegistrationNumber());
```
- `getIsCinOrLlpinRequired` (`:428-460`) fires whenever the **GSTIN's 6th character is `C` (company) or `F` (firm/LLP)** and CIN/LLPIN was not supplied.
- The lookup chain reaches `OxyzoSystemService.java:89`:
  `StringPreconditions.checkNotBlank(mobile, "Mobile Cannot be blank for System Login");`
  which throws `IllegalArgumentException` (`/home/namansharma/yoda/libs/.../StringPreconditions.java:11-17`).
- `OxyzoAuthenticationInterceptor.intercept` (`:47-58`) catches only `InvalidCredentialsException | NetworkException | CustomException` — **`IllegalArgumentException` is not caught**, so it propagates out of the `@Transactional` `createContact` and **rolls back the entire vendor create**, surfacing as `"Mobile Cannot be blank for System Login"` with nothing pointing at CIN/LLPIN.
- Same code on the update path (`:687-700`).
- `lookUpCinOrLlpinSilently` does **not exist** anywhere in the tree.
**BACKEND VERIFIED / VERIFIED.** The org GSTIN `29xxxCxxxxxxxx` has 6th char `C` — so every company vendor with a `C`-type GSTIN hits this.

**Contact types** — `commons/.../enums/ContactType.java:14-24`: `VENDOR, BUYER, CONSIGNEE, SERVICE_PROVIDER, TRANSPORTER, CONTRACTOR, SELF, ORGANISATION, AGENT, EMPLOYEE`. **No `CUSTOMER`, no `BOTH`** — a customer is `BUYER`, and multi-role is modelled as several `ContactInfo` rows under one `Contact`.

**Live registration-type mix** — **DATABASE VERIFIED / VERIFIED**, 537 contacts in the org:

| registrationType | count |
|---|---|
| `GST` | 297 |
| `WITHOUT_PAN_OR_GST` | 118 |
| `INTERNATIONAL` | 64 |
| `PAN` | 58 |

118 unregistered vendors are identified **by name alone** — precisely the collision risk ticket 1 describes for the bulk sheet.

### 4.2 `POST /api/v1/product/` — item create

`web/.../controller/inventory/ProductController.java:93-101` → `inventory/inventory-core/.../ProductServiceImpl.java:197` (`@Transactional(rollbackFor = Exception.class)` at `:195`). **This endpoint DOES carry `@Valid`** (`ProductController.java:95`) — unlike bill and contact. The only bean-validation annotation on `ProductCreateUpdateDto` is `@Size(min=2,max=8)` on `hsnCode` (`:34-35`); everything else is imperative.

| Rule | Where | Before or after save? |
|---|---|---|
| SKU uniqueness, per org — `SKU_CODE_ALREADY_EXISTS` → *"Sku Code already exists"* | `preCheckBeforeSaving`, `ProductServiceImpl.java:531-541` | **before** |
| HSN mandatory unless `RESOURCE`; **length must be exactly 4, 6 or 8** | `:551-561` | **before** |
| **Category mandatory** when `typeOfStock.isGoodsAndService()` | `:231-236` | **AFTER** |
| Secondary-unit conversion sanity | `:237-242` | **AFTER** |
| Primary unit ≠ secondary unit | `:243-245` | **AFTER** |

`isGoodsAndService()` = `RESOURCE != this && ASSET != this && CHARGE != this` (`TypeOfStock.java:49-51`) — so **`SERVICE` products also require a category**, despite the method name. `TYPE_OF_STOCK_WITH_NON_MANDATORY_HSN = {RESOURCE}` (`TypeOfStock.java:72-73`). Note the DTO's `@Size(min=2)` is **looser** than the code's `{4,6,8}` rule.

**Ticket 2 — VERDICT: the ordering defect is REAL; the stated consequence is NOT reproduced.**

The ordering is exactly as the ticket describes — **BACKEND VERIFIED / VERIFIED**:
```java
195  @Transactional(rollbackFor = Exception.class)
201      preCheckBeforeSaving(product);                 // SKU + HSN checks
202-203  String productId = ... : tokenGenerationService.getUniqueId();   // id minted in APP CODE
227      product.setId(productId);
228      product = productRepository.save(product);      // <<< THE SAVE
231-236  if (... isGoodsAndService() && categoryId blank) throw new CustomException(...);   // AFTER
237-245  ... throw new IllegalStateException("Invalid secondary unit conversion" / "Primary unit cannot be same...")  // AFTER
```
`preCheckCategoryAndSecondaryUnit` does not exist in the tree, and `GET /product/bySkuCode` is absent (only `GET /product/exists/skuCode`, a boolean) — **RUNTIME VERIFIED** against `/actuator/mappings` on the deployed server.

**However, the claim that this permanently burns the SKU is not supported.** The method is `@Transactional(rollbackFor = Exception.class)`, called externally through the `ProductService` interface (so the AOP proxy applies), with no `REQUIRES_NEW`, no `saveAndFlush`, and no try/catch swallowing the throw. Both post-save exceptions (`CustomException`, checked, covered by `rollbackFor = Exception.class`; and `IllegalStateException`, unchecked) therefore **roll the save back**.

**DATABASE VERIFIED / VERIFIED** — no orphan rows exist. Across 17,886 products in the target org:

| typeOfStock | rows | rows with no category |
|---|---|---|
| RAW_MATERIAL | 11,912 | **0** |
| PACKAGING_ITEM | 5,152 | **0** |
| STORES_AND_SPARES | 336 | **0** |
| CONSUMABLES | 159 | **0** |
| SERVICE | 75 | **0** |
| WORK_IN_PROGRESS | 1 | **0** |
| CHARGE | 173 | 173 *(exempt)* |
| RESOURCE | 73 | 72 *(exempt)* |
| ASSET | 3 | 1 *(exempt)* |
| PRODUCT | 2 | 1 *(unexplained — 1 row)* |

Every category-mandatory type has **zero** half-built rows, and there are **0** duplicate SKU codes. If the ticket's mechanism were live, the 43% failure run would have left thousands of orphans. **Conclusion: the ticket's diagnosis is wrong; its recommended fix (move the checks before the save) is still correct defensively, because the current code depends entirely on the transactional proxy and is one refactor away from the described bug.** This is a contradiction the human should be told about — it means the "43% unusable codes" had a different root cause (the ticket itself concedes most were a loader bug).

**Ledger linkage:** `ProductServiceImpl` creates **no** finance account. Grep for `financeAccount|ledger|referenceType` in the file returns only unrelated imports. Item ledgers are created lazily at *bill* time (§5.3), gated by `ENABLE_AUTOMATIC_LEDGERS_CREATION` — which **is** enabled for this org (**DATABASE VERIFIED**). On create the only side effects are `TallyStockItemCreateEvent` (`:266-271`) and `ProductCreatedEvent` (`:273-275`).

### 4.3 Bulk-upload paths

`POST /api/v1/bulkOperations/upload/{bulkUploadEntityType}` — `web/.../BulkOperationsController.java:206-222`, multipart field `file`. The enum value routes to the `@BulkOperationProcessor`-annotated service via `BulkOperationBeanFactory`.

| Service | Present in working tree? | Present in deployed jar? |
|---|---|---|
| `BulkBillCreateServiceImpl` | **YES** — `core/.../service/impl/BulkBillCreateServiceImpl.java:86` | **YES** (bean `bulkBillCreateServiceImpl`) |
| `BulkJournalVoucherServiceImpl` | **YES** — `:55` | **YES** |
| `CreditDebitNoteBulkUploadService` | **YES** — `:83` | **YES** |
| `PaymentRequestBulkUploadService` | **YES** — `:74` | **YES** |
| **`PaymentAllocationBulkUploadService`** | **ABSENT** — zero hits repo-wide | **ABSENT** — no such bean |

**RUNTIME VERIFIED / VERIFIED** via `GET /actuator/beans` (2,480 beans enumerated). `BulkUploadEntityType` in the deployed jar contains `BULK_BILL_CREATE`, `BULK_JOURNAL_VOUCHER`, `CREDIT_DEBIT_NOTE_BULK_UPLOAD` but **not `PAYMENT_ALLOCATION`**.

**Consequence: there is no way, today, to tell SMEAssist that a migrated payment settles a migrated bill.** Every migrated bill will read as unpaid. This is ticket 1's headline item and it is entirely absent from the running system.

**Semantics vs the single-create API:**
- `BulkBillCreateServiceImpl.java:230` calls `billService.create(billCreateDto)` — **the identical method** the REST controller calls. Same Redis lock, same recalculation, same counter, same approval engine. **No parallel write path.** **BACKEND VERIFIED / VERIFIED.**
- The bulk path hardcodes several fields to null/defaults (`:445-495`): `withReferences(null)`, **`withMetadata(null)`**, `withRemarks(null)`, `withExpenditureDtoList(null)`, `withBillCostDistributionDtos(EMPTY_LIST)`. **`withMetadata(null)` matters enormously: bills loaded through the bulk sheet cannot carry the Sage keys that the API path stores in `metadata` (§7.4).**
- **Per-row error handling is a transactional trap (INFERRED / HIGH).** `fetchAndCreateBill` is `@Transactional(rollbackFor = Exception.class)` (`:173-176`) and wraps each row in `try { billService.create(...) } catch (Exception e) { ...record message... }` (`:230-233`). Because `create()` is itself `@Transactional` with propagation REQUIRED, a throw marks the **shared physical transaction rollback-only**. The loop continues and the sheet reports per-row successes, but the final commit should raise `UnexpectedRollbackException` and discard **the entire batch — including rows reported as "Success"**. I could not execute this to confirm, but it follows directly from Spring's propagation semantics. The tickets describe the broad per-row catch as deliberate; this analysis says the pattern is unsafe as written.

---

## 5. Accounting / ledger derivation

### 5.1 When vouchers are created

`BillServiceImpl.publishVoucherCreateRevokeEvent`, `:1381-1417`, called at `:849`:
```java
1390  if (billDto.getBillStatus() == BillStatus.ACTIVE
1391      && billDto.getEntityLedgerVerificationStatus() == VERIFIED) {
1392      Boolean voucherAlreadyExists = voucherEntryService.existByReferenceTypeAndReferenceId(
1393          organisationId, ReferenceType.BILL, billDto.getBillId());
1395      if (BooleanUtils.isTrue(voucherAlreadyExists)) { return; }
1398      VoucherEntryCreationEvent voucherEntryCreationEvent = new VoucherEntryCreationEvent(this);
1400      voucherEntryCreationEvent.setEventType(VoucherEventType.BILL);
1405      applicationEventPublisher.publishEvent(voucherEntryCreationEvent);
```
Listener: `core/.../ledger/listeners/VoucherCreationListener.java:134` — plain **`@EventListener`**, `case BILL: billVoucherCreate(...)` at `:161-163`. Synchronous, same thread, same transaction (§3.7).

**Because this org has `LEDGER_VERIFICATION_ENABLED`, the guard at `:1390-1391` is FALSE at create time.** Ledger posting happens only on `POST /bill/{billId}/verify` (`BillController.java:421-423`), in a separate transaction. **BACKEND + DATABASE VERIFIED / VERIFIED.**

### 5.2 The legs for a purchase bill

`core/.../ledger/helper/EntityVoucherEntryCreateHelperService.java`, `getVoucherCreateDtoOnBillCreation:2447` → `getVoucherBreakageForBill:2815-3182`:

| # | Leg | Dr/Cr | Amount | Line |
|---|---|---|---|---|
| 1 | Item / Expense / Asset account, per line | **DEBIT** | `getNetItemAmount` = `qty × unitPrice` — **gross, discount NOT deducted** (`:3314-3319`) | `:2848-2893` |
| 2 | Extra charges | **DEBIT** | per-charge | `:2895-2908` |
| 3 | `Discount Received` | **CREDIT** | total discount | `:2910-2922` |
| 4 | `TCS Receivable` / `TDS Payable` (per section+rate) | **DEBIT** / **CREDIT** | `txsAmount` | `:2924-3017` |
| 5 | GST-TDS payable | **CREDIT** | `gstTxsAmount` | `:3019-3040` |
| 6 | **Input GST** — `IGST Input @ r %`, or `CGST Input @ r/2 %` + `SGST Input @ r/2 %`; **ITC-ineligible lines go to a separate expense account** | **DEBIT** | `taxable × gstPercentage / 100`, **recomputed here** | `:3042-3113` |
| 7 | `… Payable RCM @ r %` | **CREDIT** | RCM GST | `:3115-3143` |
| 8 | `CESS Payable` | **DEBIT** | total cess | `:3145-3146` |
| 9 | **Vendor / Sundry Creditor** (`bill.contactFinanceAccountId`) | **CREDIT** | `billAmount − RCM − TDS − GST-TDS` | `:3148-3166` |
| 10 | `Round Off` | DEBIT if positive, CREDIT if negative | `abs(roundOffAmount)` | `:3168-3180` |

**DATABASE VERIFIED** — the actual legs of the org's largest ACTIVE purchase bill (`IDEPHINJW2526-10`, ₹66,630,631.00), vendor GSTIN `37xxxCxxxxxxxx` (state 37 ≠ org 29 → IGST):

| Dr/Cr | Amount | Account |
|---|---|---|
| CREDIT | 66,630,631.00 | `Gst_37xxxCxxxxxxxx_INDIAN DESIGNS EXPORT PVT LTD - 9_8XNB87` (VENDOR) |
| DEBIT | 63,457,744.00 | `Job Work - Intercompany - Hindupur Unit 9_SAGE-4E2ME09-01 Expense` |
| DEBIT | 3,172,887.20 | `IGST Input @ 5.00 %` |
| DEBIT | −0.20 | `Round Off Value on Purchases_SAGE-4E1M016 Expense` |

Balanced. Note the vendor ledger is **named by GSTIN**, and the expense ledger name **embeds the Sage GL code** (`SAGE-4E2ME09-01`) — see §7.5.

### 5.3 How accounts are chosen, and when they are auto-created

Two mechanisms, both **per-organisation**:

**(a) Mapping table** — `ledger/core/.../FinanceAccountReferenceMapping.java:23`, table `financeAccountReferenceMapping`, keyed `(organisationId, referenceType, referenceNumber) → financeAccountId`. `referenceType` is `FinanceAccountReferenceType` (`commons/.../ledger/enums/FinanceAccountReferenceType.java:5-29`): `GST, PAN, ITEM_PURCHASE, ITEM_SALE, ASSET_PURCHASE, ITEM_DIRECT_EXPENSE, ITEM_IN_DIRECT_EXPENSE, BANK, IMPREST, …`. Used for the item/expense/asset legs, keyed by `productId`.

The reference type per line — `:2852-2867`:
```java
if (billType == PURCHASE) {
    if (lineItemType == CHARGE)      -> ITEM_DIRECT_EXPENSE
    else if (lineItemType == ASSET)  -> ASSET_PURCHASE
    else                             -> ITEM_PURCHASE
} else if (billType == IN_DIRECT_EXPENSE || billType == CREDIT_CARD_EXPENSE) -> ITEM_IN_DIRECT_EXPENSE
else                                 -> ITEM_DIRECT_EXPENSE
```

**(b) Name-pattern lookup** — `FinanceAccountServiceImpl.getFinanceAccountIdByName(organisationId, name)` (`:565-570`), with names built from `ledger/commons/.../constants/FinanceAccountConstant.java`:
```
GST_INPUT_PATTERN            = "%s Input @ %s %%"        e.g. "IGST Input @ 18.00 %"
GST_RCM_PAYABLE_PATTERN      = "%s Payable RCM @ %s %%"
INELIGIBLE_GST_INPUT_PATTERN = "%s @ %s %% (ITC Ineligible)"
IGST_INPUT_IMPORT_PATTERN    = "IGST Input (Import) @ %s%%"
ROUND_OFF = "Round Off"   DISCOUNT_RECEIVED = "Discount Received"
TDS_PAYABLE = "TDS Payable"   TCS_RECEIVABLE = "TCS Receivable"   CESS_PAYABLE = "CESS Payable"
```
Note the pattern difference: the auto-created GST accounts render `"@ 18.00 %"` (**with a space**) while the pre-seeded import accounts render `"@ 18.00%"` (**no space**). They are different account names and therefore different accounts.

**Auto-creation on miss:**
- **GST / RCM / ineligible-GST accounts: ALWAYS auto-created**, unconditionally (`:2153-2177`, `createGstFinanceAccountId:2307-2341`, `createIneligibleGstFinanceAccountId:2343-2383`).
- **TDS/TCS section accounts: ALWAYS auto-created** (`getOrCreateTxsFinanceAccount:4029-4078`).
- **Item/product accounts: only if `ENABLE_AUTOMATIC_LEDGERS_CREATION` is set** for `EntityName.LEDGER` (`FinanceAccountUtils.java:253-262`, via `ProductListener.java:83-105`). If not set, the miss becomes a hard failure at `:2876-2880`:
  `throw new CustomException("No Finance Account Exists for product - " + lineItemDto.getProductName());`
  which (§3.7) rolls back the whole bill. **This org has the flag ON** (**DATABASE VERIFIED**), so item ledgers are minted on demand.

### 5.4 Balance enforcement

`ledger/core/.../voucherEntry/service/Impl/VoucherEntryHelperServiceImpl.java:75,294-305`:
```java
75   COMPARISON_THRESHOLD = round(new BigDecimal(0.01), 2);
294  BigDecimal difference = abs(subtractR(creditedAmount, debitedAmount));
295  if (isGreater(difference, COMPARISON_THRESHOLD)) {
297      throw new IllegalArgumentException(format(
298          "Amount debited and credited should be equal. Credited {0}  Rs and Debited {1} Rs", ...));
```
Enforced before any row is persisted, with a **1-paisa tolerance**. An unbalanced voucher is impossible; the attempt rolls the transaction back. **BACKEND VERIFIED / VERIFIED.**

### 5.5 Ledger tables written

`voucherEntry`, `financeAccount`, `financeAccountReferenceMapping`, `voucherEntryMapping`, `voucherSearch`, `parkedVoucher`. **There is no `voucher` table** — a voucher is the set of `voucherEntry` rows sharing `voucherId`.

---

## 6. GST / tax logic

### 6.1 CGST+SGST vs IGST — decided by ADDRESS state, not GSTIN

The one decisive comparison, `EntityVoucherEntryCreateHelperService.java:3057-3065`:
```java
3057  boolean isIgst = false;
3058  if (ObjectUtils.isNotBlankObject(billDto.getCompanyBillingAddressDto())
3059      && ObjectUtils.isNotBlankObject(billDto.getContactBillingAddressDto())) {
3060      isIgst = billDto.getCompanyBillingAddressDto().getState()
3061          != billDto.getContactBillingAddressDto().getState();
3062  }
```
Duplicated as `BillDto.isIGSTApplicable()` (`commons/.../invoice/dto/BillDto.java:243-250`).

Then, per line (`:3068-3103`): `isIgst` → full rate to `IGST Input @ r %`; else the rate is **halved** (`divideR2`) and the amount halved into `CGST Input @ r/2 %` and `SGST Input @ r/2 %`.

**Three consequences, all BACKEND VERIFIED / VERIFIED:**
1. **The org's state comes from `bill.companyBillingAddressId`; the vendor's from `bill.contactBillingAddressId`.** Both are addresses, not GSTINs.
2. **There is no `placeOfSupply` field on the bill.** The concept is expressed only as this address comparison.
3. **If either address is null, `isIgst` stays `false` → CGST+SGST is applied by default.** A missing address silently produces intra-state treatment.

A GSTIN→state utility exists (`GstUtils.getGstStateCode`, first two digits) and a helper `GstUtils.getIsInterState(buyerState, vendorState)` (`:48-55`), but **neither is used in the bill-voucher path** — that path re-implements the comparison inline against the stored `Address.state`.

**`bill.isInterState` is NOT the decision input.** It is written at create time (from the delivery/PO, `BillCreateDtoService.java:949,2941`) and read only by **reports** (`ConsolidatedBillsReportServicempl.java:407-448`, `InvoiceTallyReportServiceImpl.java:199-315`). The ledger ignores it. **Reports and the ledger can therefore disagree about the same bill.**

### 6.2 The divergence, measured in live data

**DATABASE VERIFIED / VERIFIED.** Cross-tabulating `bill.isInterState` against the GST accounts actually hit, over ACTIVE bills with vouchers:

| `bill.isInterState` | IGST leg | CGST/SGST leg | bills | reading |
|---|---|---|---|---|
| 1 | yes | no | 5,926 | consistent |
| 0 | no | yes | 2,825 | consistent |
| 0 | **yes** | yes | 125 | see below |
| **0** | **yes** | **no** | **4** | **flag contradicts the ledger** |

**The 4 rows are a genuine, provable GST misclassification.** All four are bills from vendor GSTIN `29xxxPxxxxxxxx` — **state code 29, identical to the org's 29**. The transaction is intra-state and `bill.isInterState = 0` says so correctly. The ledger nonetheless booked `IGST Input @ 18.00 %`, because the vendor's stored *billing address* carries a different state from the GSTIN:

| billNumber | isInterState | gstAmount | account hit |
|---|---|---|---|
| `KXM/BR0532/25-26` | 0 | 952.20 | `IGST Input @ 18.00 %` |
| `KXM/BR0534/25-26` | 0 | 1,944.00 | `IGST Input @ 18.00 %` |
| `KXM/BR0596/25-26` | 0 | 1,057.50 | `IGST Input @ 18.00 %` |
| `KXM/BR0639/25-26` | 0 | 15,426.00 | `IGST Input @ 18.00 %` |

₹19,379.70 of input credit booked as IGST that should be CGST+SGST. **Nothing warned; nothing failed.** Because a vendor's GSTIN state and address state are maintained independently (§4.1) and only the address is consulted, this can happen to any vendor whose address was entered inconsistently.

**The 125 rows are a different thing and not a server defect.** Sampling one (`BLR611243`, vendor `29xxxCxxxxxxxx`) shows correct `CGST Input @ 9.00 %` + `SGST Input @ 9.00 %` = ₹198 matching `gstAmount`, **plus** a ₹1,097 DEBIT to `IGST Input (Import) @ 0.00%`. That extra leg is the **loader carrying customs IGST through as a zero-rated product line** mapped to the import ledger — because the server will not accept a free-standing tax amount (§3.5), so the only way to move the value is as a line. Consequence worth flagging: **import IGST is entering the books as an expense-style line against a 0% ledger, not through the GST mechanism**, so it will not present correctly in GST/ITC reporting.

### 6.3 Reverse charge (RCM)

`BillLineItemDomainDto.isRcmEnabled` (`commons/.../BillLineItemDomainDto.java:50`) is a plain `Boolean` **carried verbatim from the payload**. No code derives it from vendor registration type or a notified-goods list. **BACKEND VERIFIED / VERIFIED.**

Effect — `EntityVoucherEntryCreateHelperService.java:3116-3143`, then `:3150-3152`:
- the RCM line's GST is **still** debited to normal claimable Input GST (`:3079-3103`) — ITC is claimed;
- **and** credited to a dedicated `… Payable RCM @ r %` account (`addCompositeGstRCMTaxVoucherBreakage:2230-2305`), auto-created if missing;
- the **vendor credit is reduced**: `contactCreditAmount = billAmount − totalRCMApplied − totalTdsAmount − totalGstTdsAmount`.
- **The bill total itself is unchanged by RCM.** Only the ledger split moves.

Live data: every migrated bill carries `"sageRcm": "False"` in `metadata`, so RCM is not currently exercised by this load. **DATABASE VERIFIED.** Note the tickets' warning that the *patch's* older RCM-column parser silently accepted a typo as "true" — the current code rejects it; keep the current behaviour.

### 6.4 Unregistered and foreign vendors

- **Unregistered** (`WITHOUT_PAN_OR_GST`): no dedicated tax branch exists. Such a vendor has no GSTIN-derived state; the treatment falls out of whatever `gstPercentage` is on the line and whatever address states exist. **Nothing forces GST to zero.** **BACKEND VERIFIED / HIGH.**
- **Foreign / import** (`CommerceType.INTERNATIONAL`): vendor GST is not applied on the purchase bill. Import IGST is meant to flow through a **separate Bill-of-Entry / customs-duty voucher** (`getVoucherCreateDtoOnBillOfEntryCustomsDuty:1680-1773`) posting a `JOURNAL` voucher against `ReferenceType.BILL_OF_ENTRY` and the `IGST Input (Import) @ r%` ledger. **The migration is not using that path** — §6.2 shows it pushing customs IGST through ordinary bill lines instead. **DATABASE VERIFIED.**
- **All 10,769 migrated bills are stored `purchaseType = DOMESTIC`**, including the international vendors. **DATABASE VERIFIED / VERIFIED.**

### 6.5 Tax rate validation — THERE IS NONE

**BACKEND VERIFIED / VERIFIED.** No `@DecimalMin`/`@DecimalMax`/enum constraint on `BillLineItemCreateUpdateDto.gstPercentage` (`:58`). No `GstRate`/`TaxRate` enum exists anywhere (`TaxType` is only `{TCS, TDS, NONE}`). The canonical list `0, 0.1, 0.25, 1, 1.5, 2, 3, 5, 7.5, 12, 18, 28` appears exactly once — as an **Apache POI spreadsheet dropdown** for the bulk item-master template (`ItemMasterBulkServiceImpl.java:424-439`). It is never consulted by any create path.

So: **any `BigDecimal` is accepted as a GST rate, and §5.3 then auto-creates a ledger account named after it.**

**DATABASE VERIFIED / VERIFIED — the damage is already done.** The org's chart of accounts now holds **52 GST Input accounts**, of which roughly 45 carry rates that do not exist in Indian GST:

`0.32%, 0.35%, 1.25%, 1.34%, 2.29%, 2.46%, 2.48%, 2.49%, 2.51%, 2.52%, 2.53%, 2.66%, 4.76%, 4.86%, 4.92%, 4.95%, 4.99%, 5.01%, 5.02%, 5.45%, 6.71%, 9.01%` …

These arise because the loader must back-solve a rate that reproduces Sage's stored tax amount (§3.5). Only four accounts carry any traffic today (`IGST Input @ 18.00 %`, `CGST/SGST Input @ 9.00 %`, `CGST/SGST Input @ 2.50 %`, `IGST Input @ 5.00 %`, plus `IGST Input (Import) @ 0.00%`); the rest have **0 legs** because the bills that used them were revoked. **The accounts survived the revoke.** Revoking a bill does not delete the ledger account its rate created, so **chart-of-accounts pollution is a permanent, non-reversible side effect of any bad load.**

### 6.6 Cess, TDS, TCS

- **Cess**: per line, `IN_PERCENTAGE` of taxable or a flat `IN_RUPEES` amount (`BillServiceImpl.java:2087-2103`, warn-and-overwrite), posted in total to a single `CESS Payable` ledger (`:2116-2131`).
- **TDS/TCS**: available at bill level *or* line level, **mutually exclusive**, enforced hard (`BillServiceImpl.java:2216-2229`):
  - `txsAppliedOnLineItem == true` + non-zero bill-level `txsAmount` → `"Over all tcs/tds can not be applied if txs applied on line item"`
  - `txsAppliedOnLineItem == false` + non-zero line `txsAmount` → `"Line item tcs/tds can not be applied , if txs applied on lumpsump"`
- Posting requires the **contact's TDS configuration to name `TdsApplicableEntity.BILL`**, else (`:2937-2951`, `:2975-2989`):
  `"Can not add TDS on bill for the contact %s, As TDS Configuration is not set to %s"`
- **GST-TDS** (TDS on the GST component) is a separate mechanism (`:3019-3040`).

---

## 7. ID generation and legacy-key options

### 7.1 The generator

Every entity extends `GenericIdAbstractEntity` → `BaseAbstractEntity`, `commons/src/main/java/com/assist/commons/domain/mysql/BaseAbstractEntity.java:26-31`:
```java
@Id
@GenericGenerator(name = "entity_id_gen", strategy = "com.assist.yoda.common.mysql.EntityUniqueIdGenerator")
@GeneratedValue(generator = "entity_id_gen")
@Column(name = "id", columnDefinition = "CHAR(20)")
private String id;
```
**This is the single ID path for the whole application.** The column is `CHAR(20)` holding the decimal string of a snowflake long — confirmed in the live schema (`counter.id varchar(255)`, `voucherEntry.id char(20)`, `financeAccount.id char(20)`). **BACKEND + DATABASE VERIFIED / VERIFIED.**

The generator (external repo, `/home/namansharma/yoda/commons/.../EntityUniqueIdGenerator.java:24-30`):
```java
public Serializable generate(SharedSessionContractImplementor session, Object o) {
    Serializable id = session.getEntityPersister(null, o).getClassMetadata().getIdentifier(o, session);
    return id != null ? id : String.valueOf(instance.tokenGenerationService.nextId());
}
```
Algorithm (`/home/namansharma/yoda/commons/.../SequenceGenerator.java:16-77`): Twitter snowflake — 41-bit timestamp (custom epoch 2015-01-01Z), 10-bit node id, 12-bit sequence, spins on sequence exhaustion. **The node id is not configurable** — `createNodeId()` (`:87-107`) hashes the host's MAC addresses, falling back to `SecureRandom`, masked to 10 bits. Worth noting as a latent collision risk for containers sharing a virtual NIC.

### 7.2 Can a Sage identifier become a SMEAssist id?

**Technically yes at the persistence layer; practically NO through any API.**

- The generator is an **assigned-id-or-generate** pattern: `id != null ? id : generate`. Internal code exploits this deliberately — the branch-migration services *null the id* precisely to force regeneration (`core/.../branchMigration/EmployeeMigrationService.java:242`, `core/.../migration/MigrationService.java:2081`, `CommonMigrationService.java:247,270,537`).
- **But no create DTO exposes an `id`.** `BillCreateDto` extends `AbstractEntityDto`, which has no `id` field. `ContactCreateDto` has `contactId` only for the employee-merge path. `BillCreateConverter.convert()` (`salesManagement/.../BillCreateConverter.java:68-133`) never calls `.withId(...)`.
- Sage identifiers are alphanumeric (`ACCL047`, `IDPO2519858`, `SAGE-4E2ME09-01`) and would not fit `CHAR(20)` numeric-string semantics anyway.

**Verdict: every id must be generated by SMEAssist and mapped externally. Sage keys cannot be carried across as primary keys.** **BACKEND VERIFIED / VERIFIED.**

### 7.3 Is there a purpose-built external-reference field? No.

Checked `Bill`, `Contact`, `Product`, `CreditDebitNote`, `PaymentRequest`, `VoucherEntry`:

| Entity | Candidate | Verdict |
|---|---|---|
| `Bill` | `remarks` (`Bill.java:148-149`, plain `VARCHAR(255)`, unindexed), `metaData` (`:210-211`, `json`) | No dedicated column |
| `Contact` | `metaData` (json), `registrationNumber` (GST/PAN, not a system key) | No dedicated column |
| `Product` | `description`, `meta` (json), **`skuCode`** | No dedicated column, but see §7.5 |
| `CreditDebitNote` | `noteReason` (TEXT 1024), `closureRemarks` | Free text |
| `PaymentRequest` | `shortId` (internal display id) | No |
| `VoucherEntry` | `referenceId`/`referenceType` — **indexed**, but `ReferenceType` is a **closed enum of internal document types** | Not extensible without a code change |
| `FinanceAccount` | **`partyReferenceNumber` / `partyReferenceType`** — real columns! | Exists but **effectively unused**: only 87 of 19,794 rows populated, and `partyReferenceType` is the closed `FinanceAccountReferenceType` enum (`PAN`, `PURCHASE_ITEM`, …) with **no external-system value** |

**Two generic mapping tables were evaluated and both fail:**
- **`entitySourceMapping`** (`commons/.../entityAndSourceMapping/domain/EntitySourceMapping.java:20-47`) — has exactly the right shape (`entityId`, `entityType`, `sourceId`, `sourceType`, indexed on `sourceId`) and 374,770 live rows. **But `sourceType` and `entityType` are the same closed enum** (`EntityType.java:11-23`): `DISPATCH_ORDER, RETURN_ORDER, SALES_ORDER, DISPATCH_SCHEDULE, TRANSFER_ORDER, CONTRACT_MANUFACTURING, INVOICE, COMMERCIAL_INVOICE, DELIVERY, DELIVERY_CHALLAN, CONSIGNMENT, BOOKING`. **No `BILL`, no `CONTACT`, no `PRODUCT`, no `SAGE`.** It maps SMEAssist documents to SMEAssist documents. Unusable without a code change.
- **`entityRelationMapping`** (`core/.../entityRelationMapping/domain/EntityRelationMapping.java`) — same story, different closed enum.
- **`integration/`** holds only narrow single-purpose tables (`masterIndiaGstDetails`, `eWayBill`, `locProcessedLog`, …). **`integration/saTally` has zero JPA entities** — it is a pure JDBC bridge and persists no SMEAssist-id↔Tally-GUID row.

**`referenceEntityMapping`** (`commons/.../referenceEntityMapping/domain/ReferenceEntityMapping.java:20-49`, 437,229 rows) is the one *designed* slot: `entityId`, `entityName`, `referenceLabelId`, `label`, `value`, `referenceType`. `BillCreateDto.references` (line 127) feeds it at `BillServiceImpl.java:797-798`. It is a user-facing "reference numbers" feature requiring a pre-created `referenceLabel`, and it is **not used by this migration** — but it is the closest thing to a supported external-key slot, and unlike `metadata` it is a real indexed table.

**MAJOR FINDING: SMEAssist provides no first-class place to record a Sage primary key.** There is no external-id column, no extensible mapping table, and no lookup API. This is worth stating plainly to the human.

### 7.4 What the migration actually does — `bill.metaData`

**DATABASE VERIFIED / VERIFIED.** All 10,769 bills carry a populated `metaData` JSON blob:
```json
{"sageDoc": "3597/25-26", "sageRcm": "False", "sageItem": "IDPO2519858",
 "sageBatch": "215081006", "sageVendor": "ACCL047", "sageBillType": "PURCHASE",
 "sageTaxGroup": "GOODS", "sageTypeNote": "", "migrationSource": "IDEDAT"}
```
Contacts likewise: `{"sageVendor": "ACCL001", "stateSource": "gstin", "migrationSource": "IDEDAT", "mobileIsPlaceholder": "true"}`.

This works, but note the limits and one hazard:
- **Unindexed JSON.** "Find the SMEAssist bill for Sage doc X" is a full JSON scan over 10,769+ rows and cannot use an index.
- **No uniqueness.** Nothing prevents two bills claiming the same `sageDoc`.
- **The server writes into the same map.** `BillServiceImpl.java:769-794` injects `isMSME`, `msmeSnapshotDate`, `msmeClassificationYear`, `msmeEnterpriseType`, `msmeMajorActivity` when the vendor is flagged MSME. It uses `put` so it does not clobber the Sage keys — but the field is **shared, not the migration's private space**.
- **The bulk-upload path cannot use it at all** — `BulkBillCreateServiceImpl` hardcodes `withMetadata(null)` (§4.3). Any bill loaded via the sheet loses its Sage key entirely.

`contact.metaData` also records `"stateSource": "gstin" | "sage"` — the loader is tracking which source it used for the vendor's state. Given §6.1–6.2, that field is exactly the right thing to have recorded.

### 7.5 The de-facto legacy keys already in use

**DATABASE VERIFIED / VERIFIED.** Absent a real mechanism, the migration has encoded Sage keys into **business-visible identifier fields**:

| Where | Pattern | Count |
|---|---|---|
| `product.skuCode` | `SAGE-4E4SD01`, `SAGE-4E2ME14` | **17,452 of 17,886 products** |
| `financeAccount.name` | `Courier Charges_SAGE-4E4SD05 Expense` | **17,589 of 19,794 accounts** |
| `bill.metaData` | `sageDoc` / `sageVendor` / `sageItem` | 10,769 of 10,769 |
| `contact.metaData` | `sageVendor` | most |

Encoding the Sage GL code into the **finance account display name** and the Sage item code into the **SKU** is effective and searchable, but both are user-visible strings that a person can rename in the UI at any time, silently breaking the linkage. Neither carries a uniqueness or format constraint tied to the Sage key. This should be recorded as a deliberate, fragile design decision rather than an accident.

---

## 8. Working tree vs deployed server

### 8.1 What is in the working tree

`git log` HEAD is `2785b4c3a3 "#12425324 - Do not merge: revert the Sage migration backend changes"` (Sat 29 Aug 2026 15:50:33 +0530). **The working tree contains NONE of the migration changes.** Confirmed by direct grep — all of the following return **zero hits** repo-wide:

`preCheckCategoryAndSecondaryUnit` · `PaymentAllocationBulkUploadService` · `lookUpCinOrLlpinSilently` · `MIGRATION_REVERSE_INPUT_GST_ON_PURCHASE_DEBIT_NOTE` · `getNoteItemReferenceTypes` · `bySkuCode` · `PAYMENT_ALLOCATION`

**BACKEND VERIFIED / VERIFIED.**

### 8.2 The `.patch` is an incomplete record — MAJOR FINDING

`git apply --stat sage-migration-backend-changes.patch` covers **20 files**. `git show --stat 2785b4c3a3` (the revert) touches **32 files**. **Twelve files of reverted work appear in no patch:**

| Lost file | What it was |
|---|---|
| `inventory/.../ProductServiceImpl.java` | **Ticket 2** — move the category/secondary-unit checks before the save |
| `web/.../controller/inventory/ProductController.java` | **Ticket 2** — `GET /product/bySkuCode` |
| `contact/.../ContactServiceImpl.java` | **Ticket 3** — `lookUpCinOrLlpinSilently`, the vendor-create unblock |
| `core/.../service/impl/PaymentAllocationBulkUploadService.java` | **Ticket 1** — the new payment-allocation sheet (352 lines) |
| `ledger/.../dtos/PaymentAllocationBulkUpload.java` | its DTO (118 lines) |
| `payment/.../PaymentRequestAdvanceBillMappingConvertor.java` | advance-vs-bill mapping |
| `payment/.../PaymentRequestAdvanceBillMappingRepository.java` | " |
| `payment/.../Impl/PaymentRequestAdvanceBillMappingServiceImpl.java` | " |
| `commons/.../payment/service/PaymentRequestAdvanceBillMappingService.java` | " |
| `commons/.../organisationProperties/enums/EntityFeature.java` | **Ticket 5** — the RCM-reversal switch |
| `commons/.../organisationProperties/enums/EntityName.java` | **Ticket 5** |
| `core/.../ledger/listeners/VoucherCreationListener.java` | **Ticket 4** — the diagnostic error messages |

The revert removed **1,457 lines**; the patch restores **732**. **Roughly half the reverted work — including the two fixes the migration most needs and the entire payment-allocation feature — exists nowhere except in the reverted commits themselves.** Those commits are still reachable (`223e764da15`, `2f70c19da34`, `1b22e24102b`, `b3ae3cd5568`, `ac3f5124d97`, `3594922a575`), which is the only place to recover them from.

Separately, **the patch no longer applies**: `git apply --check` fails on 5 files (`BulkUploadEntityType`, `BulkBillCreateServiceImpl`, `BulkJournalVoucherServiceImpl`, `BulkJournalVoucherUploadDto`, `BillUpload`). The tickets already warn it was taken from an older copy. **BACKEND VERIFIED / VERIFIED.**

### 8.3 Is the checkout the code running at `SME_BASE`? — YES

`SME_BASE = http://10.22.0.165:9069/api/v1`. On that host:
```
root 448664  java ... -jar /mnt/smeassist-web.jar --server.port=9069     started Mon Aug 31 09:18:37 2026
-rw-r--r-- 195836968  Aug 29 16:16  /mnt/smeassist-web.jar
```
The jar's mtime (**Aug 29 16:16**) is **26 minutes after the revert commit** (Aug 29 15:50:33). Manifest: `Implementation-Title: web`, `Spring-Boot-Version: 2.7.8`, no `git.properties`/`build-info.properties`, so no commit hash is embedded.

**Direct symbol probe of the deployed fat jar** (Spring Boot layout: module jars under `BOOT-INF/lib/`), searching each class's constant pool:

| Symbol | Module jar | In deployed jar? |
|---|---|---|
| `PAYMENT_ALLOCATION` in `BulkUploadEntityType` | `com.ofb.assist-commons-1.66-SNAPSHOT.jar` | **NO** |
| `PAYMENT_ALLOCATION` in `ReferenceType` | " | **NO** |
| `MIGRATION_REVERSE_INPUT_GST_ON_PURCHASE_DEBIT_NOTE` in `EntityFeature` | " | **NO** |
| `preCheckCategoryAndSecondaryUnit` in `ProductServiceImpl` | `inventory-core-1.0-SNAPSHOT.jar` | **NO** |
| `lookUpCinOrLlpinSilently` in `ContactServiceImpl` | `contact-1.0.jar` | **NO** |
| `PaymentAllocationBulkUploadService` (class) | `core-1.0-SNAPSHOT.jar` | **NO** |
| `PaymentRequestAdvanceBillMapping*` (classes) | `payment-*.jar` | **NO** |
| *(control)* `getProductBySkuCode` in `ProductServiceImpl` | `inventory-core` | **YES** — and present in source at `ProductServiceImpl.java:707` |
| *(control)* `LEDGER_VERIFICATION_ENABLED`, `ENABLE_BILL_SERIES` in `EntityFeature` | `commons` | **YES** |

The two controls validate the method: a symbol present in the current source is found, one absent from the current source is not.

**Corroborated at runtime** via the (unauthenticated) actuator:
- `GET /actuator/beans` — 2,480 beans; `bulkBillCreateServiceImpl`, `bulkJournalVoucherServiceImpl`, `creditDebitNoteBulkUploadService`, `paymentRequestBulkUploadService` all present; **`paymentAllocationBulkUploadService` and `paymentRequestAdvanceBillMapping*` absent.**
- `GET /actuator/mappings` — 1,820 mappings; `POST /api/v1/bill/` present, **`GET /api/v1/product/bySkuCode` absent** (only `GET /api/v1/product/exists/skuCode`).

### 8.4 Verdict

> **The deployed server at `SME_BASE` corresponds to the current, reverted working tree. The Sage migration patch is NOT live.**
> **RUNTIME VERIFIED / VERIFIED (highest confidence in this report).**

**Every defect in tickets 1–6 is live on the server the migration is posting to:** vendors with `C`/`F`-type GSTINs are blocked by the CIN lookup; the product checks still run after the save; there is no payment-allocation sheet, so migrated payments cannot be tied to migrated bills; note-verify failures are still bare nulls; purchase-note charges still land in Direct Expenses; and the RCM-reversal switch does not exist as an enum value, so ticket 5 cannot even be configured.

**One schema artefact contradicts the code and needs stating:** the table **`paymentRequestAdvanceBillMapping` exists in the deployed database with 200 rows**, although its entity, repository, convertor and service were all reverted and are absent from the running jar. Spring Boot's `ddl-auto` is not set in `BOOT-INF/classes/application.properties`, so the schema is not auto-managed and the table simply survived from an earlier build that had the patch. **It is orphaned: no running code reads or writes it.** Anyone reasoning about "what is deployed" from the schema alone will reach the wrong conclusion — the schema is ahead of the code.

### 8.5 Relevant deployed configuration for the target org

**DATABASE VERIFIED / VERIFIED** — `organisationProperty` rows for `1029113552088076445`:

| entityName | feature | Consequence for the migration |
|---|---|---|
| `ALL` | **`LEDGER_VERIFICATION_ENABLED`** | Bills post with **no ledger entries**; a second `POST /bill/{id}/verify` is mandatory (§3.7, §5.1) |
| `BILL` | **`ENABLE_BILL_SERIES`** | `billSeriesNumber` is **mandatory** and the counter is consumed on every bill (§3.8) |
| `LEDGER` | **`ENABLE_AUTOMATIC_LEDGERS_CREATION`** | Missing item ledgers are auto-created rather than failing the bill (§5.3) |
| `BILL` | `ADHOC_PURCHASE_BILL`; `ALL` `ADHOC_PROCUREMENT` | Bills can be created without a PO/GRN — what makes this migration possible at all |
| `BILL` | `DISABLE_VOUCHER_REMAINING_AUTO_MAP_BY_DEFAULT` | Voucher auto-mapping off by default |
| `NOTE` | `NOTES_ENABLE_SALES_PURCHASE_LEDGERS` | |
| `TALLY` | `ENABLE_TALLY_LEDGER`, `ENABLE_TALLY_STOCK_ITEM`, `ENABLE_CONTACT_TALLY_LEDGER_ONLY`, `CREATE_INDEPENDENT_LEDGER_OR_STOCK_ITEM_IN_TALLY` | **Tally sync is ON.** Every migrated bill/product/contact also emits Tally events (`BillCreateOrUpdateEvent`, `TallyStockItemCreateEvent`) — a downstream side effect nobody has scoped |
| `INVOICE` | `AUTO_TDS_DEDUCTION`, `PROFORMA_INVOICE_SERIES_ENABLED` | |
| `ALL` | `ENABLE_WHATSAPP_NOTIFICATION`, `ENABLE_EMAIL_NOTIFICATION`, `ENABLE_INBOX_NOTIFICATION` | Migration activity may be generating user notifications |
| — | **`MIGRATION_REVERSE_INPUT_GST_ON_PURCHASE_DEBIT_NOTE`** | **Not present, and the enum value does not exist in the deployed jar.** Ticket 5 is unimplementable as things stand |

**Security note (out of scope but should not go unreported):** the actuator on the devbox is **fully open and unauthenticated**, exposing `/actuator/env`, `/actuator/beans`, `/actuator/configprops`, `/actuator/threaddump` and **`/actuator/heapdump`**. A heap dump would contain credentials and customer data. **RUNTIME VERIFIED / VERIFIED.**

---

## 9. Server-side rules checklist

### 9.1 Rules that REJECT — a document must satisfy all of these

**Bill create** (`BillServiceImpl.create`, all **BACKEND VERIFIED / VERIFIED**):

| # | Rule | Line | Error |
|---|---|---|---|
| B1 | Vendor `ContactStatus` must be `ACTIVE` | aspect `:82-89` | `"<name> is not in active state"` |
| B2 | `lineItemDtoList` non-empty | `:651-653`, `:2025-2027`, `:856-858` | `"Line Items cannot be empty"` |
| B3 | `billEntityMappingDtos` non-empty | `:654-656` | `"Bill Entity Mapping cannot be empty"` |
| B4 | `billNumber` unique per (org, contact, financial year) among non-rejected | `:703-709` | `"Bill number already exists"` |
| B5 | `billSeriesNumber.series` non-blank — **because `ENABLE_BILL_SERIES` is ON** | `:717-720` | `"Bill series number is required"` |
| B6 | No existing bill for the same DELIVERY (PURCHASE only) | `:667-671` | `"Purchase Bill Already Exists for delivery %s"` |
| B7 | Voucher date vs GRN date | `:698-699` | |
| B8 | Vendor credit limit not exceeded | `:766-767` | |
| B9 | No line item references a disabled product | `:2113-2122` | `"Cannot proceed with bill creation as item(s) %s are marked disabled"` |
| B10 | `billAmount >= 0` (**zero IS allowed** — the non-zero guard at `:760-763` is commented out) | `:2213-2215` | `"Bill amount can not be less than zero"` |
| B11 | TDS/TCS at bill level XOR line level | `:2216-2229` | `"Over all tcs/tds can not be applied if txs applied on line item"` / `"Line item tcs/tds can not be applied , if txs applied on lumpsump"` |
| B12 | Both billing addresses must be `AddressStatus.ACTIVE` | `:2245-2255` | `"Address : %s is not active"` |
| B13 | A charge cannot be both a line-item charge and a lump-sum charge | `:2054-2058` | `"Charge can be either on lineItem or as a lumpsum - %s"` |
| B14 | `gstUsedForBill` must be a non-ISD GSTIN (unless `ISD_EXPENSE`) | `:2031-2046` | `"Can not select ISD Gst Number %s in case of %s bill"` |
| B15 | Credit-card bills: transaction must be `PENDING` | `:2230-2243` | `"Card Transaction is already on %s status"` |
| B16 | Every line item's product must resolve to a finance account | helper `:2876-2880` | `"No Finance Account Exists for product - %s"` |
| B17 | TDS requires the contact's `TdsApplicableEntity == BILL` | helper `:2937-2951`, `:2975-2989` | `"Can not add TDS on bill for the contact %s, As TDS Configuration is not set to %s"` |
| B18 | Voucher must balance within 1 paisa | `VoucherEntryHelperServiceImpl.java:294-305` | `"Amount debited and credited should be equal…"` |
| B19 | DB NOT NULL: `contactId`, `contactName`, `contactType`, `billDate`, `dueDate`, `billAmount`, `taxableAmount`, `isInterState`, `billStatus`, `contactBillingAddressId`, `companyBillingAddressId`, `autoMapRemainingVoucher`, `purchaseType` | `Bill.java` | raw constraint violation |

**Bare NPEs that behave as required-field rules but name no field:**

| # | Trigger | Line |
|---|---|---|
| B20 | line item `cessType` is null | `:2087` |
| B21 | `hasRoundOff` is null (unboxed `Boolean`) | `:2176` |
| B22 | `contactDto` is null | `:705` |

**Contact create:** `registrationType` non-blank (`:1410-1412`); GSTIN format if `GST` (`:1413-1423`); PAN format if `PAN` (`:1424-1432`); uniqueness on registration number, or on (companyName, labelName) for unregistered (`:1435-1461`); `country` + `currencies` if `INTERNATIONAL` (`:225-232`); addresses present (`:219-221`); CIN/LLPIN mandatory if the org requires them (`:302-306`); **plus the CIN-lookup landmine (§4.1) and the null-businessInfo NPE at `:288-293`.**

**Product create:** SKU unique per org (`:531-541`); HSN present and length ∈ {4,6,8} unless `RESOURCE` (`:551-561`); category present when `isGoodsAndService()` (`:231-236`); secondary-unit conversion sane and ≠ primary (`:237-245`).

### 9.2 SILENT TRANSFORMATIONS — the dangerous category

Rules that **change your data and return `200 OK`**. Hunted for specifically; ranked by migration impact.

| # | Transformation | Where | Why it is dangerous |
|---|---|---|---|
| **S1** | **`billAmount` is overwritten with the server's recomputed total, unconditionally, even when it matched.** Only a log line + a Google Chat webhook record the difference — neither reaches the caller. | `BillServiceImpl.java:2187-2195` | **Sage's stated invoice total silently becomes something else.** The API says `200 OK`. Only a read-back comparison detects it. |
| **S2** | **`gstAmount` is overwritten** with `Σ(line taxable × line rate / 100)`. | `:2124-2132` | Sage's stated tax amount is discarded. Where Sage's tax is not a clean slab of the taxable value, the books will differ from the filed return. |
| **S3** | **A GST rate the server has never seen auto-creates a new ledger account**, with no validation against legal GST rates. | helper `:2153-2177` | Already produced ~45 illegal-rate accounts (§6.5). **Revoking the bills does not remove the accounts.** Permanent chart-of-accounts pollution. |
| **S4** | **The counter returns a number it did not reserve** when the supplied value is below the stored value — logs `"update should not be possible"` and returns success. | `CounterServiceImpl.java:311-330` | **3,025 bill rows already share a colliding `billSeriesNumber`**, including ACTIVE/ACTIVE pairs (§3.8). |
| **S5** | **CGST/SGST vs IGST is decided by billing-address state, silently ignoring the GSTIN.** If either address is null it defaults to CGST/SGST. | helper `:3057-3065` | **Proven: 4 bills booked as IGST for a vendor whose GSTIN state matches the org's** (§6.2). ₹19,379.70 of input credit misclassified with no warning. Affects filed returns. |
| **S6** | **`bill.isInterState` is stored but never used for the tax decision** — reports read it, the ledger recomputes. | `:3057` vs `ConsolidatedBillsReportServicempl.java:407-448` | Reports and the trial balance can state different GST treatments for the same bill. 129 divergent rows found. |
| **S7** | **`itemPrice` and `cessAmount` are overwritten per line** on mismatch. | `:2080`, `:2102` | Line-level detail silently changes. |
| **S8** | **`roundOffAmount` is always overwritten**; round-off is computed **only if `hasRoundOff` is true**, otherwise forced to zero. | `:2176-2196` | Sage's round-off cannot be carried; it must be engineered into the line values. |
| **S9** | **MSME classification is snapshotted into `bill.metaData` from the vendor's CURRENT state.** | `:769-794` | A bill dated January 2026 is stamped with the vendor's MSME status as of the load date. Historically wrong, and it feeds MSME 45-day reporting. |
| **S10** | **`discountAmount` is recomputed only when `discountType` is the exact string `"IN_PERCENTAGE"`**; any other value (typo, null) means the payload amount is trusted. | `:2135-2153` | Inconsistent trust model driven by an unvalidated free-text field. |
| **S11** | **`txsAmount` is recomputed for `NONE`/`TCS` but TRUSTED for `TDS`.** | `:2157-2172` | TDS amounts pass through unchecked while TCS does not. |
| **S12** | **`ContactCreateDto.state` is accepted and silently discarded** — never persisted; state is re-derived from the GSTIN on read, and is `null` for unregistered vendors. | `ContactConvertor.java:79` vs `:179-199`, `:133-135` | You cannot set a vendor's state. For 118 unregistered vendors it is permanently null. Root cause of S5. |
| **S13** | **`isGstClaimable == false/null` routes input GST to an ITC-Ineligible expense account** instead of claimable ITC. | helper `:3079-3103` | Silently converts recoverable tax into expense. |
| **S14** | **`isRcmEnabled` is taken verbatim from the payload**, never derived. | `BillLineItemDomainDto.java:50` | A wrong flag books RCM with no cross-check against vendor registration. |
| **S15** | **The bulk-bill path forces `metadata`, `references`, `remarks` to null.** | `BulkBillCreateServiceImpl.java:445-495` | Bills loaded via the sheet **lose their Sage keys entirely** (§7.4). |
| **S16** | **`billLineItem.taxableAmount` is not written back** — the header uses the recomputed value, the row keeps the caller's. | `:2065-2071` (no setter call) | Header and lines can disagree permanently. Unconfirmed — see §10. |
| **S17** | **A rejected create still consumes a snowflake id and may leave `entityStatusTracker` / approval rows** written before the save. | `ProductServiceImpl.java:208,214` | Ids are cheap, but tracker/approval residue may survive if any of those run in their own transaction. Unconfirmed. |

### 9.3 What the migration must do to be correct, not merely accepted

1. **Never trust the response's echo of your own numbers — read the bill back and compare.** S1/S2 mean acceptance proves nothing. (The loader already does this: `post_sage_bills.py:2896-2967`.)
2. **Engineer the line values so the server's arithmetic lands on Sage's total.** There is no other way to preserve `AMTINVCHC`.
3. **Send `cessType` and `hasRoundOff` on every line/bill** or take a nameless NPE.
4. **Send both billing addresses, with correct states** — they, not the GSTIN, decide CGST/SGST vs IGST.
5. **Reconcile every vendor's address state against its GSTIN state before loading** — this is the S5 defect, and it is silent.
6. **Call `POST /bill/{id}/verify`** — without it the bill has no ledger entries.
7. **Do not delete or recreate a counter mid-load**, and do not choose series names where one is a prefix of another (`SAGE` / `SAGE27`).
8. **Accept that ids must be generated and mapped**, and record the Sage key in `bill.metaData` — which rules out the bulk sheet for anything whose provenance must survive.
9. **Expect chart-of-accounts pollution from any fractional GST rate, and know it is not reversible by revoking the bills.**

---

## 10. What I could NOT prove

Stated honestly; each is a real gap in this analysis.

1. **Whether `billLineItem.taxableAmount` diverges from the header in practice (S16).** I proved the recomputed per-line taxable value is never written back to the DTO (`:2065-2071` has no setter call, unlike `itemPrice` at `:2080`). I did not trace `BillLineItemService.createOrUpdate` to confirm which value reaches the column, nor run the DB comparison. **Confidence LOW.**

2. **The exact cause of the 1 `PRODUCT`-type row with no category.** Every category-mandatory type shows zero orphans except this single row. It may be a merger-context bypass (`SMEAssistContext.isMergerContext()`, `ProductServiceImpl.java:231`), pre-existing data, or a genuine instance of ticket 2's mechanism. One row is not enough to decide. **Confidence LOW.**

3. **Whether the bulk-upload `UnexpectedRollbackException` actually fires (§4.3).** This follows from Spring propagation semantics on the code as written, but I did not run a bulk upload — that would be a write, and is out of scope. **INFERRED / HIGH, not verified.**

4. **Whether `@Valid` on `POST /product/` changes anything materially.** It is present, but the DTO's only constraint is `@Size(min=2,max=8)` on `hsnCode`, which the imperative `{4,6,8}` check supersedes. I did not test the interaction.

5. **What the 125 "both IGST and CGST/SGST" bills mean in aggregate.** I sampled one and established the mechanism (import IGST carried as a zero-rated line to `IGST Input (Import) @ 0.00%`). I did not verify all 125 share that shape, nor quantify the total value routed this way. **Confidence MEDIUM.**

6. **The precise history of the counter rows.** Two soft-deleted counters exist alongside two live ones. I proved the code path that permits reuse (`CounterServiceImpl.java:311-330`) and measured the resulting collisions, but I could not reconstruct *who* deleted the counters or exactly which collisions came from mechanism 1 vs mechanism 2.

7. **Whether the ~80-number discrepancy between counter values and bill rows is genuine gaps.** Counter values sum to ~10,849 against 10,769 bill rows, and both series start at 2 rather than 1. The same-transaction rollback (§3.8) argues gaps should not occur, so this is more likely an artefact of the delete/recreate. I did not resolve it. **Confidence LOW.**

8. **The behaviour of the reverted code.** I established with certainty what is *absent*. I did not read the reverted commits' diffs in detail to characterise what the fixes would do if restored — only the tickets' description of them.

9. **Anything about the note (credit/debit) and payment creation paths in comparable depth.** Tickets 4, 5 and 6 concern `EntityVoucherEntryCreateHelperService.getVoucherCreateDtoOnNoteCreation` (`:3356`) and note charge-line account selection. I confirmed the method exists and the patch's changes are absent, but did not trace the note path end to end. **This is the largest uncovered area.**

10. **Whether Tally sync is actively mirroring the migrated data.** Four TALLY features are enabled on the org and bill/product creates publish Tally events. I did not check whether an agent is connected or what it has pushed. Potentially a significant unscoped side effect.

---

## 11. Open questions for the human

1. **The `.patch` is missing half the reverted work (§8.2), including the ticket-2 and ticket-3 fixes and the whole payment-allocation feature. Is anyone aware?** If those commits are ever garbage-collected, that work is gone. Recovering it from `223e764da15..3594922a575` should happen before anything else.

2. **3,025 bills carry a duplicated `billSeriesNumber`, some ACTIVE-vs-ACTIVE (§3.8). Is that acceptable in a system of record?** If not, this needs a remediation decision *before* more loading, because the counter will keep doing it.

3. **Four bills book IGST for a same-state vendor (§6.2), and the mechanism affects any vendor whose address state disagrees with its GSTIN state. Has anyone reconciled address state against GSTIN state across all 297 GST-registered vendors?** This directly affects GST returns.

4. **~45 illegal GST-rate ledger accounts now exist and survived the revoke (§6.5). Who owns cleaning up the chart of accounts, and is the loader still capable of creating more?**

5. **Ticket 2's stated cause is not supported by the code or the data (§4.2) — `@Transactional` does roll the save back, and there are zero orphan product rows.** Should the ticket be re-diagnosed? The "43% unusable codes" had some other cause, which is still unexplained.

6. **Without `PaymentAllocationBulkUploadService`, every migrated bill will show as unpaid. Is that the accepted end state for this phase, or a blocker?** There is no other mechanism in the deployed server.

7. **`LEDGER_VERIFICATION_ENABLED` makes bill creation a two-step operation. Was that deliberate for the migration, or should it be off during the load?** Two ACTIVE bills already sit without ledger entries.

8. **Tally sync is enabled on this org (§8.5). Is the migrated history being pushed to Tally, and is that intended?**

9. **The MSME snapshot stamps each migrated bill with the vendor's *current* MSME status (S9), not the status as at the bill date. Does that matter for MSME 45-day compliance reporting on FY2025-26?**

10. **Ticket 5 cannot be implemented as written — `MIGRATION_REVERSE_INPUT_GST_ON_PURCHASE_DEBIT_NOTE` does not exist in the deployed `EntityFeature` enum, and that file is one of the twelve missing from the patch.** Given the tickets say Indian Designs already filed FY2026 returns on the reversal basis, what is the plan?

11. **The devbox actuator is unauthenticated and exposes `/actuator/heapdump` (§8.5).** Should be closed regardless of the migration.

12. **Should the migration record Sage keys somewhere more durable than an unindexed JSON column and a SKU-code prefix (§7.3–7.5)?** Today the linkage lives in fields a user can rename in the UI.

---

*End of report. Every code claim carries a `file:line`; every data claim was re-run against the live `smeassist` database on 2026-09-05. No file in the backend repo, no database row, and no API state was modified in producing this analysis.*
