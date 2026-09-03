import sys; sys.path.insert(0, "/home/namansharma/Desktop/sage-pull/work")
from sage import q
print("Does one item ever appear under more than one invoice unit (RCPUNIT)?")
for r in q("""
WITH b AS (SELECT * FROM APOBL WHERE IDTRXTYPE=12 AND SRCEAPPL='PO'
             AND DATEINVC BETWEEN 20260101 AND 20260430 AND RTRIM(CODECURN)='INR'),
 j AS (SELECT h.INVHSEQ FROM b JOIN POINVH1 h
        ON RTRIM(h.INVNUMBER)=RTRIM(b.IDINVC) AND RTRIM(h.VDCODE)=RTRIM(b.IDVEND)),
 iu AS (SELECT RTRIM(l.ITEMNO) item, COUNT(DISTINCT RTRIM(l.RCPUNIT)) units
          FROM POINVL l JOIN j ON j.INVHSEQ=l.INVHSEQ
         WHERE RTRIM(l.ITEMNO)<>'' GROUP BY RTRIM(l.ITEMNO))
SELECT COUNT(*) items, SUM(CASE WHEN units>1 THEN 1 ELSE 0 END) multi_unit FROM iu"""):
    print("  ", {k: str(v) for k, v in r.items()})
print("\nDoes the invoice unit match the item's own stocking unit?")
for r in q("""
WITH b AS (SELECT * FROM APOBL WHERE IDTRXTYPE=12 AND SRCEAPPL='PO'
             AND DATEINVC BETWEEN 20260101 AND 20260430 AND RTRIM(CODECURN)='INR'),
 j AS (SELECT h.INVHSEQ FROM b JOIN POINVH1 h
        ON RTRIM(h.INVNUMBER)=RTRIM(b.IDINVC) AND RTRIM(h.VDCODE)=RTRIM(b.IDVEND))
SELECT COUNT(*) lines,
       SUM(CASE WHEN RTRIM(l.RCPUNIT)=RTRIM(c.STOCKUNIT) THEN 1 ELSE 0 END) same,
       SUM(CASE WHEN RTRIM(l.RCPUNIT)<>RTRIM(c.STOCKUNIT) THEN 1 ELSE 0 END) differ
  FROM POINVL l JOIN j ON j.INVHSEQ=l.INVHSEQ
  LEFT JOIN ICITEM c ON RTRIM(c.FMTITEMNO)=RTRIM(l.ITEMNO)"""):
    print("  ", {k: str(v) for k, v in r.items()})
