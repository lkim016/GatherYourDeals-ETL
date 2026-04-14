import re

# _CHUNK_THRESHOLD_CHARS = 2000   # raised from 1000 — most receipts fit in one chunk

# --- Global Receipt Cleaning Regexes ---

_PRICE_RE       = re.compile(r"(\d+\.?\d*)")
_UNIT_STRIP     = re.compile(r"\b(W|EA|PK|F|N|X|O|T|PC|CT|BG|LT|BT|CN|OZ|MRJ|RQ|FV|IQF|WLD|each)\b", re.IGNORECASE)
_PRICE_FMT      = re.compile(r"^\d+\.\d{2}\s*[A-Za-z]?$")  # "4.79", "4.79S", "4.79 S"
_ITEM_CODE      = re.compile(r"^\d{4,}\s+")            # leading 4+ digit item/barcode code
_PRICE_LETTER   = re.compile(r"^(\d+\.?\d*)[A-Za-z]+$")  # "11.99A", "8.99N"
_NON_PRODUCT    = re.compile(
    r"\b(tax|saving|savings|discount|instant\s+saving|subtotal|total|"
    r"redemp|crv|deposit|donation|charity|bag\s+fee|bottle\s+dep|"
    # Payment terminal / card slip lines
    r"approved|customer\s+copy|card\s+number|retain\s+this|"
    r"ref\.?\s*#|auto\s*#|visa\s+credit|entry\s+id|datetime|"
    r"transaction\s+id|debit\s+card|credit\s+card|"
    # Promotional / footer text
    r"fuel\s+points|thank\s+you\s+for\s+shopping|earn\s+\d+|"
    r"opportunity\s+awaits|join\s+our\s+team|now\s+hiring|feedback|"
    r"closing\s+balance|points\s+redeemed|pc\s+optimum|"
    # Sale/discount modifier lines (not standalone products)
    r"member\s+saving|digital\s+coupon|coupon\s+saving|"
    r"instant\s+saving|price\s+reduction|"
    # Payment noise
    r"balance\s+due|change\s+due|cash\s+back|gift\s+card)\b",
    re.IGNORECASE,
)
# Structural junk: URLs, EMV AIDs (A000...), "Item N" placeholders,
# approval code lines ("00 APPROVED"), standalone short codes ("SC")
_JUNK_NAME = re.compile(
    r"(www\.|\.com\b|\.org\b|jobs\.|"          # URLs
    r"^[Aa][0-9a-fA-F]{8,}|"                   # EMV AID e.g. A0000000031010
    r"^\d{2,3}\s+[A-Z]{2,}|"                   # "00 APPROVED", "03 DECLINED"
    r"^item\s*\d*$|"                            # "Item", "Item 1", "Item 2"
    r"^\*{2,}|"                                 # "*** CUSTOMER COPY ***"
    r"^\*\d+|"                                  # card number fragments: "*8424"
    r"^mgr:|^date:|^time:|"                     # manager/timestamp footer fields
    r"^account\s*:|^card\s+type\s*:|"           # payment terminal fields
    r"^trans(?:action)?\s*,?\s+type\s*:|"       # "Trans, Type: PURCHASE"
    r"^rec#|"                                   # receipt reference numbers: "REC#2-5279"
    r"^\(sale\)\s*$|"                           # bare "(SALE)" line
    r"^\(?\d{3}\)?[\s\-]\d{3}[\s\-]\d{4}|"     # phone numbers (604) 688-0911
    r"^\d{2}/\d{2}/\d{2,4}\s+\d{1,2}:\d{2}|"  # timestamps 02/19/26 7:53:13 PM
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}|"       # ISO timestamps 2026-02-19 07:53
    r"^\d+[A-Za-z]{1,2}\s+\d+$|"               # OCR noise: "1mt 4", "3oz 2"
    r"^[A-Za-z]{1,2}mt\s+\d+$|"                # OCR noise: "Imt 6", "imt 4"
    r"^SC\s+\d+|"                               # Ingles store codes: "SC 3547", "SC 3547 A"
    r"^\$[\d,]+$|"                              # dollar-prefixed numbers: "$15,000,000"
    r"@\s*\$?\d+\.?\d*\s*/|"                   # per-unit pricing lines: "NET 1b @ $1.49/1b", "1.160 kg @ $1.72/kg"
    r"^[a-z]{4,8}[0-9]?$)",                    # all-lowercase garbled OCR: "euoju", "emo2"
    re.IGNORECASE,
)
# Sale-modifier prefix — remove "(SALE)" prefix from duplicated item names
_SALE_PREFIX = re.compile(r"^\(sale\)\s+", re.IGNORECASE)

_MAX_ITEM_PRICE = 99.0   # prices above this are almost certainly totals/subtotals
_MIN_ITEM_PRICE = 0.50   # prices below this are almost certainly CA CRV / deposit fees bleeding in

# Add this to your regex definitions
_ADDRESS_LEAK = re.compile(
    r"\b(\d{3,}\s+(?:N|S|E|W|North|South|East|West)\s+)?[\w\s]{2,}(Street|St|Avenue|Ave|Road|Rd|Blvd|Boulevard|Drive|Dr|Way|Court|Ct|Circle|Cir|Lane|Ln|Suite|Ste|Unit|Floor|Fl)\b",
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Long-receipt chunking
# ---------------------------------------------------------------------------

_CHUNK_HEADER_LINES    = 6      # lines to prepend to every chunk (store/date context)
_CHUNK_MAX_CHARS       = 700    # max body chars per chunk (excluding prepended header)
_CHUNK_OVERLAP_LINES   = 6      # overlap between chunks — 6 lines keeps item+price+CA REDEMP VA together


# ---------------------------------------------------------------------------
# Tier 1 — OCR noise filter
# ---------------------------------------------------------------------------
_NOISE_LINE = re.compile(
    r"^\s*(?:"
    r"sub\s*total|subtotal|total|net\s*total|grand\s*total|"
    r"hst|gst|pst|qst|vat|tax|surcharge|"
    r"payment|cash|credit|debit|visa|mastercard|interac|amex|"
    r"change\s*due|balance\s*due|amount\s*due|amount\s*tendered|"
    r"savings?|you\s*saved|instant\s*savings?|member\s*savings?|everyday\s*savings?|"
    r"discount|coupon|points?|rewards?|loyalty|"
    r"thank\s*you|please\s*come|visit\s*us|survey|"
    r"receipt\s*#|store\s*#|ref\s*#|trans\s*#|auth\s*#|approval|"
    r"approved|declined|pin\s*verified|customer\s*copy|merchant\s*copy|"
    r"crv|ca\s*redemp|deposit|bottle\s*dep|bag\s*fee|"
    r"cashier|operator|terminal|"
    r"\*{2,}|={3,}|-{3,}|#{3,}"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Tier 3b — Targeted repair for items with null/missing price
# ---------------------------------------------------------------------------
# When primary extraction leaves items with null price, re-query the LLM with
# only the 5-line OCR window around that item — much cheaper than reprocessing
# the full receipt (~50 tokens vs 6985).  If the repair still fails, escalate
# to a stronger model via OpenRouter.

_REPAIR_ESCALATION_MODEL = "google/gemini-flash-1.5"   # cheap + strong structured extraction


# ---------------------------------------------------------------------------
# Weight-priced item recovery
# ---------------------------------------------------------------------------
# Some receipts (e.g. No Frills) print items as:
#   BANANA
#   1.160 kg @ $1.72/kg  2.00        ← weight, per-unit rate, and total on one line
#   CELERY STICKS
#   0.075 kg @ $3.49/kg              ← total on the next line
#   0.26
#
# The LLM often extracts the item name but returns null price/amount because the
# ---------------------------------------------------------------------------
# Store-name aliases: LLM sometimes returns ALL-CAPS brand codes; map them
# back to the canonical "as-written" store name used in the ground truth.
# Keys are upper-cased for case-insensitive lookup.
# ---------------------------------------------------------------------------
_STORE_ALIASES: dict[str, str] = {
    "NOFRILLS":         "No Frills",
    "NO FRILLS":        "No Frills",
    "COSTCO WHOLESALE": "Costco Wholesale",
    "COSTCO":           "Costco Wholesale",
    "VONS":             "Vons",
    "VONS STORE":       "Vons",
}



# Known Canadian stores — when OCR has no explicit CAD marker, infer from store name.
_CANADIAN_STORE_NAMES: frozenset[str] = frozenset({
    "No Frills",
    "Real Canadian Superstore",
    "T&T Supermarket",
    "House of Dosa",
    "House of Dosa- Downtown",
    "Loblaws",
    "Sobeys",
    "Metro",
    "FreshCo",
    "Food Basics",
    "Independent",
})

# Known US stores — used to override an LLM-hallucinated non-USD currency code.
_US_STORE_KEYWORDS: tuple[str, ...] = (
    "KROGER", "INGLES", "WALMART", "WAL-MART", "TARGET", "VONS",
    "RALPHS", "SAFEWAY", "ALBERTSONS", "PUBLIX", "HEB", "WHOLE FOODS",
    "TRADER JOE", "COSTCO",  # Costco has US and CA locations; OCR marker takes precedence
    "FARM & TABLE", "FARM AND TABLE",
    "CVS", "WALGREENS", "RITE AID",
)

# OCR number spacing (`1. 160`, `1. 72`) confuses it.  This regex + function
# parses the raw OCR deterministically and injects the correct price/amount.
_WEIGHT_ITEM_RE = re.compile(
    r'([\d]+\.[\d]+)\s*kg\s*@\s*\$\s*([\d]+\.[\d]+)\s*/kg(?:\s+([\d]+\.[\d]+))?',
    re.IGNORECASE,
)
_NORM_SPACED_NUM  = re.compile(r'(\d)\.\s+(\d)')   # "1. 160" → "1.160"
_DANGLING_PRICE   = re.compile(r'^\$?(\d+\.\d{2})\s*$')   # a line that is ONLY a price: "2.00", "$2.00"
_ENDS_WITH_PRICE  = re.compile(r'\$?\d+\.\d{2}\s*$')      # line already ends with a price

# Matches lines that look like a street address: start with a number followed by
# a street name, optionally followed by city/state/ZIP on the same or next line.
_STREET_LINE = re.compile(
    r"^\s*\d+\s+\w[\w\s,\.#-]{5,}(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|pkwy|hwy|cyn|canyon)\b",
    re.IGNORECASE,
)
_CITY_STATE_ZIP = re.compile(r"[A-Z][a-zA-Z\s]+,?\s+[A-Z]{2}\s+\d{5}", re.IGNORECASE)

# Regex for dates paired with a transaction time — the most reliable way to
# identify the actual purchase timestamp vs promotional/contest dates.
_TX_DATE_TIME_RE = re.compile(
    r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+\d{1,2}:\d{2}'   # MM/DD/YY HH:MM
    r'|(\d{4}[/\-]\d{2}[/\-]\d{2})\s+\d{2}:\d{2}',           # YYYY-MM-DD HH:MM
)
# Months for written-out dates like "Feb 10 2026"
_MONTH_NAMES = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}
_WRITTEN_DATE_RE = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\s+(20\d{2})\b',
    re.IGNORECASE,
)


# Known location/neighborhood names that LLMs sometimes hallucinate as store names.
# Maps OCR-visible chain keyword → canonical store name.

# _LOCATION_OVERRIDES: dict[str, str] = {
#     "TARGET":   "Target",
# }

_KNOWN_CHAIN_RE = re.compile(
    r'\b(target|kroger|walmart|wal-mart|vons|ralphs|safeway|albertsons|costco'
    r'|no\s+frills|real\s+canadian|t&t\s+supermarket|your\s+independent\s+grocer'
    r'|independent\s+grocer|kin\'?s\s+farm|house\s+of\s+dosa|ingles|farm\s*&\s*table'
    r'|publix|heb|whole\s+foods|trader\s+joe|marquis\s+wine)\b',
    re.IGNORECASE,
)

_CHAIN_NAMES: dict[str, str] = {
    "target": "Target", "kroger": "Kroger", "walmart": "Walmart",
    "vons": "Vons", "ralphs": "Ralphs", "safeway": "Safeway",
    "costco": "Costco Wholesale", "ingles": "Ingles",
}
