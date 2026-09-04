"""Shopper profile: what the store already knows about this shopper.

Two things live here -- preferences the shopper has told us (a shoe size, a hair
concern) and the orders they have actually completed. The agent reads both so it
asks about a size once rather than every time, and it writes back through
`remember()` so the next conversation starts better informed.

Seeded from `profile.json` and kept in memory afterwards: a returning shopper is
the interesting demo, and a fresh dict every restart is the honest scope for a
build with no database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import audit

PROFILE_PATH = Path(__file__).with_name("profile.json")

# Preference keys the agent is expected to use, with the phrasing the UI shows.
# Anything else it invents is still stored, just without a friendly label.
KNOWN_PREFERENCES = {
    "shoe_size": "shoe size",
    "hair_concern": "hair concern",
    "skin_type": "skin type",
    "preferred_brand": "preferred brand",
}


def label_for(key: str) -> str:
    """Human phrasing for a preference key.

    Sizes are stored per person -- `sister_shoe_size`, `daughter_shoe_size` --
    because one household is several sets of feet, so labels are derived rather
    than enumerated.
    """
    if key in KNOWN_PREFERENCES:
        return KNOWN_PREFERENCES[key]
    if key.endswith("_shoe_size"):
        return key[: -len("_shoe_size")].replace("_", " ") + "'s shoe size"
    return key.replace("_", " ")

_PROFILE: dict[str, Any] = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def snapshot() -> dict[str, Any]:
    """Everything the agent may see about the shopper, plus display labels."""
    return {
        "preferences": dict(_PROFILE["preferences"]),
        "preference_labels": {key: label_for(key) for key in _PROFILE["preferences"]},
        "history": [dict(order) for order in _PROFILE["history"]],
    }


def get(key: str) -> str | None:
    return _PROFILE["preferences"].get(_normalise(key))


def purchase_count(product_id: str) -> int:
    """How many times this shopper has bought this exact product. Used to decide
    whether an out-of-stock item is worth mentioning to them by name."""
    return sum(1 for order in _PROFILE["history"] if order.get("product_id") == product_id)


def remember(key: str, value: str) -> dict[str, Any]:
    """Save one preference. Logged, because a store quietly building a profile of
    someone is exactly the thing an audit trail should make visible."""
    key = _normalise(key)
    value = str(value).strip()[:80]
    previous = _PROFILE["preferences"].get(key)
    _PROFILE["preferences"][key] = value
    audit.log("SYSTEM", "preference_saved", {
        "key": key,
        "label": label_for(key),
        "value": value,
        "replaced": previous,
    })
    return snapshot()


def record_purchase(product_id: str, product_name: str, total_display: str, date: str) -> None:
    """Add one purchased line to the history the agent reads next time.

    Recorded per product rather than per order, because "have they bought this
    before" is a question about a product.
    """
    _PROFILE["history"].insert(0, {
        "date": date, "product_id": product_id,
        "product": product_name, "total": total_display,
    })


def _normalise(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
