# RESOLVED — the 75 "burned" SKUs are healthy products locked out by a stale blocklist

**Class:** finding — resolves an open question · **Severity: MEDIUM, and cheap to fix** ·
**Evidence:** DATABASE VERIFIED + SCRIPT VERIFIED · **Confidence: VERIFIED**

The backend analyst left this open: *"Ticket 2 appears misdiagnosed — the save at
`ProductServiceImpl.java:228` does precede the checks, but `@Transactional(rollbackFor =
Exception.class)` rolls it back and the DB shows **zero** orphan rows across every
category-mandatory type, so the 'burned SKU' cause is unexplained."*

I can close it. **The backend analyst was right that nothing is orphaned — and the reason
is that nothing failed.** The 75 products exist, are complete, and are perfectly usable.

---

## 1. What the crosswalk claims

`work/crosswalk_live.json` carries a `burned` map with 75 entries:

```
ID41681A-TG01-412|NOS   ->  SAGE-ID41681ATG01412-NOS
ID41681B-TG01-414|NOS   ->  SAGE-ID41681BTG01414-NOS
ID41681C-TG01|NOS       ->  SAGE-ID41681CTG01-NOS
...
```

The loader's own definition, `post_sage_bills.py:3494-3496`:
```python
# Taken by a row adoption cannot see. Same rule as the GL
# pseudo-items: never mint a -R2 variant.
print("  BURNED %s - SKU taken by a row adoption cannot see" % sku)
state.xw.setdefault("burned", {})[key] = sku
```
And `:3394-3398` records a deliberate narrowing — only a *genuine adoption failure* is
recorded as burned, because *"this used to record every failure… so the burned list could
not be read as evidence of anything."*

So "burned" means: **`POST /product` returned "Sku Code already exists", and the lookup
that should have adopted the existing row could not find it.**

---

## 2. What the database says

```sql
SELECT CAST(isDeleted AS UNSIGNED) del, COUNT(*) n FROM product
WHERE organisationId=<org> AND skuCode IN (<the 75 SKUs>) GROUP BY del;
```
→ **`del 0 · n 75`** — all 75 exist, none deleted.

And they are not half-built:

```sql
SELECT COUNT(*) products,
       SUM(p.categoryId IS NOT NULL AND p.categoryId<>'')  with_category,
       SUM(p.hsnCode   IS NOT NULL AND p.hsnCode<>'')      with_hsn,
       SUM(p.hsnCode='9999')                                placeholder_hsn,
       SUM(EXISTS(SELECT 1 FROM financeAccountReferenceMapping m
                  WHERE m.referenceNumber=p.id AND m.organisationId=<org> AND m.isDeleted=0)) with_ledger,
       SUM(EXISTS(SELECT 1 FROM billLineItem l WHERE l.productId=p.id)) ever_used_on_a_bill
FROM product p WHERE p.organisationId=<org> AND p.isDeleted=0 AND p.skuCode IN (<the 75>);
```

| products | with category | with HSN | placeholder HSN | **with ledger** | **ever used on a bill** |
|---:|---:|---:|---:|---:|---:|
| 75 | 75 | 75 | 5 | **75** | **0** |

**Every one of the 75 is complete — category, HSN and a finance-account mapping — and not
one has ever appeared on a bill line.**

---

## 3. What actually happened

This was never a data-integrity failure. It was a **lookup** failure:

1. The product already existed (created by an earlier run, or by the PowerShell generation).
2. `POST /product` correctly rejected the duplicate SKU.
3. The loader's adoption path tried to find the existing row and **could not** — because,
   as commit `c06303a` puts it, **"searchKey does not search"**. The product lookup was
   broken.
4. The loader, correctly refusing to mint a `-R2` variant, recorded the SKU as burned.
5. The lookup was later **fixed** (`c06303a` "Replace the product lookup: searchKey does not
   search"). **The blocklist was not cleared.**

So the burned list is a fossil of a bug that no longer exists. `ticket 2` in
`sage-migration-tickets.md` — *"a rejected product create still uses up the product code"* —
describes a real hazard, but **it is not what produced these 75 entries**, which is exactly
why the backend analyst found no orphans.

---

## 4. Why it still matters

`ensure_item_products` skips any key in `burned`, permanently. So:

- **75 complete, ledgered, categorised products are invisible to the loader.**
- Any goods document whose lines need one of them fails with a "no product" reason and is
  counted in the 9,217 goods failures.
- They will never self-heal: the crosswalk is consulted before the API, so the fixed lookup
  is never given the chance to succeed.

This belongs to the pattern that recurs throughout this audit — **a defect was fixed in the
code, and the state it had already written was left behind**, exactly as with the mis-headed
ledgers (`risks/01`). Here, unusually, the leftover state is *cheap and safe* to clear.

---

## 5. Recommendation — REPORTED, NOT IMPLEMENTED

1. **Clear the `burned` map** in `work/crosswalk_live.json` and re-run `goods-masters`. With
   the corrected lookup, all 75 should adopt cleanly onto the existing product ids. Nothing
   needs creating and nothing needs deleting.
2. **Verify first, in one read-only pass**, that the current lookup finds all 75 by SKU —
   that converts this from "should work" to "will work" before anything is changed.
3. **Give the blocklist an expiry.** A permanent skip-list keyed on a transient API failure
   will fossilise again the next time a lookup breaks. Record *why* and *when* alongside the
   SKU, so a later run can re-evaluate rather than inherit.
4. **Check the 5 with placeholder HSN separately** — they belong to the `9999` population in
   `contradictions/02`, and adopting them will carry that placeholder onto bill lines.
