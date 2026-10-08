"""Turn decisions: score positions, simulate every pairing of our options with the opponent's,
and pick the option that does best against the replies the opponent is likely to choose."""

from __future__ import annotations

import itertools
import math
import time

from . import calc
from .calc import effective_speed, move_data
from .engine import Action, resolve, slot_options
from .model import Mon, State

MON_VALUE = 100.0


def mon_value(mon: Mon, state: State, side: int) -> float:
    if not mon.alive:
        return 0.0
    v = MON_VALUE * (0.4 + 0.6 * mon.hp_frac)
    physical = mon.stat("atk") >= mon.stat("spa")
    if mon.status == "brn":
        v *= 0.7 if physical and mon.ability != "guts" else 0.92
    elif mon.status == "par":
        v *= 0.85
    elif mon.status == "slp":
        v *= 0.7 if mon.status_turns > 1 else 0.85
    elif mon.status in ("psn", "tox"):
        v *= 0.9 if mon.ability != "poisonheal" else 1.0
    b = mon.boosts
    main = b["atk"] if physical else b["spa"]
    v *= 1 + 0.10 * max(-3, min(main, 3))
    v *= 1 + 0.05 * max(-2, min(b["def"] + b["spd"], 4))
    v *= 1 + 0.06 * max(-2, min(b["spe"], 2)) * (0 if state.field.trickroom else 1)
    return v


def evaluate(state: State, side: int = 0) -> float:
    """Position value for `side`: remaining Pokémon and their health, minus the opponent's."""
    total = 0.0
    for si, sd in enumerate(state.sides):
        sign = 1.0 if si == side else -1.0
        score = sum(mon_value(m, state, si) for m in sd.mons if not m.vol.get("not_brought"))
        score += sd.unseen * MON_VALUE
        alive = sd.alive_count()
        score += 25.0 * alive
        score += 5.0 * min(sd.tailwind, 3) * min(alive, 2)
        score += 2.5 * (min(sd.reflect, 3) + min(sd.lightscreen, 3) + 1.5 * min(sd.auroraveil, 3))
        total += sign * score
    # Trick Room favours whichever side is slower
    if state.field.trickroom:
        total += 4.0 * min(state.field.trickroom, 3) * _tr_edge(state, side)
    w = state.winner()
    if w is not None:
        total += 1000.0 if w == side else (-1000.0 if w == 1 - side else 0.0)
    return total


def _tr_edge(state: State, side: int) -> float:
    def speeds(si):
        return [effective_speed(m, state, si) for m in state.sides[si].mons if m.alive]
    mine, theirs = speeds(side), speeds(1 - side)
    if not mine or not theirs:
        return 0.0
    return (sum(theirs) / len(theirs) - sum(mine) / len(mine)) / 40.0


def joint_actions(per_slot: list[list[Action]]) -> list[tuple[Action, Action]]:
    out = []
    for a, b in itertools.product(per_slot[0], per_slot[1]):
        if a[0] == "switch" and b[0] == "switch" and a[1] == b[1]:
            continue
        if a[0] == "pass" and b[0] == "pass" and (len(per_slot[0]) > 1 or len(per_slot[1]) > 1):
            continue
        out.append((a, b))
    return out


def _actions_for(side: int, ours: tuple[Action, Action], theirs: tuple[Action, Action]):
    return [list(ours), list(theirs)] if side == 0 else [list(theirs), list(ours)]


def _prune_slot(state: State, side: int, slot: int, options: list[Action], partner: Action,
                opp_default: tuple[Action, Action], keep: int) -> list[Action]:
    if len(options) <= keep:
        return options
    scored = []
    for opt in options:
        ours = (opt, partner) if slot == 0 else (partner, opt)
        nxt = resolve(state, _actions_for(side, ours, opp_default))
        scored.append((evaluate(nxt, side), opt))
    scored.sort(key=lambda t: -t[0])
    best = [o for _, o in scored[:keep]]
    # Always keep one switch and Protect-type moves in the candidate set: they matter for robustness
    for _, o in scored[keep:]:
        if o[0] == "move" and o[1] in ("protect", "detect", "spikyshield", "kingsshield", "silktrap",
                                        "banefulbunker", "burningbulwark") and o not in best:
            best.append(o)
            break
    return best


def default_action(state: State, side: int, slot: int, options: list[Action]) -> Action:
    """Greedy guess: the option with the most immediate expected damage."""
    mon = state.sides[side].active_mon(slot)
    if mon is None:
        return options[0]
    best, best_v = options[0], -1.0
    for opt in options:
        if opt[0] != "move":
            continue
        v = immediate_damage_value(state, side, slot, mon, opt)
        if v > best_v:
            best, best_v = opt, v
    return best


def immediate_damage_value(state: State, side: int, slot: int, mon: Mon, opt: Action) -> float:
    mid, target = opt[1], opt[2]
    md = move_data(mid)
    if md["category"] == "Status":
        return 0.0
    foes = [(1 - side, sl, m) for sl in (0, 1) if (m := state.sides[1 - side].active_mon(sl)) is not None]
    if md.get("target") in ("allAdjacentFoes", "allAdjacent"):
        targets = foes
    else:
        targets = [t for t in foes if t[1] == target - 1] or foes[:1]
    spread = len(targets) > 1
    total = 0.0
    for ts, _, t in targets:
        rolls = calc.damage(state, side, mon, ts, t, mid, spread=spread)
        acc = calc.accuracy(mon, t, mid, state)
        frac = min(1.0, calc.expected(rolls) / max(1, t.hp))
        total += acc * (frac + 0.5 * calc.ko_chance(rolls, t.hp))
    return total


def decide(state: State, our_options: list[list[Action]], side: int = 0, opp_options: list[list[Action]] | None = None,
           budget_s: float = 8.0, keep: int = 5, beta: float = 0.06, robustness: float = 0.3,
           return_scores: bool = False):
    """Best joint action for `side`. our_options/opp_options are per-slot lists of engine Actions."""
    t0 = time.time()
    opp = 1 - side
    our_options = [_drop_failing(state, side, sl, opts) for sl, opts in enumerate(our_options)]
    if opp_options is None:
        opp_options = [slot_options(state, opp, 0), slot_options(state, opp, 1)]
    if not opp_options[0]:
        opp_options[0] = [("pass",)]
    if not opp_options[1]:
        opp_options[1] = [("pass",)]

    opp_default = (default_action(state, opp, 0, opp_options[0]), default_action(state, opp, 1, opp_options[1]))
    my_default = (default_action(state, side, 0, our_options[0]), default_action(state, side, 1, our_options[1]))

    mine0 = _prune_slot(state, side, 0, our_options[0], my_default[1], opp_default, keep)
    mine1 = _prune_slot(state, side, 1, our_options[1], my_default[0], opp_default, keep)
    theirs0 = _prune_slot(state, opp, 0, opp_options[0], opp_default[1], my_default, keep)
    theirs1 = _prune_slot(state, opp, 1, opp_options[1], opp_default[0], my_default, keep)
    J0 = joint_actions([mine0, mine1]) or [(our_options[0][0], our_options[1][0])]
    J1 = joint_actions([theirs0, theirs1]) or [(opp_options[0][0], opp_options[1][0])]

    M = []
    for i, a in enumerate(J0):
        row = []
        for b in J1:
            nxt = resolve(state, _actions_for(side, a, b))
            row.append(evaluate(nxt, side))
        M.append(row)
        if time.time() - t0 > budget_s and i >= 3:
            J0 = J0[: i + 1]
            break

    n0, n1 = len(J0), len(J1)
    p = [1.0 / n0] * n0
    q = [1.0 / n1] * n1
    for _ in range(6):
        # Opponent: softmax over their value against our current mix
        opp_vals = [-sum(p[i] * M[i][j] for i in range(n0)) for j in range(n1)]
        q = _softmax(opp_vals, beta)
        my_vals = [sum(q[j] * M[i][j] for j in range(n1)) for i in range(n0)]
        p_new = _softmax(my_vals, beta * 2)
        p = [0.5 * a + 0.5 * b for a, b in zip(p, p_new)]

    likely = sorted(range(n1), key=lambda j: -q[j])
    mass, top = 0.0, []
    for j in likely:
        top.append(j)
        mass += q[j]
        if mass >= 0.8:
            break
    scores = []
    for i in range(n0):
        exp_v = sum(q[j] * M[i][j] for j in range(n1))
        worst = min(M[i][j] for j in top)
        # Tiny tie-breaker: between equal plans, prefer the one that does more damage right now
        tie = 0.01 * sum(immediate_damage_value(state, side, sl, m, J0[i][sl])
                         for sl in (0, 1) if J0[i][sl][0] == "move"
                         and (m := state.sides[side].active_mon(sl)) is not None)
        scores.append((1 - robustness) * exp_v + robustness * worst + tie)
    best = max(range(n0), key=lambda i: scores[i])
    if return_scores:
        ranked = sorted(range(n0), key=lambda i: -scores[i])
        return J0[best], [(J0[i], scores[i]) for i in ranked[:5]], [(J1[j], q[j]) for j in likely[:5]]
    return J0[best]


def _drop_failing(state: State, side: int, slot: int, options: list[Action]) -> list[Action]:
    """Remove moves that are certain to fail (Fake Out after the first turn), unless nothing else is left."""
    from .engine import FIRST_TURN_ONLY
    mon = state.sides[side].active_mon(slot)
    if mon is None or mon.turns_out == 0:
        return options
    kept = [o for o in options if not (o[0] == "move" and o[1] in FIRST_TURN_ONLY)]
    return kept or options


def _softmax(vals: list[float], beta: float) -> list[float]:
    m = max(vals)
    ex = [math.exp(beta * (v - m)) for v in vals]
    z = sum(ex)
    return [e / z for e in ex]


def position_value(state: State, side: int, budget_s: float = 2.0) -> float:
    """How good a position is when it's our move: value of our best joint action (cheap version)."""
    ours = [slot_options(state, side, 0), slot_options(state, side, 1)]
    best, ranked, _ = decide(state, ours, side=side, budget_s=budget_s, keep=3, return_scores=True)
    return ranked[0][1]
