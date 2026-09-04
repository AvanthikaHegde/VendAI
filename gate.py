"""The safety core: order records plus the single gate every payment passes.

The LLM proposes; this module disposes. Nothing here trusts the model or the
browser -- an order is only ever a *request*, and `gate()` re-derives the price
from the catalog and re-checks every limit on every payment attempt. The limits
are constants in this file, never prompt text, because prompt text can be
argued with and a constant cannot.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import audit
import catalog

# --- Authorized-spend policy. Backend-only; the model never sees or sets these.
MAX_TXN_PAISE = 500000          # Rs 5,000 ceiling on any single AI-initiated payment
MAX_QUANTITY = 2                # an agent buying in bulk is almost always a mistake
REQUIRE_APPROVAL = True         # a human must approve the exact order before it can be paid
MAX_PAYMENT_ATTEMPTS = 2        # hard retry cap; failures must not loop

Stage = Literal["preview", "payment"]

_ORDERS: dict[str, dict[str, Any]] = {}
_ORDER_SEQ = itertools.count(1)


@dataclass
class GateResult:
    """Outcome of one gate run, shaped for both humans and the audit trail."""

    allowed: bool
    code: str | None = None
    reason: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason, "checks": self.checks}


def _rupees(paise: int) -> str:
    """Paise -> a display string. Formatting only; money math stays in paise."""
    return f"\u20b9{paise / 100:,.2f}".replace(".00", "")


def build_line(product_id: str, quantity: int = 1,
               size: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one requested line against the catalog and price it from there.

    The caller chooses the product, the quantity and the size. Every other
    number on the line comes from the catalog, here and again in `gate()`.
    """
    product = catalog.get_product(product_id)
    if product is None:
        return None, f"No product with id {product_id!r} exists in the catalog."
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None, "Quantity must be a whole number."
    if quantity < 1:
        return None, "Quantity must be at least 1."

    if catalog.required_choice(product) == "size":
        if not size:
            return None, (f"{product['name']} needs a size. Available: "
                          + ", ".join(product["sizes"]) + ".")
        if not catalog.size_available(product, size):
            return None, (f"Size {size} is not available for {product['name']}. "
                          "Available: " + ", ".join(product["sizes"]) + ".")
        size = catalog.canonical_size(product, size)

    return {
        "product_id": product["id"],
        "product_name": product["name"],
        "category": product["category"],
        "quantity": quantity,
        "size": size,
        "unit_price_paise": product["price_paise"],
        "line_total_paise": product["price_paise"] * quantity,
    }, None


def summarise(items: list[dict[str, Any]]) -> str:
    """One line of text for an order that may hold several products."""
    first = f"{items[0]['product_name']} × {items[0]['quantity']}"
    if len(items) == 1:
        return first
    return f"{first} + {len(items) - 1} more"


def create_order(items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Create a pending order from one or more requested lines. Moves no money."""
    if not items:
        return None, "There is nothing to order."

    lines: list[dict[str, Any]] = []
    for requested in items:
        line, error = build_line(requested.get("product_id", ""),
                                 requested.get("quantity", 1) or 1,
                                 requested.get("size"))
        if line is None:
            return None, error
        lines.append(line)

    order_id = f"ORD-{next(_ORDER_SEQ):04d}"
    order = {
        "id": order_id,
        "created_at": datetime.now().strftime("%H:%M"),
        "items": lines,
        "summary": summarise(lines),
        "total_paise": sum(line["line_total_paise"] for line in lines),
        "currency": "INR",
        "approved": False,
        "status": "proposed",
        "attempts": 0,
        "payment_link_id": None,
        "payment_url": None,
        "block_reason": None,
    }
    _ORDERS[order_id] = order
    return order, None


def create_single_order(product_id: str, quantity: int = 1,
                        size: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Buy-now convenience: one product, straight to a pending order."""
    return create_order([{"product_id": product_id, "quantity": quantity, "size": size}])


def get_order(order_id: str) -> dict[str, Any] | None:
    return _ORDERS.get((order_id or "").strip().upper())


def approve_order(order_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Record the human approval for one specific order.

    Approval is per-order and is consumed by `gate()`; it is not a session-wide
    "yes" that a later, different order could ride on.
    """
    order = get_order(order_id)
    if order is None:
        return None, "Unknown order."
    if order["status"] in {"paid", "cancelled"}:
        return None, f"Order is already {order['status']}."
    order["approved"] = True
    order["status"] = "approved"
    audit.log("USER", "approval_given", {
        "order_id": order["id"],
        "product": order["summary"],
        "total": _rupees(order["total_paise"]),
    })
    return order, None


def cancel_order(order_id: str) -> dict[str, Any] | None:
    order = get_order(order_id)
    if order is None:
        return None
    order["status"] = "cancelled"
    order["approved"] = False
    audit.log("USER", "order_cancelled", {"order_id": order["id"]}, status="ok")
    return order


def record_attempt(order_id: str) -> None:
    """Count a payment attempt. Called once a payment link actually exists, so a
    blocked request never burns one of the user's two attempts."""
    order = get_order(order_id)
    if order is not None:
        order["attempts"] += 1


def limits() -> dict[str, Any]:
    """The policy, in a form the UI can display. Display only -- the UI showing
    a limit is not what enforces it."""
    return {
        "max_txn_paise": MAX_TXN_PAISE,
        "max_txn_display": _rupees(MAX_TXN_PAISE),
        "max_quantity": MAX_QUANTITY,
        "require_approval": REQUIRE_APPROVAL,
        "max_payment_attempts": MAX_PAYMENT_ATTEMPTS,
    }


def _check(name: str, label: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "label": label, "ok": ok, "detail": detail}


def gate(order_id: str, stage: Stage = "payment", record: bool = True) -> GateResult:
    """The one place a payment can be authorized. There is no other path.

    Checks run in order and stop at the first failure, so the reason returned is
    the actual reason. `stage="preview"` runs the same policy minus the human
    checks (approval, attempts) purely so the UI can warn early -- it can never
    authorize anything, because only the "payment" stage is called before a
    payment link is created.
    """
    order = get_order(order_id)
    if order is None:
        return GateResult(False, "unknown_order", "That order does not exist.")

    checks: list[dict[str, Any]] = []
    items = order["items"]

    # 1-3. Every line is re-validated against the catalog: the product still
    #      exists, there is stock for the quantity asked, the size is one the
    #      product actually comes in, and the price is re-derived here. This is
    #      what makes a tampered price or a stale cart harmless.
    for line in items:
        product = catalog.get_product(line["product_id"])
        if product is None:
            return _block(order, stage, "product_missing",
                          f"{line['product_name']} is no longer in the catalog.", checks, record)
        if product["stock"] < line["quantity"]:
            return _block(order, stage, "out_of_stock",
                          f"{product['name']} is out of stock (requested {line['quantity']}, "
                          f"available {product['stock']}).", checks, record)
        if catalog.required_choice(product) == "size":
            if not line.get("size") or not catalog.size_available(product, line["size"]):
                return _block(order, stage, "size_required",
                              f"Pick a size for {product['name']} before paying. Available: "
                              + ", ".join(product["sizes"]) + ".", checks, record)
        if line["quantity"] > MAX_QUANTITY:
            return _block(order, stage, "quantity_cap",
                          f"Quantity {line['quantity']} of {product['name']} exceeds the AI "
                          f"purchase limit of {MAX_QUANTITY} per product.", checks, record)
        line["unit_price_paise"] = product["price_paise"]
        line["line_total_paise"] = product["price_paise"] * line["quantity"]

    requested_total = order["total_paise"]
    catalog_total = sum(line["line_total_paise"] for line in items)
    order["total_paise"] = catalog_total
    order["summary"] = summarise(items)

    noun = "product" if len(items) == 1 else "products"
    checks.append(_check("product_verified", "Products verified", True, f"{len(items)} {noun}"))
    checks.append(_check("stock_verified", "In stock", True, "every line available"))
    sized = [line for line in items if line.get("size")]
    if sized:
        checks.append(_check("size_verified", "Sizes confirmed", True,
                             ", ".join(f"{line['product_name'].split()[0]} {line['size']}" for line in sized)))
    price_note = f"catalog {_rupees(catalog_total)} = requested {_rupees(requested_total)}"
    if requested_total != catalog_total:
        price_note = f"catalog {_rupees(catalog_total)} overrode requested {_rupees(requested_total)}"
    checks.append(_check("price_verified", "Prices verified", True, price_note))
    checks.append(_check("quantity_within_cap", "Quantities within cap", True,
                         f"max {MAX_QUANTITY} per product"))

    # 4. The spend ceiling, applied to the whole order -- a cart of small items
    #    is still one payment, and it is the payment that is capped.
    if catalog_total > MAX_TXN_PAISE:
        return _block(order, stage, "spend_cap",
                      f"Total {_rupees(catalog_total)} exceeds the authorized AI spending limit of "
                      f"{_rupees(MAX_TXN_PAISE)}. This purchase needs a human to make it directly.",
                      checks, record)
    checks.append(_check("within_limit", "Within spending limit", True,
                         f"{_rupees(catalog_total)} of {_rupees(MAX_TXN_PAISE)}"))

    if stage == "preview":
        return _allow(order, stage, checks, record)

    # 6. Explicit, per-order human approval. The agent cannot set this flag;
    #    only the /approve endpoint, called by a human click, can.
    if REQUIRE_APPROVAL and not order["approved"]:
        return _block(order, stage, "approval_required",
                      "This order has not been approved by you yet. Nothing is charged without approval.",
                      checks, record)
    checks.append(_check("approved", "Human approval on file", True, "approved for this exact order"))

    # 7. Retry cap. Enforced here, not in the UI, so a hand-crafted request
    #    cannot retry a failing payment forever.
    if order["attempts"] >= MAX_PAYMENT_ATTEMPTS:
        return _block(order, stage, "attempt_cap",
                      f"Attempt limit reached ({order['attempts']} of {MAX_PAYMENT_ATTEMPTS}). "
                      "No further payment attempts are allowed for this order.",
                      checks, record)
    checks.append(_check("attempts_within_cap", "Attempts remaining", True,
                         f"{order['attempts']} of {MAX_PAYMENT_ATTEMPTS} used"))

    return _allow(order, stage, checks, record)


def _allow(order: dict[str, Any], stage: Stage, checks: list[dict[str, Any]], record: bool = True) -> GateResult:
    result = GateResult(True, None, None, checks)
    if stage == "payment" and record:
        order["block_reason"] = None
        audit.log("SYSTEM", "gate_passed", {
            "order_id": order["id"],
            "total": _rupees(order["total_paise"]),
            "limit": _rupees(MAX_TXN_PAISE),
            "checks": [c["label"] for c in checks],
        })
    return result


def _block(order: dict[str, Any], stage: Stage, code: str, reason: str,
           checks: list[dict[str, Any]], record: bool = True) -> GateResult:
    checks = checks + [_check(code, "Blocked", False, reason)]
    if not record:
        return GateResult(False, code, reason, checks)
    order["block_reason"] = reason
    order["status"] = "blocked"
    # Logged at both stages: a block the shopper was warned about early is still
    # a block, and the trail should show when it was first detected.
    audit.log("SYSTEM", "gate_blocked", {
        "order_id": order["id"],
        "stage": stage,
        "code": code,
        "reason": reason,
    }, status="blocked")
    return GateResult(False, code, reason, checks)
