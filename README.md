# VendAI - agentic commerce on Razorpay (test mode)

A conversational shopping agent for a three-store marketplace. You say what you
want in plain language; it asks what it does not know, remembers the answer,
shortlists a few real products, and turns your choice into a Razorpay test-mode
payment - with every money decision made, explained and logged by the backend.

**The principle the whole build is arranged around: the LLM proposes, the backend
disposes.** The model decides what to offer and what to ask. The backend is the
sole authority on whether money moves. There is no code path that reaches
`razorpay_client.create_payment_link` without passing
`gate.gate(order_id, stage="payment")` first.

Built for Razorpay's "AI Growth & Agentic Commerce" track. Python 3.11+, FastAPI,
one static page, no database, no build step.

---

## Quickstart

```bash
pip install -r requirements.txt
uvicorn app:app --reload
# open http://127.0.0.1:8000
```

That is the whole setup. **No API keys are needed** - the app runs fully in mock
mode out of the box, including the payment and failure paths. Open the URL, not
the `file://` path: the page needs the API behind it.

To use real credentials, `cp .env.example .env` and fill in what you have. Both
keys are independent and optional:

| Variable | Unset | Set |
| --- | --- | --- |
| `OPENAI_API_KEY` | deterministic keyword agent, same tools and same gate | full LLM tool-calling agent |
| `RAZORPAY_KEY_ID` / `_SECRET` | `plink_MOCK_...` links against a local stand-in page | real Payment Links via the test API |
| `OPENAI_MODEL` | `gpt-4o` | any chat-completions model |

Test keys only. A `rzp_live_...` key is refused by
`razorpay_client._credentials()` and reported in the header as *Payments
unavailable* rather than crashing the page.

---

## Try it in 3 minutes

1. Click the chip **Shoes for my daughter under Rs 2,000**. The agent checks the
   stored profile, finds no kids shoe size, and **asks for one** - it does not
   guess.
2. Answer `size 12`. It saves the size (visible in the audit trail and under
   *What the store remembers*) and offers four in-stock options in UK 12, each
   with a reason and a **Why this?** breakdown. The out-of-stock pair is shown
   but not recommended, and cannot be chosen.
3. **Choose** one. The approval screen shows product, size, total, the Rs 5,000
   AI limit, what remains, and the checks that just ran.
4. **Approve & pay.** A Payment Link is created and its status polled live.
5. On the payment page choose **Simulate failure**. The app reports "Payment
   wasn't completed - no charge was made" and offers one retry. Fail it again
   and retry disappears - the cap lives in `gate()`, so a hand-made
   `POST /orders/{id}/pay` is refused too.
6. Ask for **a shampoo that suits me**. It uses the dandruff-prone hair concern
   already on file instead of asking again.
7. Ask for the **27 inch QHD gaming monitor** (Rs 22,999). The backend blocks it
   before any payment exists, with the reason logged.

The **Activity** rail fills in live throughout; the **Orders** tab lists every
order - paid, blocked, cancelled or still awaiting approval.

---

## How it fits together

```
Browser (static/index.html)
   |  fetch()
   v
app.py  -->  agent.py (LLM + 5 tools)  -->  catalog.py  (source of truth)
   |                                        profile.py  (what we remember)
   |                                        cart.py     (picked lines)
   |
   +-->  gate.py            the only authority on whether money moves
   |
   +-->  razorpay_client.py Payment Links, test mode or mock
   |
   +-->  audit.py           append-only trail, rendered live in the UI
```

| File | What it does |
| --- | --- |
| `catalog.py` / `catalog.json` | 36 products across **electronics**, **footwear** and **personal care**. Source of truth for every price, size and stock level. Four items are deliberately out of stock. |
| `profile.py` / `profile.json` | What the store knows about the shopper: saved preferences and past orders. The agent reads it before asking, and writes back what it learns. |
| `agent.py` | The tool loop over five tools - `get_shopper_profile`, `remember_preference`, `search_products`, `get_product_details`, `propose_purchase`. The model may only speak about products its tools returned. |
| `cart.py` | The lines the shopper has picked. Validates that a line is orderable at all (real product, in stock, valid size) and leaves every question about money to the gate. |
| `gate.py` | **The safety core.** Re-derives price from the catalog and re-checks stock, size, quantity cap, spend cap, human approval and the retry cap on every payment request. |
| `razorpay_client.py` | Creates and polls Payment Links in test mode, with an identical mock implementation behind the same interface when no keys are set. |
| `audit.py` | Append-only record of every step, with the counters shown in the UI. |
| `app.py` | Routes, wiring, and serving the one static page. |
| `eval.py` | Honest recommendation-accuracy check over a fixed query set. |

### The path a purchase takes

```
"shoes for my daughter under 2,000"
   -> agent reads the profile, finds no kids shoe size
   -> asks ONE question: "What kids shoe size do you need?"
"size 12"
   -> saves it (logged), searches in UK 12, offers 4-5 in-stock options
   -> you pick one; the backend drafts a *pending order* (no money yet)
   -> you approve explicitly; gate() re-checks everything
   -> Razorpay Payment Link -> status polled -> resolved and logged
```

---

## The safety core

Every payment request passes through one `gate()` call, which stops at the first
failed check and returns the real reason. These are **backend constants, never
prompt text** - prompt text can be argued with, a constant cannot.

| Control | Value | Enforced in |
| --- | --- | --- |
| Max single AI payment | Rs 5,000 | `gate.MAX_TXN_PAISE` |
| Max quantity per order | 2 | `gate.MAX_QUANTITY` |
| Explicit human approval | required, per order | `gate.REQUIRE_APPROVAL` |
| Max payment attempts | 2 | `gate.MAX_PAYMENT_ATTEMPTS` |
| Valid size for footwear | required | `catalog.REQUIRED_CHOICE` + `gate()` |

What this means concretely:

- **A client that sends its own price is ignored.** The gate overwrites the total
  with `catalog price x quantity` before the amount reaches Razorpay.
- **A client that sends a size the shoe does not come in is refused outright.**
- **Approval is per order**, consumed by `gate()`. It is not a session-wide "yes"
  that a later, different order could ride on, and the agent cannot set it - only
  the `/approve` endpoint, called by a human click, can.
- **A cart is one order, one payment, one spend check.** The cap applies to the
  total, not to each item - the only reading that actually limits what an agent
  can spend.
- **A blocked request costs no attempt.** Attempts are counted only once a
  payment link actually exists.

---

## Two design decisions

**Sizes are stored per person** - `shoe_size`, `sister_shoe_size`,
`daughter_shoe_size` - so "shoes for my sister" never silently reuses the
shopper's own size. The rule is enforced in the tool layer, not the prompt:
`search_products` returns no footwear at all when it has no size on file for that
person, handing back the question to ask instead, and `remember_preference`
refuses to record a size the shopper did not state in that message. A model asked
politely not to guess will still guess; a tool that returns nothing cannot be
talked around.

Who you are shopping for persists across turns, so a follow-up that names nobody
new - "actually she wants pink ones" - stays with the same person instead of
falling back to the shopper's own size. It changes only when the shopper names
someone else or says "for me". A follow-up carrying no product words of its own
("sorry, for my sister") is folded into the request already in progress rather
than treated as a new one.

**Out-of-stock products are never offered.** `search_products` filters them out,
so they cannot appear as an option or be chosen by mistake. They are mentioned in
exactly two cases, both handled by `catalog.unavailable_matches`: the shopper
asked for that specific item, or they have bought it before. Then the agent says
so in one sentence and offers the closest in-stock alternatives.

> "The Anti-Dandruff Shampoo 100ml (Travel) you buy regularly is out of stock
> right now. Here are 5 alternatives..."

The seeded profile buys that travel shampoo twice, which is what makes the second
case demonstrable.

Everything the store remembers is visible in the **Activity** rail under *What
the store remembers about you* - a profile the shopper cannot see is a profile
they cannot correct.

---

## Razorpay integration

`razorpay_client.py` exposes three functions - create, fetch, cancel - and the
mock implementation sits behind the same interface, so nothing upstream knows or
cares which mode it is in. That is also what keeps the real path honest.

**Mock mode (default, no keys).** Payment links become `plink_MOCK_...` pointing
at a local stand-in for Razorpay's hosted page with *Pay* and *Simulate failure*
buttons, so the entire flow - including the failure path and the retry cap - is
demonstrable with zero credentials.

**Test mode (`rzp_test_...` keys).** Links are created through the real API at
`POST /v1/payment_links` and polled at `GET /v1/payment_links/{id}`. One
asymmetry is worth knowing about, because it shapes the code:

> Success on Razorpay's hosted page flips the link to `paid` and the poller
> resolves it. **Failure does not** - Razorpay deliberately leaves a failed link
> `created` so the shopper can try again on the same page.

So the app resolves failure from the shopper's side instead: *I couldn't complete
this payment* calls `POST /orders/{id}/payment/abandon`, which cancels the link
(`POST /v1/payment_links/{id}/cancel`). That produces a terminal status, and the
order then travels the ordinary failure path - same audit rows, same gate ruling
on whether a retry is allowed. Polling starts at 2s, slows to 6s after 30s, and
gives up after 5 minutes rather than hammering the API for as long as a tab
happens to stay open.

Every payment link carries a `reference_id` prefixed with a per-process
`RUN_ID`, because Razorpay requires it to be unique account-wide while order ids
restart at `ORD-0001` on every boot.

**Verification status, stated plainly:** mock mode is exercised end to end. The
test-key path is written against the documented API but has not been run against
real credentials here, so it is not claimed as verified.

---

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/config` | limits, payment mode, stores, categories |
| GET | `/catalog` | full product list |
| GET | `/profile` | saved preferences and past orders |
| POST | `/chat` | one agent turn; returns a reply plus options or a pending order |
| GET/POST | `/cart` | read the cart, or add a line |
| POST | `/cart/{line_id}/remove` | drop a line |
| POST | `/cart/checkout` | turn the cart into one pending order |
| GET | `/orders` | this session's orders plus the profile's past ones |
| POST | `/orders` | draft an order (product, quantity, size) |
| POST | `/orders/{id}/approve` | record human approval |
| POST | `/orders/{id}/pay` | run the gate, then create a Payment Link |
| POST | `/orders/{id}/payment/abandon` | shopper couldn't pay: cancel the link, resolve the order |
| GET | `/orders/{id}/status` | poll Razorpay and resolve the order |
| POST | `/orders/{id}/cancel` | cancel the order |
| GET | `/audit?since=` | audit trail and counters |

---

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

---

## Certain Notes

- **State is in memory.** Restarting the server clears orders, the audit trail
  and any preference learned during the session; `profile.json` is the seed it
  returns to.
- **One shopper.** Chat history, cart and session are module-level globals, so
  every visitor shares one shopper.
- **No authentication.** Order ids are sequential and `/approve` is open, so the
  human-approval gate is only as strong as knowing who clicked.
- **Polling, not webhooks.** A payment completed after the tab closes is not
  seen. Live use would need `payment_link.paid` with HMAC signature verification.
- **`/orders/{id}/pay` is not idempotent.** A double-click mints two links for
  one order - cosmetic in test mode, not in live.
- **Payment Links only** - no Checkout.js, no card widget, no signature flow on
  the frontend.

The security *core* carries over unchanged: catalog-derived pricing, per-order
approval, the retry cap, and no path to a charge that skips `gate()`. What is
missing for production is everything around it - durability, identity,
idempotency and webhooks.
