"""Append-only audit trail.

Every step that touches a product, a price, an approval or a payment lands here
in order. Nothing in the app is allowed to edit or delete an entry -- if a
decision was made, the trail shows it, including the ones that blocked money.
In-memory on purpose: this is a demo, and a list keeps the guarantee obvious.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any, Literal

Actor = Literal["USER", "AI", "SYSTEM"]
Status = Literal["ok", "blocked", "failed"]

_ENTRIES: list[dict[str, Any]] = []
_SEQ = itertools.count(1)


def log(actor: Actor, action: str, detail: dict[str, Any] | None = None, status: Status = "ok") -> dict[str, Any]:
    """Append one entry and return it. Callers log rather than ask permission --
    the trail is written even when the action was refused."""
    entry = {
        "seq": next(_SEQ),
        "time": datetime.now().strftime("%H:%M:%S"),
        "actor": actor,
        "action": action,
        "detail": detail or {},
        "status": status,
    }
    _ENTRIES.append(entry)
    return entry


def entries(since_seq: int = 0) -> list[dict[str, Any]]:
    """Entries after `since_seq`, so the UI can poll for just the new rows."""
    return [e for e in _ENTRIES if e["seq"] > since_seq]


def counters() -> dict[str, int]:
    """Headline counts for the UI metrics strip. Derived from the trail itself,
    so the numbers can never drift from what actually happened."""
    return {
        "transactions": sum(1 for e in _ENTRIES if e["action"] == "payment_link_created"),
        "successful": sum(1 for e in _ENTRIES if e["action"] == "payment_succeeded"),
        "failed": sum(1 for e in _ENTRIES if e["action"] == "payment_failed"),
        # Counted per order, not per log line: warning early and blocking again
        # at payment time is one blocked purchase, not two.
        "blocked": len({e["detail"].get("order_id") for e in _ENTRIES if e["status"] == "blocked"} - {None}),
    }
