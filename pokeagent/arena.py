"""Full matches offline: draft -> team preview -> battle, between configurable agents.

    python -m pokeagent.arena --matches 20 --a ours --b naive
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass
from typing import Callable

from . import draft as drafting
from . import preview as previewing
from .dex import to_id
from .model import Build
from .roles import prior
from .sample_cards import SAMPLE_CARDS
from .sim import play_sides, policy_greedy, policy_greedy_plus, policy_search

SNAKE = "ABBAABBAABBA"


@dataclass
class AgentSpec:
    draft: Callable
    preview: Callable
    battle: Callable


def draft_ours(available, mine, theirs, pool, rng):
    return drafting.choose_pick(available, mine, theirs, pool, picks_left_mine=6 - len(mine))[0]


def draft_naive(available, mine, theirs, pool, rng):
    """LLM-like: famous/strong-looking Pokémon first, a little noise."""
    return max(available, key=lambda c: prior(Build.from_card(c)) + rng.random() * 1.5)["card_id"]


def preview_ours(mine, theirs, rng):
    return previewing.choose_lineup(mine, theirs, budget_s=6.0, rng=rng)


def preview_naive(mine, theirs, rng):
    order = sorted(range(len(mine)), key=lambda i: -prior(mine[i]) - rng.random())
    return order[:4], order[:2]


AGENTS = {
    "ours": AgentSpec(draft_ours, preview_ours, policy_search),
    "naive": AgentSpec(draft_naive, preview_naive, policy_greedy),
    "naive_search": AgentSpec(draft_naive, preview_naive, policy_search),
    "ours_greedy": AgentSpec(draft_ours, preview_ours, policy_greedy),
    "ours_nopreview": AgentSpec(draft_ours, preview_naive, policy_search),
    "naive_ourpreview": AgentSpec(draft_naive, preview_ours, policy_search),
}


def run_draft(pool: list[dict], a: AgentSpec, b: AgentSpec, a_first: bool, rng: random.Random):
    rosters = {"A": [], "B": []}
    agents = {"A": a, "B": b}
    available = list(pool)
    for turn in SNAKE:
        seat = turn if a_first else ("B" if turn == "A" else "A")
        other = "B" if seat == "A" else "A"
        items = {to_id(c["item"]) for c in rosters[seat]}
        offer = [c for c in available if to_id(c["item"]) not in items]
        if not offer:
            continue
        pick_id = agents[seat].draft(offer, rosters[seat], rosters[other], pool, rng)
        card = next(c for c in offer if c["card_id"] == pick_id)
        rosters[seat].append(card)
        available.remove(card)
    return rosters["A"], rosters["B"]


def match(a: AgentSpec, b: AgentSpec, rng: random.Random, a_first: bool) -> int:
    pool = rng.sample(SAMPLE_CARDS, 18)
    ra, rb = run_draft(pool, a, b, a_first, rng)
    ba, bb = [Build.from_card(c) for c in ra], [Build.from_card(c) for c in rb]
    bring_a, leads_a = a.preview(ba, bb, rng)
    bring_b, leads_b = b.preview(bb, ba, rng)
    four_a, four_b = [ba[i] for i in bring_a], [bb[i] for i in bring_b]
    la = (bring_a.index(leads_a[0]), bring_a.index(leads_a[1]))
    lb = (bring_b.index(leads_b[0]), bring_b.index(leads_b[1]))
    return play_sides(four_a, four_b, a.battle, b.battle, rng, leads0=la, leads1=lb)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=10)
    ap.add_argument("--a", default="ours")
    ap.add_argument("--b", default="naive")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    a, b = AGENTS[args.a], AGENTS[args.b]
    wins = [0, 0, 0]
    t0 = time.time()
    for m in range(args.matches):
        w = match(a, b, rng, a_first=(m % 2 == 0))
        wins[w if w >= 0 else 2] += 1
        print(f"match {m + 1}: {['A', 'B', 'draw'][w if w >= 0 else 2]}  "
              f"[{wins[0]}-{wins[1]}-{wins[2]}] ({time.time() - t0:.0f}s)", flush=True)
    print(f"{args.a} {wins[0]} - {wins[1]} {args.b} (draws {wins[2]})")


if __name__ == "__main__":
    main()
