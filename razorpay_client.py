"""Razorpay Payment Links (test mode) behind a create / fetch / cancel interface.

If RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are absent the module runs in mock
mode: it mints plink_MOCK_... links whose status the demo drives by hand. The
interface is identical either way, so nothing upstream knows or cares which
mode it is in -- which is also what keeps the real path honest.

Test mode only. There is no branch in this file that can reach live keys.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Literal

import requests

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 15

LinkStatus = Literal["created", "paid", "cancelled", "expired"]

# Mock links live here; keyed by link id. Mirrors the fields we read from the API.
_MOCK_LINKS: dict[str, dict[str, Any]] = {}


class RazorpayError(RuntimeError):
    """Anything that stops us from creating or reading a payment link."""


def _credentials() -> tuple[str, str] | None:
    key_id = (os.getenv("RAZORPAY_KEY_ID") or "").strip()
    key_secret = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
    if not key_id or not key_secret:
        return None
    if not key_id.startswith("rzp_test_"):
        # A live key here would move real money on a demo click. Refuse it.
        raise RazorpayError("Only Razorpay test keys (rzp_test_...) are accepted by this app.")
    return key_id, key_secret


def is_mock() -> bool:
    """True when no test credentials are configured."""
    return _credentials() is None


def create_payment_link(amount_paise: int, reference_id: str, description: str) -> dict[str, Any]:
    """Create a payment link for an amount the caller has already had gated.

    `amount_paise` is passed through untouched -- the value that arrives here is
    the catalog-derived total from `gate.py`, never a client-supplied number.
    """
    credentials = _credentials()
    if credentials is None:
        return _create_mock_link(amount_paise, reference_id, description)

    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": reference_id,
        "description": description[:255],
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    }
    try:
        response = requests.post(
            f"{API_BASE}/payment_links", json=payload, auth=credentials, timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RazorpayError(f"Could not reach Razorpay: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise RazorpayError(_error_message(response))

    body = response.json()
    return {"id": body["id"], "short_url": body["short_url"], "status": body.get("status", "created")}


def fetch_payment_link(link_id: str) -> dict[str, Any]:
    """Current state of a payment link: created | paid | cancelled | expired."""
    credentials = _credentials()
    if credentials is None or link_id.startswith("plink_MOCK_"):
        return _fetch_mock_link(link_id)

    try:
        response = requests.get(
            f"{API_BASE}/payment_links/{link_id}", auth=credentials, timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RazorpayError(f"Could not reach Razorpay: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise RazorpayError(_error_message(response))

    body = response.json()
    return {
        "id": body["id"],
        "status": body.get("status", "created"),
        "short_url": body.get("short_url"),
        "amount": body.get("amount"),
    }


def cancel_payment_link(link_id: str) -> dict[str, Any]:
    """Cancel a link the shopper could not complete, so the status resolves.

    This exists because of an asymmetry in the real API: a *successful* payment
    flips the link to `paid`, but a *failed* one leaves it `created` so the
    shopper can try again on the same page. Nothing upstream would ever resolve
    that, and the retry cap would never be reached. This app counts attempts
    itself and mints a fresh link per attempt, so an abandoned link is already
    dead to us -- cancelling it is what turns "the shopper gave up" into a
    terminal status the normal polling path knows how to handle.
    """
    credentials = _credentials()
    if credentials is None or link_id.startswith("plink_MOCK_"):
        return force_mock_status(link_id, "cancelled")

    try:
        response = requests.post(
            f"{API_BASE}/payment_links/{link_id}/cancel", auth=credentials, timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RazorpayError(f"Could not reach Razorpay: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        # Most often: the link is already paid or already cancelled. Either way
        # the caller re-polls, so the real status wins over this one's opinion.
        raise RazorpayError(_error_message(response))

    body = response.json()
    return {
        "id": body["id"],
        "status": body.get("status", "cancelled"),
        "short_url": body.get("short_url"),
        "amount": body.get("amount"),
    }


def force_mock_status(link_id: str, status: LinkStatus) -> dict[str, Any]:
    """Mock-only: stand in for the shopper choosing Success or Failure on
    Razorpay's hosted test page."""
    link = _MOCK_LINKS.get(link_id)
    if link is None:
        raise RazorpayError("Unknown mock payment link.")
    if status not in ("paid", "cancelled", "expired", "created"):
        raise RazorpayError(f"Unsupported mock status {status!r}.")
    link["status"] = status
    return dict(link)


def _create_mock_link(amount_paise: int, reference_id: str, description: str) -> dict[str, Any]:
    link_id = f"plink_MOCK_{secrets.token_hex(6)}"
    link = {
        "id": link_id,
        "status": "created",
        "amount": amount_paise,
        "reference_id": reference_id,
        "description": description,
        # Served by this app: a stand-in for Razorpay's hosted test page.
        "short_url": f"/mock/payment/{link_id}",
    }
    _MOCK_LINKS[link_id] = link
    return {"id": link_id, "short_url": link["short_url"], "status": "created"}


def _fetch_mock_link(link_id: str) -> dict[str, Any]:
    link = _MOCK_LINKS.get(link_id)
    if link is None:
        raise RazorpayError("Unknown payment link.")
    return dict(link)


def _error_message(response: requests.Response) -> str:
    """Razorpay's error text if it sent one, otherwise the status code. Never
    echoes credentials or headers back to the caller."""
    try:
        return response.json().get("error", {}).get("description") or f"Razorpay returned HTTP {response.status_code}."
    except ValueError:
        return f"Razorpay returned HTTP {response.status_code}."
