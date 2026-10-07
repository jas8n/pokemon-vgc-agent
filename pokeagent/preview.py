"""Team Preview: choose 4 of 6 to bring and the 2 leads."""

from __future__ import annotations

import itertools
import random
import time

from . import roles
from .engine import start_battle
from .model import Build, Field, Mon, Side, State
from .roles import duel, register, roles_of
from .search import decide
from .engine import slot_options


def lineup_score(ours: list[str], theirs: list[str], weights: list[float] | None = None) -> float:
    """Heuristic value of bringing `ours` (card keys) against the opponent's pool `theirs`."""
    weights = weights or [1.0] * len(theirs)
    wsum = sum(weights)
    # How well we answer each of their Pokémon, and how badly each of ours is threatened
    answer = sum(w * max(duel(o, t) for o in ours) for t, w in zip(theirs, weights)) / wsum
    second = sum(w * sorted((duel(o, t) for o in ours), reverse=True)[1] for t, w in zip(theirs, weights)) / wsum
    exposure = sum(sum(w * max(0.0, duel(t, o)) for t, w in zip(theirs, weights)) / wsum for o in ours) / len(ours)
    rs = [roles_of(o) for o in ours]
    bonus = 0.0
    bonus += 0.15 if any(r.fake_out for r in rs) else 0.0
    bonus += 0.12 if any(r.intimidate for r in rs) else 0.0
    bonus += 0.15 if any(r.speed_control for r in rs) else 0.0
    bonus += 0.08 if any(r.redirect for r in rs) else 0.0
    bonus += 0.05 * min(2, sum(1 for r in rs if r.spread))
    tr = any(r.trick_room for r in rs)
    if tr:
        bonus += 0.05 * sum(1 for r in rs if r.speed < 80)
    weather = {r.weather for r in rs if r.weather}
    bonus += 0.1 * sum(1 for r in rs if r.weather_abuser and r.weather_abuser.rstrip("?") in weather)
    bonus += 0.03 * sum(roles.prior(roles._BUILDS[o]) for o in ours) / len(ours)
    return answer + 0.5 * second - 0.4 * exposure + bonus


def predict_bring(theirs: list[str], ours: list[str], top: int = 3) -> list[tuple[list[str], float]]:
    """The opponent's most likely fours, with weights."""
    scored = sorted(((lineup_score(list(c), ours), list(c)) for c in itertools.combinations(theirs, 4)),
                    key=lambda t: -t[0])
    best = scored[:top]
    total = sum(max(0.05, s - best[-1][0] + 0.1) for s, _ in best)
    return [(c, max(0.05, s - best[-1][0] + 0.1) / total) for s, c in best]


def lead_value(state_builds: tuple[list[Build], list[Build]], my_leads: tuple[int, int],
               their_leads: tuple[int, int], budget_s: float = 0.6) -> float:
    """Turn-1 search value of a lead pairing (both sides' back Pokémon included)."""
    mine, theirs = state_builds
    s0 = Side([Mon.fresh(b) for b in mine], list(my_leads))
    s1 = Side([Mon.fresh(b) for b in theirs], list(their_leads))
    state = State([s0, s1], Field())
    start_battle(state)
    ours = [slot_options(state, 0, 0), slot_options(state, 0, 1)]
    _, ranked, _ = decide(state, ours, side=0, budget_s=budget_s, keep=4, return_scores=True)
    return ranked[0][1]


def choose_leads(mine: list[Build], theirs: list[Build], their_lead_guesses: list[tuple[tuple[int, int], float]],
                 deadline: float) -> tuple[int, int]:
    pairs = list(itertools.combinations(range(len(mine)), 2))
    scores = {p: 0.0 for p in pairs}
    for p in pairs:
        for tl, w in their_lead_guesses:
            if time.time() > deadline:
                break
            scores[p] += w * lead_value((mine, theirs), p, tl)
        # Keep an answer in the back: the two not leading should still cover their threats
    return max(pairs, key=lambda p: scores[p])


def guess_their_leads(theirs: list[Build], mine: list[Build]) -> list[tuple[tuple[int, int], float]]:
    keys_t = [register(b) for b in theirs]
    keys_m = [register(b) for b in mine]
    scored = []
    for p in itertools.combinations(range(len(theirs)), 2):
        rs = [roles_of(keys_t[i]) for i in p]
        s = sum(max(duel(keys_t[i], m) for m in keys_m) for i in p)
        s += 0.4 * sum(r.fake_out or r.intimidate for r in rs) + 0.3 * sum(r.speed_control for r in rs)
        s += 0.1 * sum(roles.prior(theirs[i]) for i in p)
        scored.append((s, p))
    scored.sort(key=lambda t: -t[0])
    top = scored[:3]
    return [(p, w) for (_, p), w in zip(top, (0.5, 0.3, 0.2))]


def choose_lineup(my_builds: list[Build], opp_builds: list[Build], budget_s: float = 30.0,
                  rng: random.Random | None = None, simulate: bool = True) -> tuple[list[int], list[int]]:
    """Return (indexes of the 4 to bring, indexes of the 2 leads), both into my_builds."""
    t0 = time.time()
    rng = rng or random.Random(0)
    mk = [register(b) for b in my_builds]
    ok = [register(b) for b in opp_builds]
    their_fours = predict_bring(ok, mk)
    weights = [0.0] * len(ok)
    for four, w in their_fours:
        for k in four:
            weights[ok.index(k)] += w
    weights = [0.25 + w for w in weights]
    ranked = sorted(itertools.combinations(range(len(mk)), 4),
                    key=lambda c: -lineup_score([mk[i] for i in c], ok, weights))
    candidates = [list(c) for c in ranked[:6]]

    best = candidates[0]
    if simulate and len(candidates) > 1:
        from .sim import play, policy_search
        results = {tuple(c): 0.0 for c in candidates}
        games = 0
        sim_deadline = t0 + budget_s * 0.6
        while time.time() < sim_deadline:
            for c in candidates:
                for four, w in their_fours:
                    if time.time() > sim_deadline:
                        break
                    mine_cards = [my_builds[i] for i in c]
                    theirs_cards = [opp_builds[ok.index(k)] for k in four]
                    w_ = _play_builds(mine_cards, theirs_cards, rng)
                    results[tuple(c)] += w * (1.0 if w_ == 0 else 0.5 if w_ == -1 else 0.0)
            games += 1
            if games >= 6:
                break
        heur = {tuple(c): lineup_score([mk[i] for i in c], ok, weights) for c in candidates}
        best = list(max(candidates, key=lambda c: results[tuple(c)] / max(1, games) + 0.5 * heur[tuple(c)]))

    mine4 = [my_builds[i] for i in best]
    their_guess = guess_their_leads(opp_builds, mine4)
    lead_pair = choose_leads(mine4, opp_builds, their_guess, deadline=t0 + budget_s)
    leads = [best[lead_pair[0]], best[lead_pair[1]]]
    return best, leads


def _play_builds(mine: list[Build], theirs: list[Build], rng: random.Random) -> int:
    from .sim import play_sides, policy_greedy_plus
    return play_sides(mine, theirs, policy_greedy_plus, policy_greedy_plus, rng)
