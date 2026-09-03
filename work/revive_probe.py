import sys, json
sys.path.insert(0, "/home/namansharma/Desktop/sage-pull")
import post_sage_bills as m
api = m.Api()
PID = "1544577130968416256"
st, b = api.call("PATCH", "/product/%s/ACTIVE" % PID)
print("PATCH /product/{id}/ACTIVE ->", st, "ok" if api.ok(st, b) else api.err(b)[:120])
body = {"productId": PID, "productName": "Power Charges, IDEPL-14",
        "skuCode": "SAGE-4E2ME02-14", "unit": "OTH", "unitOfMeasurement": "OTH",
        "typeOfStock": "CHARGE", "hsnCode": "996719", "isManageInventory": False,
        "itemStatus": "ACTIVE"}
for verb, path in (("PUT", "/product/"), ("PUT", "/product/%s" % PID),
                   ("POST", "/product/update")):
    st, b = api.call(verb, path, body)
    print("%-5s %-22s -> %s %s" % (verb, path, st,
          "ok" if api.ok(st, b) else api.err(b)[:90]))
