"""Merchant name cleanup and keyword-based categorization.

Bank descriptions look like "NETFLIX.COM 866-579-7172 CA" or
"SQ *BLUE BOTTLE COFFEE 0423". normalize_merchant() turns those into a stable
key ("netflix", "blue bottle coffee") so charges from the same merchant group
together. Users can override any merchant's category in the UI.
"""
import re

# Starting categories. Users can add and delete categories in Settings; the
# live list is the `categories` table (see db.py).
DEFAULT_CATEGORIES = [
    "Income",
    "Transfers",
    "Housing",
    "Utilities & Bills",
    "Subscriptions",
    "Groceries",
    "Dining",
    "Transportation",
    "Shopping",
    "Health",
    "Entertainment",
    "Travel",
    "Fees",
    "Other",
]

# Categories that aren't real spending (moving money between your own accounts).
NON_SPENDING = {"Transfers", "Income"}

# Spending math depends on Income and Transfers, and Other is the fallback,
# so these three can't be deleted.
LOCKED_CATEGORIES = {"Income", "Transfers", "Other"}

# Categories get one of 8 colorblind-checked color slots (see style.css).
# The rest show in gray. New categories take the first free slot.
COLOR_SLOTS = 8
DEFAULT_COLOR_SLOTS = {
    "Housing": 1, "Dining": 2, "Groceries": 3, "Transportation": 4, "Shopping": 5,
    "Utilities & Bills": 6, "Subscriptions": 7, "Entertainment": 8,
}

# Checked in order against the lowercased raw description; first match wins.
KEYWORDS = [
    ("Transfers", ["transfer", "payment thank you", "thank you", "autopay", "autopymt", "internet payment", "online payment", "card payment",
                   "credit card pmt", "epay", "to savings", "from savings", "venmo cashout",
                   "contribution", "electronic payment", "mobile payment", "payment - thank"]),
    ("Income", ["payroll", "direct dep", "salary", "interest paid", "dividend", "tax refund"]),
    ("Housing", ["rent", "mortgage", "hoa ", "property mgmt", "apartments"]),
    ("Utilities & Bills", ["electric", "energy", "water", "gas co", "utility", "comcast", "xfinity",
                           "verizon", "at&t", "t-mobile", "tmobile", "internet", "insurance",
                           "geico", "progressive", "state farm", "gci", "chugach", "enstar"]),
    ("Subscriptions", ["netflix", "spotify", "hulu", "disney", "hbo", "max.com", "paramount",
                       "youtube", "apple.com/bill", "icloud", "google storage", "amazon prime",
                       "prime video", "audible", "patreon", "adobe", "microsoft", "dropbox",
                       "chatgpt", "openai", "claude.ai", "anthropic", "nytimes", "peacock",
                       "siriusxm", "planet fitness", "gym", "fitness", "apple.combill"]),
    ("Groceries", ["grocery", "safeway", "fred meyer", "kroger", "whole foods", "trader joe",
                   "costco", "carrs", "aldi", "wegmans", "publix", "h-e-b", "sprouts", "market",
                   "supermarket", "fred-meyer", "iga", "foodland", "food store", "super a"]),
    ("Dining", ["restaurant", "cafe", "coffee", "starbucks", "dunkin", "mcdonald", "burger",
                "pizza", "taco", "chipotle", "grill", "kitchen", "bar ", "pub", "doordash",
                "uber eats", "grubhub", "sq *", "tst*", "bakery", "sushi", "subway", "saloon", "brew",
                "tavern", "taproom", "domino", "qdoba", "tim horton", "bistro", "diner", "deli ",
                "bbq", "thai", "pho ", "sandwich", "wendy", "kfc", "dairy queen", "arby", "panda express",
                "five guys", "chick-fil", "jimmy john", "papa john", "noodle", "ramen", "steakhouse"]),
    ("Transportation", ["shell", "chevron", "exxon", "tesoro", "holiday", "fuel", "gas station",
                        "uber", "lyft", "parking", "transit", "dmv", "jiffy lube", "auto", "conoco", "petro canada",
                        "pilot", "speedway", "gas n", "7-eleven", "circle k", "maverik", "sinclair", "valero",
                        "arco", "car wash", "tire"]),
    ("Travel", ["airline", "alaska air", "delta", "united", "southwest", "hotel", "airbnb",
                "marriott", "hilton", "expedia", "booking.com", "american air", "kiwi.com",
                "airways", "air canada", "motel", "ferry", "amtrak"]),
    ("Health", ["pharmacy", "cvs", "walgreens", "clinic", "medical", "dental", "hospital",
                "doctor", "vision", "health", "pharm"]),
    ("Entertainment", ["cinema", "theater", "theatre", "steam", "playstation", "xbox",
                       "nintendo", "ticketmaster", "concert", "museum", "ski area", "ski resort",
                       "bowling", "golf"]),
    ("Fees", ["fee", "overdraft", "interest charge", "finance charge"]),
    ("Shopping", ["amazon", "amzn", "target", "walmart", "best buy", "home depot", "lowe's",
                  "ikea", "etsy", "ebay", "rei", "nordstrom", "old navy", "apple store"]),
]

# Keywords must start at a word boundary, so "rent" doesn't match "current".
_PATTERNS = [
    (category, re.compile("|".join(r"(?<![a-z])" + re.escape(k) for k in keywords)))
    for category, keywords in KEYWORDS
]

_NOISE_WORDS = {
    "pos", "debit", "credit", "purchase", "card", "recurring", "payment", "ach", "withdrawal",
    "online", "pmt", "ppd", "web", "id", "ref", "des", "indn", "co", "inc", "llc", "com",
    "checkcard", "visa", "mc", "sq", "tst", "pp", "paypal", "the", "www", "bill", "usa", "us",
    "amzn", "mktp", "pmts", "subscr", "store", "whse",
}


def normalize_merchant(description: str, payee: str | None = None) -> str:
    s = (payee or description).lower()
    s = re.sub(r"https?://", " ", s)
    s = re.sub(r"\.(com|net|org|io)\b", " ", s)
    s = re.sub(r"[^a-z&' ]+", " ", s)  # drop digits, punctuation, store numbers
    words = [w for w in s.split() if len(w) > 1 and w not in _NOISE_WORDS]
    # Trailing two-letter word is almost always a US state code.
    if len(words) > 1 and len(words[-1]) == 2:
        words = words[:-1]
    key = " ".join(words[:2])
    return key or (payee or description).lower().strip()


def display_name(merchant: str) -> str:
    return " ".join(w.capitalize() for w in merchant.split())


def categorize(description: str, amount: float) -> str:
    text = description.lower()
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            # A positive amount at a store (a refund) keeps the store's
            # category, so it offsets spending instead of counting as income.
            return category
    return "Income" if amount > 0 else "Other"
