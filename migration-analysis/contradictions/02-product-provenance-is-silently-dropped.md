# Contradiction #2 — three sources disagree about whether placeholder products are flagged

**Severity: MEDIUM-HIGH** · **Evidence:** SCRIPT VERIFIED + DATABASE VERIFIED +
DOCUMENTED · **Confidence: VERIFIED**

Three sources make incompatible statements about the same thing. The database wins, and
the consequence is that **1,591 placeholder products cannot be distinguished from
correctly-classified ones**.

---

## 1. The three statements

**(a) The loader says it flags them.** `post_sage_bills.py:238-242`:
> *"…cannot. Flagged `hsnIsDefault` in metaData exactly like `EXPENSE_SAC`."*
> `EXPENSE_SAC = "996719"  # DEFAULT awaiting finance sign-off; flagged in metaData`

And it genuinely sends them — `post_sage_bills.py:2083-2084`:
```python
"metaData": {"sageAccount": acct, "migrationSource": "IDEDAT",
             "hsnIsDefault": "true"},
```

**(b) The project's own agent brief asserts they are there.**
`.claude/agents/sage-recon.md`, under *"Known modelling differences — not defects"*:
> *"`9999` HSN. A deliberate visible placeholder… **`996719` (`EXPENSE_SAC`).** Same shape:
> a flagged default awaiting finance sign-off. **Both carry `hsnIsDefault` / `sacIsDefault`
> in `metaData`.**"*

**(c) The database says the field is empty.**
```sql
SELECT COUNT(*) products, SUM(meta IS NULL) meta_null
FROM product WHERE organisationId=<org> AND isDeleted=0;
```
→ `products 17,450 · meta_null 17,450`

**`product.meta` is NULL on every single migrated product.**

---

## 2. It is not that the platform drops metadata generally

The same run's **bills** keep theirs. A real row from `bill.metadata`:

```json
{"sageDoc": "PRRABH.KER", "sageRcm": "False", "sageItem": "12", "sageBatch": "17121",
 "sageVendor": "OTHX008", "sageBillType": "DIRECT_EXPENSE", "sageTaxGroup": "NOTAX",
 "sageTypeNote": "mixed: DIRECT_EXPENSE 25364.00, IN_DIRECT_EXPENSE 15945.00",
 "migrationSource": "IDEDAT"}
```

So provenance survives on `bill` and is discarded on `product`. **`POST /product` accepts
`metaData` and does not persist it.** The loader gets a `200`, records success, and the
flag is gone. This is the platform's characteristic failure shape — *the call reports
success and silently loses data* — the same class as errors #1–#5 in the handover's own
register.

*(`contact` has no `meta` column at all, so contact provenance has nowhere to go either.)*

---

## 3. Why it matters, and it is sharper than it looks

The brief's advice — treat `9999` and `996719` as *known, flagged* placeholders rather than
mismatches — **cannot be followed**, because the flag does not exist in the data. The only
way left to find them is to match the literal code. For `9999` that is tolerable: it is not
a valid HSN, so the value is self-identifying.

**For `996719` it is not.** `996719` is a *real, valid SAC* — "other support services /
goods transport support". So:

| | products |
|---:|---|
| carrying `hsnCode = '9999'` | 1,419 |
| carrying `hsnCode = '996719'` | 172 |
| **of the 172, distinguishable as deliberate placeholders** | **0** |

A product legitimately classified at SAC 996719 is now **indistinguishable** from one the
migration defaulted there awaiting finance sign-off. The flag that was designed to make
that distinction was sent, accepted, and dropped.

And the population is not stable: the project's own notes record that the 1,419 figure
includes roughly 850 products that took the `9999` default **only because Sage was
unreachable during one run** — a run that "succeeded". Those are not genuinely
unclassifiable items; they are collateral from an outage, and there is no flag on them
saying so.

---

## 4. What the database can still tell you

Provenance is not entirely lost. `bill.metadata` carries `sageDoc`, `sageVendor`,
`sageItem`, `sageBatch`, `sageBillType`, `sageTaxGroup`, `sageRcm` and `migrationSource`,
and `billSeriesPrefix IN ('SAGE','SAGE27')` identifies migrated bills. So a product's Sage
origin can be **recovered indirectly** by joining through the bill lines it appears on.

That does not help the 17,126 products that have never appeared on a bill line — for those,
`work/crosswalk_live.json` on one laptop is the only remaining record of what they came from.

---

## 5. Recommendation — REPORTED, NOT IMPLEMENTED

1. **Establish with the backend team whether `POST /product` is supposed to persist
   `metaData`.** If yes, it is a backend bug; if no, the loader is writing into a field that
   does not exist and should stop pretending it has provenance.
2. **Correct `.claude/agents/sage-recon.md`.** It instructs future agents to rely on a flag
   that is not in the data — a brief that is confidently wrong is worse than one that is
   silent, because it stops the reader from checking.
3. **Do not use `996719` as a placeholder value.** A placeholder must be impossible to
   confuse with a real classification, which is exactly why `9999` was a good choice and
   `996719` is not.
4. **Recover the ~850 outage-induced `9999` products** while the crosswalk still exists —
   they are repairable from Sage's `ICITEMO.HSNCODE` now that Sage is reachable, and they
   are a different population from the ~572 items that genuinely have no HSN.
