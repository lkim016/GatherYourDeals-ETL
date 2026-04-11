"""
Unit tests for flatten_receipt() and the updated _NON_PRODUCT filter.
No API calls — uses synthetic receipt dicts that mirror real ETL output.
"""
import sys, json
sys.path.insert(0, ".")
from etl import flatten_receipt, _VALID_PRICE_RE, _FLAT_NON_PRODUCT

# ── helpers ────────────────────────────────────────────────────────────────

def make_receipt(items, store="Kroger", date="2021.06.14", lat=32.95628, lon=-97.2803):
    return {
        "storeName":    store,
        "purchaseDate": date,
        "latitude":     lat,
        "longitude":    lon,
        "items":        items,
    }

def flat(name, price, amount="1"):
    return {"productName": name, "price": price, "amount": amount}

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

def check(label, condition):
    print(f"  [{PASS if condition else FAIL}] {label}")
    return condition

# ── tests ──────────────────────────────────────────────────────────────────

all_passed = True

print("\n=== 1. Happy path — real product fields propagate correctly ===")
receipt = make_receipt([flat("REYNOLDS WRAP FOIL", "3.69USD")])
rows = flatten_receipt(receipt)
all_passed &= check("one row returned", len(rows) == 1)
all_passed &= check("productName correct", rows[0]["productName"] == "REYNOLDS WRAP FOIL")
all_passed &= check("price correct",       rows[0]["price"] == "3.69USD")
all_passed &= check("amount correct",      rows[0]["amount"] == "1")
all_passed &= check("storeName correct",   rows[0]["storeName"] == "Kroger")
all_passed &= check("purchaseDate correct",rows[0]["purchaseDate"] == "2021.06.14")
all_passed &= check("latitude correct",    rows[0]["latitude"] == 32.95628)
all_passed &= check("longitude correct",   rows[0]["longitude"] == -97.2803)

print("\n=== 2. Noise filtering — non-product items dropped ===")
noisy = make_receipt([
    flat("REYNOLDS WRAP FOIL", "3.69USD"),   # keep
    flat("Donation",           "5.00USD"),   # drop — donation
    flat("Charity",            "1.00USD"),   # drop — charity
    flat("Bag Fee",            "0.10USD"),   # drop — bag fee
    flat("CA Redemption Value","0.05USD"),   # drop — crv
    flat("Bottle Deposit",     "0.25USD"),   # drop — bottle dep
    flat("Subtotal",           "9.99USD"),   # drop — subtotal
    flat("Tax",                "0.87USD"),   # drop — tax
    flat("Total",             "10.86USD"),   # drop — total
    flat("Member Savings",    "-1.00USD"),   # drop — savings
    flat("Digital Coupon",    "-0.50USD"),   # drop — coupon
    flat("Balance Due",        "9.36USD"),   # drop — balance due
    flat("Cash Back",          "0.00USD"),   # drop — cash back
    flat("Gift Card",          "5.00USD"),   # drop — gift card
])
rows = flatten_receipt(noisy)
names = [r["productName"] for r in rows]
all_passed &= check("only 1 row kept (REYNOLDS WRAP FOIL)", len(rows) == 1)
all_passed &= check("correct item kept", names == ["REYNOLDS WRAP FOIL"])

print("\n=== 3. Price format gate — malformed prices dropped ===")
bad_prices = make_receipt([
    flat("GOOD ITEM",     "4.99USD"),   # keep
    flat("NULL PRICE",    ""),          # drop — empty
    flat("NO CURRENCY",   "4.99"),      # drop — no currency suffix
    flat("WRONG FORMAT",  "4.9USD"),    # drop — only 1 decimal digit
    flat("LETTER PRICE",  "4.99A"),     # drop — wrong suffix format
    flat("NONE PRICE",    None),        # drop — None
])
rows = flatten_receipt(bad_prices)
all_passed &= check("only GOOD ITEM kept", len(rows) == 1)
all_passed &= check("correct item kept",   rows[0]["productName"] == "GOOD ITEM")

print("\n=== 4. Missing productName dropped ===")
no_names = make_receipt([
    {"productName": None,  "price": "3.00USD", "amount": "1"},
    {"productName": "",    "price": "3.00USD", "amount": "1"},
    {"productName": "EGGS","price": "4.99USD", "amount": "1"},
])
rows = flatten_receipt(no_names)
all_passed &= check("only EGGS kept", len(rows) == 1 and rows[0]["productName"] == "EGGS")

print("\n=== 5. Amount defaults to '1' when missing ===")
no_amount = make_receipt([
    {"productName": "MILK", "price": "2.99USD", "amount": None},
])
rows = flatten_receipt(no_amount)
all_passed &= check("amount defaults to '1'", rows[0]["amount"] == "1")

print("\n=== 6. Multiple valid items all returned ===")
multi = make_receipt([
    flat("APPLES",  "2.49USD", "4lb"),
    flat("MILK",    "3.99USD"),
    flat("BREAD",   "2.99USD"),
])
rows = flatten_receipt(multi)
all_passed &= check("3 rows returned", len(rows) == 3)
all_passed &= check("weight amount preserved", rows[0]["amount"] == "4lb")

print("\n=== 7. CAD prices accepted ===")
cad = make_receipt([flat("BANANAS", "1.49CAD")], store="No Frills")
rows = flatten_receipt(cad)
all_passed &= check("CAD price accepted", len(rows) == 1 and rows[0]["price"] == "1.49CAD")

print("\n=== 8. Vons sample JSON (from draft/) ===")
vons = {
    "storeName": "Vons",
    "purchaseDate": "2025.10.01",
    "latitude": 33.8453,
    "longitude": -117.7512,
    "items": [
        {"productName": "OPN Nat Granola",    "price": "4.79USD", "amount": "1"},
        {"productName": "Granola Blubry Flx", "price": "4.79USD", "amount": "1"},
        {"productName": "Lucerne Milk 1% LF", "price": "2.79USD", "amount": "1"},
        {"productName": "Bonduelle Bistro",   "price": "3.33USD", "amount": "1"},
        {"productName": "Bonduelle Bistro",   "price": "3.33USD", "amount": "1"},
        {"productName": "Bonduelle Bistro",   "price": "3.33USD", "amount": "1"},
        {"productName": "Donation",           "price": "5.00USD", "amount": "1"},  # should be dropped
    ],
}
rows = flatten_receipt(vons)
names = [r["productName"] for r in rows]
all_passed &= check("Donation dropped",          "Donation" not in names)
all_passed &= check("6 product rows kept",       len(rows) == 6)
all_passed &= check("Bonduelle appears 3 times", names.count("Bonduelle Bistro") == 3)

print("\n=== 9. General fix: all-lowercase names dropped (garbled OCR) ===")
garbled = make_receipt([
    flat("lonipito diiv",  "3.49CAD"),   # drop — all lowercase
    flat("ug lo zyob",     "3.49CAD"),   # drop — all lowercase
    flat("REAL PRODUCT",   "3.49CAD"),   # keep
    flat("Banana",         "2.00CAD"),   # keep — Title Case is fine
])
rows = flatten_receipt(garbled)
names = [r["productName"] for r in rows]
all_passed &= check("garbled 'lonipito diiv' dropped",  "lonipito diiv" not in names)
all_passed &= check("garbled 'ug lo zyob' dropped",     "ug lo zyob" not in names)
all_passed &= check("REAL PRODUCT kept",                "REAL PRODUCT" in names)
all_passed &= check("Banana (Title Case) kept",         "Banana" in names)

print("\n=== 10. General fix: store name in product name dropped ===")
store_leak = make_receipt([
    flat("BRANDON'S NO FRILLS VANCOUVER", "3.49CAD"),  # drop — store name inside
    flat("EGGS",                          "3.49CAD"),  # keep
], store="No Frills")
rows = flatten_receipt(store_leak)
names = [r["productName"] for r in rows]
all_passed &= check("store-name leak dropped",  "BRANDON'S NO FRILLS VANCOUVER" not in names)
all_passed &= check("EGGS kept",                "EGGS" in names)

print("\n=== 11. General fix: substring dedup at same price ===")
substr = make_receipt([
    flat("GRD A LRG",          "5.45CAD"),   # drop — substring of longer name below
    flat("GRD A LRG BRWN MRJ", "5.45CAD"),  # keep — longer
    flat("HMGZD MILK",         "3.04CAD"),   # keep — unique price
    flat("HMGZD MILK",         "3.04CAD"),   # keep — exact duplicate (different purchase)
])
rows = flatten_receipt(substr)
names = [r["productName"] for r in rows]
all_passed &= check("fragment 'GRD A LRG' dropped",         "GRD A LRG" not in names)
all_passed &= check("full name 'GRD A LRG BRWN MRJ' kept",  "GRD A LRG BRWN MRJ" in names)
all_passed &= check("HMGZD MILK kept (unique price)",        "HMGZD MILK" in names)

# ── summary ────────────────────────────────────────────────────────────────

print("\n" + ("=" * 50))
if all_passed:
    print(f"  [{PASS}] All tests passed")
else:
    print(f"  [{FAIL}] Some tests FAILED — see above")
print()
sys.exit(0 if all_passed else 1)
