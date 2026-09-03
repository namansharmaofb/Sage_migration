import sys; sys.path.insert(0, "/home/namansharma/Desktop/sage-pull/work")
from sage import q
print("IDTRX values in the window (AP-direct):")
for r in q("""SELECT IDTRX, RTRIM(SRCEAPPL) src, COUNT(*) n FROM APIBH
               WHERE DATEINVC BETWEEN 20260101 AND 20260430
               GROUP BY IDTRX, RTRIM(SRCEAPPL) ORDER BY n DESC"""):
    print("   IDTRX=%s src=%-4s %s" % (r["IDTRX"], r["src"], r["n"]))
print()
for r in q("""
SELECT COUNT(*) apibh_docs FROM APIBH h
 WHERE h.DATEINVC BETWEEN 20260101 AND 20260430
   AND h.IDTRX = 12 AND RTRIM(h.SRCEAPPL)='AP' AND RTRIM(h.CODECURN)='INR'
   AND RTRIM(h.CODETAXGRP) NOT IN ('VAT','NRVAT','NRST','NRVATST')
   AND EXISTS (SELECT 1 FROM APIBD d WHERE d.CNTBTCH=h.CNTBTCH AND d.CNTITEM=h.CNTITEM)
   AND NOT EXISTS (SELECT 1 FROM APIBD d
        WHERE d.CNTBTCH=h.CNTBTCH AND d.CNTITEM=h.CNTITEM
          AND LEFT(RTRIM(d.IDGLACCT),2)<>'4E'
          AND LEFT(RTRIM(d.IDGLACCT),4)<>'2A7T'
          AND LEFT(RTRIM(d.IDGLACCT),7) NOT IN ('1L8TX14','1L8TX15','1L8TX16'))"""):
    print("APIBH docs passing the same filter as the proven extract:", r["apibh_docs"])
    print("(the .psv extract, built from APOBL, holds 11,256)")
