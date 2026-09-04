"""FastAPI app: routes, wiring, and the one static page.

Everything money-related funnels through `gate.gate()` before `razorpay_client`
is ever called. There is deliberately no endpoint that creates a payment link
without that call, and the agent has no way to reach one.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

import agent  # noqa: E402  (imported after load_dotenv so the API key is visible)
import audit  # noqa: E402
import catalog  # noqa: E402
import gate  # noqa: E402
import cart  # noqa: E402
import profile  # noqa: E402
import razorpay_client as razorpay  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="VendAI - agentic commerce demo")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Single-shopper demo state. A real deployment would key these by session.
CHAT_HISTORY: list[dict[str, str]] = []
SESSION: dict[str, Any] = {}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def rupees(paise: int) -> str:
    return f"\u20b9{paise / 100:,.2f}".replace(".00", "")


def order_view(order: dict[str, Any]) -> dict[str, Any]:
    """An order plus the display fields the UI needs. The UI never does money
    math -- it renders strings the backend derived from the catalog."""
    remaining = gate.MAX_TXN_PAISE - order["total_paise"]
    return {
        **order,
        "items": [{**line,
                   "unit_price_display": rupees(line["unit_price_paise"]),
                   "line_total_display": rupees(line["line_total_paise"])}
                  for line in order["items"]],
        "total_display": rupees(order["total_paise"]),
        "limit_display": rupees(gate.MAX_TXN_PAISE),
        "remaining_display": rupees(remaining) if remaining >= 0 else None,
        "attempts_remaining": max(0, gate.MAX_PAYMENT_ATTEMPTS - order["attempts"]),
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/config")
def config() -> dict[str, Any]:
    """What the UI needs to render itself honestly: the real limits and whether
    payments are mocked."""
    return {
        "merchant": "VendAI",
        "mock_mode": razorpay.is_mock(),
        "llm_enabled": bool(os.getenv("OPENAI_API_KEY")),
        "limits": gate.limits(),
        "stores": catalog.stores(),
        "categories": catalog.categories(),
    }


@app.get("/profile")
def get_profile() -> dict[str, Any]:
    """What the store remembers about this shopper. Shown in the UI so the
    profile behind the recommendations is never invisible to them."""
    return profile.snapshot()


@app.get("/catalog")
def get_catalog() -> dict[str, Any]:
    return {"products": catalog.all_products()}


@app.post("/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    """One agent turn. May return a proposed order; never a payment."""
    result = agent.chat(request.message, CHAT_HISTORY, SESSION)
    CHAT_HISTORY.append({"role": "user", "content": request.message})
    CHAT_HISTORY.append({"role": "assistant", "content": result["reply"]})
    if result.get("order"):
        result["order"] = order_view(result["order"])
    return {**result, "counters": audit.counters()}


@app.post("/orders/{order_id}/approve")
def approve(order_id: str) -> dict[str, Any]:
    """The human approval step. This is the only thing that sets the flag the
    gate looks for, and it is reachable only from a user click."""
    order, error = gate.approve_order(order_id)
    if order is None:
        return {"ok": False, "reason": error}
    return {"ok": True, "order": order_view(order)}


@app.post("/orders/{order_id}/cancel")
def cancel(order_id: str) -> dict[str, Any]:
    order = gate.cancel_order(order_id)
    if order is None:
        return {"ok": False, "reason": "Unknown order."}
    return {"ok": True, "order": order_view(order)}


@app.post("/orders/{order_id}/pay")
def pay(order_id: str) -> dict[str, Any]:
    """Run the gate, then -- and only then -- create a Razorpay payment link.

    A blocked request is a normal outcome, not an error: it returns 200 with the
    reason so the UI can explain it, and it costs the shopper no attempt.
    """
    decision = gate.gate(order_id, stage="payment")
    order = gate.get_order(order_id)
    if not decision.allowed:
        return {"ok": False, "blocked": True, "gate": decision.as_dict(),
                "order": order_view(order) if order else None}

    try:
        link = razorpay.create_payment_link(
            amount_paise=order["total_paise"],
            reference_id=f"{order['id']}-A{order['attempts'] + 1}",
            description=order["summary"],
        )
    except razorpay.RazorpayError as exc:
        # A payment processor outage must read as "nothing was charged", never
        # as a stack trace, and must not burn one of the two attempts.
        audit.log("SYSTEM", "payment_link_failed", {"order_id": order["id"], "error": str(exc)}, status="failed")
        return {"ok": False, "blocked": False, "reason": f"Could not reach the payment provider. {exc}",
                "order": order_view(order)}

    gate.record_attempt(order["id"])
    order["payment_link_id"] = link["id"]
    order["payment_url"] = link["short_url"]
    order["status"] = "awaiting_payment"
    audit.log("SYSTEM", "payment_link_created", {
        "order_id": order["id"],
        "payment_link_id": link["id"],
        "amount": rupees(order["total_paise"]),
        "attempt": order["attempts"],
        "mode": "mock" if razorpay.is_mock() else "razorpay_test",
    })
    return {"ok": True, "order": order_view(order), "gate": decision.as_dict(),
            "payment_url": link["short_url"], "counters": audit.counters()}


@app.get("/orders/{order_id}/status")
def status(order_id: str) -> dict[str, Any]:
    """Poll Razorpay and resolve the order. Failure is handled here, once, so
    every caller sees the same safe outcome."""
    order = gate.get_order(order_id)
    if order is None:
        return {"ok": False, "reason": "Unknown order."}
    if not order.get("payment_link_id"):
        return {"ok": True, "order": order_view(order), "payment_status": None,
                "message": None, "counters": audit.counters()}

    try:
        link = razorpay.fetch_payment_link(order["payment_link_id"])
    except razorpay.RazorpayError as exc:
        audit.log("SYSTEM", "status_poll_failed", {"order_id": order["id"], "error": str(exc)}, status="failed")
        return {"ok": False, "order": order_view(order),
                "message": "Could not read the payment status just now. No charge was made."}

    payment_status = link.get("status", "created")
    message = None

    if payment_status == "paid" and order["status"] != "paid":
        order["status"] = "paid"
        audit.log("SYSTEM", "payment_succeeded", {
            "order_id": order["id"], "payment_link_id": link["id"], "amount": rupees(order["total_paise"]),
        })
        # A completed purchase becomes history the agent can use next time,
        # recorded per product so "have they bought this before" stays answerable.
        today = datetime.now().strftime("%Y-%m-%d")
        for line in order["items"]:
            profile.record_purchase(line["product_id"], line["product_name"],
                                    rupees(line["line_total_paise"]), today)
        if order.get("source") == "cart":
            cart.clear()
        message = f"Payment successful. {order['summary']} for {rupees(order['total_paise'])}."
    elif payment_status in ("cancelled", "expired") and order["status"] != "failed":
        order["status"] = "failed"
        audit.log("SYSTEM", "payment_failed", {
            "order_id": order["id"], "payment_link_id": link["id"], "razorpay_status": payment_status,
            "attempt": order["attempts"],
        }, status="failed")
    elif payment_status == "created" and order.get("last_polled_status") != "created":
        # Logged once per attempt, not once per poll -- a trail full of "still
        # waiting" rows is a trail nobody reads.
        audit.log("SYSTEM", "status_polled", {"order_id": order["id"], "payment_status": payment_status})
    order["last_polled_status"] = payment_status

    if order["status"] == "failed":
        # The retry decision is the gate's, not the UI's. Asking it here means
        # the button the shopper sees always matches what the backend will allow.
        retry_check = gate.gate(order["id"], stage="payment", record=False)
        message = "Payment wasn't completed - no charge was made."
        if not retry_check.allowed and retry_check.code == "attempt_cap":
            message += " No further retries allowed - attempt limit reached."
        return {"ok": True, "order": order_view(order), "payment_status": payment_status,
                "message": message, "retry_allowed": retry_check.allowed,
                "retry_block_reason": retry_check.reason, "counters": audit.counters()}

    return {"ok": True, "order": order_view(order), "payment_status": payment_status,
            "message": message, "counters": audit.counters()}


@app.get("/audit")
def get_audit(since: int = 0) -> dict[str, Any]:
    return {"entries": audit.entries(since), "counters": audit.counters()}


@app.get("/mock/payment/{link_id}", include_in_schema=False)
def mock_payment_page(link_id: str) -> HTMLResponse:
    """Stand-in for Razorpay's hosted test page, used only in mock mode.

    Razorpay's real test page offers Success and Failure; so does this, so the
    demo flow is the same shape with or without keys.
    """
    try:
        link = razorpay.fetch_payment_link(link_id)
    except razorpay.RazorpayError:
        return HTMLResponse("<h1>Unknown payment link</h1>", status_code=404)

    amount = rupees(link.get("amount", 0))
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Mock payment - {amount}</title>
<style>
 body {{ font-family: system-ui, sans-serif; background:#FAFAF8; color:#1A1A19;
        display:grid; place-items:center; height:100vh; margin:0; }}
 .card {{ background:#fff; border:1px solid #E6E5E0; border-radius:12px; padding:32px; width:340px; text-align:center; }}
 .amt {{ font-size:28px; font-weight:600; margin:8px 0 4px; }}
 .meta {{ color:#6B6A65; font-size:13px; margin-bottom:24px; }}
 button {{ width:100%; padding:12px; border-radius:8px; font-size:15px; cursor:pointer; margin-top:8px; border:1px solid #E6E5E0; }}
 .pay {{ background:#5A4BDA; color:#F5F4FF; border-color:#5A4BDA; }}
 .done {{ color:#6B6A65; font-size:13px; margin-top:16px; }}
</style></head>
<body><div class="card">
  <div class="meta">MOCK PAYMENT PAGE &middot; no real money</div>
  <div class="amt">{amount}</div>
  <div class="meta">{link.get("description", "")}<br>{link_id}</div>
  <button class="pay" onclick="finish('paid')">Pay {amount}</button>
  <button onclick="finish('cancelled')">Simulate failure</button>
  <div class="done" id="done"></div>
</div>
<script>
async function finish(outcome) {{
  await fetch('/mock/payment/{link_id}/' + outcome, {{ method: 'POST' }});
  document.getElementById('done').textContent =
    outcome === 'paid' ? 'Paid. Return to the shop tab.' : 'Payment cancelled. Return to the shop tab.';
}}
</script></body></html>""")


@app.post("/mock/payment/{link_id}/{outcome}", include_in_schema=False)
def mock_payment_outcome(link_id: str, outcome: str) -> dict[str, Any]:
    """Mock-only status driver. Real mode never reaches it -- the client refuses
    to force a status on a link it did not mint."""
    try:
        link = razorpay.force_mock_status(link_id, "paid" if outcome == "paid" else "cancelled")
    except razorpay.RazorpayError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "status": link["status"]}


class ProposeRequest(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None


@app.get("/orders")
def order_history() -> dict[str, Any]:
    """Orders from this session, followed by the ones already on the shopper's
    profile. Both are shown so the history reads as one list rather than as two
    different kinds of truth."""
    session_orders = [{
        **order_view(order),
        "when": "today " + order.get("created_at", ""),
        "source": "session",
    } for order in gate.all_orders()]

    session_ids = {line["product_id"] for order in session_orders if order["status"] == "paid"
                   for line in order["items"]}
    past = [{
        "id": None,
        "summary": entry["product"],
        "items": [],
        "total_display": entry["total"],
        "status": "paid",
        "when": entry["date"],
        "source": "profile",
    } for entry in profile.snapshot()["history"] if entry["product_id"] not in session_ids]

    return {"orders": session_orders + past}


@app.post("/orders")
def create_order(request: ProposeRequest) -> dict[str, Any]:
    """Draft an order from a UI click ("Review & buy").

    Same code path as the agent's propose_purchase: the price is re-derived from
    the catalog and the gate is previewed, so a click cannot buy on better terms
    than a model can.
    """
    order, error = gate.create_single_order(request.product_id, request.quantity, request.size)
    if order is None:
        return {"ok": False, "reason": error}
    order["source"] = "buy_now"
    audit.log("USER", "product_selected", {
        "order_id": order["id"], "product": order["summary"],
        "total": rupees(order["total_paise"]),
    })
    preview = gate.gate(order["id"], stage="preview")
    return {"ok": True, "order": order_view(order), "gate_preview": preview.as_dict()}


class CartRequest(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None


def cart_view() -> dict[str, Any]:
    """The cart plus what the limits mean for it, priced from the catalog."""
    lines = cart.lines()
    total = cart.total_paise()
    return {
        "lines": [{**line,
                   "line_total_display": rupees(line["line_total_paise"])} for line in lines],
        "count": cart.count(),
        "total_paise": total,
        "total_display": rupees(total),
        "limit_display": rupees(gate.MAX_TXN_PAISE),
        "within_limit": total <= gate.MAX_TXN_PAISE,
    }


@app.get("/cart")
def get_cart() -> dict[str, Any]:
    return cart_view()


@app.post("/cart")
def add_to_cart(request: CartRequest) -> dict[str, Any]:
    line, error = cart.add(request.product_id, request.quantity, request.size)
    if line is None:
        return {"ok": False, "reason": error, "cart": cart_view()}
    return {"ok": True, "line": line, "cart": cart_view()}


@app.post("/cart/{line_id}/remove")
def remove_from_cart(line_id: str) -> dict[str, Any]:
    return {"ok": cart.remove(line_id), "cart": cart_view()}


@app.post("/cart/checkout")
def checkout() -> dict[str, Any]:
    """Turn the cart into one pending order.

    One order, one payment, one spend check: a cart of cheap things is still a
    single charge, and `gate()` caps the charge rather than the item.
    """
    lines = cart.lines()
    if not lines:
        return {"ok": False, "reason": "Your cart is empty."}

    order, error = gate.create_order([
        {"product_id": line["product_id"], "quantity": line["quantity"], "size": line["size"]}
        for line in lines
    ])
    if order is None:
        return {"ok": False, "reason": error}
    order["source"] = "cart"
    audit.log("USER", "cart_checkout", {
        "order_id": order["id"], "product": order["summary"],
        "lines": len(lines), "total": rupees(order["total_paise"]),
    })
    preview = gate.gate(order["id"], stage="preview")
    return {"ok": True, "order": order_view(order), "gate_preview": preview.as_dict()}
