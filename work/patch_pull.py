#!/usr/bin/env python3
"""Add the UNFILTERED item master and the IC category master to pull.py.

Run ON THE DEVBOX:  python3 patch_pull.py
Then:               cd /root/indiandesign/converter
                    python3 pull.py pull --only items_master --only item_category --truncate

Q["items"] is NOT touched: it is PO-scoped by design and emit/bills.py depends
on that population.
"""
import io, shutil, time

stamp = time.strftime("%Y%m%d-%H%M")
P = "/root/indiandesign/converter/pull.py"
S = "/root/indiandesign/converter/schema/03_gap.sql"

src = io.open(P, encoding="utf-8").read()
# Same staleness guard the brief specifies: no ratetax1 means a stale copy that
# would revert the 2 Sep stated-GST-rate work.
assert "ratetax1" in src, "STALE pull.py (no ratetax1) - refusing to write"

shutil.copy(P, P + ".bak-" + stamp)
shutil.copy(S, S + ".bak-" + stamp)

entries = '''
# The UNFILTERED item master. Q["items"] above is PO-scoped BY DESIGN and
# emit/bills.py depends on that population, so it is left exactly as it is.
#
# Scope is the whole point here: ICITEM holds 1,196,108 items and only 17,129
# have ever appeared on a Jan-Apr purchase invoice, so the PO-scoped pull sees
# 1.4% of the master and NO finished goods - Indian Designs manufactures them,
# so they are never purchased. Category 6FGOOD appears on 11 PO invoice lines
# in all of history.
#
# ITEMBRKID is carried because it classifies item TYPE better than CATEGORY
# does: 15 values against 50 categories, never blank on any row, and FINGUD
# spans 38,528 items where category 6FGOOD holds 11,222. STOCKITEM and SELLABLE
# are carried only to record that they are useless here - both are 1 on all
# 1,196,108 rows.
Q["items_master"] = ("sage_item_master", [
    "item_no_fmt", "item_no_raw", "description", "category", "stock_unit",
    "item_break_id", "ctrl_account", "def_price_list", "segment1",
    "stock_item", "sellable", "inactive", "date_last_maint"], """
SELECT RTRIM(i.FMTITEMNO), RTRIM(i.ITEMNO),
  REPLACE(REPLACE(REPLACE(RTRIM(i.[DESC]),CHAR(13),' '),CHAR(10),' '),'~^~',' '),
  RTRIM(i.CATEGORY), RTRIM(i.STOCKUNIT), RTRIM(i.ITEMBRKID),
  RTRIM(i.CNTLACCT), RTRIM(i.DEFPRICLST),
  REPLACE(REPLACE(REPLACE(RTRIM(i.SEGMENT1),CHAR(13),' '),CHAR(10),' '),'~^~',' '),
  CAST(i.STOCKITEM AS varchar(4)), CAST(i.SELLABLE AS varchar(4)),
  CAST(i.INACTIVE AS varchar(4)), CAST(i.DATELASTMN AS varchar(8))
FROM ICITEM i
ORDER BY i.FMTITEMNO""")

# ICCATG - the IC category master, and the only place Sage's own human-readable
# category descriptions live. 55 rows; 50 are used by ICITEM and 5 (1EINST,
# 1EQUIP, 1FFGHT, 1GENR, 1VEHIC) have no items at all.
Q["item_category"] = ("sage_item_category", [
    "category", "description", "cogs_account", "revenue_account",
    "return_account", "variance_account", "damage_account", "ics_exp_account",
    "def_price_list", "inactive"], """
SELECT RTRIM(c.CATEGORY),
  REPLACE(REPLACE(REPLACE(RTRIM(c.[DESC]),CHAR(13),' '),CHAR(10),' '),'~^~',' '),
  RTRIM(c.COGSACCT), RTRIM(c.REVENUACCT), RTRIM(c.RETURNACCT),
  RTRIM(c.VARIANACCT), RTRIM(c.DAMAGEACCT), RTRIM(c.ICSEXPACCT),
  RTRIM(c.DEFPRICLST), CAST(c.INACTIVE AS varchar(4))
FROM ICCATG c
ORDER BY c.CATEGORY""")

'''

anchor = '\nQ["bills"] = ('
assert src.count(anchor) == 1, "bills anchor not unique"
src = src.replace(anchor, "\n" + entries.strip("\n") + "\n\n" + 'Q["bills"] = (', 1)

old_order = '''ORDER = ["vendors", "items", "bills", "goods", "services",
         "ap_dist", "ap_obl", "ap_obp", "gl_acct", "gl_hier", "gl_srce",
         "gl_afs", "gl_post", "ap_bank"]'''
new_order = '''ORDER = ["vendors", "items", "item_category", "items_master",
         "bills", "goods", "services",
         "ap_dist", "ap_obl", "ap_obp", "gl_acct", "gl_hier", "gl_srce",
         "gl_afs", "gl_post", "ap_bank"]'''
assert src.count(old_order) == 1, "ORDER anchor not found"
src = src.replace(old_order, new_order, 1)
io.open(P, "w", encoding="utf-8").write(src)

ddl = '''
-- ICITEM, UNFILTERED. Q["items"]/sage_item is PO-scoped and holds 17,129 rows;
-- this is the whole master at 1,196,108. PK is Sage's own key, FMTITEMNO:
-- pull_one uses REPLACE INTO, so a staging key narrower than the source's
-- loses rows with no error.
DROP TABLE IF EXISTS sage_item_master;
CREATE TABLE sage_item_master (
  item_no_fmt     VARCHAR(64)  NOT NULL,
  item_no_raw     VARCHAR(64)      NULL,
  description     VARCHAR(256)     NULL,
  category        VARCHAR(8)       NULL,
  stock_unit      VARCHAR(16)      NULL,
  -- classifies item TYPE better than category: RAWMAT, FINGUD, WIP, ASSET...
  item_break_id   VARCHAR(8)       NULL,
  ctrl_account    VARCHAR(8)       NULL,
  def_price_list  VARCHAR(8)       NULL,
  segment1        VARCHAR(32)      NULL,
  stock_item      VARCHAR(4)       NULL,
  sellable        VARCHAR(4)       NULL,
  inactive        VARCHAR(4)       NULL,
  date_last_maint VARCHAR(8)       NULL,
  PRIMARY KEY (item_no_fmt),
  KEY ix_item_master_cat (category),
  KEY ix_item_master_brk (item_break_id)
) ENGINE=InnoDB;

-- ICCATG. Sage's own category descriptions, which nothing in staging had.
DROP TABLE IF EXISTS sage_item_category;
CREATE TABLE sage_item_category (
  category         VARCHAR(8)   NOT NULL,
  description      VARCHAR(256)     NULL,
  cogs_account     VARCHAR(64)      NULL,
  revenue_account  VARCHAR(64)      NULL,
  return_account   VARCHAR(64)      NULL,
  variance_account VARCHAR(64)      NULL,
  damage_account   VARCHAR(64)      NULL,
  ics_exp_account  VARCHAR(64)      NULL,
  def_price_list   VARCHAR(8)       NULL,
  inactive         VARCHAR(4)       NULL,
  PRIMARY KEY (category)
) ENGINE=InnoDB;
'''
with io.open(S, "a", encoding="utf-8") as fh:
    fh.write(ddl)

import ast
ast.parse(io.open(P, encoding="utf-8").read())
print("pull.py + 03_gap.sql patched and pull.py parses. Backups: .bak-" + stamp)
