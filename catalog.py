"""Product catalog: loading, lookup and keyword search.

The catalog is the source of truth for the whole app. The LLM never states a
price or a stock level of its own -- it calls into here, and `gate.py` re-reads
from here before any money moves. Prices are integer paise so nothing is ever
rounded on the way to Razorpay.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("catalog.json")

# Passed as max_price_paise to mean "no ceiling at all". Distinct from None,
# which still lets a budget written into the query text apply.
NO_BUDGET = 10 ** 12

# Words that carry no shopping signal; dropped before scoring.
_STOPWORDS = {
    "a", "an", "and", "the", "for", "with", "under", "below", "less", "than",
    "me", "my", "i", "want", "need", "buy", "get", "find", "show", "some",
    "good", "nice", "please", "to", "of", "in", "on", "is", "it", "that",
    "best", "top", "recommend", "looking", "something", "rs", "inr", "rupees",
}

# Shopper vocabulary -> catalog vocabulary. Keeps search honest when someone
# says "headset" or "screen" instead of the category name we happen to use.
_SYNONYMS = {
    "headset": "headphones",
    "headsets": "headphones",
    "headphone": "headphones",
    "earbuds": "headphones",
    "earphones": "headphones",
    "mouse": "mice",
    "mouses": "mice",
    "keyboard": "keyboards",
    "monitor": "monitors",
    "display": "monitors",
    "screen": "monitors",
    "laptop": "laptops",
    "notebook": "laptops",
    "gamer": "gaming",
    "esports": "gaming",
    "quiet": "silent",
    "noise": "anc",
    "cancelling": "anc",
    "cancellation": "anc",
    "light": "lightweight",
    # Marketplace vocabulary: footwear and personal care.
    "shoe": "footwear",
    "shoes": "footwear",
    "sneaker": "footwear",
    "sneakers": "footwear",
    "sandal": "footwear",
    "sandals": "footwear",
    "trainers": "footwear",
    "mens": "men",
    "man": "men",
    "gents": "men",
    "womens": "women",
    "woman": "women",
    "ladies": "women",
    "daughter": "girls",
    "son": "boys",
    "sister": "women",
    "wife": "women",
    "mom": "women",
    "mother": "women",
    "brother": "men",
    "husband": "men",
    "dad": "men",
    "father": "men",
    "girl": "girls",
    "boy": "boys",
    "kid": "kids",
    "child": "kids",
    "children": "kids",
    "toddler": "kids",
    "baby": "kids",
    "dandruff": "anti_dandruff",
    "sulphate": "sulphate_free",
    "sulfate": "sulphate_free",
    "hairfall": "hair_fall",
    "shampoos": "shampoo",
    "soaps": "soap",
    "gentle": "sensitive_skin",
    "sensitive": "sensitive_skin",
}

# Categories where a product cannot be ordered until the shopper picks a variant.
# The agent asks for it; `gate.py` refuses the payment without it.
REQUIRED_CHOICE = {"footwear": "size"}


def _load() -> dict[str, Any]:
    with CATALOG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


_CATALOG = _load()
_PRODUCTS: list[dict[str, Any]] = _CATALOG["products"]
_BY_ID: dict[str, dict[str, Any]] = {p["id"]: p for p in _PRODUCTS}
_BY_CATEGORY: set[str] = {p["category"] for p in _PRODUCTS}


def all_products() -> list[dict[str, Any]]:
    """Every product, in catalog order."""
    return [dict(p) for p in _PRODUCTS]


def get_product(product_id: str) -> dict[str, Any] | None:
    """One product by id, or None. Callers must handle None -- an unknown id is
    exactly the case where a hallucinated product would otherwise slip through."""
    product = _BY_ID.get((product_id or "").strip().upper())
    return dict(product) if product else None


def categories() -> list[str]:
    return sorted({p["category"] for p in _PRODUCTS})


def stores() -> list[str]:
    return sorted({p["store"] for p in _PRODUCTS})


def required_choice(product: dict[str, Any]) -> str | None:
    """The variant a shopper must pick before this product can be bought, if any."""
    return REQUIRED_CHOICE.get(product["category"]) if product.get("sizes") else None


def size_available(product: dict[str, Any], size: str) -> bool:
    """Case- and spacing-tolerant size match, because shoppers type "uk3", "3"
    and "UK 3" for the same shoe."""
    wanted = _normalise_size(size)
    return any(_normalise_size(option) == wanted for option in product.get("sizes", []))


def canonical_size(product: dict[str, Any], size: str) -> str | None:
    wanted = _normalise_size(size)
    for option in product.get("sizes", []):
        if _normalise_size(option) == wanted:
            return option
    return None


def _normalise_size(size: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(size).lower()).replace("uk", "")


def parse_budget_paise(text: str) -> int | None:
    """Pull a budget out of free text: "under 3000", "Rs. 2,500", "2k".

    Used when the caller did not pass an explicit max_price_paise, so a plain
    keyword search still respects a budget the shopper clearly stated.
    """
    lowered = (text or "").lower().replace(",", "")
    match = re.search(r"(?:under|below|less than|max|budget|upto|up to|within)\s*(?:rs\.?|inr|\u20b9)?\s*(\d+(?:\.\d+)?)\s*(k)?", lowered)
    if not match:
        match = re.search(r"(?:rs\.?|inr|\u20b9)\s*(\d+(?:\.\d+)?)\s*(k)?", lowered)
    if not match:
        return None
    rupees = float(match.group(1)) * (1000 if match.group(2) == "k" else 1)
    return int(round(rupees * 100))


def tokens(text: str) -> list[str]:
    """Lowercase words with stopwords dropped and shopper synonyms applied.
    Shared with the agent so both score against the same vocabulary."""
    raw = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = []
    for word in raw:
        word = _SYNONYMS.get(word, word)
        if word not in _STOPWORDS:
            out.append(word)
    return out


def _score(product: dict[str, Any], query_tokens: list[str]) -> float:
    """Relevance of one product to the query tokens.

    Weighting reflects how shoppers actually talk: the category and the product
    name carry the intent, attributes confirm it, the description is a weak
    tiebreaker.
    """
    score = 0.0
    name_tokens = set(tokens(product["name"]))
    desc_tokens = set(tokens(product["description"]))
    category = product["category"]
    attributes = product["attributes"]

    store = product["store"]
    for token in query_tokens:
        if token == category or token == category.rstrip("s"):
            score += 4.0
        if token == store or token in store.split():
            score += 2.0
        if token in name_tokens:
            score += 3.0
        # "wireless", "mechanical", "low_latency" etc. asked for and present.
        if attributes.get(token) is True:
            score += 2.0
        elif token == "wired" and attributes.get("wireless") is False:
            score += 2.0
        elif attributes.get(token) is False:
            score -= 3.0
        if token in desc_tokens:
            score += 1.0
    return score


def _ranked(query: str, max_price_paise: int | None, category: str | None,
            attributes: dict[str, Any] | None, size: str | None) -> list[tuple[float, int, dict[str, Any]]]:
    """Every product that passes the hard filters, ordered by relevance.

    Stock is deliberately *not* a filter here -- both callers need to see the
    out-of-stock matches, one to drop them and one to talk about them.
    """
    query_tokens = tokens(query)
    if query.strip() and not query_tokens:
        # Words we know nothing about ("ok", "hmm"). Returning every product
        # sorted by price would look like a recommendation; it is not one.
        return []
    wanted_category = _SYNONYMS.get((category or "").lower(), (category or "").lower())
    if wanted_category and wanted_category not in _BY_CATEGORY:
        wanted_category = wanted_category + "s" if wanted_category + "s" in _BY_CATEGORY else wanted_category

    results: list[tuple[float, int, dict[str, Any]]] = []
    for product in _PRODUCTS:
        if max_price_paise is not None and product["price_paise"] > max_price_paise:
            continue
        if wanted_category and product["category"] != wanted_category:
            continue
        if attributes and any(product["attributes"].get(k) != v for k, v in attributes.items()):
            continue
        if size and product.get("sizes") and not size_available(product, size):
            continue

        score = _score(product, query_tokens)
        if query_tokens and score <= 0:
            continue
        results.append((score, product["price_paise"], product))

    results.sort(key=lambda row: (-row[0], row[1]))
    return results


def search_products(
    query: str = "",
    max_price_paise: int | None = None,
    category: str | None = None,
    attributes: dict[str, Any] | None = None,
    size: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Products a shopper can actually buy right now, best match first.

    Out-of-stock items are excluded rather than ranked last: offering something
    that cannot be bought wastes the shopper's attention. `unavailable_matches`
    exists for the one case where mentioning it is useful.
    """
    if max_price_paise is None:
        max_price_paise = parse_budget_paise(query)

    ranked = [row for row in _ranked(query, max_price_paise, category, attributes, size)
              if row[2]["stock"] > 0]
    return [dict(product) for _, _, product in ranked[:limit]]


def unavailable_matches(
    query: str = "",
    max_price_paise: int | None = None,
    category: str | None = None,
    attributes: dict[str, Any] | None = None,
    size: str | None = None,
) -> list[dict[str, Any]]:
    """Out-of-stock products that matched this query strongly enough to be worth
    naming -- either the best match overall, or close behind it.

    `was_best` marks the case that matters most: the shopper asked for something
    specific and it happens to be the thing we cannot sell them.
    """
    if max_price_paise is None:
        max_price_paise = parse_budget_paise(query)

    ranked = _ranked(query, max_price_paise, category, attributes, size)
    return [
        {"product": dict(product), "was_best": index == 0}
        for index, (_, _, product) in enumerate(ranked[:3])
        if product["stock"] == 0
    ]
