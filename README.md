# Sage → SMEAssist migration

Pulls AP data out of a Sage 300 company database (read-only) and posts it into
SMEAssist, a Java/Spring ERP, through its REST API.

The working window for this codebase is the **Jan–Apr 2026** AP-direct bills:
`post_sage_bills.py` is a port of the original PowerShell loader
(`load_janapr_bills.ps1`) with six field-level defects fixed, each annotated
inline against its defect number.

## What is in the repo

| Path | What it is |
|---|---|
| `post_sage_bills.py` | The loader. All phases live here — see below. |
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

## Running

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

## Ground rules this code holds to

- **Sage is read-only.** Every statement sent to it is a `SELECT`.
- **Read the tax rate Sage states; never divide to infer one** (defect 4.1).
  Rates are validated against the legal GST slabs, never invented.
- **Nothing is guessed.** Cases with no honest answer — a malformed GSTIN, a
  domestic recoverable line with no matching input head — are held for a
  decision and reported, not filled in with a plausible value.
- **Master creation is awkward to reverse**, so it stays scoped to what the
  run intends to post.
