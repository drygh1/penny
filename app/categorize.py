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
    "Credit Card Payments",
    "Investments",
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
    "Cash",
    "Fees",
    "Other",
]
# Defaults added after the first release, for databases seeded before them:
# name -> the category it goes just before.
LATER_DEFAULT_CATEGORIES = {"Cash": "Fees", "Investments": "Housing", "Credit Card Payments": "Investments"}

# Categories that aren't spending: money coming in, money moving between your
# own accounts, paying off a card (the purchases on it already counted), and
# money put into investments (it's still yours, so it counts toward what you saved).
NON_SPENDING = {"Transfers", "Credit Card Payments", "Income", "Investments"}

# Spending math depends on Income and Transfers, and Other is the fallback,
# so these three can't be deleted.
LOCKED_CATEGORIES = {"Income", "Transfers", "Other"}

# Each category gets its own color slot (see style.css): 20 colors, ordered so
# neighbors stay distinct with colorblindness too. New categories take the
# first free slot; only past 20 categories does one show in gray.
COLOR_SLOTS = 20
DEFAULT_COLOR_SLOTS = {
    "Housing": 1, "Dining": 2, "Groceries": 3, "Transportation": 4, "Shopping": 5,
    "Utilities & Bills": 6, "Subscriptions": 7, "Entertainment": 8,
    "Income": 9, "Transfers": 10, "Health": 11, "Travel": 12, "Fees": 13, "Other": 14, "Cash": 15,
    "Investments": 16, "Credit Card Payments": 17,
}

# Checked in order against the lowercased raw description; first match wins.
KEYWORDS = [
    # Card-side wording; payments sent from checking are caught by _CARD_PAYMENT below.
    ("Credit Card Payments", ["payment thank you", "thank you", "payment - thank", "card payment",
                              "credit card pmt", "crcardpmt", "cardmember serv"]),
    ("Transfers", ["transfer", "autopay", "autopymt", "internet payment", "online payment",
                   "epay", "to savings", "from savings", "venmo cashout",
                   "contribution", "electronic payment", "mobile payment", "payment - thank",
                   "to share", "from share",  # credit unions call accounts "shares"
                   "online pmt", "e-payment",
                   "acctverify"]),  # the cents a brokerage sends to verify an account
    # Before Income, so money coming back out of a brokerage isn't counted as earned.
    ("Investments", ["fid bkg", "fidelity", "moneyline", "robinhood", "coinbase", "vanguard", "schwab",
                     "brokerage", "betterment", "wealthfront", "acorns", "e*trade", "etrade"]),
    ("Income", ["payroll", "direct dep", "autodep", "salary", "interest paid", "dividend", "tax refund"]),
    # Before the store keywords, so an ATM inside a cafe is still cash. "ATM Fee"
    # lines have no "#", so they stay in Fees.
    ("Cash", ["atm #", "atm withdrawal", "cash withdrawal"]),
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

# How each brokerage shows up in your bank's descriptions, keyed by a word in
# the institution's name ("Fidelity Investments"). Lets the Net worth chart see
# money moving into accounts that don't report their own transactions.
INSTITUTION_WORDS = {
    "fidelity": ["fidelity", "fid bkg"],
    "coinbase": ["coinbase"],
    "robinhood": ["robinhood"],
    "vanguard": ["vanguard"],
    "schwab": ["schwab"],
    "betterment": ["betterment"],
    "wealthfront": ["wealthfront"],
    "acorns": ["acorns"],
    "e*trade": ["e*trade", "etrade"],
}


def institution_words(org_name: str | None) -> list[str]:
    """Words that name this institution in a bank description; [] if unknown."""
    org = (org_name or "").lower()
    return next((words for key, words in INSTITUTION_WORDS.items() if key in org), [])


# Keywords must start at a word boundary, so "rent" doesn't match "current".
_PATTERNS = [
    (category, re.compile("|".join(r"(?<![a-z])" + re.escape(k) for k in keywords)))
    for category, keywords in KEYWORDS
]

# Paying a card from checking: a card issuer and a payment word, like
# "ACH BK OF AMER VISA TYPE: ONLINE PMT" or "DISCOVER E-PAYMENT".
_CARD_ISSUER = re.compile(
    r"(?<![a-z])(amex|american express|discover|chase card|chase credit|capital one|citi card|citicard|"
    r"barclay|synchrony|bk of amer|bank of america|wells fargo card|apple card|applecard|"
    r"credit one|us bank card|visa|mastercard)")
_PAYMENT_WORD = re.compile(r"(?<![a-z])(e-?payment|payment|pymt|pmt|autopay)")
# Money arriving on a card labeled as a payment, like "BA ELECTRONIC PAYMENT".
_PAYMENT_RECEIVED = re.compile(r"(?<![a-z])(electronic|online|mobile|internet|ach|autopay)\W*(payment|pmt|pymt)")
_NOT_A_CARD = re.compile(r"mortgage|home loan|auto loan|purchase")


def _is_card_payment(text: str, amount: float) -> bool:
    if _NOT_A_CARD.search(text):
        return False
    if amount < 0:
        return bool(_CARD_ISSUER.search(text) and _PAYMENT_WORD.search(text))
    return bool(_PAYMENT_RECEIVED.search(text))


_NOISE_WORDS = {
    "pos", "debit", "credit", "purchase", "card", "recurring", "payment", "ach", "withdrawal",
    "online", "pmt", "ppd", "web", "id", "ref", "des", "indn", "co", "inc", "llc", "com",
    "checkcard", "visa", "mc", "sq", "tst", "pp", "paypal", "the", "www", "bill", "usa", "us",
    "amzn", "mktp", "pmts", "subscr", "store", "whse",
}


_ACH_COMPANY = re.compile(r"\bco:\s*(.+?)\s*(?:ach trace|name:|data:|type:|$)")
_OWN_TRANSFER = re.compile(r"^(?:withdrawal |deposit )?(?:home banking )?(to|from) share\b")
_BANK_PREFIX = re.compile(r"^(?:withdrawal|deposit)\b\s*(?:home banking\b)?")
_PURCHASE_DATE_TAIL = re.compile(r"date:?\s*\d.*$")


def _strip_bank_wording(description: str) -> str:
    """Reduce a bank's raw description to the part naming the merchant.

    Only used when there's no payee (CSV imports). Credit unions wrap every
    line in "Withdrawal ... Date 10/24/26 PURCH ..." and name ACH senders
    after "CO:". Transfers between shares get the same names SimpleFIN uses.
    """
    s = description.lower().replace("%%", " ")
    if m := _OWN_TRANSFER.match(s):
        return f"transfer {m[1]} share"
    if m := _ACH_COMPANY.search(s):
        return m[1]
    return _PURCHASE_DATE_TAIL.sub("", _BANK_PREFIX.sub("", s))


def normalize_merchant(description: str, payee: str | None = None) -> str:
    s = payee.lower() if payee else _strip_bank_wording(description)
    s = re.sub(r"https?://", " ", s)
    s = re.sub(r"\.(com|net|org|io)\b", " ", s)
    s = re.sub(r"[^a-z&' ]+", " ", s)  # drop digits, punctuation, store numbers
    words = [w for w in s.split() if len(w) > 1 and w not in _NOISE_WORDS]
    # Trailing two-letter word is almost always a US state code.
    if len(words) > 1 and len(words[-1]) == 2:
        words = words[:-1]
    key = " ".join(words[:2])
    return key or (payee or description).lower().strip()


def description_key(description: str | None) -> str:
    """A description without its digits, so every "VENMO ... PAYMENT ... Trace 0910..."
    line reads the same. Description rules match against this."""
    return " ".join(re.sub(r"\d+", " ", (description or "").lower()).split())


def display_name(merchant: str) -> str:
    return " ".join(w.capitalize() for w in merchant.split())


def categorize(description: str, amount: float) -> str:
    text = description.lower()
    if _is_card_payment(text, amount):
        return "Credit Card Payments"
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            # A positive amount at a store (a refund) keeps the store's
            # category, so it offsets spending instead of counting as income.
            return category
    return "Income" if amount > 0 else "Other"
