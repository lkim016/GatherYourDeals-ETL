import re

# Identifies savings/discount lines in the spatial layout so they can be
# labeled [S] and associated with the item above them.
_SAVINGS_LINE = re.compile(
    r"\b(savings?|you\s*saved|instant\s*savings?|member\s*savings?|"
    r"everyday\s*savings?|digital\s*coupon|coupon\s*savings?|discount)\b",
    re.IGNORECASE,
)


# Spatial-layout noise filter — same as _NOISE_LINE but intentionally keeps
# savings/discount lines so the LLM can compute the final discounted price.
_SPATIAL_NOISE_LINE = re.compile(
    r"^\s*(?:"
    r"sub\s*total|subtotal|total|net\s*total|grand\s*total|"
    r"hst|gst|pst|qst|vat|tax|surcharge|"
    r"payment|cash|credit|debit|visa|mastercard|interac|amex|"
    r"us\s+debit|us\s+credit|"                          # "US DEBIT Purchase" etc.
    r"change\s*due|change\b|balance\s*due|balance\b|"   # bare CHANGE / BALANCE lines
    r"amount\s*due|amount\s*tendered|"
    r"purchase\s*:|purchase\b|"                         # "PURCHASE: 9.06"
    r"verified|pin\s*verified|"                         # "VERIFIED BY PIN"
    r"aid\s*:|tc\s*:|ref\s*#|trans\s*#|auth\s*#|approval|"  # transaction codes
    r"thank\s*you|please\s*come|visit\s*us|survey|"
    r"tell\s*us|earn\b|fuel\s*point|fuel\b|"            # loyalty program footer
    r"remaining\b.*point|total\b.*point|"               # "Remaining May Fuel Points"
    r"annual\s*card|you\s*saved|with\s*our|"            # savings summary footer
    r"go\s*to\s*www|www\.|feedback|hiring|"             # URLs / HR footer
    r"receipt\s*#|store\s*#|"
    r"approved|declined|customer\s*copy|merchant\s*copy|"
    r"crv|ca\s*redemp|deposit|bottle\s*dep|bag\s*fee|"
    r"your\s+cashier|cashier|operator|terminal|"        # "Your cashier was Jamie" etc.
    r"\*{4,}|={3,}|-{3,}|#{3,}"                        # symbol-only lines (\b removed — \W next to \W has no boundary)
    r")(?:\b|$|\s)",                                    # word boundary OR end OR whitespace
    re.IGNORECASE,
)

# currency
_CURRENCY_MARKERS = [
    (re.compile(r'\bCAD\b|\bCAD\$|C\$|\$CAD', re.IGNORECASE), "CAD"),
    (re.compile(r'\bGBP\b|£',                                  re.IGNORECASE), "GBP"),
    (re.compile(r'\bEUR\b|€',                                  re.IGNORECASE), "EUR"),
]


_US_STORE_OCR_RE = re.compile(
    r'\b(KROGER|INGLES|INGLE\'?S|WALMART|WAL-MART|TARGET|VONS|RALPHS|SAFEWAY'
    r'|ALBERTSONS|PUBLIX|H-?E-?B|WHOLE\s+FOODS|TRADER\s+JOE\'?S'
    r'|FARM\s*&\s*TABLE|CVS|WALGREENS|RITE\s+AID)\b',
    re.IGNORECASE,
)