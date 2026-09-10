# Project structure — what actually exists

*Lead-agent discovery pass. Everything below was read off disk on 2026-09-05.*

Evidence labels used throughout this audit: **DOCUMENTED** (an internal document says
so) · **DATABASE VERIFIED** (a query returned it) · **BACKEND VERIFIED** (the Java
source says so) · **SCRIPT VERIFIED** (the migration code says so) · **INFERRED**.

---

## 1. The four evidence sources are all live

| Source | Location | Reachable now? | Evidence |
|---|---|---|---|
| **A — Sage 300 DB** | MS SQL Server 2025, database `IDEDAT`, host from `.env:SQL_HOST`, port 1433 | **YES** | DATABASE VERIFIED — `SELECT @@VERSION` returned `Microsoft SQL Server 2025 (RTM) - 17.0.1000.7 (X64)`; 1,110 tables, 575 non-empty |
| **B — SMEAssist DB** | MySQL 8.0.46 on `.env:SME_DB_HOST`, reached over ssh as root | **YES** | DATABASE VERIFIED — schemas `smeassist` (285 tables), `smeassist_audit` (4), `idedat_staging` (21), `yoda`, `yoda_audit` |
| **C — SMEAssist backend** | `~/Desktop/PROJECTS/smeassist` — Java/Spring Boot, multi-module Maven, ~50 modules | **YES, on disk** | Source tree present; git HEAD `2785b4c3a3` |
| **D — Migration scripts** | `~/Desktop/sage-pull` (this folder) | **YES** | 8,518 lines of Python, 417 of shell, 3,000 of Markdown findings |

The SMEAssist **API** is also live: `.env:SME_BASE` → port 9069 answers.

> **This is not a folder of dead artefacts.** Both databases are live and the loader
> has already written thousands of rows into the target. Read-only discipline is not
> ceremony here — a careless `dryrun` invocation reaches the live API.

---

## 2. The starting folder, annotated

### 2.1 The loader and its orchestration

| Path | Lines | What it is |
|---|---|---|
| `post_sage_bills.py` | **3,943** | The whole loader. 9 phases: `cleanup`, `masters`, `dryrun`, `post`, `verify`, `legs`, `goods-masters`, `goods-dryrun`, `goods-post`. Reads Sage over TDS, posts to SMEAssist REST, verifies over MySQL. |
| `run_all.sh` | 163 | Orchestrator: preflight (`.env` → API → **Sage host relocation** → auth token), a PID lock, then `masters → post → goods-masters → goods-post → reconcile`. `--check` is read-only and deliberately runs *during* a live run. |
| `extract.sql` | 425 | Sage extraction SQL, split on `-- @@name:` markers into 7 blocks: `bills_header`, `bills_lines`, `notes_header`, `notes_lines`, `vendors`, `gl_accounts`, `control_counts`. Every statement is a `SELECT`. |
| `devbox.sh` | 19 | Runs a command in `/root/indiandesign/converter` on the devbox over ssh, piping the auth cookie via stdin so it never reaches a process list. |
| `requirements.txt` | 2 | `pymssql==2.4.0`, `requests==2.31.0` |

**Note on `extract.sql`**: its own header says *"pull.py reads it, splits it on the
@@name markers"* — and **`pull.py` is not in this tree** (the README admits this). So
the CSVs in `output/` were produced by a runner that is no longer present. That is a
reproducibility gap, flagged for the audit. `work/patch_pull.py` (133 lines) appears to
be a patcher *for* that missing runner. **Confidence: HIGH** that `output/*.csv` cannot
currently be regenerated from this folder alone.

### 2.2 Working scripts (`work/`)

Grouped by what they are for — names are not reliable, so these are classified by
reading them:

- **Reconciliation** (the largest block): `master_recon.py` (1,066), `value_recon.py`
  (650), `reconcile.py` (189), `failure_report.py` (133), `item_master_recon.py` (59)
- **Repair / one-off writes** — *these write to the live system; not to be run in
  this phase*: `repost_stranded_parts.py` (218), `repost_po_items.py` (116),
  `make_passthru_ledgers.py` (104), `fix_qty_unitprice.py` (86), `cleanup_probes.py`
  (82), `cleanup_pilot.py` (48), `fix_misnamed_products.py`, `revive_probe.py`,
  `rename_probe.py`
- **Master-data builders**: `build_item_categories.py` (262), `build_cin_map.py` (168)
- **Infrastructure**: `find_sage.py` (257) — locates the Sage box when DHCP moves it;
  `sage.py` (38) — the read-only connection helper; `q.sh` — SQL over ssh to the devbox
- **Probes** (13 files, read-only investigations): `probe_state_*.py`,
  `probe_stock_type*.py`, `probe_real_item.py`, `probe_asset_ledger.py`,
  `probe_missing_state.py`, `apibh_probe.py`, `apibh_scope.py`, `ap_summary.py`,
  `po_validate.py`, `po_holds.py`, `check_po_items.py`, `unit_consistency.py`,
  `try_one_contact.py`

### 2.3 The project's own findings (3,000 lines of Markdown)

These are prior conclusions, and they are **source D, not truth** — every claim in them
is a candidate for verification:

| File | Lines | Subject |
|---|---|---|
| `work/RECON-BOTH-SITES.md` | 450 | Two-sided reconciliation design |
| `work/CHANGES-2026-09-03.md` | 381 | A day's changes, annotated |
| `work/FIX-PROPOSAL.md` | 285 | Proposed fixes (not applied?) |
| `work/RUNBOOK-JanApr-2026.md` | 199 | The run book |
| `FINDINGS-ITEM-MASTER.md` | 172 | Item master problems |
| `work/FINDINGS-BURNED-SKUS.md` | 172 | SKUs consumed by failed creates |
| `work/FINDINGS-READBACK-BROKEN.md` | 153 | Post-verify readback was broken |
| `work/FINDINGS-FROM-LAPTOP-2.md` | 108 | Cross-machine findings |
| `work/FINDINGS-EXPENSE-SAC.md` | 100 | SAC codes for expense lines |
| `MACHINE-CHANGES.md` | 625 | Infrastructure changes (gitignored) |

### 2.4 Data and state (all gitignored)

| Path | Size | What |
|---|---|---|
| `work/crosswalk_live.json` | **4.2 MB** | The live Sage↔SMEAssist id map — the migration's memory |
| `work/failures-report.json` | 2.1 MB | Everything not posted, with reason and amount |
| `work/posted.log` | 390 KB | Append-only, one line per posted document — the resume ledger |
| `work/master-mismatches.json` | 523 KB | Master-data differences |
| `work/value-mismatches.json` | 365 KB | Per-document value differences |
| `work/po_items_cache.json` | 367 KB | PO item cache |
| `work/goods_failures.json` | 366 KB | Goods-stream failures |
| `work/reconcile-report.json` | 106 KB | Sage vs SMEAssist, per document |
| `output/*.csv` | 2.8 MB | Extracted Sage data (7 files) |
| `ref/*.psv` | 2.7 MB | The Windows-era extracts, pipe-separated |

**Risk noted immediately**: the crosswalk lives in an **untracked local file**, and it
has been hand-repaired at least four times — `crosswalk_live.json.b4-intl2-*`,
`.before-intl-*`, `.pre-unburn-101355`. Meanwhile `idedat_staging.crosswalk` in MySQL
is **0 rows**. DATABASE VERIFIED. The migration's identity map is a single file on one
developer's laptop.

### 2.5 Backup sprawl

Nine `post_sage_bills.py.*` snapshots (67 KB → 193 KB), plus `.b4-*` copies of the
crosswalk and held-contacts. Useful as history; also a sign of edits made under
pressure directly against a live loader.

---

## 3. What is NOT in the starting folder (and matters)

| Missing | Where it actually is | Consequence |
|---|---|---|
| SMEAssist backend source | `~/Desktop/PROJECTS/smeassist` | Source C is outside the folder; had to be located |
| SMEAssist frontends | `~/Desktop/PROJECTS/smeassist-fe`, `-admin` | Used to confirm which APIs are UI-reachable |
| `pull.py` (the extract runner) | **nowhere on this machine** | `output/*.csv` not reproducible from this tree |
| The Windows-era pipeline | `~/Desktop/sage-pull/ref/` holds its outputs and its PowerShell loader only | Generation 1 code itself is on another machine |
| The bulk-upload route | `~/Desktop/smeassist-bulk-templates/` | A **second, different** migration mechanism — see §4 |
| Sage documentation | No Sage 300 vendor PDFs exist on this machine | Sage table meanings must be proven from data, not read |

**Finding — the only PDF that matters is not Sage documentation.** A full sweep of
`~/Desktop` found 40 PDFs; 39 are MSME/Udyam certificates belonging to an unrelated
project. The single relevant one is
`~/Desktop/Admin pdf/SME Assist __ Finance Account - Organisation Finance Accounts.pdf`
— a print of **SMEAssist's** chart-of-accounts screen, not Sage's. It contains **no
hyperlinks** (verified by scanning the file for URIs — none present). So the
instruction to *follow hyperlinks in the PDFs* has no target: there are none.
**There is no Sage 300 vendor documentation available in this environment at all.**
Every Sage table meaning in this audit therefore rests on data evidence plus one
internal handover document — never on a vendor manual.

---

## 4. Two migration mechanisms exist, and they are different

This is a structural fact the folder layout hides.

**Mechanism 1 — bulk Excel upload** (`~/Desktop/smeassist-bulk-templates/`, Aug 2026).
Eight sheets uploaded in a fixed order to `POST /bulkOperations/upload/<ENTITY_TYPE>`:
ledgers → HSN → items → suppliers → service providers → unregistered contacts →
bill series → bulk bill create. Its own README records that on 2026-08-20, **eight runs
across 24 rows produced only 2 bills**, blocked by two prerequisites no sheet creates
(a pincode-matched billing address on both contact and org, and exactly one finance
account per contact). DOCUMENTED.

**Mechanism 2 — direct REST posting** (`post_sage_bills.py`, Sep 2026). The current
approach. Creates masters and bills through the ordinary create APIs.

The backend tickets in `~/Desktop/PROJECTS/smeassist/sage-migration-tickets.md` describe
work done for **mechanism 1** ("two doors into SMEAssist: the bulk upload sheets and the
normal create APIs"). Whether the current loader depends on any of that backend work —
which is **currently reverted on the branch** (HEAD is *"revert the Sage migration
backend changes"*) — is a live question this audit must answer.

---

## 5. Git state

Branch `value-recon`, 13 commits, remote `origin` has both `main` and `value-recon`.

**Five files are modified and uncommitted** (+282 / −54 lines):
`post_sage_bills.py`, `run_all.sh`, `work/find_sage.py`, `work/master_recon.py`,
`work/repost_stranded_parts.py`.

Commit history reads as a field diary rather than a feature log — *"Repost the bills
that posted short of Sage"*, *"Post the international vendors; stop four sources of
false failure"*, *"Find the Sage box when DHCP moves it; stop running blind"*,
*"Replace the product lookup: searchKey does not search"*, *"Pace the API in one place;
stop burning SKUs on throttled lookups"*. Each names a defect found in production.

---

## 6. The operating environment is fragile, and the code knows it

`run_all.sh` documents two environmental facts that shape data quality:

1. **The Sage server is a Wi-Fi DHCP laptop, not a server.** Its address moved four
   times in five days, twice into a different /24. A stale address does not fail
   cleanly — the office subnet overlaps the lease range, so an unrelated device answers.
   There is also *a second SQL Server on the network that refuses the migration's
   login*, so an open port is not proof of Sage. DOCUMENTED (`run_all.sh` comments).
2. **Running with Sage unreachable silently corrupts data.** Per the same comments:
   `item_hsn_map()` returns `{}`, `resolve_item_hsn()` skips its `ICITEMO` tier, and the
   run *"succeeds"* while stamping a permanent misclassification that later runs skip —
   *"what wrote 1,419 products with the 9999 placeholder HSN"*. DOCUMENTED; the count
   is under independent verification in this audit.

That second point is the clearest example of the pattern this audit is looking for:
**a failure that reports success.**
