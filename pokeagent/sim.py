"""Offline games between policies, using our own engine (randomized mode).

    python -m pokeagent.sim --games 20 --p0 search --p1 greedy
"""

from __future__ import annotations

import argparse
import random
import time

from . import calc
from .engine import resolve, slot_options, start_battle
from .model import Build, Field, Mon, Side, State
from .sample_cards import SAMPLE_CARDS
from .search import decide, default_action, joint_actions


def matchup(state: State, side: int, mon: Mon) -> float:
    """How well `mon` lines up against the foes currently on the field (offence minus danger)."""
    off = dfn = 0.0
    for sl in (0, 1):
        foe = state.sides[1 - side].active_mon(sl)
        if foe is None:
            continue
        off = max(off, max((min(1.0, calc.expected(calc.damage(state, side, mon, 1 - side, foe, m)) / foe.hp)
                            for m in mon.build.moves), default=0.0))
        dfn = max(dfn, max((min(1.0, calc.expected(calc.damage(state, 1 - side, foe, side, mon, m)) / mon.hp)
                            for m in foe.build.moves), default=0.0))
    return off - dfn + 0.3 * mon.hp_frac


def replace_best(state: State, side: int, slot: int) -> int | None:
    bench = state.sides[side].bench()
    if not bench:
        return None
    return max(bench, key=lambda i: matchup(state, side, state.sides[side].mons[i]))


def policy_random(state, side, rng):
    a = rng.choice(slot_options(state, side, 0))
    opts1 = [o for o in slot_options(state, side, 1) if not (o[0] == "switch" and a[0] == "switch" and o[1] == a[1])]
    return [a, rng.choice(opts1 or [("pass",)])]


def policy_greedy(state, side, rng):
    return [default_action(state, side, sl, slot_options(state, side, sl)) for sl in (0, 1)]


def policy_search(state, side, rng):
    ours = [slot_options(state, side, 0), slot_options(state, side, 1)]
    return list(decide(state, ours, side=side, budget_s=5))


def policy_search2(state, side, rng):
    ours = [slot_options(state, side, 0), slot_options(state, side, 1)]
    return list(decide(state, ours, side=side, budget_s=8, depth=2))


POLICIES = {"random": policy_random, "greedy": policy_greedy, "search": policy_search, "search2": policy_search2}


def make_side(cards: list[dict], leads: tuple[int, int] = (0, 1)) -> Side:
    mons = [Mon.fresh(Build.from_card(c)) for c in cards]
    return Side(mons=mons, active=list(leads))


def play(cards0: list[dict], cards1: list[dict], p0, p1, rng: random.Random, max_turns: int = 40,
         verbose: bool = False) -> int:
    state = State([make_side(cards0), make_side(cards1)], Field())
    start_battle(state)
    for _ in range(max_turns):
        acts = [p0(state, 0, rng), p1(state, 1, rng)]
        if verbose:
            print(f"T{state.turn}", acts)
        state = resolve(state, acts, rng=rng, replace=replace_best)
        w = state.winner()
        if w is not None:
            return w
    # Tiebreak on remaining Pokémon then HP
    def score(sd):
        return (sd.alive_count(), sum(m.hp_frac for m in sd.mons))
    a, b = score(state.sides[0]), score(state.sides[1])
    return 0 if a > b else 1 if b > a else -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--p0", default="search")
    ap.add_argument("--p1", default="greedy")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    wins = [0, 0, 0]
    t0 = time.time()
    cards = []
    for g in range(args.games):
        if g % 2 == 0:  # each pair of games swaps the same two teams between the policies
            cards = rng.sample(SAMPLE_CARDS, 8)
        team0, team1 = (cards[:4], cards[4:]) if g % 2 == 0 else (cards[4:], cards[:4])
        w = play(team0, team1, POLICIES[args.p0], POLICIES[args.p1], rng, verbose=args.v)
        wins[w if w >= 0 else 2] += 1
        print(f"game {g + 1}: winner {['p0', 'p1', 'draw'][w if w >= 0 else 2]}  ({time.time() - t0:.0f}s)")
    print(f"{args.p0} {wins[0]} - {wins[1]} {args.p1} (draws {wins[2]})")


if __name__ == "__main__":
    main()


# ---- helpers for preview simulations and full-pipeline matches ----

def policy_greedy_plus(state, side, rng):
    ours = [slot_options(state, side, 0), slot_options(state, side, 1)]
    return list(decide(state, ours, side=side, budget_s=0.3, keep=3))


def quick_leads(builds, opp_builds) -> tuple[int, int]:
    from .preview import guess_their_leads
    return guess_their_leads(builds, opp_builds)[0][0]


def play_sides(builds0, builds1, p0, p1, rng: random.Random, max_turns: int = 30,
               leads0=None, leads1=None, verbose: bool = False) -> int:
    l0 = leads0 or quick_leads(builds0, builds1)
    l1 = leads1 or quick_leads(builds1, builds0)
    s0 = Side([Mon.fresh(b) for b in builds0], list(l0))
    s1 = Side([Mon.fresh(b) for b in builds1], list(l1))
    state = State([s0, s1], Field())
    start_battle(state)
    for _ in range(max_turns):
        acts = [p0(state, 0, rng), p1(state, 1, rng)]
        if verbose:
            print(f"T{state.turn}", acts)
        state = resolve(state, acts, rng=rng, replace=replace_best)
        w = state.winner()
        if w is not None:
            return w
    a = (state.sides[0].alive_count(), sum(m.hp_frac for m in state.sides[0].mons))
    b = (state.sides[1].alive_count(), sum(m.hp_frac for m in state.sides[1].mons))
    return 0 if a > b else 1 if b > a else -1
