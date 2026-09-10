# Sage → SMEAssist migration

Pulls data out of a Sage 300 company database (read-only) and posts it into
SMEAssist, a Java/Spring ERP, through its REST API.

Two populations, two loaders that share one set of machinery:

| | loader | Sage source | posts |
|---|---|---|---|
| **Payables** | `post_sage_bills.py` | `APOBL` + `APIBD` | bills, against a VENDOR ledger |
| **Receivables** | `post_sage_invoices.py` | `AROBL` + `OEINVH`/`OEINVD` | sales invoices, against a BUYER ledger |

The working window for the payables side is the **Jan–Apr 2026** AP-direct
bills: `post_sage_bills.py` is a port of the original PowerShell loader
(`load_janapr_bills.ps1`) with six field-level defects fixed, each annotated
inline against its defect number.

The receivables side loads what Sage still shows **open at the 1-Apr-2026
cutover**. It imports the payables loader rather than copying it — the API
client and its rate gate, the GSTIN/state/pincode/HSN derivation, the
item-product builder, the crosswalk and the posted log all come from there, so
a defect fixed on one side is fixed on both. See
[AR (sales) invoices](#ar-sales-invoices).

## What is in the repo

| Path | What it is |
|---|---|
| `post_sage_bills.py` | The payables loader. All its phases live here — see below. |
| `post_sage_invoices.py` | The receivables loader. Imports the above for everything not specific to a sales document. |
| `extract.sql` | The Sage extraction queries, split on `@@name` markers; each becomes `output/<name>.csv`. Every statement is a `SELECT`. |
| `work/*.py`, `work/*.sh` | Probes, one-off repair scripts, and the run wrappers used while working the migration out. |
| `work/*.md` | Findings and the run book: what broke, why, and what was decided. |
| `requirements.txt` | `pymssql` for Sage over TDS, `requests` for the SMEAssist API. |

Not in the repo, by design — see `.gitignore`:

- **`.env`** — SQL password and the SMEAssist auth token.
- **Extracted data** (`output/`, `ref/`, `state/`, `*.csv`, `*.psv`, the
  crosswalk JSONs). It carries real vendor names, GSTINs, addresses, invoice
  numbers and amounts, and it is reproducible from `extract.sql`.
- **Run artefacts** (`logs/`, `work/logs/`, `*.log`) and local `*.bak` snapshots.
- **Internal infrastructure records** — host names, firewall rules and SQL
  login details from the setup phase.

Host addresses, the target org's identity (id, namespace, GSTIN, PAN) and
third-party GSTINs have been removed from the tracked files; they come from
`.env` at run time. GSTIN examples surviving in comments are masked
(`29XXXPX0001X1ZN`) with the state code and the 6th character — the entity-type
test the code makes — left intact, because the surrounding prose turns on them.

> `extract.sql` refers to a `pull.py` runner that is not part of this tree.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # then fill it in
```

`post_sage_bills.py` refuses to start unless `SME_BASE`, `SME_ORG_ID`,
`SME_NAMESPACE`, `SME_ORG_GSTIN` and `SME_ORG_PAN` are all set. That is
deliberate: it posts under a real tax identity, and a plausible-but-wrong
default would file bills against the wrong organisation.

The auth token is short-lived. Refresh it before a long run — either
`SME_TOKEN` in `.env`, or `--token` per invocation.

## Running everything

```bash
./run_all.sh            # every AP phase that still has work, in order
./run_all.sh --check    # reconcile + failure report + AR self test, writes nothing
./run_all.sh --from goods-masters   # start at a given phase
./run_all.sh --with-ar  # ALSO load the AR (sales) invoices
```

The AR phases are **opt-in**. They post sales invoices under an export
exemption, against AR/OE column names that are assumed rather than inventoried
(below), so a plain `./run_all.sh` does not silently widen to include them.
With `--with-ar` the run gates on `ar-selftest` and then on `ar-probe`, and
refuses to post if either fails.

`run_all.sh` preflights `.env`, the API, Sage and the auth token, then runs
`masters` -> `post` -> `goods-masters` -> `goods-post` and finishes with a
reconciliation. Every phase is resumable: bills already in `work/posted.log`
are skipped and masters already in the crosswalk are skipped, so re-running
after an interruption continues rather than repeats.

It runs phases **strictly one at a time**, and refuses to start if another
phase is live. That is not only about the shared crosswalk — two phases
against this API throttle each other badly, measured at 12 bills/min
concurrent against 63 bills/min alone, so serial is also simply faster.
`--check` is read-only and deliberately works while a run is in flight.

Afterwards:

| file | what it holds |
|---|---|
| `work/reconcile-report.json` | Sage vs SMEAssist, per document |
| `work/failures-report.json` | everything not posted, with reason and amount |

## Running a single phase

```
./post_sage_bills.py <phase> [--pilot] [--limit N] [--token T]
```

| Phase | Does |
|---|---|
| `cleanup` | Remove smoke-test leftovers and reconcile the bill counters. |
| `masters` | Create the products and contacts the selected bills need. |
| `dryrun` | Build every payload, post nothing, print grouped skip reasons. |
| `post` | Create + verify. Resumable. |
| `verify` | The definition-of-done queries, run against MySQL. |
| `legs` | Inspect one document's legs (`--docs vendor|invoice`). |
| `goods-masters`, `goods-dryrun`, `goods-post` | The same three steps for the goods (PO-matched) population. |

`--pilot` selects ~10 bills chosen to cover the distinct shapes, not the first
10. Start with `dryrun`, then `masters --pilot`, then `post --pilot`.

## AR (sales) invoices

```
./post_sage_invoices.py <phase> [--docs INV ...] [--limit N] [--token T] [--no-enrich]
```

| Phase | Does |
|---|---|
| `ar-probe` | Check every assumed AR/OE table and column against `INFORMATION_SCHEMA`, report the gaps with candidate names, and print the population. Writes nothing. |
| `ar-selftest` | Run the conversion arithmetic and every guard against known figures. **No Sage, no API, no token.** |
| `ar-masters` | Buyer contacts (with the BUYER role and its Debtors ledger) and sale products, scoped to the selection. |
| `ar-dryrun` | Build every payload, post nothing, group the hold reasons. |
| `ar-post` | Create + verify. Resumable. |
| `ar-recon` | Posted total against Sage's own `AROBL` open total. |
| `ar-legs` | One invoice's voucher legs, and whether they balance. |

Set `SME_LUT` in `.env` first, or every export is held:
`EXPORT_WITHOUT_PAYMENT` asserts a Letter of Undertaking **on the invoice**,
and one LUT covers one financial year — see [the LUT is
annual](#the-lut-is-annual-and-this-population-is-not) below.

Start here, in this order:

```bash
./post_sage_invoices.py ar-selftest              # proves the arithmetic, needs nothing
./post_sage_invoices.py ar-probe                 # settles the schema against the real Sage
./post_sage_invoices.py ar-dryrun                # the whole population, no writes
./post_sage_invoices.py ar-masters --docs <inv>  # scoped to one invoice first
./post_sage_invoices.py ar-post    --docs <inv>
./post_sage_invoices.py ar-legs    --docs <inv>  # confirm the voucher ties
```

### What the open population actually is

Measured by `ar-probe` and `ar-dryrun` against the live company database:

| | documents | invoiced INR |
|---|---:|---:|
| Open at the cutover (`AROBL`, `AMTDUEHC > 0`, before 1-Apr-2026) | **259** | **330,630,578.18** |
| Shape cleanly and are ready for masters | **164** | |
| Held, with a reason and an amount, in `work/ar_held.json` | **95** | |

37 customers, dates spanning 12-Nov-2021 to 31-Mar-2026, in USD (the bulk),
INR and GBP. Open balance across the 259 is INR 130,428,668.69.

Measured with `SME_LUT` carrying **only** the FY2025-26 LUT, which is the only
one in hand. The held 95, largest first — none is a failure, each is a
decision:

| held | docs | invoiced INR | what it is |
|---|---:|---:|---|
| no LUT for the invoice's financial year | 38 | 52,930,699 | FY2021-22, FY2023-24 and FY2024-25 exports. Supply those LUT numbers and they shape. |
| no O/E lines (`SRCEAPPL='AR'`) | 34 | 98,835,107 | **A population this loader does not cover** — see below. |
| no destination country anywhere | 9 | 17,964,649 | `OEINVH.SHPCOUNTRY` blank *and* `ARCUS.CODECTRY` blank. An export invoice states where the goods went; this one does not. |
| Sage rounds each tax authority separately | 7 | 1,636,185 | See *the one-paise wall* below. |
| no `IESHPRGH` row | 4 | 752,498 | No shipping bill in the export register. |
| document total × rate ≠ home total | 1 | 2,054,959 | Sage disagrees with itself by 86 paise. |
| a line at zero quantity | 1 | 190,253 | |
| taxable + stated GST ≠ home total | 1 | 37,475 | Off by 370.90. |

**The 34 A/R-direct invoices are the biggest single gap, and they are out of
scope here by the same split the payables side has.** They were keyed straight
into A/R rather than raised in Order Entry, so Sage holds no record of what was
sold — no item, no quantity, no unit price. Loading them needs the A/R
distribution table and a revenue-account pseudo-item, which is exactly what
`post_sage_bills.py` does for AP-direct bills against `APIBD`. That is a second
population, not a bug in this one; it is counted and priced above so the
decision to build it can be made on numbers.

**The one-paise wall.** Sage rounds SGST and CGST to the paise *separately*, so
5% on 1,005.00 becomes 25.13 + 25.13 = 50.26 where a single slab gives 50.25.
The platform recomputes `gstAmount` from the line's one rate and ignores what
is sent, so **no legal slab satisfies both figures**. The payables path met the
same wall on reverse charge and chose to post and log the variance. A sale is
**held** instead: the alternative is a receivable filed at a total Sage does not
state, and one paise wrong in a ledger is far harder to find later than one line
in a report. 7 documents, INR 1.6M — if that trade is the wrong one for your
run, it is a single branch in `classify_invoice()`.

### The LUT is annual, and this population is not

A Letter of Undertaking is granted for **one financial year**, and the invoice
carries its number as `igstexemptionNumber` — it is a statutory reference on
the document, not a cosmetic field. The shapeable exports span four years:

| financial year | export documents | invoiced INR |
|---|---:|---:|
| 2021-2022 | 1 | 1,790,054 |
| 2023-2024 | 26 | 21,739,031 |
| 2024-2025 | 10 | 18,666,890 |
| 2025-2026 | 115 | 132,207,670 |

So `SME_LUT` takes one entry per year, and an export whose year has no entry is
held rather than stamped with another year's number:

```
SME_LUT=2023-2024:<number>@<yyyymmdd>,2024-2025:<number>@<yyyymmdd>,2025-2026:<number>@<yyyymmdd>
```

Note that the LUT quoted in the migration brief, `AD290426001291E` dated
1-Apr-2026, is an **FY2026-27** document — and **none** of the 152 shapeable
exports is in FY2026-27. It covers none of them.

### Reproducing the proven invoice

The export that proves this path end to end — Columbia `IDK2526E39840`,
USD 26,405.41 @ 92.55 = INR 24,43,820.70 — is **settled** in Sage
(`AMTDUEHC = 0`), so the cutover rule correctly excludes it from the load.
Reproducing a document and loading the migration are different questions:

```bash
./post_sage_invoices.py ar-dryrun --docs IDK2526E39840 --include-settled
```

`--include-settled` requires `--docs`, and on `ar-post` it prints a warning:
posting an already-settled document creates a receivable that should not exist.
`ar-selftest` covers the same invoice's arithmetic with no database at all.

### The schema names, and what the probe found

Everything on the payables side was written against a schema inventoried
column by column, and that inventory
(`migration-analysis/sage/01-sage-schema-inventory.md`) records AR (54 tables)
and OE (37 tables) as **out of scope** — the migration moved payables only.
The AR/OE names therefore came from the migration brief, not from that
inventory. They are registered in one place, `SCHEMA` in
`post_sage_invoices.py`.

**`ar-probe` has since checked them against the live database, and six were
wrong.** The names in the code are the corrected ones:

| the brief said | it is really | |
|---|---|---|
| `AROBL.IDTRXTYPE` | `TRXTYPEID` | An invoice is **12 or 14**, not 1, and the pair splits by `SRCEAPPL` — 12/AR keyed into A/R, 14/OE from Order Entry. 149,739 OE rows against 28,983 AR. |
| `ARCUS.LEGALNAME` | *does not exist* | `BRN` carries the GSTIN (188 of 431 customers); `IDTAXREGI1..5` are empty on **every** row. |
| `OEINVD.INVDLINE` | `LINENUM` | |
| `OEINVD.UNIT` | `INVUNIT` | |
| `OEINVD.EXTINVNET` | `EXTINVMISC` | The extended amount, despite the name — verified line for line against the proven invoice. |
| `OEINVD.RATETAX1` | `TRATE1..5` + `TAUTH1..5` | Tax is stated **per authority**. `TRATE1` alone reads 2.5 on an intra-state sale and called the 5% slab illegal on 22 of 32 documents. |
| `IESHPRGH.CODECTRY` | *does not exist* | The register carries no country at all; the destination is `OEINVH.SHPCOUNTRY`. Port is `PRTOFLOAD`, bill is `SBNO`/`SBDATE`. |

Re-run `ar-probe` before trusting these against any other company database.

Two consequences of the design, both deliberate:

- `ar-probe` reports every gap with candidates, and **every load runs the same
  check first and refuses to start on one**. A renamed column is named once,
  not discovered as a half-finished query on the twentieth invoice of a run
  that has already created masters.
- And the check that makes an assumed name *safe* rather than merely visible:
  `classify_invoice()` re-multiplies Sage's own stated document total by its own
  stated exchange rate and requires the product to equal `AROBL.AMTINVCHC` to
  the paise. If `INVNETWTX` or `INRATE` is the wrong column, every invoice is
  held with the delta printed. None is posted at a figure Sage does not state.
  It currently catches exactly one real document, off by 86 paise.

What the probe did **not** support is worth recording too: Sage does not
maintain `quantity × unit price = extension`. `IDEPLACC2025-010` states a unit
price of 0.000018 against an extension of 2,850.00 over 30,000 units, with no
discount on the line and neither `PRIUNTPRC` nor `UNITCONV` closing the gap.
The **extension** is the money, so it is what posts and the unit price is
derived from it; Sage's own stated price is kept on the line's `metaData` and
the divergence is listed in `work/ar_price_notes.json`. Asserting that identity
held 5 real documents over something Sage never promised.

`IESHPRGH`, the export shipping register, is the least certain of the lot and
is deliberately **best-effort**: a rename there holds the exports with a precise
reason instead of refusing the whole load.

### Where a sales invoice is not a bill

- **It is priced in the customer's currency and booked in the org's.** The
  voucher is always in rupees, so the document posts in INR at rate 1 with
  every line carrying its converted amount. Converting each line independently
  misses `AMTINVCHC` by up to n/2 paise, so the last line absorbs the residual.

  Confirmed against the backend rather than inferred:
  `EntityVoucherEntryCreateHelperService` builds the `SALES` voucher from
  `getVoucherBreakageForInvoice()`, whose revenue leg is

  ```java
  private BigDecimal getNetItemAmount(InvoiceLineItemDto dto) {
      return multiply(dto.getQuantity(), dto.getUnitPrice());   // no rate
  }
  ```

  and `invoiceDto.getConversionRate()` appears **nowhere** in that class — the
  controller reads it, credit/debit notes read it, the commercial-invoice
  converters read it, the voucher path does not. So a rate sent on an invoice
  is silently ignored and a document-currency amount would post as rupees.
  That method is also why the revenue leg is `quantity × unitPrice` rather than
  the line amount, so the unit price is kept at the six decimals the column
  stores and is where the residual is absorbed. `getTaxableAmount()` is the
  same product plus `taxableOtherCharge` less `discountAmount`, so the platform
  **derives** `taxableAmount` too and ignores what is sent — asserting
  `q2(unitPrice × quantity) == taxableAmount` is what makes the derived figure
  equal the intended one.
- **The counterparty is a BUYER**, a different ledger (Sundry Debtors) from the
  VENDOR ledger (Creditors) the bills path resolves. A party that both buys and
  sells is **one** contact with **both** roles — `POST /contact/` refuses a
  second one on the duplicate GSTIN anyway. The payables crosswalk is consulted
  first for exactly this reason; it currently offers 351 parties by GSTIN and
  17,220 item products, so a customer or item the AP run already created is
  reused rather than duplicated (the product gains an `ITEM_SALE` ledger beside
  its purchase one).
- **An export carries no GST.** Under a Letter of Undertaking it is
  `EXPORT_WITHOUT_PAYMENT` at 0%, and the document *asserts* the LUT number —
  so an export with no LUT for **its own financial year** is held, not posted
  under an exemption nobody can defend.
- **The document decides whether a sale is an export, not the customer
  master.** `ARCUS.CODECTRY` is blank on many export customers — Columbia, the
  reference invoice's own buyer, among them — so the master alone reads them as
  domestic and the foreign currency then holds every one of their invoices: 83
  of the 259 open documents, before this was fixed. The ship-to country and the
  invoice currency are stated on the document, and a blank field in a master
  file is not evidence against them.

### Not on this path

| | Why, and what happens |
|---|---|
| Documents settled before cutover | Opening trial balance. Re-posting one would recognise its revenue twice, so only `AMTDUEHC > 0` is selected. |
| Collections after cutover | These settle an invoice through `POST /receipt/` with the invoice in `references[]`. That payload contract is not proven on this box, so `ar-recon` **quantifies the backlog** from `AROBP` and posts nothing. |
| Scrip sales (ROSCTL, RoDTEP) | A scrip credits an asset head (`2A7SL20`); it is not revenue against a buyer. Detected and held for a journal voucher. |
| Inter-unit self-invoices | Same PAN on both sides — filed as `TRANSFER_INVOICE` off its own counter, which the loader does handle. |

Corrections: `PUT /invoice/{id}/REVOKED` soft-deletes the legs (`isDeleted=1`),
so every leg query filters `isDeleted=0`.

### The one place something is not read from a source

Sage carries no address for most export customers, and the platform's address
rows are `NOT NULL`. With `GEMINI_API_KEY` set, an international party with
no Sage address gets one from Gemini — fenced: it is told never to invent a
street, its own `LOW`-confidence answers are discarded, and every contact built
this way is stamped `addressSource=gemini-enriched:<confidence>` in `metaData`
so it can be re-sourced from a real document later. `--no-enrich`, or an unset
key, takes the flagged placeholder instead. Use the backend's already-configured
key (`ai.properties` → `gemini.api.key`); do not mint a new one.

It lives in `post_sage_bills.py` and serves **both** loaders, and it is also
asked for a **postal code** — see [foreign postal codes are
resolved](#foreign-postal-codes-are-resolved-not-stored) below. A code it
returns is put through the platform's own geo table before anything is filed
under it, so the enrichment cannot introduce one the platform does not already
know.

Verified on this org while wiring it up: the BUYER contact category
`1029658153400144541` is "Domestic Buyer" (the only `BUYER` category the org
has, so export customers use it too), `taxation` already carries
`BUYER`/`NONE` rows, and **no TDS or auto-deduction feature is enabled** on the
org — 49 features are on and not one of them matches `tds` or `deduct`, so
`INVOICE -> AUTO_TDS_DEDUCTION` is off, as the brief requires.

Work items land in `work/ar_held.json`, `work/buyers_held.json` and
`work/ar_failures.json`; live ids in `work/crosswalk_ar.json`, posted documents
in `work/posted_ar.log`.

## Foreign postal codes are resolved, not stored

A foreign postal code is not a free-text field on this platform. The address
create looks the **(code, country) pair** up in the platform's own geo table
and refuses a miss with `Invalid Zipcode` — the same answer
`GET /pincode/{code}?country={COUNTRY}` gives, from the same table. (An Indian
miss reads `Invalid Pincode`: different message, different table.)

That refusal cost **21 payables vendors and 3 receivables buyers**, every one of
them holding a real postal code Sage recorded. Worse, it arrived *after* the
contact was created, so the payables path had to roll the contact back and the
receivables path — where `DELETE /contact/{id}` is a 404 — could not.

So the code is settled **before** the contact exists, by
`verified_foreign_pincode()`, which offers candidates to the table in order and
takes the first it resolves:

| | candidate | |
|---|---|---|
| 1 | Sage's own code | as recorded |
| 2 | …its digits | `530-8605` → `5308605` |
| 3 | …reshaped to the level that country's table is populated at | Japan `5308605` → `530-8605`; Taiwan `40654` → `406` |
| 4 | codes Gemini gives for the **city** Sage recorded | only if a key is set |
| 5 | `999077` | the platform's own placeholder |

Nothing is sent that the table has not already confirmed, so the enrichment
cannot introduce a code the platform does not independently know, and a
reshaping never adds a digit — it is the same nearest-level reading
`resolve_item_hsn()` applies to HSN. The winning source is recorded as
`pinCodeSource` in `metaData`.

Two things make step 4 safe enough to keep:

- **It asks about a place, not a party.** "What are the postal codes of Dhaka"
  makes no claim about the vendor beyond the city Sage already recorded for it,
  and one answer serves every vendor in that city — which matters, because the
  enrichment budget is **20 calls per day** (below). The per-vendor question
  (`resolve_address_ai`) is never asked from this path; it is only *reused*
  when a caller already holds an answer for its own reasons.
- **The code has to resolve to the right city.** The lookup returns the city it
  resolved to, and it must equal the one Sage recorded — equality after
  normalising, never containment. Without that check a **Taipei** vendor was
  filed under `236`, which is *New Taipei*: adjacent, different, and it verified
  perfectly. A code is only as good as the place it resolves to.

### The enrichment budget is 20 calls a day

The backend's key is free-tier, and the binding quota is
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` = **20 — per day, per
model**, not per minute. That is a fact worth knowing before planning a run
over 64 international vendors.

It is also why a 429 is never swallowed. Discarded, it reads exactly like
"there is no postal code for this party", and the vendor would be held for a
reason that is not the real one. Instead the hold says the quota was exhausted
and that nothing was asked — one is finished by re-running, the other needs a
row in the platform's geo table. `GEMINI_MODEL` is the lever for headroom,
since the cap is per model.

**The table is sparse, and that is the real blocker.** Probed against the live
platform:

| country | what it holds |
|---|---|
| `JAPAN` | well covered, but **only hyphenated** — `530-8605` resolves, `5308605` does not |
| `UNITED_STATES` | well covered, the 5-digit ZIP (6 of 6 probed) |
| `TAIWAN` | the **3-digit district** only, and just 12 of 409 probed |
| `CHINA` | `999077`, plus real 6-digit codes |
| `HONG_KONG` | `999077`, `000000` |
| `VIETNAM` | `70000`, `700000` |
| `BANGLADESH` | `1000`, `1212` |
| `SOUTH_KOREA` | **nothing** — 0 hits in 65 probes spanning `01000`–`63999` |
| `TURKEY` | **nothing** — 0 hits in 82 probes spanning `01000`–`81999` |

`999077` is Hong Kong's, not a universal placeholder — it resolves for
`HONG_KONG` and `CHINA` and nowhere else, which is why the receivables path's
earlier blind retry onto it still lost its three buyers.

A Korean or Turkish party therefore **cannot** be given an address today, and is
held with that stated rather than retried into the same refusal. The fix for
those is a row in the platform's geo table — `POST /pincode/update` exists and
takes one — which changes shared reference data for every org on the box, so it
is a decision to take deliberately, not something either loader does on its own.

## Ground rules this code holds to

- **Sage is read-only.** Every statement sent to it is a `SELECT`.
- **Read the tax rate Sage states; never divide to infer one** (defect 4.1).
  Rates are validated against the legal GST slabs, never invented. On a sale a
  stated rate that is not a legal slab is held rather than snapped into shape —
  the rate is what the customer was actually charged.
- **Nothing is guessed.** Cases with no honest answer — a malformed GSTIN, a
  domestic recoverable line with no matching input head — are held for a
  decision and reported, not filled in with a plausible value.
- **Master creation is awkward to reverse**, so it stays scoped to what the
  run intends to post.
