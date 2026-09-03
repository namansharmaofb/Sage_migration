import sys; sys.path.insert(0, "/home/namansharma/Desktop/sage-pull/work")
from sage import q
cols = [c["COLUMN_NAME"] for c in q(
  "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='APIBH' ORDER BY ORDINAL_POSITION")]
print("APIBH (%d cols):\n%s\n" % (len(cols), ", ".join(cols)))
for r in q("""
  SELECT TOP 3 RTRIM(IDVEND) vendor, RTRIM(IDINVC) invoice, CNTBTCH, CNTITEM,
         DATEINVC, DATEDUE, CAST(AMTGROSTOT AS decimal(20,2)) grostot,
         CAST(AMTTAXTOT AS decimal(20,2)) taxtot,
         CAST(AMTINVCTOT AS decimal(20,2)) invctot,
         RTRIM(CODETAXGRP) grp, RTRIM(CODECURN) curn
    FROM APIBH WHERE DATEINVC BETWEEN 20260101 AND 20260430"""):
    print({k: str(v).strip() for k, v in r.items()})
