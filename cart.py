"""The shopping cart: lines the shopper has picked but not yet paid for.

The cart holds intent, not authority. It validates that a line is orderable at
all -- real product, in stock, valid size -- and leaves every question about
money to `gate.py`, which re-derives the prices when the cart becomes an order.
In memory, like the rest of this build.
"""

from __future__ import annotations

import itertools
from typing import Any

import audit
import catalog

_LINES: dict[str, dict[str, Any]] = {}
_LINE_SEQ = itertools.count(1)


def add(product_id: str, quantity: int = 1, size: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Add one line, or top up the quantity of a line that already matches."""
    product = catalog.get_product(product_id)
    if product is None:
        return None, f"No product with id {product_id!r} exists in the catalog."
    if product["stock"] <= 0:
        return None, f"{product['name']} is out of stock."
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None, "Quantity must be a whole number."
    if quantity < 1:
        return None, "Quantity must be at least 1."

    if catalog.required_choice(product) == "size":
        if not size:
            return None, f"{product['name']} needs a size. Available: " + ", ".join(product["sizes"]) + "."
        if not catalog.size_available(product, size):
            return None, (f"Size {size} is not available for {product['name']}. Available: "
                          + ", ".join(product["sizes"]) + ".")
        size = catalog.canonical_size(product, size)

    for line in _LINES.values():
        if line["product_id"] == product["id"] and line["size"] == size:
            line["quantity"] += quantity
            audit.log("USER", "cart_updated", {"product": product["name"], "quantity": line["quantity"]})
            return dict(line), None

    line_id = f"L{next(_LINE_SEQ):03d}"
    line = {"line_id": line_id, "product_id": product["id"], "product_name": product["name"],
            "category": product["category"], "size": size, "quantity": quantity}
    _LINES[line_id] = line
    audit.log("USER", "cart_added", {"product": product["name"], "size": size, "quantity": quantity})
    return dict(line), None


def remove(line_id: str) -> bool:
    line = _LINES.pop(line_id, None)
    if line is None:
        return False
    audit.log("USER", "cart_removed", {"product": line["product_name"]})
    return True


def clear() -> None:
    _LINES.clear()


def lines() -> list[dict[str, Any]]:
    """Cart lines with prices read fresh from the catalog every time, so a cart
    left open across a price change never shows a stale total."""
    out = []
    for line in _LINES.values():
        product = catalog.get_product(line["product_id"])
        if product is None:
            continue
        out.append({**line,
                    "unit_price_paise": product["price_paise"],
                    "unit_price_display": product["price_display"],
                    "line_total_paise": product["price_paise"] * line["quantity"],
                    "stock": product["stock"]})
    return out


def total_paise() -> int:
    return sum(line["line_total_paise"] for line in lines())


def count() -> int:
    return sum(line["quantity"] for line in _LINES.values())
