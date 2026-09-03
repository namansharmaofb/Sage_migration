# Full ICITEM master — findings, 2 Sep 2026

Read directly from Sage over TDS (read-only SELECTs). The staging pull was NOT run:
`SAGE_USER`/`SAGE_PASSWORD` are not set on the devbox, and per the brief I did not
work around that. `pull.py` and `03_gap.sql` edits are prepared but not applied —
see "Not done" at the end.

## 1. Full master vs purchased

| | items |
|---|---:|
| `ICITEM` (unfiltered) | **1,196,108** |
| `idedat_staging.sage_item` (PO-scoped) | 17,129 |
| share of the master staging can see | **1.43%** |

`INACTIVE <> 0` on 333,583 of the master.

The scoping hypothesis is confirmed from the other direction: of the 50 categories in
the master, only **30** appear on a Jan–Apr purchase invoice — exactly the 30 staging
holds.

## 2. Category census

50 categories in `ICITEM`; `ICCATG` has 55 rows and carries the descriptions. Every
item category resolves to an `ICCATG` row. Five `ICCATG` categories have no items:
`1EINST`, `1EQUIP`, `1FFGHT`, `1GENR`, `1VEHIC`.

| cls | category | Sage `ICCATG` description | items | inactive |
|---|---|---|---:|---:|
| 4 | 4ACCES | Other Accessories | 547,317 | 187,770 |
| 4 | 4PACK | Other Packing Materials | 276,846 | 29,782 |
| 5 | 5WPSEW | WIP - Sewing | 58,724 | 23,468 |
| 5 | 5WPCUT | WIP - Cutting | 47,930 | 23,808 |
| 4 | 4FASHL | Shell Fabric | 44,908 | 7,479 |
| 4 | 4FABRI | Fabric | 43,247 | 33,656 |
| 4 | 4FAINT | Interlining | 37,622 | 5,841 |
| 4 | 4LABEL | Labels | 36,481 | 1,882 |
| 7 | 7IKEA | IKEA | 29,688 | 5,503 |
| 4 | 4THRED | Threads | 23,772 | 2,364 |
| 6 | 6FGOOD | **Finished Goods** | **11,222** | 4,559 |
| 4 | 4CARTN | Carton Box | 8,962 | 1,172 |
| 4 | 4BUTON | Buttons | 7,591 | 695 |
| 8 | 8JWPRO | Job Work - Processing | 6,279 | 1,569 |
| 7 | 7DOMES | Domestic Sales | 4,070 | 2,440 |
| 4 | 4POLYB | Poly Bag | 3,729 | 554 |
| 4 | 4ZIPER | Zippers | 2,068 | 671 |
| 2 | 2MNMEC | Maintenance - Mechanical | 1,228 | 0 |
| 4 | 4VELCR | Velcros | 1,068 | 142 |
| 4 | 4ELAST | Elastics | 989 | 97 |
| 4 | 4HANGR | Hanger | 816 | 123 |
| 2 | 2MNELE | Maintenance - Electricals | 697 | 0 |
| 2 | 2MNNED | Maintenance - Needles | 137 | 0 |
| 3 | 3OTGEN | Other Items - General | 133 | 0 |
| 2 | 2MNOTH | Maintenance - Others | 96 | 0 |
| 3 | 3OTSTN | Other Items - Stationery | 72 | 0 |
| S | SAMLCL | Sampling Goods - Local | 71 | 2 |
| 1 | 1PLMAC | Plant & Machinery | 67 | 0 |
| 2 | 2MNCOM | Maintenance - Computer | 63 | 1 |
| 2 | 2MNCHE | Maintenance - Chemicals | 52 | 0 |
| S | SAMEXP | Sampling Goods - Export | 39 | 2 |
| 1 | 1FURNI | Furniture & Fixtures | 21 | 0 |
| 1 | 1SOFTW | Software | 19 | 0 |
| 3 | 3OTCHG | Other Charges | 18 | 0 |
| 2 | 2MNPRN | Maintenance - Printing | 15 | 0 |
| 2 | 2MNEMB | Maintenance - Embroidery | 15 | 0 |
| 1 | 1COMPU | Computer | 8 | 0 |
| 2 | 2MNSER | Maintenance - Services | 8 | 0 |
| 8 | 8JWEMB | Job Work - Embroidery (REV) | 5 | 0 |
| 9 | 9TRHFR | Trading Home Furnishing | 2 | 2 |
| S | SCRFAB | Scrap Fabric | 2 | 0 |
| 2 | 2MNSOF | Maintenance - Software | 2 | 0 |
| 1 | 1AIRCO | Air Conditioners | 2 | 0 |
| S | SCRMNT | Scrap Maintenance & Other Items | 1 | 0 |
| S | SAMFAB | Sampling Fabric | 1 | 0 |
| S | SCRACC | Scrap Accessories | 1 | 0 |
| 9 | 9TRSTN | Trading Stationery | 1 | 1 |
| 9 | 9TRBAG | Trading Bags & Pouches | 1 | 0 |
| 8 | 8JWPCW | Job Work - Piece Work | 1 | 0 |
| 8 | 8JWPRN | Job Work - Printing (REV) | 1 | 0 |

## 3. Is there a finished-goods category? YES

`6FGOOD` — Sage's own description is literally **"Finished Goods"** — **11,222 items**,
4,559 of them inactive. Finished goods ARE in `ICITEM`; they are not in a separate
sales or style master.

They were invisible to staging for exactly the reason predicted: across **all of
history**, only **11** `6FGOOD` items have ever appeared on a PO invoice line. Indian
Designs manufactures them, so they are never purchased, and a pull reached only
through `POINVL` can never see them.

## 4. The 1/2/3/4 prefix reading — incomplete, not wrong

The four classes are correct as far as they go, but they are only what a
purchase-scoped extract can see. The master has **ten** prefixes, and 13.4% of all
items sit in the six that staging never had:

| prefix | meaning (from Sage's own descriptions) | items | share |
|---|---|---:|---:|
| 4 | raw material & trims | 1,035,416 | 86.6% |
| 5 | **WIP — cutting, sewing** | 106,654 | 8.9% |
| 7 | **sales — IKEA, domestic** | 33,758 | 2.8% |
| 6 | **finished goods** | 11,222 | 0.9% |
| 8 | **job work** | 6,286 | 0.5% |
| 2 | maintenance, spares, consumables | 2,313 | 0.2% |
| 3 | other — general & stationery | 223 | 0.0% |
| 1 | capital assets | 117 | 0.0% |
| S | **sampling & scrap** | 115 | 0.0% |
| 9 | **trading** | 4 | 0.0% |

Corrections to the earlier labels, now that Sage's own descriptions are available:

- **`4ACCES` is "Other Accessories" and is the largest category in the business at
  547,317 items** — staging saw 1,231 of them (0.2%).
- **`4FASHL` is "Shell Fabric"**, not a fashion label. It is fabric, not a trim.
- `4FAINT` is "Interlining"; `4PACK` is "Other Packing Materials".
- Class 1 is broader than "capital assets" as counted before (6 items): the master
  adds `1PLMAC` Plant & Machinery (67) and `1AIRCO` Air Conditioners (2), for 117.

## 5. A column that classifies item type better than CATEGORY: `ITEMBRKID`

`ITEMBRKID` — 15 values, **never blank on any of the 1,196,108 rows**. It is a type
axis, where CATEGORY is a commodity axis, and it cuts across categories:

| ITEMBRKID | items | distinct categories spanned |
|---|---:|---:|
| RAWMAT | 876,840 | 15 |
| RAWSHD | 151,621 | 15 |
| WIPSHD | 108,629 | 5 |
| **FINGUD** | **38,528** | 8 |
| WIP | 10,774 | 4 |
| OLDOC | 6,904 | 4 |
| MTMCEL | 1,553 | 2 |
| MAINT | 640 | 10 |
| OTHER | 205 | 2 |
| MAINTD | 120 | 1 |
| ASSET | 117 | 5 |
| STITEM | 116 | 3 |
| SAMPLE | 45 | 6 |
| SERVIS | 9 | 1 |
| EMBPRN | 7 | 3 |

The headline: **`FINGUD` spans 38,528 items where category `6FGOOD` holds 11,222** —
3.4x more finished goods than the category alone reports. For a raw-material vs
finished-goods split, `ITEMBRKID` is the better key.

Columns that do NOT help, checked and ruled out:

- `STOCKITEM` and `SELLABLE` are **1 on all 1,196,108 rows** — no discrimination.
- `TARIFFCODE` is blank on all rows.
- `CNTLACCT` mirrors CATEGORY one-for-one (each control account maps to exactly one
  category), so it adds nothing.
- `SEGMENT1` has 36 values but 10,234 blanks; `SEGMENT2` is blank on 922,454.

## 6. Timing and size

Every figure here came from aggregate SELECTs against the live master; the row count
returned in 1.4 s and the full 50-row census in a few seconds. The master is 1.2M
rows — small enough to re-pull, but ~70x the current `sage_item`, so it wants its own
table (`sage_item_master`) rather than widening the existing one.

## Not done, and why

- **The staging pull did not run.** `pull.py check` reports TCP and TDS fine to
  `ofbl-1649` from the devbox, but `SAGE_USER`/`SAGE_PASSWORD` are not set there.
  The brief says stop and ask rather than work around it, so I did.
- **`pull.py` and `03_gap.sql` are not edited.** The patch is written and ready at
  `work/patch_pull.py` (adds `Q["items_master"]` and `Q["item_category"]`, leaves
  `Q["items"]` untouched, extends `ORDER`, appends both `CREATE TABLE`s with Sage's
  own keys as PKs — `FMTITEMNO` and `CATEGORY`). Copying it to the devbox was blocked
  by the sandbox, so it needs to be applied there by hand.
- Once applied: `python3 pull.py pull --only items_master --only item_category --truncate`
