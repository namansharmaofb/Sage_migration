import sys, json
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
import post_sage_bills as m
api = m.Api()
pid = "1544577130968416256"     # SAGE-4E2ME02-14, wrongly named "4E2ME02-14"
st, body = api.get("/product/%s" % pid)
print("GET /product/{id} ->", st)
d = api.data(body)
if isinstance(d, dict):
    print({k: d.get(k) for k in ("productId", "skuCode", "productName", "typeOfStock",
                                 "unitOfMeasurement", "hsnCode", "categoryId")})
else:
    print(str(body)[:250])
