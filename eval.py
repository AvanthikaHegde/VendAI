"""Recommendation accuracy over a fixed query set, across all three stores.

Measures the retrieval the agent actually depends on: each query goes through
`catalog.search_products` -- the same tool the LLM calls -- and the top result is
compared against a hand-labelled expected product. With --llm (and an API key)
it instead runs full agent turns and scores whichever product the model put on
the recommendation card, which is slower and costs tokens.

Run: python eval.py [--llm]
"""

from __future__ import annotations

import sys

import catalog

# (query, expected best-match product id). Labelled by hand from the catalog,
# before running the eval -- not adjusted afterwards to flatter the number.
QUERIES: list[tuple[str, str]] = [
    ("wireless gaming headphones under 3000", "P001"),
    ("cheap wired gaming headset", "P003"),
    ("noise cancelling headphones for travel", "P005"),
    ("wireless earbuds for calls", "P004"),
    ("premium wireless gaming headset with anc", "P002"),
    ("mechanical keyboard with hot swappable switches", "P006"),
    ("wireless mechanical keyboard", "P007"),
    ("quiet membrane keyboard for office typing", "P008"),
    ("fastest optical gaming keyboard", "P009"),
    ("lightweight wireless gaming mouse under 2000", "P010"),
    ("pro wireless gaming mouse with rgb", "P011"),
    ("silent mouse for the office", "P012"),
    ("144hz gaming monitor", "P014"),
    ("27 inch qhd gaming monitor", "P015"),
    ("4k monitor for photo editing", "P016"),
    ("lightweight ultrabook for travel", "P017"),
    ("gaming laptop with an rtx gpu", "P018"),
    ("large desk mat for keyboard and mouse", "P019"),
    # No query for the USB-C hub or the wired mouse: their best match is out of
    # stock, and an out-of-stock product is no longer a valid recommendation.
    # Harder half: phrased the way a shopper talks, deliberately avoiding the
    # catalog's own vocabulary. These are where the number gets honest.
    ("something to hear game footsteps without wires", "P001"),
    ("budget mouse for pc games", "P010"),
    ("headphones for long flights", "P005"),
    ("biggest highest refresh rate screen for gaming", "P015"),
    ("a laptop that can run games", "P018"),
    # Footwear: sizes are handled separately, so these score the shoe itself.
    ("school shoes for my son", "F004"),
    ("lightweight running shoes for kids", "F001"),
    ("waterproof outdoor sneakers for kids", "F002"),
    ("casual velcro sneakers for girls", "F003"),
    ("cushioned running shoes for women", "F005"),
    ("walking shoes for women", "F006"),
    ("mens casual sneakers", "F007"),
    # Personal care.
    ("shampoo for dandruff", "C003"),
    ("tear free baby shampoo", "C005"),
    ("sulphate free shampoo for dry hair", "C006"),
    ("shampoo for hair fall", "C004"),
    ("soap for sensitive skin", "C001"),
    ("charcoal soap for oily skin", "C002"),
    ("citrus body wash", "C007"),
]


def _search_top(query: str) -> str | None:
    # Deliberately the shopper-facing search, out-of-stock items excluded: this
    # measures what actually gets recommended, not what a ranking could find.
    results = catalog.search_products(query, limit=1)
    return results[0]["id"] if results else None


def _agent_top(query: str) -> str | None:
    import agent

    result = agent.chat(query, [], {})
    product = result.get("product")
    return product["id"] if product else None


def main() -> int:
    use_llm = "--llm" in sys.argv
    pick = _agent_top if use_llm else _search_top
    mode = "full agent turn" if use_llm else "catalog search tool"

    correct = 0
    print(f"Recommendation accuracy ({mode})\n")
    for query, expected in QUERIES:
        actual = pick(query)
        hit = actual == expected
        correct += hit
        print(f"  {'PASS' if hit else 'FAIL'}  {query:48s} expected {expected}, got {actual}")

    total = len(QUERIES)
    print(f"\nQueries run: {total}\nCorrect:     {correct}\nAccuracy:    {correct / total:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
