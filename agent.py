"""The shopping agent: an LLM that can only see the marketplace through tools.

The model decides what to *offer* and what to *ask*. It cannot decide that money
moves -- `propose_purchase` drafts a pending order and nothing else, and
`gate.py` re-checks that order before a payment link can exist.

It reads the shopper profile so it asks for a shoe size once rather than every
time, and writes back through `remember_preference`. The provider is isolated in
`_complete()`: swapping OpenAI for Anthropic is that one function.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import audit
import catalog
import gate
import profile

# gpt-4o-mini was measured following these instructions poorly: it asked for a
# size it had already been given and contradicted its own tool results. The
# tool layer refuses bad outcomes either way, but the wording matters here.
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_TOOL_ROUNDS = 5
MAX_OPTIONS = 5

SYSTEM_PROMPT = """You are VendAI, a shopping assistant for a marketplace with three stores:
electronics, footwear, and personal care.

How to handle a request:
1. If the request depends on something personal -- a shoe size, a hair or skin
   concern -- call get_shopper_profile first. If the profile already has it, use
   it and mention in half a sentence that you did. If it does not, ask ONE short
   question and stop there. Never guess a size.
2. Once you have what you need, call search_products and offer the best three to
   five matches. If the tool returns "must_ask", ask exactly that question and
   say nothing else -- it returned no products because policy will not let it.
3. Describe the shortlist in ONE or TWO sentences: name the closest match and
   what separates it. Never number or list the products, and never restate their
   prices -- the shopper is already looking at them as cards beside your reply.
4. When the shopper tells you something worth keeping (a size, a preference),
   call remember_preference so you do not have to ask again next time. Sizes
   belong to a person, so save them under that person: shoe_size for the shopper
   themself, daughter_shoe_size, sister_shoe_size, brother_shoe_size and so on.
   Never reuse one person's size for another -- when the shopper mentions a
   different person, look for that person's size and ask if you do not have it.
5. When they pick one, call propose_purchase with that product id and, for
   footwear, the size. That drafts an order for their approval and charges
   nothing.

Rules:
- Only ever mention products your tools returned. Never invent a name, a price,
  a size or a stock level.
- Never offer something that is out of stock. search_products returns those
  separately under "unavailable". Mention one only when the shopper clearly asked
  for that specific item, or when it is marked previously_purchased -- then say in
  one sentence that it is out of stock and offer the closest alternatives instead.
- Never say a payment happened, and never state a spending limit or promise a
  purchase will go through -- the backend decides that after they approve.
- Be brief and warm. Plain sentences only: no markdown, no numbered lists, no
  bullet points, no headings. Two sentences is usually the right length.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_shopper_profile",
            "description": (
                "Saved preferences (sizes, hair or skin concerns) and past purchases. "
                "Call this before asking the shopper for a detail they may already have given."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_preference",
            "description": "Save one detail about the shopper for future conversations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Snake_case key, e.g. kids_shoe_size, shoe_size, hair_concern, skin_type.",
                    },
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the marketplace catalog. Always search before offering anything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What the shopper wants, in their words."},
                    "max_price_paise": {"type": "integer", "description": "Budget ceiling in paise (Rs 2,000 = 200000)."},
                    "category": {"type": "string", "enum": catalog.categories()},
                    "attributes": {
                        "type": "object",
                        "description": 'Exact attribute match, e.g. {"kids": true, "anti_dandruff": true}.',
                        "additionalProperties": True,
                    },
                    "size": {"type": "string", "description": 'Footwear size, e.g. "UK 12". Only for footwear.'},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Full details for one product id, including available sizes.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_purchase",
            "description": (
                "Draft a pending order for the shopper to approve. Does NOT charge anything. "
                "Call it only once the shopper has chosen a specific product."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "quantity": {"type": "integer", "default": 1},
                    "size": {"type": "string", "description": "Required for footwear."},
                },
                "required": ["product_id"],
            },
        },
    },
]


# Attributes worth showing a shopper, and how to phrase them.
_ATTRIBUTE_LABELS = {
    "wireless": "wireless", "low_latency": "low-latency", "microphone": "microphone",
    "anc": "noise cancelling", "mechanical": "mechanical", "hot_swappable": "hot-swappable",
    "rgb": "RGB", "lightweight": "lightweight", "silent": "silent", "gaming": "built for gaming",
    "kids": "for kids", "girls": "for girls", "boys": "for boys", "women": "for women",
    "men": "for men", "running": "for running", "walking": "for walking", "school": "school-ready",
    "velcro": "velcro straps", "washable": "washable", "waterproof": "water resistant",
    "cushioned": "cushioned", "casual": "casual", "anti_dandruff": "anti-dandruff",
    "sulphate_free": "sulphate free", "sensitive_skin": "for sensitive skin",
    "tear_free": "tear free", "natural": "natural", "hair_fall": "for hair fall",
    "oily_skin": "for oily skin", "dry_hair": "for dry hair", "antibacterial": "antibacterial",
}


def _rupees(paise: int) -> str:
    return f"\u20b9{paise / 100:,.2f}".replace(".00", "")


def match_reasons(product: dict[str, Any], query: str) -> list[str]:
    """Short "why this one" bullets, derived from the catalog rather than from the
    model, so a card cannot claim something the product does not have."""
    reasons: list[str] = []
    budget = catalog.parse_budget_paise(query)
    if budget is not None and product["price_paise"] <= budget:
        reasons.append(f"under your {_rupees(budget)} budget")
    query_words = set(catalog.tokens(query))
    for key, label in _ATTRIBUTE_LABELS.items():
        if product["attributes"].get(key) is True and (key in query_words or label.split()[0] in query_words):
            reasons.append(label)
    if len(reasons) < 2:
        for key, label in _ATTRIBUTE_LABELS.items():
            if product["attributes"].get(key) is True and label not in reasons:
                reasons.append(label)
            if len(reasons) == 3:
                break
    reasons.append("in stock" if product["stock"] > 0 else "out of stock")
    return reasons[:5]


def as_option(product: dict[str, Any], query: str) -> dict[str, Any]:
    """One offered product, with everything the card needs to be honest: the
    reasons, the sizes it comes in, and whether the AI is even allowed to buy it."""
    return {
        "product": product,
        "reasons": match_reasons(product, query),
        "within_limit": product["price_paise"] <= gate.MAX_TXN_PAISE,
        "needs_size": catalog.required_choice(product) == "size",
        "sizes": product.get("sizes", []),
    }


def _tool_profile(_args: dict[str, Any]) -> Any:
    snapshot = profile.snapshot()
    audit.log("AI", "profile_read", {
        "known": sorted(snapshot["preferences"]),
        "past_orders": len(snapshot["history"]),
    })
    return snapshot


def _same_size(left: str, right: str) -> bool:
    digits = lambda value: re.sub(r"[^0-9]", "", str(value))
    return bool(digits(left)) and digits(left) == digits(right)


def _tool_remember(args: dict[str, Any], turn: dict[str, Any]) -> Any:
    key, value = args.get("key", ""), args.get("value", "")
    if not key or not value:
        return {"error": "Both key and value are required."}

    # A size may only be recorded if the shopper actually said it in this message.
    # Without this, a model that guesses a sister's size can write the guess to
    # the profile and read it back a moment later as an established fact.
    if key.endswith("shoe_size"):
        stated = _extract_size(turn.get("query", ""))
        if not stated or not _same_size(stated, value):
            audit.log("SYSTEM", "preference_rejected",
                      {"key": key, "value": value, "reason": "size not stated by the shopper"},
                      status="blocked")
            return {"error": ("Refused: the shopper did not give that size in this message. "
                              "Ask them for it instead of inferring it from anyone else.")}

    if key.endswith("shoe_size"):
        value = _extract_size(turn.get("query", "")) or value
    profile.remember(key, value)
    return {"saved": True, "key": key, "value": value}


def _resolved_size_key(turn: dict[str, Any], recipient: str | None) -> tuple[str, str | None]:
    """Whose size a message is talking about, as (preference key, recipient).

    A bare "5" names nobody, but it is answering the question we just asked, so
    it belongs to whoever that question was about -- not to the shopper merely
    because the reply was short.
    """
    # Resolved once per turn: a model may search several times in one turn, and
    # the second call must not re-attribute the answer to a different person.
    if turn.get("size_key"):
        return turn["size_key"], turn.get("size_owner")

    if recipient is None and _extract_size(turn.get("query", "")):
        # A bare "12" names nobody. It answers whatever was last asked about, so
        # attribute it there: to the question we forced, or failing that, to the
        # person named in the previous message. The model asks questions of its
        # own accord too, and those must land on the right person as well.
        pending = turn.setdefault("session", {}).get("pending")
        if pending and pending.get("key") != "__who__":
            key, owner = pending["key"], pending.get("recipient")
        else:
            owner = _recipient(turn.get("previous_query", ""))
            key = _size_key(owner)
    else:
        key, owner = _size_key(recipient), recipient
    turn["size_key"], turn["size_owner"] = key, owner
    return key, owner


def _authorised_size(turn: dict[str, Any], key: str) -> str | None:
    """The only size we are willing to search with.

    Two sources count: a size the shopper stated in this very message, or one
    already on file for this person. A size the model supplied from anywhere else
    is discarded -- a model that infers a sister's shoe size from her brother's
    is guessing, and this is the layer that refuses guesses.
    """
    stated = _extract_size(turn.get("query", ""))
    return stated or profile.get(key)


def _tool_search(args: dict[str, Any], turn: dict[str, Any]) -> Any:
    query = args.get("query", "")
    recipient = _recipient(turn.get("query", ""))
    # Fold the recipient into the query so "for my sister" reaches the women's
    # section even when the model searched for the bare word "shoes".
    if recipient and recipient not in query.lower():
        query = f"{query} for my {recipient}"

    probe = catalog.search_products(query, max_price_paise=args.get("max_price_paise"),
                                    category=args.get("category"), attributes=args.get("attributes"),
                                    limit=MAX_OPTIONS)
    needs_size = any(catalog.required_choice(product) == "size" for product in probe)
    key, size_owner = _resolved_size_key(turn, recipient)
    size = _authorised_size(turn, key) if needs_size else args.get("size")
    session = turn.setdefault("session", {})

    prefs = profile.snapshot()["preferences"]
    people = _known_people(prefs)
    if (needs_size and not size and recipient is None and len(people) > 1
            and not _says_self(turn.get("query", ""))):
        # Several people on file and nobody named: guessing here is how a sister
        # ends up with her brother's shoes.
        session["pending"] = {"key": "__who__", "recipient": None, "query": query}
        turn["must_ask"] = _who_question(people)
        audit.log("AI", "clarification_asked",
                  {"missing": "recipient", "about": query, "enforced_by": "search tool"})
        turn["last_results"] = []
        return {"available": [], "must_ask": turn["must_ask"],
                "why": "Several people have sizes on file and this request names none of them. "
                       "Ask exactly that question; do not assume."}

    if needs_size and not size:
        # Nothing is returned, so there is nothing for the model to offer. The
        # question is the only move left -- policy enforced by what the tool
        # hands back, not by a rule in the prompt the model may talk itself out of.
        session["pending"] = {"key": key, "recipient": size_owner, "query": query}
        audit.log("AI", "clarification_asked",
                  {"missing": key, "about": query, "enforced_by": "search tool"})
        turn["last_results"] = []
        # The turn ends here whatever the model does next: `chat()` returns this
        # question verbatim rather than letting it search its way around the gap.
        turn["must_ask"] = _size_question(size_owner)
        return {
            "available": [],
            "must_ask": _size_question(recipient),
            "why": (f"No shoe size is on file for {size_owner or 'the shopper'}. Ask exactly that "
                    f"question and nothing else. When they answer, call remember_preference with "
                    f"key '{key}', then search again. Never reuse another person's size."),
        }

    # A size the shopper just stated is worth keeping whether or not the model
    # thinks to call remember_preference. Memory is the system's job.
    if needs_size and size and profile.get(key) != size:
        profile.remember(key, size)


    results, relaxation = _search_widening(query, size, args)
    unavailable = catalog.unavailable_matches(
        query=query, max_price_paise=args.get("max_price_paise"),
        category=args.get("category"), attributes=args.get("attributes"), size=size)

    audit.log("AI", "agent_search", {
        "query": query,
        "max_price": _rupees(args["max_price_paise"]) if args.get("max_price_paise") else None,
        "category": args.get("category"), "size": size,
        "results": [p["id"] for p in results],
        "unavailable": [hit["product"]["id"] for hit in unavailable] or None,
    })
    turn["last_results"] = results or turn.get("last_results", [])
    turn["unavailable"] = unavailable
    turn["size"] = size or turn.get("size")

    turn["relaxation"] = relaxation
    return {
        "shopping_for": size_owner or "the shopper",
        "size_used": size,
        "widened": relaxation,
        "available": [{"id": p["id"], "name": p["name"], "price": p["price_display"],
                       "category": p["category"], "stock": p["stock"],
                       "sizes": p.get("sizes", []), "attributes": p["attributes"]} for p in results],
        "unavailable": [{"id": hit["product"]["id"], "name": hit["product"]["name"],
                         "reason": "out of stock",
                         "was_asked_for": hit["was_best"],
                         "previously_purchased": profile.purchase_count(hit["product"]["id"])}
                        for hit in unavailable],
    }


_RELAXATIONS = [
    ({"attributes": None}, "ignoring the exact feature filter"),
    ({"attributes": None, "max_price_paise": catalog.NO_BUDGET}, "looking past the budget you named"),
    ({"attributes": None, "max_price_paise": catalog.NO_BUDGET, "category": None},
     "searching the whole store"),
]


def _search_widening(query: str, size: str | None, args: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Search, widening the filters until there is a shortlist worth showing.

    A model will happily search for "green nike shoes" in a catalog that has
    neither greens nor Nikes. Returning nothing teaches the shopper nothing, so
    the constraints are dropped one at a time and the caller is told which.
    """
    # Resolve the budget up front: a ceiling written into the query text has to
    # become an explicit filter before it can be relaxed.
    base = {"max_price_paise": args.get("max_price_paise") or catalog.parse_budget_paise(query),
            "category": args.get("category"), "attributes": args.get("attributes")}
    results = catalog.search_products(query=query, size=size, limit=MAX_OPTIONS, **base)
    if len(results) >= 3:
        return results, None

    relaxation: str | None = None
    for overrides, note in _RELAXATIONS:
        wider = catalog.search_products(query=query, size=size, limit=MAX_OPTIONS,
                                        **{**base, **overrides})
        if len(wider) > len(results):
            results, relaxation = wider, note
        if len(results) >= 3:
            break
    return results, relaxation


def _tool_details(args: dict[str, Any], turn: dict[str, Any]) -> Any:
    product = catalog.get_product(args.get("product_id", ""))
    audit.log("AI", "product_details", {"product_id": args.get("product_id"), "found": product is not None})
    if product is None:
        return {"error": "No such product id. Use search_products and only use ids it returns."}
    turn["last_results"] = [product]
    return product


def _tool_propose(args: dict[str, Any], turn: dict[str, Any]) -> Any:
    """Draft an order. This is the model's last word on the purchase -- from here
    the backend takes over and the model's opinion stops mattering."""
    order, error = gate.create_single_order(
        args.get("product_id", ""), args.get("quantity", 1) or 1, args.get("size") or turn.get("size"))
    if order is None:
        audit.log("AI", "propose_purchase", {"product_id": args.get("product_id"), "error": error}, status="failed")
        return {"error": error}

    product = catalog.get_product(order["items"][0]["product_id"])
    audit.log("AI", "product_selected", {
        "order_id": order["id"], "product": order["summary"],
        "total": _rupees(order["total_paise"]),
    })

    # Run the real policy early so the shopper hears about a block now, not after
    # they have approved something the backend was never going to allow.
    preview = gate.gate(order["id"], stage="preview")
    turn["order"] = order
    turn["product"] = product
    turn["gate_preview"] = preview.as_dict()

    return {
        "order_id": order["id"], "product": order["summary"],
        "total": _rupees(order["total_paise"]), "status": "pending_user_approval",
        "will_be_blocked": None if preview.allowed else preview.reason,
        "note": "Nothing has been charged. The shopper must approve before any payment is created.",
    }


def _run_tool(name: str, args: dict[str, Any], turn: dict[str, Any]) -> Any:
    if name == "get_shopper_profile":
        return _tool_profile(args)
    if name == "remember_preference":
        return _tool_remember(args, turn)
    if name == "search_products":
        return _tool_search(args, turn)
    if name == "get_product_details":
        return _tool_details(args, turn)
    if name == "propose_purchase":
        return _tool_propose(args, turn)
    return {"error": f"Unknown tool {name}."}


def _complete(messages: list[dict[str, Any]]) -> Any:
    """The only provider-specific function in the app.

    To move to Anthropic, rewrite this to call messages.create with the same tool
    schemas and return an object exposing .content and .tool_calls.
    """
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS, temperature=0.2,
    )
    return response.choices[0].message


def chat(message: str, history: list[dict[str, str]], session: dict[str, Any]) -> dict[str, Any]:
    """One agent turn: shopper message in, reply plus options or a pending order out."""
    audit.log("USER", "user_request", {"message": message})
    turn: dict[str, Any] = {"query": message, "order": None, "product": None,
                            "last_results": [], "size": None, "session": session}

    turn["previous_query"] = next(
        (entry["content"] for entry in reversed(history) if entry["role"] == "user"), "")
    if not (_extract_size(message) or _names_someone(message) or _says_self(message)):
        # This message answers nothing -- it starts something new.
        session.pop("pending", None)

    if not os.getenv("OPENAI_API_KEY"):
        return _keyword_turn(message, session, turn)

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": h["role"], "content": h["content"]} for h in history[-8:]]
    messages.append({"role": "user", "content": message})

    reply = ""
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            assistant = _complete(messages)
            tool_calls = getattr(assistant, "tool_calls", None) or []
            messages.append({
                "role": "assistant",
                "content": assistant.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ] or None,
            })
            if not tool_calls:
                reply = (assistant.content or "").strip()
                break
            for call in tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _run_tool(call.function.name, args, turn)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
            if turn.get("must_ask"):
                # A tool refused to answer until the shopper does. Ending the turn
                # here is what stops the model rephrasing the search until it gets
                # a list it can offer.
                reply = turn["must_ask"]
                break
    except Exception as exc:  # a model outage must not take the store down
        audit.log("SYSTEM", "agent_error", {"error": type(exc).__name__}, status="failed")
        return _keyword_turn(message, session, turn)

    if not reply:
        reply = "I could not put that into words. Try asking again in a slightly different way."
    return _finish(reply, turn, session)


def _finish(reply: str, turn: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Package the turn for the UI and remember what was on the table."""
    options = [as_option(product, turn.get("query", "")) for product in turn.get("last_results", [])]
    if turn.get("order"):
        # Once one product is drafted, the option list has served its purpose.
        chosen = {line["product_id"] for line in turn["order"]["items"]}
        options = [option for option in options if option["product"]["id"] in chosen]
    if options:
        session["last_option_ids"] = [option["product"]["id"] for option in options]

    # The question we asked has been answered; stop treating it as outstanding.
    if turn.get("size_key") and turn.get("size"):
        turn.setdefault("session", {}).pop("pending", None)

    audit.log("AI", "agent_reply", {
        "reply": reply,
        "offered": [option["product"]["id"] for option in options] or None,
        "order_id": (turn.get("order") or {}).get("id"),
    })
    return {
        "reply": reply,
        "options": options,
        "order": turn.get("order"),
        "gate_preview": turn.get("gate_preview"),
        "size": turn.get("size"),
        "profile": profile.snapshot(),
    }


# --- Deterministic fallback -------------------------------------------------
# Used when no OPENAI_API_KEY is set, or if the model call fails mid-demo. Same
# tools, same profile, same gate -- so the ask/offer/approve story stays
# demonstrable with zero credentials.

# Who the shopper is buying for. A size belongs to a person, not to an account:
# reusing one person's size for another is how you ship the wrong shoes.
_RECIPIENTS = {
    "daughter": "daughter", "son": "son", "sister": "sister", "brother": "brother",
    "wife": "wife", "husband": "husband", "partner": "partner",
    "mom": "mother", "mum": "mother", "mother": "mother",
    "dad": "father", "father": "father",
    "niece": "niece", "nephew": "nephew", "friend": "friend",
    "kid": "kid", "child": "child", "baby": "baby",
}


def _says_self(message: str) -> bool:
    """An explicit "it's for me", so we stop asking who."""
    text = message.strip().lower()
    return bool(re.search(r"\b(for )?(me|myself|my own)\b", text)) and not _names_someone(text)


def _names_someone(message: str) -> bool:
    return any(word in _RECIPIENTS for word in re.findall(r"[a-z]+", message.lower()))


def _known_people(prefs: dict[str, Any]) -> list[str | None]:
    """Everyone we hold a shoe size for, the shopper included (as None)."""
    people: list[str | None] = []
    for key in prefs:
        if key == "shoe_size":
            people.append(None)
        elif key.endswith("_shoe_size"):
            people.append(key[: -len("_shoe_size")])
    return people


def _who_question(people: list[str | None]) -> str:
    names = ["you" if person is None else f"your {person}" for person in people]
    return "Who am I shopping for - " + ", ".join(names[:-1]) + f" or {names[-1]}?"


def _recipient(message: str) -> str | None:
    """Who this purchase is for, or None when it is the shopper themself."""
    for word in re.findall(r"[a-z]+", message.lower()):
        if word in _RECIPIENTS:
            return _RECIPIENTS[word]
    return None


def _size_key(recipient: str | None) -> str:
    return f"{recipient}_shoe_size" if recipient else "shoe_size"


def _size_phrase(recipient: str | None) -> str:
    return f"your {recipient}'s shoe size" if recipient else "your shoe size"


def _size_question(recipient: str | None) -> str:
    return (f"What shoe size does your {recipient} take?" if recipient
            else "What's your shoe size?")


def _merge_with_topic(message: str, topic: str) -> str:
    """Fold a bare follow-up into the request already in progress.

    "sorry, for my sister" carries no product words of its own -- it edits the
    last request rather than starting a new one.
    """
    recipient = _recipient(message)
    if not recipient:
        return message
    cleaned = re.sub(r"\bfor (my|our) [a-z]+\b", "", topic, flags=re.I)
    cleaned = re.sub(r"\bfor (me|myself)\b", "", cleaned, flags=re.I)
    return " ".join(f"{cleaned} for my {recipient}".split())

# Saved preferences that make a search better, and the words that call for them.
_PROFILE_HINTS = {
    "hair_concern": {"shampoo", "hair", "scalp", "anti_dandruff", "hair_fall"},
    "skin_type": {"soap", "bodywash", "skin", "body", "sensitive_skin", "wash"},
}


def _profile_hint(query_tokens: set[str]) -> tuple[str, str] | None:
    """The one saved preference worth folding into this search, if we have it."""
    for key, triggers in _PROFILE_HINTS.items():
        if query_tokens & triggers:
            value = profile.get(key)
            if value:
                return key, value
    return None


def _extract_size(message: str) -> str | None:
    """Pull a shoe size out of a reply like "12", "uk 12" or "size 12 please".

    Budget phrases are stripped first so "under 2000" cannot be read as a size.
    """
    text = re.sub(r"(?:under|below|less than|upto|up to|within|rs\.?|inr|\u20b9)\s*\d[\d,]*", " ", message.lower())
    match = re.search(r"\b(?:uk\s*)?(\d{1,2})\b", text)
    if not match:
        return None
    number = int(match.group(1))
    return f"UK {number}" if 1 <= number <= 13 else None


def _unavailable_note(unavailable: list[dict[str, Any]]) -> str:
    """One sentence about an out-of-stock item, but only when it is genuinely the
    shopper's business: they asked for that item, or they have bought it before.
    Otherwise silence -- nobody needs a list of things they cannot have."""
    for hit in unavailable:
        product = hit["product"]
        bought = profile.purchase_count(product["id"])
        if not (hit["was_best"] or bought):
            continue
        audit.log("AI", "unavailable_flagged", {
            "product": product["name"], "product_id": product["id"],
            "previously_purchased": bought, "asked_for": hit["was_best"],
        })
        if bought:
            # Two or more purchases is a habit, and worth acknowledging as one.
            how = "you buy regularly" if bought >= 2 else "you've bought before"
            return f"The {product['name']} {how} is out of stock right now. "
        return f"The {product['name']} you asked about is out of stock right now. "
    return ""


def _offer(query: str, size: str | None, turn: dict[str, Any], session: dict[str, Any],
           prefix: str = "") -> dict[str, Any]:
    """Search, then hand the shopper the shortlist rather than one verdict.

    Everything offered is in stock -- the catalog filters the rest out -- so the
    only out-of-stock item that reaches the reply is one worth naming.
    """
    turn["query"] = query
    turn["size"] = size
    _tool_search({"query": query, "size": size}, turn)
    products = turn.get("last_results", [])
    note = _unavailable_note(turn.get("unavailable", []))

    if not products:
        if note:
            return _finish(prefix + note + "I don't have anything else in stock that matches it. "
                           "Tell me what else you had in mind and I'll look again.", turn, session)
        return _finish(prefix + "I could not find anything in stock matching that across the three "
                       "stores. Try naming a category - shoes, headphones, shampoo, soap.", turn, session)

    if turn.get("relaxation"):
        note += f"Nothing matched exactly, so I'm {turn['relaxation']}. "

    best = products[0]
    cheapest = min(products, key=lambda p: p["price_paise"])
    noun = "alternative" if note else "option"
    count = "1 " + noun if len(products) == 1 else f"{len(products)} {noun}s"

    reply = prefix + note
    reply += f"Here {'is' if len(products) == 1 else 'are'} {count}"
    reply += f" in size {size}" if size else ""
    reply += f". {best['name']} at {best['price_display']} is the closest match"
    if cheapest["id"] != best["id"]:
        reply += f", and {cheapest['name']} at {cheapest['price_display']} is the cheaper option"
    reply += ". Pick the one you want and I'll draft the order - nothing is charged until you approve."
    return _finish(reply, turn, session)


def _keyword_turn(message: str, session: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    # 1a. Answering "who is this for?"
    who_resolved = False
    pending = session.get("pending")
    if pending and pending.get("key") == "__who__":
        if _recipient(message) or _says_self(message):
            session.pop("pending", None)
            message = (_merge_with_topic(message, pending["query"])
                       if _recipient(message) else pending["query"])
            # Answered. Asking again on the very next line would be a loop.
            who_resolved = True

    # 1b. Answering a size question.
    pending = session.get("pending")
    if pending and pending.get("key") != "__who__":
        size = _extract_size(message)
        if size:
            profile.remember(pending["key"], size)
            session.pop("pending", None)
            return _offer(pending["query"], size, turn, session,
                          prefix=f"Saved that as {_size_phrase(pending['recipient'])}, "
                                 "so I won't ask again. ")

    # 2. A fresh search, in-stock only.
    results = catalog.search_products(message, limit=MAX_OPTIONS)

    # 3. No product words of its own? Then it is a follow-up on the last request
    #    ("sorry, for my sister"), not a new one.
    if not results and session.get("topic"):
        merged = _merge_with_topic(message, session["topic"])
        if merged != message:
            audit.log("AI", "context_carried", {"from": session["topic"], "to": merged})
            message = merged
            results = catalog.search_products(message, limit=MAX_OPTIONS)

    if results:
        session["topic"] = message

    # 4. Footwear cannot be ordered without a size, and the size belongs to
    #    whoever the shoes are for -- never to whoever asked.
    if any(catalog.required_choice(product) == "size" for product in results):
        recipient = _recipient(message)
        people = _known_people(profile.snapshot()["preferences"])
        if recipient is None and len(people) > 1 and not who_resolved and not _says_self(message):
            session["pending"] = {"key": "__who__", "recipient": None, "query": message}
            audit.log("AI", "clarification_asked", {"missing": "recipient", "about": message})
            turn["last_results"] = []
            return _finish(_who_question(people), turn, session)

        key = _size_key(recipient)
        known = profile.get(key)
        _tool_profile({})
        if known:
            return _offer(message, known, turn, session,
                          prefix=f"Using {_size_phrase(recipient)} ({known}). ")
        session["pending"] = {"key": key, "recipient": recipient, "query": message}
        audit.log("AI", "clarification_asked", {"missing": key, "about": message})
        turn["last_results"] = []
        return _finish(_size_question(recipient) + " I'll save it against "
                       + (f"your {recipient}" if recipient else "you")
                       + " so you only have to tell me once.", turn, session)

    # 5. Personal care: fold in what the shopper already told us about their
    #    hair or skin rather than making them repeat it.
    hint = _profile_hint(set(catalog.tokens(message)))
    if hint:
        key, value = hint
        _tool_profile({})
        label = profile.label_for(key)
        return _offer(f"{message} {value}", None, turn, session,
                      prefix=f"Going by the {label} you told me before ({value}). ")

    return _offer(message, None, turn, session)
