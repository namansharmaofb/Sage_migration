# Sage 300 → SMEAssist — read-only migration audit

> **Start with [`FINAL-REPORT.md`](FINAL-REPORT.md)** — the full findings, and the
> *"What I need you to correct"* section that this whole phase exists to produce.

**Phase 0. Discovery and validation only. Nothing was changed.**

Every database statement in this audit was a `SELECT` / `SHOW` / `DESCRIBE`. No migration
script or phase was executed — not even `dryrun`, which reaches the live API. No backend
code, migration script, configuration or data was modified. Nothing was committed, pushed
or deployed. Where something was found to be wrong, it is reported here and left alone.

Produced 2026-09-05 by a lead analyst coordinating nine specialist agents, against four
independent evidence sources: live Sage 300 (`IDEDAT`), live SMEAssist (MySQL + REST), the
SMEAssist backend source, and the migration scripts.

---

## Read this first, if you read nothing else

**1. Do not run `./run_all.sh` until the currency defect is fixed.** The goods path reads
`sage_bill_hdr.doc_total` — the **source-currency** amount — and posts it as INR at rate 1.0.
1,468 foreign-currency documents are contact-ready and will post on the next run:
**₹38.18 crore understated**. Nothing currently in the project detects it, because the
reconciler reads the same wrong column on both sides. → `risks/04`

**2. The migration's arithmetic is good; its completeness is not.** Of 9,371 documents
compared, only **3** differ from Sage by ≥ ₹1. But only **34.2% of in-window Sage documents
by count, and 13.5% by value**, have crossed. The problem is scope and blockers, not
transformation logic. → `validation/01`

**3. The single highest-leverage action is not code.** 10,209 of 11,179 blocked documents
(91%) are waiting on **289 vendor contacts**, and 157 of those are held for one missing
CIN/LLPIN file that does not exist in Sage. → `risks/02`

**4. This is a rehearsal.** The target org is `wonderblues`, a stand-in on the devbox — not
Indian Designs. Every number here measures method fidelity, not a delivered migration.

---

## The reports

### Orientation
| File | What it covers |
|---|---|
| `project-map/01-project-structure.md` | What is in the starting folder, what is not, where the other three evidence sources live, and the two competing migration mechanisms |
| `project-map/02-documentation-analysis.md` | Every document found, what it claims, which claims survived checking — and why there is **no Sage 300 vendor documentation anywhere** in this environment |

### Source A — Sage 300
| File | What it covers |
|---|---|
| `sage/00-the-exclusion-funnel-attributed.md` | The **₹104.43 crore** that never enters the pipeline, split into four decisions with four different answers |
| `business-flows/01-sage-actual-flow.md` | The **proven** Sage flow, hop by hop, with real linking keys; document traces; the Jan–Apr 2026 population; flow anomalies |

### Source B — SMEAssist database
| File | What it covers |
|---|---|
| `smeassist/01-target-schema-and-state.md` | Target schema (39 of 285 tables in use), constraints, and exactly what is loaded today |

### Source C — SMEAssist backend
| File | What it covers |
|---|---|
| `backend/01-smeassist-backend-analysis.md` | How the server actually behaves: amounts recalculated and silently overwritten, ids always generated, GST decided by billing-address state, deployed-vs-checkout |

### Source D — migration scripts
| File | What it covers |
|---|---|
| `migration-scripts/01-script-audit.md` | Per-phase audit of the loader, `extract.sql`, `run_all.sh` and 30 `work/` scripts; independent validation of every transformation that carries money |
| `migration-scripts/02-bugs-and-risks.md` | Every defect ranked CRITICAL/HIGH/MEDIUM/LOW, with blast radius and whether it has already damaged live data |

### Findings
| File | What it covers |
|---|---|
| `risks/01-mis-headed-item-ledgers.md` | **₹8.72 Cr** of expense under the wrong head — misclassification, not double-counting |
| `risks/02-the-real-blocker-is-289-vendors.md` | 10,209 documents blocked by 289 vendor contacts; 157 need one CIN/LLPIN file |
| `risks/03-discounts-are-never-read.md` | Discount hardcoded to zero; 531 of 531 documents tie once it is applied |
| `risks/04-goods-path-posts-foreign-currency-as-rupees.md` | **The most time-critical finding.** ₹48.03 Cr armed, ₹38.18 Cr on the next run |
| `risks/05-the-75-burned-skus-are-not-burned.md` | 75 healthy products locked out by a stale blocklist — cheap to clear |
| `risks/06-two-documents-carry-all-the-variance.md` | ₹1,457,957.66 on two documents **is** the migration's entire per-document value error |

### Contradictions
| File | What it covers |
|---|---|
| `contradictions/01-the-two-reports-disagree.md` | The project's own two reports differ by 6,827 documents; the gap closes **exactly** |
| `contradictions/02-product-provenance-is-silently-dropped.md` | Three sources disagree on whether placeholder products are flagged; the database wins |
| *(adversarial validation)* | **NOT PRODUCED** — the agent was stopped before reporting. See "What is missing" below |

### Validation and reconciliation
| File | What it covers |
|---|---|
| `validation/00-lead-independent-checks.md` | The lead's own 18 checks, including **corrections to two agents' conclusions and to one of my own** |
| `validation/02-evidence-matrix.md` | Every load-bearing claim, and which of the five sources supports it |
| `validation/01-financial-validation.md` | Accounting model both sides, value reconciliation, tax, RCM, opening balances, and the sceptic's list |
| `reconciliation/01-reconciliation-strategy.md` | How we will eventually *prove* the migration is correct — lead-authored, condensed |

### Mapping
| File | What it covers |
|---|---|
| `mappings/01-object-and-field-mapping.md` | Object map (master / transaction / derived / opening balance), the two rules that govern all of it, and the money-carrying fields — lead-authored, condensed |

### Proposed fixes
| File | What it covers |
|---|---|
| `proposed-fixes/01-proposed-fixes.md` | What should eventually change, ranked. **Nothing here has been implemented.** |

---

## How to read the evidence labels

Every conclusion carries at least one:

| Label | Means |
|---|---|
| `DATABASE VERIFIED` | a query against live Sage or SMEAssist returned it |
| `BACKEND VERIFIED` | the Java source says so, cited to `file:line` |
| `SCRIPT VERIFIED` | the migration code says so, cited to `file:line` |
| `DOCUMENTED` | an internal document asserts it (**not** the same as proven) |
| `INFERRED` | reasoned, not directly observed |

and a confidence: `VERIFIED` / `HIGH` / `MEDIUM` / `LOW` / `UNKNOWN`.

**`DOCUMENTED` is the weakest label here**, because there is no Sage 300 vendor
documentation in this environment — only internal handover notes. Sage table meanings were
proven from data, never from an expanded abbreviation.

---

## What is missing, and why

Three of the nine agents were stopped before reporting. Two of their outputs were replaced by
condensed lead-authored versions (`mappings/01`, `reconciliation/01`), each carrying a
provenance note and explicit `NOT COVERED` sections. One was **not** replaced:

**Adversarial validation was not performed.** An agent was dispatched whose only job was to
attack the load-bearing claims — the join keys, the denominators, the absence claims, the
arithmetic that closes "too neatly". It did not report.

That matters for how you read this audit. The findings here were cross-checked in three ways
— the lead re-ran the load-bearing claims independently (`validation/00`, 18 checks), several
agents converged on the same defects from different directions, and three claims were
corrected as a result. But **nobody was specifically tasked with proving the conclusions
wrong**, and that is a different and stronger test than agreement.

Treat the `VERIFIED` labels as "independently reproduced", not as "survived a hostile
review". The claims most deserving of a sceptical second look are listed at the end of
`FINAL-REPORT.md`.

## What this audit deliberately did not do

- It did not fix anything, including defects that are actively damaging data.
- It did not run the migration, any phase of it, or any repair script.
- It did not resolve the business decisions it surfaces — scope exclusions, the CIN file,
  the direct/indirect classification of 132 products. Those are the human's to make.
