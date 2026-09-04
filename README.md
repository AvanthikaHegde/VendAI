# VendAI - agentic commerce on Razorpay (test mode)

A conversational shopping agent for a three-store marketplace. It asks what it
does not know, remembers the answer, shortlists a few products, and turns your
choice into a real Razorpay test-mode payment.

**The principle the whole build is arranged around: the LLM proposes, the
backend disposes.** The model decides what to offer and what to ask. The backend
is the sole authority on whether money moves.

## Architecture

- `catalog.py` reads `catalog.json` - 36 products across **electronics**, **footwear** and **personal care** - and is the source of truth for every price, size and stock level.
- `profile.py` holds what the store knows about the shopper: saved preferences and past orders. The agent reads it before asking, and writes back what it learns.
- `agent.py` runs an OpenAI tool loop over five tools (`get_shopper_profile`, `remember_preference`, `search_products`, `get_product_details`, `propose_purchase`). It may only speak about products its tools returned.
- `cart.py` holds the lines the shopper has picked. It validates that a line is orderable at all (real product, in stock, valid size) and leaves every question about money to the gate.
- `gate.py` is the sole authority on whether money moves: it re-derives the price from the catalog and re-checks stock, size, quantity cap, spend cap, human approval and the retry cap on every payment request.
- `razorpay_client.py` creates Payment Links in test mode and polls their status, with an identical mock implementation when no keys are set. `audit.py` records every step append-only; `app.py` wires it together and serves `static/index.html`.

There is no code path that reaches `razorpay_client.create_payment_link` without
passing `gate.gate(order_id, stage="payment")` first.

## How a conversation goes

```
"shoes for my daughter under 2,000"
   -> agent reads the profile, finds no kids shoe size
   -> asks ONE question: "What kids shoe size do you need?"
"size 12"
   -> saves it (logged in the audit trail), searches in UK 12
   -> offers 4-5 options with prices, reasons and stock
   -> you pick one; the backend drafts the order
   -> approval screen: total vs AI limit, verification checks
   -> Razorpay payment link -> status polled -> audit trail complete
```

Sizes are stored per person - `shoe_size`, `sister_shoe_size`,
`daughter_shoe_size` - so "shoes for my sister" never silently reuses the
shopper's own size. That rule is enforced in the tool layer, not the prompt:
`search_products` returns no footwear at all when it has no size on file for
that person, handing back the question to ask instead, and `remember_preference`
refuses to record a size the shopper did not state in that message. A model
asked politely not to guess will still guess; a tool that returns nothing cannot
be talked around. A follow-up that carries no product words of its own
("sorry, for my sister") is folded into the request already in progress rather
than being treated as a new one.

Ask for a shampoo instead and it uses the hair concern already on file rather
than asking again. Everything it remembers is visible in the rail's **Activity** tab under
*What the store remembers about you*, and every order - paid, blocked, cancelled
or still awaiting approval - is listed under the **Orders** tab - a profile the shopper cannot see is a
profile they cannot correct.

## Spending controls (backend constants, never prompt text)

| Control | Value | Enforced in |
| --- | --- | --- |
| Max single AI payment | Rs 5,000 | `gate.MAX_TXN_PAISE` |
| Max quantity per order | 2 | `gate.MAX_QUANTITY` |
| Explicit human approval | required, per order | `gate.REQUIRE_APPROVAL` |
| Max payment attempts | 2 | `gate.MAX_PAYMENT_ATTEMPTS` |
| Valid size for footwear | required | `catalog.REQUIRED_CHOICE` + `gate()` |

## Out of stock

Out-of-stock products are never offered - `search_products` filters them out, so
they cannot appear as an option or be chosen by mistake. They are mentioned in
conversation in exactly two cases, both handled by `catalog.unavailable_matches`:
the shopper asked for that specific item, or they have bought it before. Then the
agent says so in one sentence and offers the closest in-stock alternatives.

> "The Anti-Dandruff Shampoo 100ml (Travel) you buy regularly is out of stock
> right now. Here are 5 alternatives..."

The seeded profile buys that travel shampoo twice, which is what makes the second
case demonstrable.

An order holds one or more lines, so a cart is one order, one payment and one
spend check - the cap applies to the total, not to each item, which is the only
reading that actually limits what an agent can spend.

A client that sends its own price is ignored: the gate overwrites the total with
`catalog price x quantity` before the amount reaches Razorpay. A client that
sends a size the shoe does not come in is refused outright.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env      # optional - see below
uvicorn app:app --reload
# open http://127.0.0.1:8000   (not the file:// path - the page needs the API)
```

**Mock mode (no keys, works out of the box).** Leave `RAZORPAY_KEY_ID` /
`RAZORPAY_KEY_SECRET` unset. Payment links become `plink_MOCK_...` pointing at a
local stand-in for Razorpay's hosted page with *Pay* and *Simulate failure*
buttons, so the whole flow including the failure path is demonstrable with zero
credentials. Leaving `OPENAI_API_KEY` unset additionally swaps the LLM for a
deterministic keyword agent that uses the same tools, the same profile and the
same gate - it asks the same clarifying question and offers the same shortlist.

**Real test mode.** Put `rzp_test_...` keys and an `OPENAI_API_KEY` in `.env`.
The agent defaults to `gpt-4o`; override with `OPENAI_MODEL`. `gpt-4o-mini` was
measured on the same six-turn conversation and repeatedly asked for a size it had
just been given, so it is not the default.
Payment links are created through the live test API and status is driven by
choosing Success or Failure on Razorpay's hosted test page. Live keys are
rejected by `razorpay_client._credentials()`.

## Demo script (about 3 minutes)

1. Click **Shoes for my daughter under Rs 2,000**. The agent checks the profile,
   finds no kids shoe size, and asks for one - it does not guess.
2. Answer `size 12`. It saves the size (visible in the audit trail and in *What
   the store remembers*) and offers four in-stock options in UK 12, each with a
   reason and a **Why this?** breakdown. The out-of-stock pair is shown but not
   recommended and cannot be chosen.
3. **Choose** one. The approval screen shows product, size, total, the Rs 5,000
   AI limit, what remains, and the checks that just ran.
4. **Approve & pay** - a Payment Link is created and polled live.
5. On the payment page choose **Simulate failure**. The app reports "Payment
   wasn't completed - no charge was made" and offers one retry. Fail it again
   and retry disappears: the cap is enforced in `gate()`, so a hand-made
   `POST /orders/{id}/pay` is refused too.
6. Ask for **a shampoo that suits me**. It uses the dandruff-prone hair concern
   already on file instead of asking, and shortlists accordingly.
7. Ask for the **27 inch QHD gaming monitor** (Rs 22,999). The backend blocks it
   before any payment exists, with the reason logged.

## Honest metrics

`python eval.py` scores the top recommendation for 37 hand-labelled queries
across all three stores, through the same in-stock search the shopper gets.

```
Queries run: 37
Correct:     33
Accuracy:    89.2%
```

That is the keyword search tool measured on its own, with no API key present.
Queries phrased in catalog vocabulary pass; the four failures are deliberate
paraphrases ("something to hear game footsteps without wires", "headphones for
long flights") where matching words is not the same as understanding meaning.
`python eval.py --llm` scores the full agent turn instead, which is the number to
quote when an `OPENAI_API_KEY` is configured; it has not been run here, so it is
not reported.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/config` | limits, mock/live mode, stores, categories |
| GET | `/catalog` | full product list |
| GET | `/profile` | saved preferences and past orders |
| POST | `/chat` | one agent turn; returns a reply plus options or a pending order |
| GET/POST | `/cart` | read the cart, or add a line |
| POST | `/cart/{line_id}/remove` | drop a line |
| POST | `/cart/checkout` | turn the cart into one pending order |
| GET | `/orders` | order history: this session's orders plus the profile's past ones |
| POST | `/orders` | draft an order (product, quantity, size) |
| POST | `/orders/{id}/approve` | record human approval |
| POST | `/orders/{id}/pay` | run the gate, then create a Payment Link |
| GET | `/orders/{id}/status` | poll Razorpay and resolve the order |
| POST | `/orders/{id}/cancel` | cancel |
| GET | `/audit?since=` | audit trail and counters |

State is in memory: restarting the server clears orders, the audit trail and any
preference learned during the session (`profile.json` is the seed it returns to).
